"""KD1: the Dojah pull client (app/services/dojah.py): request shape, retries, error classification, the whitelist that keeps the
registry photo and image links out of memory. Ported from Weysure's tests/test_dojah_client.py; everything runs on the fake
(app/tests/fake_dojah.py), nothing touches the network. Names, ids and numbers are placeholders."""
import json

import httpx
import pytest

from app.services import dojah
from app.services.dojah import (
    BAD_RESPONSE, NETWORK, NOT_CONFIGURED, NOT_FOUND, RATE_LIMITED, REFERENCE_MISMATCH, REJECTED, TIMEOUT, UNAUTHORISED, UNAVAILABLE, WALLET_EMPTY,
    DojahClient, DojahError,
)
from app.tests.fake_dojah import (
    SANDBOX_BVN, SANDBOX_NIN, SENTINEL_PHOTO, SENTINEL_URLS, TEST_APP_ID, TEST_BASE_URL, TEST_SECRET_KEY, fake_dojah,  # noqa: F401
    new_reference,
)


@pytest.fixture
def client(fake_dojah):  # noqa: F811
    return DojahClient.from_settings()


def test_the_request_is_a_get_with_the_secret_key_and_app_id_and_the_reference(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref)
    client.get_verification(ref)
    (request,) = fake_dojah.requests
    assert request.method == "GET" and str(request.url).startswith(TEST_BASE_URL + "/api/v1/kyc/verification?")
    assert request.url.params["reference_id"] == ref
    assert request.headers["authorization"] == TEST_SECRET_KEY  # no "Bearer"
    assert request.headers["appid"] == TEST_APP_ID


def test_a_completed_nin_verification_is_read_into_typed_fields(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref, first_name="Amina", middle_name="Ngozi", last_name="Bello", date_of_birth="12-Apr-1990",
                                   confidence=88.5)
    v = client.get_verification(ref)
    assert (v.reference_id, v.verification_status, v.verification_type) == (ref, "Completed", "NIN")
    assert (v.government_status, v.selfie_status, v.selfie_confidence, v.registry_type) == (True, True, 88.5, "nin")
    assert v.registry == {"nin": SANDBOX_NIN, "first_name": "Amina", "middle_name": "Ngozi", "last_name": "Bello",
                          "date_of_birth": "12-Apr-1990"}


def test_a_bvn_verification_reads_the_bvn_key(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref, id_type="bvn", id_number=SANDBOX_BVN)
    v = client.get_verification(ref)
    assert v.registry_type == "bvn" and v.registry["bvn"] == SANDBOX_BVN and v.verification_type == "BVN"


def test_the_photo_and_the_image_links_are_never_read_into_the_result(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref)
    v = client.get_verification(ref)
    blob = repr(v) + str(v.registry) + str(v.message)
    assert SENTINEL_PHOTO not in blob and not any(url in blob for url in SENTINEL_URLS.values())
    assert set(v.registry) <= {"nin", "first_name", "middle_name", "last_name", "date_of_birth"}


def test_the_registry_record_and_the_message_are_hidden_from_repr(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref, first_name="Amina", last_name="Bello", government=False, message="Record not found")
    v = client.get_verification(ref)
    assert v.message and "Amina" not in repr(v) and SANDBOX_NIN not in repr(v) and "Record not found" not in repr(v)


def test_the_id_type_falls_back_to_the_one_record_present(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref, id_type="bvn", id_number=SANDBOX_BVN, verification_type=None)
    assert client.get_verification(ref).registry_type == "bvn"


def test_an_unknown_id_type_is_left_out_and_its_record_is_not_read(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script_verification(ref, id_type="passport", id_number="A00000000", verification_type="PASSPORT")
    v = client.get_verification(ref)
    assert v.registry_type is None and v.registry == {}


@pytest.mark.parametrize("confidence", [True, "92", float("nan"), float("inf"), None])
def test_a_confidence_that_is_not_a_real_number_is_dropped(client, fake_dojah, confidence):
    ref = new_reference()
    entity = fake_dojah.entity(ref, confidence=confidence)
    # json accepts NaN and Infinity on the wire even though httpx's own encoder refuses to write them
    fake_dojah.script(httpx.Response(200, content=json.dumps({"entity": entity}, allow_nan=True).encode()))
    assert client.get_verification(ref).selfie_confidence is None


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_a_status_that_is_not_a_real_boolean_is_unknown(client, fake_dojah, flag):
    ref = new_reference()
    fake_dojah.script_verification(ref, government=None, selfie=None)
    fake_dojah.verifications[ref]["data"] = {"government_data": {"status": flag}, "selfie": {"status": flag}}
    v = client.get_verification(ref)
    assert (v.government_status, v.selfie_status) == (None, None)


@pytest.mark.parametrize("body", [[], "text", {"entity": []}, {"entity": {"verification_status": "Completed"}}, {}])
def test_an_answer_without_our_reference_is_not_used(client, fake_dojah, body):
    ref = new_reference()
    fake_dojah.script(httpx.Response(200, json=body))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(ref)
    exc = excinfo.value
    # not an outage and not retryable: it must never stall the reconcile cron behind one odd attempt
    assert (exc.kind, exc.retryable, exc.outage, exc.needs_ops_alert) == (REFERENCE_MISMATCH, False, False, False)


def test_an_answer_about_another_reference_is_never_used(client, fake_dojah):
    ref, other = new_reference(), new_reference()
    fake_dojah.script_verification(other)
    fake_dojah.script(httpx.Response(200, json={"entity": fake_dojah.verifications[other]}))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(ref)
    assert excinfo.value.kind == REFERENCE_MISMATCH


def test_an_unwrapped_entity_is_accepted(client, fake_dojah):
    ref = new_reference()
    entity = fake_dojah.entity(ref)
    fake_dojah.script(httpx.Response(200, json=entity))
    assert client.get_verification(ref).reference_id == ref


def test_a_non_json_answer_is_a_bad_response(client, fake_dojah):
    fake_dojah.script(httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.kind == BAD_RESPONSE and excinfo.value.retryable


@pytest.mark.parametrize("reference", ["", "has space", "a/b", "x" * 129, "ünï", "a?b=c"])
def test_a_reference_we_may_not_put_in_a_query_is_refused_before_any_request(client, fake_dojah, reference):
    with pytest.raises(ValueError):
        client.get_verification(reference)
    assert fake_dojah.requests == []


@pytest.mark.parametrize("secret, app_id", [("", TEST_APP_ID), (TEST_SECRET_KEY, ""), ("", "")])
def test_an_empty_key_or_app_id_sends_nothing(fake_dojah, secret, app_id):
    with pytest.raises(DojahError) as excinfo:
        DojahClient(base_url=TEST_BASE_URL, secret_key=secret, app_id=app_id).get_verification(new_reference())
    assert excinfo.value.kind == NOT_CONFIGURED and excinfo.value.needs_ops_alert and fake_dojah.requests == []


# --- error classification ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("arrange, kind, retryable, outage, alert", [
    ("wallet_empty", WALLET_EMPTY, False, True, True),
    ("unauthorised", UNAUTHORISED, False, True, True),
    ("rate_limited", RATE_LIMITED, True, True, False),
    ("server_error", UNAVAILABLE, True, True, False),
    ("outage", UNAVAILABLE, True, True, False),
    ("timeout", TIMEOUT, True, True, False),
])
def test_failures_map_onto_kinds(client, fake_dojah, arrange, kind, retryable, outage, alert):
    getattr(fake_dojah, arrange)()
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    exc = excinfo.value
    assert (exc.kind, exc.retryable, exc.outage, exc.needs_ops_alert) == (kind, retryable, outage, alert)


