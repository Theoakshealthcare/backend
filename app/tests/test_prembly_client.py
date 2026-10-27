"""The Prembly client: request shape, parsing, error mapping (response_code 01/02/03/07, HTTP 402/429) and what stays out of
logs and reprs. Runs against ``FakePrembly`` through an ``httpx.MockTransport``; nothing leaves the machine."""
import base64
import hashlib
import hmac
import logging
from decimal import Decimal

import httpx
import pytest

from app.services import prembly
from app.services.prembly import (
    BAD_RESPONSE, NETWORK, NOT_CONFIGURED, NOT_FOUND, RATE_LIMITED, REJECTED, TIMEOUT, UNAUTHORISED, UNAVAILABLE, WALLET_EMPTY,
    WATCHLISTED, PremblyClient, PremblyError, classify,
)
from app.tests.fake_prembly import (
    TEST_BASE_URL, TEST_ORG_ID, TEST_SECRET_KEY, TEST_WEBHOOK_KEY, FakePrembly, sign, signed_webhook, webhook_payload,
)

SID = "sdk_session_placeholder_0001"


@pytest.fixture
def client(fake_prembly):
    return PremblyClient.from_settings()


def test_get_session_parses_the_documented_fields(fake_prembly, client):
    fake_prembly.script_session(SID, overall_result="passed", full_name="Test Person", phone="+2340000000000", total_cost="620.00")
    session = client.get_session(SID)
    assert session.session_id == SID and session.overall_result == "passed" and session.status == "completed"
    assert session.total_cost == Decimal("620.00") and session.currency == "NGN"
    assert session.started_at.year == 2026 and session.completed_at > session.started_at
    assert session.is_used is False and session.payload is None
    assert session.full_name == "Test Person" and session.end_user_phone == "+2340000000000"


def test_request_carries_the_secret_key_and_org_id_on_the_session_path(fake_prembly, client):
    fake_prembly.script_session(SID)
    client.get_session(SID)
    request = fake_prembly.requests[0]
    assert request.method == "GET" and str(request.url).startswith(TEST_BASE_URL)
    assert request.url.path == f"/api/v1/checker-widget/sdk/sessions/{SID}/"
    assert request.headers["x-api-key"] == TEST_SECRET_KEY and request.headers["x-organisation-id"] == TEST_ORG_ID


def test_payload_only_when_asked_for(fake_prembly, client):
    fake_prembly.script_session(SID, face_confidence=91.5, liveness_confidence=88.0, id_number="00000000000")
    assert client.get_session(SID).payload is None
    session = client.get_session(SID, include_payload=True)
    assert session.payload["face_data"]["confidence"] == 91.5
    assert fake_prembly.requests[-1].url.params["include_payload"] == "true"


def test_unknown_session_is_not_found(client, fake_prembly):
    with pytest.raises(PremblyError) as caught:
        client.get_session("sdk_session_missing")
    assert caught.value.kind == NOT_FOUND and caught.value.response_code == "01" and not caught.value.retryable


@pytest.mark.parametrize("bad", ["", "a/b", "../x", "a b", "x?y=1", "a" * 129, "sess#1"])
def test_a_session_id_that_is_not_a_plain_token_never_reaches_the_url(fake_prembly, client, bad):
    with pytest.raises(ValueError):
        client.get_session(bad)
    assert fake_prembly.requests == []


# --- error mapping ---------------------------------------------------------------------------
@pytest.mark.parametrize("arrange,kind,code,status,retryable,outage,alert", [
    (FakePrembly.outage, UNAVAILABLE, "02", 200, True, True, False),
    (FakePrembly.wallet_empty, WALLET_EMPTY, "03", 200, False, True, True),
    (lambda f: f.wallet_empty(http_402=True), WALLET_EMPTY, None, 402, False, True, True),
    (FakePrembly.rate_limited, RATE_LIMITED, None, 429, True, True, False),
    (FakePrembly.server_error, UNAVAILABLE, None, 500, True, True, False),
    (FakePrembly.unauthorised, UNAUTHORISED, None, 401, False, True, True),
])
def test_failures_map_to_one_error(fake_prembly, client, arrange, kind, code, status, retryable, outage, alert):
    fake_prembly.script_session(SID)
    arrange(fake_prembly)
    with pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    error = caught.value
    assert (error.kind, error.response_code, error.http_status) == (kind, code, status)
    assert (error.retryable, error.outage, error.needs_ops_alert) == (retryable, outage, alert)


