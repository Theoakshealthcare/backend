"""C3 item 1: push-token endpoints and the Expo PushService. Expo is always a MockTransport."""

import httpx
import pytest

from app.core.config import settings
from app.db.models.push_token import PushToken
from app.services import push
from app.services.push import PushService
from app.tests.factories import auth_headers, make_caregiver, make_patient, make_user, refresh_headers

URL = "/api/v1/devices/push-token"


def tok(n: int) -> str:
    return f"ExponentPushToken[placeholder-{n:04d}]"


def rows(db):
    db.expire_all()
    return {t.token: t for t in db.query(PushToken).all()}


# --- register ---------------------------------------------------------------------


def test_register_requires_authentication(db, client):
    body = {"token": tok(1), "platform": "ios"}
    assert client.post(URL, json=body).status_code == 401
    cg = make_caregiver(db)
    assert client.post(URL, json=body, headers=refresh_headers(cg.user)).status_code == 401
    assert rows(db) == {}


@pytest.mark.parametrize("maker", [make_caregiver, make_patient])
def test_register_creates_a_row_for_the_caller(db, client, maker):
    user = maker(db).user
    r = client.post(URL, json={"token": tok(1), "platform": "ios"}, headers=auth_headers(user))
    assert r.status_code == 200, r.text
    assert r.json() == {"token": tok(1), "platform": "ios"}
    row = rows(db)[tok(1)]
    assert row.user_id == user.id and row.platform == "ios"
    assert row.created_at is not None and row.last_seen_at is not None


def test_registering_again_refreshes_last_seen_without_duplicating(db, client):
    cg = make_caregiver(db)
    h = auth_headers(cg.user)
    client.post(URL, json={"token": tok(1), "platform": "android"}, headers=h)
    first = rows(db)[tok(1)]
    created, seen = first.created_at, first.last_seen_at
    r = client.post(URL, json={"token": tok(1), "platform": "ios"}, headers=h)
    assert r.status_code == 200
    again = rows(db)
    assert len(again) == 1
    assert again[tok(1)].created_at == created
    assert again[tok(1)].last_seen_at > seen
    assert again[tok(1)].platform == "ios"


def test_a_token_that_moves_to_another_account_follows_the_new_login(db, client):
    old, new = make_caregiver(db), make_caregiver(db)
    client.post(URL, json={"token": tok(1), "platform": "ios"}, headers=auth_headers(old.user))
    client.post(URL, json={"token": tok(1), "platform": "ios"}, headers=auth_headers(new.user))
    all_rows = rows(db)
    assert len(all_rows) == 1 and all_rows[tok(1)].user_id == new.user_id


@pytest.mark.parametrize(
    "body",
    [
        {"token": "not-an-expo-token", "platform": "ios"},
        {"token": "ExponentPushToken[]", "platform": "ios"},
        {"token": tok(1), "platform": "windows-phone"},
        {"token": tok(1)},
        {"platform": "ios"},
        {"token": "ExponentPushToken[" + "x" * 300 + "]", "platform": "ios"},
    ],
)
def test_register_validates_the_body(db, client, body):
    cg = make_caregiver(db)
    assert client.post(URL, json=body, headers=auth_headers(cg.user)).status_code == 422
    assert rows(db) == {}


def test_expo_push_token_prefix_is_accepted(db, client):
    cg = make_caregiver(db)
    r = client.post(URL, json={"token": "ExpoPushToken[abc-123]", "platform": "android"},
                    headers=auth_headers(cg.user))
    assert r.status_code == 200


# --- delete -----------------------------------------------------------------------


def test_delete_requires_authentication(db, client):
    assert client.request("DELETE", URL, json={"token": tok(1)}).status_code == 401


def test_delete_removes_only_the_callers_token_and_is_idempotent(db, client):
    mine, theirs = make_caregiver(db), make_caregiver(db)
    client.post(URL, json={"token": tok(1), "platform": "ios"}, headers=auth_headers(mine.user))
    client.post(URL, json={"token": tok(2), "platform": "ios"}, headers=auth_headers(theirs.user))

    # someone else's token: same answer as success, but nothing is deleted
    r = client.request("DELETE", URL, json={"token": tok(2)}, headers=auth_headers(mine.user))
    assert r.status_code == 204
    assert set(rows(db)) == {tok(1), tok(2)}

    for _ in range(2):
        r = client.request("DELETE", URL, json={"token": tok(1)}, headers=auth_headers(mine.user))
        assert r.status_code == 204
    assert set(rows(db)) == {tok(2)}