def test_a_400_that_talks_about_the_balance_is_a_wallet_problem(client, fake_dojah):
    fake_dojah.wallet_empty(http_400=True)
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.kind == WALLET_EMPTY and excinfo.value.http_status == 400


def test_an_unknown_reference_is_not_found_and_not_an_outage(client, fake_dojah):
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    exc = excinfo.value
    assert (exc.kind, exc.retryable, exc.outage) == (NOT_FOUND, False, False)


def test_another_4xx_is_rejected_and_not_an_outage(client, fake_dojah):
    fake_dojah.script(httpx.Response(422, json={"error": "bad reference"}))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    exc = excinfo.value
    assert (exc.kind, exc.retryable, exc.outage) == (REJECTED, False, False)


def test_a_rate_limit_carries_the_wait_it_was_told_capped(client, fake_dojah):
    fake_dojah.script(httpx.Response(429, json={}, headers={"Retry-After": "9999"}))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.retry_after == dojah.MAX_RETRY_AFTER_SECONDS
    fake_dojah.script(httpx.Response(429, json={}))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.retry_after == dojah.DEFAULT_RATE_LIMIT_WAIT_SECONDS


# --- retries: only a 5xx or a transport error ------------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 429])
def test_a_4xx_is_not_retried(client, fake_dojah, status):
    fake_dojah.script(httpx.Response(status, json={}))
    with pytest.raises(DojahError):
        client.get_verification(new_reference())
    assert len(fake_dojah.requests) == 1


def test_a_5xx_is_retried_with_backoff_then_succeeds(client, fake_dojah, monkeypatch):
    sleeps = []
    monkeypatch.setattr(dojah, "_sleep", sleeps.append)
    ref = new_reference()
    fake_dojah.script(httpx.Response(503, json={}), httpx.Response(500, json={}))
    fake_dojah.script_verification(ref)
    assert client.get_verification(ref).reference_id == ref
    assert len(fake_dojah.requests) == 3 and sleeps == [0.5, 1.0]


def test_a_transport_error_is_retried_then_succeeds(client, fake_dojah):
    ref = new_reference()
    fake_dojah.script(httpx.ConnectError("boom"))
    fake_dojah.script_verification(ref)
    assert client.get_verification(ref).reference_id == ref
    assert len(fake_dojah.requests) == 2


def test_three_failures_exhaust_the_attempts_and_raise_the_last_kind(client, fake_dojah):
    fake_dojah.script(httpx.Response(503, json={}), httpx.Response(503, json={}), httpx.Response(502, json={}))
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.kind == UNAVAILABLE and excinfo.value.http_status == 502 and len(fake_dojah.requests) == 3


def test_a_connection_failure_that_never_recovers_is_a_network_error(client, fake_dojah):
    fake_dojah.script(*[httpx.ConnectError("boom")] * 3)
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.kind == NETWORK and len(fake_dojah.requests) == 3


def test_a_timeout_that_never_recovers_is_a_timeout(client, fake_dojah):
    fake_dojah.timeout()
    with pytest.raises(DojahError) as excinfo:
        client.get_verification(new_reference())
    assert excinfo.value.kind == TIMEOUT and len(fake_dojah.requests) == 3


def test_an_error_carries_neither_the_key_nor_a_body(client, fake_dojah, caplog):
    fake_dojah.script(httpx.Response(500, json={"error": "SENTINEL-BODY-TEXT"}), httpx.Response(500, json={}), httpx.Response(500, json={}))
    with caplog.at_level("DEBUG"):
        with pytest.raises(DojahError) as excinfo:
            client.get_verification(new_reference())
    text = str(excinfo.value) + caplog.text
    assert TEST_SECRET_KEY not in text and "SENTINEL-BODY-TEXT" not in text and TEST_APP_ID not in text