def test_rate_limited_carries_the_wait(fake_prembly, client):
    fake_prembly.rate_limited()
    with pytest.raises(PremblyError) as caught:
        client.list_undelivered()
    assert caught.value.retry_after == 60.0


def test_rate_limited_without_a_header_waits_a_minute_and_a_huge_header_is_capped():
    assert prembly._retry_after(httpx.Response(429)) == 60.0
    assert prembly._retry_after(httpx.Response(429, headers={"Retry-After": "86400"})) == prembly.MAX_RETRY_AFTER_SECONDS
    assert prembly._retry_after(httpx.Response(429, headers={"Retry-After": "-5"})) == 0.0


def test_watchlisted_is_never_retried(fake_prembly, client):
    fake_prembly.script(httpx.Response(200, json={"status": False, "response_code": "07", "message": "placeholder"}))
    with pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    assert caught.value.kind == WATCHLISTED and not caught.value.retryable and not caught.value.outage


def test_response_code_wins_over_a_plain_http_status():
    assert classify(200, "02") == UNAVAILABLE
    assert classify(400, "07") == WATCHLISTED
    assert classify(404, None) == NOT_FOUND
    assert classify(422, None) == REJECTED
    assert classify(503, None) == UNAVAILABLE
    assert classify(None, None) == BAD_RESPONSE


def test_a_success_response_code_is_not_an_error(fake_prembly, client):
    fake_prembly.script(httpx.Response(200, json={"status": True, "response_code": "00", "data": {"session_id": SID}}))
    assert client.get_session(SID).session_id == SID


def test_a_refusal_wrapped_in_http_200_is_rejected(fake_prembly, client):
    fake_prembly.script(httpx.Response(200, json={"status": False, "message": "placeholder"}))
    with pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    assert caught.value.kind == REJECTED


def test_timeouts_and_connection_failures(fake_prembly, client):
    fake_prembly.timeout()
    with pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    assert caught.value.kind == TIMEOUT and caught.value.retryable
    fake_prembly.recover()
    fake_prembly.script(httpx.ConnectError("refused"))
    with pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    assert caught.value.kind == NETWORK and caught.value.retryable


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>gateway</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json={"status": True, "data": {"session_id": ""}}),
    httpx.Response(200, json={"status": True, "data": {"session_id": "sdk_session_some_other_one"}}),
])
def test_an_answer_we_cannot_read_is_a_bad_response(fake_prembly, client, response):
    fake_prembly.script(response)
    with pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    assert caught.value.kind == BAD_RESPONSE and caught.value.retryable


def test_missing_credentials_send_nothing(fake_prembly):
    for kwargs in ({"secret_key": "", "org_id": TEST_ORG_ID}, {"secret_key": TEST_SECRET_KEY, "org_id": ""}):
        with pytest.raises(PremblyError) as caught:
            PremblyClient(base_url=TEST_BASE_URL, **kwargs).get_session(SID)
        assert caught.value.kind == NOT_CONFIGURED and caught.value.needs_ops_alert
    assert fake_prembly.requests == []


def test_an_injected_transport_is_used(fake_prembly):
    other = FakePrembly()
    other.script_session(SID, overall_result="failed")
    session = PremblyClient(base_url=TEST_BASE_URL, secret_key=TEST_SECRET_KEY, org_id=TEST_ORG_ID,
                            transport=httpx.MockTransport(other.transport_handler)).get_session(SID)
    assert session.overall_result == "failed" and fake_prembly.requests == [] and len(other.requests) == 1


# --- response shapes behind the seam (UNCONFIRMED against real Prembly) -----------------------
@pytest.mark.parametrize("body", [
    {"data": {"session_id": SID, "overall_result": "passed"}},
    {"data": {"session": {"session_id": SID, "overall_result": "passed"}}},
    {"data": {"sessions": [{"session_id": "other"}, {"session_id": SID, "overall_result": "passed"}]}},
    {"session_id": SID, "overall_result": "passed"},
])
def test_by_id_answer_shapes_that_are_accepted(body):
    assert prembly._extract_session(body, SID).overall_result == "passed"