# --- PushService ------------------------------------------------------------------


def seed_tokens(db, count, user=None):
    user = user or make_caregiver(db).user
    for i in range(count):
        db.add(PushToken(user_id=user.id, token=tok(i), platform="ios"))
    db.commit()
    return user


def test_sends_one_message_per_token_with_the_given_payload(db, expo):
    user = seed_tokens(db, 2)
    sent = PushService().send_to_users(db, [user.id], title="T", body="B", data={"booking_id": "x"})
    assert sent == 2
    assert [m["to"] for m in expo.messages] == [tok(0), tok(1)]
    for m in expo.messages:
        assert m["title"] == "T" and m["body"] == "B" and m["data"] == {"booking_id": "x"}
        assert m["priority"] == "high" and m["sound"] == "default"


def test_uses_the_configured_url_timeout_and_optional_access_token(db, expo, monkeypatch):
    user = seed_tokens(db, 1)
    monkeypatch.setattr(settings, "EXPO_PUSH_TIMEOUT_SECONDS", 3.5)
    monkeypatch.setattr(settings, "EXPO_ACCESS_TOKEN", "placeholder-expo-access-token")
    PushService().send_to_users(db, [user.id], title="T", body="B", data={})
    req = expo.requests[0]
    assert str(req.url) == "http://expo.test/--/api/v2/push/send"
    assert req.extensions["timeout"]["read"] == 3.5
    assert req.headers["authorization"] == "Bearer placeholder-expo-access-token"


def test_no_authorization_header_without_an_access_token(db, expo, monkeypatch):
    user = seed_tokens(db, 1)
    monkeypatch.setattr(settings, "EXPO_ACCESS_TOKEN", None)
    PushService().send_to_users(db, [user.id], title="T", body="B", data={})
    assert "authorization" not in expo.requests[0].headers


def test_batches_of_at_most_100(db, expo):
    user = seed_tokens(db, 250)
    assert PushService().send_to_users(db, [user.id], title="T", body="B", data={}) == 250
    assert [len(b) for b in expo.batches] == [100, 100, 50]


def test_users_without_tokens_cause_no_request(db, expo):
    stranger = make_caregiver(db).user
    assert PushService().send_to_users(db, [stranger.id], title="T", body="B", data={}) == 0
    assert PushService().send_to_users(db, [], title="T", body="B", data={}) == 0
    assert expo.requests == []


def test_device_not_registered_tokens_are_pruned_and_others_kept(db, expo):
    user = seed_tokens(db, 3)
    expo.not_registered = {tok(1)}
    expo.other_errors = {tok(2): "MessageRateExceeded"}
    sent = PushService().send_to_users(db, [user.id], title="T", body="B", data={})
    assert sent == 1  # only tok(0) was accepted
    assert set(rows(db)) == {tok(0), tok(2)}  # a rate-limit error is not a dead device


def test_pruning_works_across_batches(db, expo):
    user = seed_tokens(db, 150)
    expo.not_registered = {tok(5), tok(120)}
    PushService().send_to_users(db, [user.id], title="T", body="B", data={})
    assert len(rows(db)) == 148 and tok(5) not in rows(db) and tok(120) not in rows(db)


@pytest.mark.parametrize("failure", ["http500", "timeout", "connect"])
def test_expo_failures_never_raise_and_keep_the_tokens(db, expo, failure):
    user = seed_tokens(db, 2)
    if failure == "http500":
        expo.status_code = 500
    elif failure == "timeout":
        expo.raise_error = httpx.ReadTimeout("slow")
    else:
        expo.raise_error = httpx.ConnectError("down")
    assert PushService().send_to_users(db, [user.id], title="T", body="B", data={}) == 0
    assert len(rows(db)) == 2


def test_a_garbled_expo_response_is_survived(db, monkeypatch):
    user = seed_tokens(db, 1)
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    assert PushService().send_to_users(db, [user.id], title="T", body="B", data={}) == 0
    assert len(rows(db)) == 1


def test_without_a_mock_the_test_network_guard_is_what_stops_a_real_call(db):
    """Proves tests can never reach Expo: the harness blocks httpx, and the service swallows it."""
    user = seed_tokens(db, 1)
    assert push._transport is None
    assert PushService().send_to_users(db, [user.id], title="T", body="B", data={}) == 0
    assert len(rows(db)) == 1