def test_a_by_id_list_without_the_session_is_not_found():
    with pytest.raises(PremblyError) as caught:
        prembly._extract_session({"data": {"sessions": [{"session_id": "other"}]}}, SID)
    assert caught.value.kind == NOT_FOUND


# --- undelivered list ------------------------------------------------------------------------
def test_list_undelivered_pages(fake_prembly, client):
    for n in range(5):
        fake_prembly.script_session(f"sdk_session_{n}", is_used=(n == 4))
    first = client.list_undelivered(page=1, page_size=2)
    assert [s.session_id for s in first.sessions] == ["sdk_session_0", "sdk_session_1"] and first.has_more
    assert first.total == 4 and (first.page, first.page_size) == (1, 2)
    last = client.list_undelivered(page=2, page_size=2)
    assert [s.session_id for s in last.sessions] == ["sdk_session_2", "sdk_session_3"] and last.has_more
    assert client.list_undelivered(page=3, page_size=2).sessions == () and not client.list_undelivered(page=3, page_size=2).has_more
    request = fake_prembly.requests[0]
    assert request.url.path == "/api/v1/checker-widget/sdk/sessions/undelivered/"
    assert dict(request.url.params) == {"page": "1", "page_size": "2"}


def test_a_short_page_ends_the_drain_whatever_total_pages_says(fake_prembly, client):
    fake_prembly.script(httpx.Response(200, json={"status": True, "data": {
        "sessions": [{"session_id": "sdk_session_a"}],
        "pagination": {"total": 10, "page": 1, "page_size": 2, "total_pages": 11}}}))  # the docs' own inconsistent sample
    page = client.list_undelivered(page=1, page_size=2)
    assert not page.has_more and page.total_pages == 11


def test_list_without_the_wrapper_is_a_bad_response(fake_prembly, client):
    fake_prembly.script(httpx.Response(200, json={"results": []}))
    with pytest.raises(PremblyError) as caught:
        client.list_undelivered()
    assert caught.value.kind == BAD_RESPONSE


# --- privacy ---------------------------------------------------------------------------------
def test_personal_fields_stay_out_of_repr_and_logs(fake_prembly, client, caplog):
    fake_prembly.script_session(SID, full_name="Sentinel Fullname", email="sentinel@example.com", phone="+2349999999999",
                                id_number="99999999999", face_confidence=90.0)
    session = client.get_session(SID, include_payload=True)
    for secret in ("Sentinel Fullname", "sentinel@example.com", "+2349999999999", "99999999999"):
        assert secret not in repr(session) and secret not in str(session)
    fake_prembly.wallet_empty()
    with caplog.at_level(logging.DEBUG), pytest.raises(PremblyError) as caught:
        client.get_session(SID)
    assert TEST_SECRET_KEY not in caplog.text and TEST_SECRET_KEY not in str(caught.value)


# --- the fake's own helpers (K3 relies on them) -----------------------------------------------
def test_sign_is_base64_hmac_sha256_of_the_raw_body():
    body = b'{"a": 1}'
    expected = base64.b64encode(hmac.new(TEST_WEBHOOK_KEY.encode(), body, hashlib.sha256).digest()).decode()
    assert sign(body, TEST_WEBHOOK_KEY) == expected


def test_signed_webhook_is_over_exactly_the_bytes_it_returns_and_tokens_are_unique():
    payload = webhook_payload(SID, oakly_ref="11111111-1111-1111-1111-111111111111", face_confidence=92.0)
    body, headers = signed_webhook(payload)
    assert headers["x-prembly-signature"] == sign(body, TEST_WEBHOOK_KEY)
    assert signed_webhook(payload)[1]["token"] != headers["token"]
    assert signed_webhook(payload, token="fixed")[1]["token"] == "fixed"
    assert sign(body + b" ", TEST_WEBHOOK_KEY) != headers["x-prembly-signature"]


def test_the_fake_refuses_wrong_credentials(fake_prembly):
    wrong = PremblyClient(base_url=TEST_BASE_URL, secret_key="not-the-key", org_id=TEST_ORG_ID)
    fake_prembly.script_session(SID)
    with pytest.raises(PremblyError) as caught:
        wrong.get_session(SID)
    assert caught.value.kind == UNAUTHORISED
