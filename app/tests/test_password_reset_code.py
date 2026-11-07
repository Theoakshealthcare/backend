"""Password reset by emailed 5-digit code: POST /auth/password-recovery then POST /auth/reset-password {email, code, ...}.

The real email sender runs (only the SMTP transport is faked, see test_password_recovery), so the code is read out of
the captured message the way a user reads it. The clock the service uses is pinned where a test needs expiry.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models.admin import AdminRateLimit
from app.db.models.user import User
from app.main import app as fastapi_app
from app.services import password_reset
from app.services.change_password import ChangePasswordService
from app.tests.factories import DEFAULT_PASSWORD, auth_headers, make_caregiver, make_patient
from app.tests.test_password_recovery import smtp, sign_in  # noqa: F401  (smtp is a fixture)

RECOVER = "/api/v1/auth/password-recovery"
RESET = "/api/v1/auth/reset-password"
REFRESH = "/api/v1/auth/refresh"
ME = "/api/v1/auth/me"
NEW_PASSWORD = "Brand-New-Pass-42"
RECOVERY_REPLY = {"message": "Password recovery email sent"}
INVALID = {"code": "invalid_or_expired_code", "message": "This code is incorrect or has expired. Request a new code."}
LOCKED = {"code": "too_many_attempts", "message": "Too many incorrect attempts. Request a new code."}


def code_from(html: str) -> str:
    match = re.search(r">\s*([0-9]{5})\s*</p>", html)
    assert match, "the email carries no 5-digit code"
    return match.group(1)


def wrong_code(real: str) -> str:
    return "00000" if real != "00000" else "11111"


def reset(client, email, code, password=NEW_PASSWORD):
    return client.post(RESET, json={"email": email, "code": code, "new_password": password})


def request_code(client, smtp, email) -> str:
    """Ask for a code and return the newest one in the outbox."""
    before = len(smtp.collect())
    assert client.post(RECOVER, json={"email": email}).status_code == 200
    mails = smtp.collect()
    assert len(mails) == before + 1, "no new email was sent"
    return code_from(mails[-1][2])


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = datetime.utcnow()

        def __call__(self):
            return self.now

        def advance(self, **kw):
            self.now += timedelta(**kw)

    c = Clock()
    monkeypatch.setattr(password_reset, "_now", c)
    return c


# --- the happy path ---------------------------------------------------------------------------------------


def test_the_whole_flow_ends_with_the_new_password_working(db, client, smtp):
    user = make_patient(db).user
    code = request_code(client, smtp, user.email)

    r = reset(client, user.email, code)

    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Password updated successfully"}  # no tokens come back
    assert r.headers["cache-control"] == "no-store"
    assert sign_in(client, user, NEW_PASSWORD).status_code == 200
    assert sign_in(client, user, DEFAULT_PASSWORD).status_code == 401


def test_the_email_shows_the_code_and_has_no_link_carrying_it(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    [(recipient, subject, html)] = smtp.collect()
    assert recipient == user.email
    assert subject == f"{settings.PROJECT_NAME} - Your password reset code"
    assert code not in subject
    assert "href=" not in html and "oaklycare://" not in html  # the code is never part of a URL
    assert "15 minutes" in html


def test_the_code_is_five_digits_and_stored_only_as_a_keyed_hash(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    assert re.fullmatch(r"[0-9]{5}", code)
    db.expire_all()
    stored = db.get(User, user.id)
    assert stored.password_reset_code_hash == password_reset.hash_code(user.id, code)
    assert code not in stored.password_reset_code_hash
    assert stored.password_reset_code_hash != password_reset.hash_code(user.id, wrong_code(code))
    assert stored.password_reset_code_expires_at > datetime.utcnow()
    # the same digits for another account hash differently (the user id is in the message)
    assert password_reset.hash_code(user.id, code) != password_reset.hash_code(make_patient(db).user.id, code)


def test_a_reset_ends_every_other_session_and_the_user_can_sign_in_after(db, client, smtp):
    user = make_caregiver(db).user
    old = auth_headers(user)
    old_refresh = client.post("/api/v1/auth/login/mobile",
                              json={"email_or_phone": user.email, "password": DEFAULT_PASSWORD}).json()["refresh_token"]
    assert client.get(ME, headers=old).status_code == 200
    code = request_code(client, smtp, user.email)

    assert reset(client, user.email, code).status_code == 200

    assert client.get(ME, headers=old).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": old_refresh}).status_code == 401
    fresh = sign_in(client, user, NEW_PASSWORD)
    assert fresh.status_code == 200
    assert client.get(ME, headers={"Authorization": f"Bearer {fresh.json()['access_token']}"}).status_code == 200
    db.expire_all()
    assert db.get(User, user.id).token_version == 1


def test_a_successful_reset_clears_the_code_and_the_counters(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    assert reset(client, user.email, wrong_code(code)).status_code == 400
    assert reset(client, user.email, code).status_code == 200

    db.expire_all()
    stored = db.get(User, user.id)
    assert stored.password_reset_code_hash is None and stored.password_reset_code_expires_at is None
    assert stored.password_reset_token is None
    assert db.scalars(select(AdminRateLimit).where(AdminRateLimit.key.like("pr%"))).all() == []


# --- one code, once ---------------------------------------------------------------------------------------


def test_a_code_works_once(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    assert reset(client, user.email, code).status_code == 200

    r = reset(client, user.email, code, password="Another-Pass-77")

    assert r.status_code == 400
    assert r.json()["detail"] == INVALID
    assert sign_in(client, user, NEW_PASSWORD).status_code == 200


def test_a_new_request_makes_the_previous_code_useless(db, client, smtp):
    user = make_caregiver(db).user
    first = request_code(client, smtp, user.email)
    second = request_code(client, smtp, user.email)
    if first == second:  # one chance in 100000; the point is that the older one is replaced
        second = request_code(client, smtp, user.email)
        first = "00000" if second != "00000" else "11111"

    assert reset(client, user.email, first).json()["detail"] == INVALID
    assert reset(client, user.email, second).status_code == 200


def test_concurrent_double_submit_uses_the_code_once(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    def submit(password):
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            return reset(c, user.email, code, password=password).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(submit, [NEW_PASSWORD, "Another-Pass-77"]))

    assert sorted(codes) == [200, 400], codes
    db.expire_all()
    assert db.get(User, user.id).token_version == 1  # reset once, not twice


# --- expiry ------------------------------------------------------------------------------------------------


def test_a_code_expires_after_fifteen_minutes(db, client, smtp, clock):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    clock.advance(minutes=15, seconds=1)
    r = reset(client, user.email, code)

    assert r.status_code == 400
    assert r.json()["detail"] == INVALID
    assert sign_in(client, user, DEFAULT_PASSWORD).status_code == 200  # untouched


def test_a_code_still_works_just_before_it_expires(db, client, smtp, clock):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    clock.advance(minutes=14, seconds=59)

    assert reset(client, user.email, code).status_code == 200


# --- wrong guesses ----------------------------------------------------------------------------------------


def test_a_wrong_code_is_a_stable_400(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    r = reset(client, user.email, wrong_code(code))

    assert r.status_code == 400
    assert r.json() == {"detail": INVALID}


@pytest.mark.parametrize("bad", ["", "1234", "123456", "abcde", "12 45", "１２３４５", "12345\n6"])
def test_a_malformed_code_gets_the_same_answer(db, client, smtp, bad):
    user = make_caregiver(db).user
    request_code(client, smtp, user.email)

    r = reset(client, user.email, bad)

    assert r.status_code == 400
    assert r.json() == {"detail": INVALID}


def test_five_wrong_codes_kill_the_code_even_for_the_right_one(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    wrong = wrong_code(code)
    for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS):
        assert reset(client, user.email, wrong).status_code == 400

    r = reset(client, user.email, code)

    assert r.status_code == 429
    assert r.json() == {"detail": LOCKED}
    assert sign_in(client, user, NEW_PASSWORD).status_code == 401  # nothing changed
    assert sign_in(client, user, DEFAULT_PASSWORD).status_code == 200


def test_a_fourth_wrong_code_still_leaves_the_right_one_working(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS - 1):
        assert reset(client, user.email, wrong_code(code)).status_code == 400

    assert reset(client, user.email, code).status_code == 200


def test_requesting_a_new_code_after_a_lockout_starts_over(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS):
        reset(client, user.email, wrong_code(code))
    assert reset(client, user.email, code).status_code == 429

    fresh = request_code(client, smtp, user.email)

    assert reset(client, user.email, fresh).status_code == 200


def test_a_burst_of_guesses_cannot_get_past_the_limit(db, client, smtp):
    """The address is locked while the guess is checked and counted, so exactly the allowed number of wrong guesses are
    answered 400 and every other call 429, even when they all arrive together."""
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    wrong = wrong_code(code)

    def guess(_):
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            return reset(c, user.email, wrong).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(guess, range(10)))

    assert codes.count(400) == settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS, codes
    assert codes.count(429) == 10 - settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS, codes
    assert reset(client, user.email, code).status_code == 429


def test_counters_idle_for_an_hour_start_again(db, client, smtp, clock):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS - 1):
        reset(client, user.email, wrong_code(code))
    clock.advance(minutes=14)  # the code is still live
    reset(client, user.email, wrong_code(code))  # the fifth: locked
    assert reset(client, user.email, code).status_code == 429
    clock.advance(hours=1, minutes=1)

    # the code itself has expired by now, so this is a plain wrong/expired answer, no longer a lockout
    assert reset(client, user.email, code).status_code == 400


# --- weak passwords ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("weak", ["short", "aaaaaaaaaa", "x" * 80])
def test_a_weak_password_is_refused_and_the_code_survives(db, client, smtp, weak):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS + 2):  # not guesses: never counted, never locks
        r = reset(client, user.email, code, password=weak)
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "weak_password"

    assert reset(client, user.email, code).status_code == 200  # the same code still works
    assert sign_in(client, user, NEW_PASSWORD).status_code == 200


def test_weak_password_is_reported_without_looking_at_the_code(db, client, smtp):
    """No oracle: a wrong code with a weak password gets the policy answer, exactly as the right code does."""
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)

    right = reset(client, user.email, code, password="short")
    wrong = reset(client, user.email, wrong_code(code), password="short")
    unknown = reset(client, "nobody@example.com", "12345", password="short")

    assert right.status_code == wrong.status_code == unknown.status_code == 400
    assert right.json() == wrong.json() == unknown.json()


# --- unknown addresses ---------------------------------------------------------------------------------------


def test_unknown_email_gets_the_identical_recovery_reply_and_no_mail(db, client, smtp):
    known = make_caregiver(db).user

    r_known = client.post(RECOVER, json={"email": known.email})
    r_unknown = client.post(RECOVER, json={"email": "nobody@example.com"})

    assert r_known.status_code == r_unknown.status_code == 200
    assert r_known.json() == r_unknown.json() == RECOVERY_REPLY
    assert [to for to, _s, _h in smtp.collect()] == [known.email]


def test_reset_for_an_unknown_email_is_indistinguishable_from_a_wrong_code(db, client, smtp):
    known = make_caregiver(db).user
    code = request_code(client, smtp, known.email)

    r_known = reset(client, known.email, wrong_code(code))
    r_unknown = reset(client, "nobody@example.com", "12345")

    assert (r_known.status_code, r_known.json()) == (r_unknown.status_code, r_unknown.json()) == (400, {"detail": INVALID})


def test_the_lockout_is_the_same_for_unknown_and_known_addresses(db, client, smtp):
    known = make_caregiver(db).user
    code = request_code(client, smtp, known.email)
    for email in (known.email, "nobody@example.com"):
        results = [reset(client, email, wrong_code(code)) for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS + 1)]
        assert [r.status_code for r in results] == [400] * settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS + [429], email
        assert results[-1].json() == {"detail": LOCKED}


def test_a_request_for_an_unknown_address_lifts_its_lockout_like_a_known_one(db, client, smtp):
    for email, known in (("nobody@example.com", None), (make_caregiver(db).user.email, True)):
        for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS):
            reset(client, email, "12345")
        assert reset(client, email, "12345").status_code == 429
        assert client.post(RECOVER, json={"email": email}).json() == RECOVERY_REPLY
        assert reset(client, email, "99999").status_code == 400, f"lockout not lifted for {email} (known={known})"


def test_an_inactive_account_gets_no_code_and_cannot_reset(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    user = db.get(User, user.id)
    user.is_active = False
    db.commit()

    assert reset(client, user.email, code).json()["detail"] == INVALID
    before = len(smtp.collect())
    assert client.post(RECOVER, json={"email": user.email}).json() == RECOVERY_REPLY
    assert len(smtp.collect()) == before


# --- request throttle ----------------------------------------------------------------------------------------


def test_requests_are_throttled_per_address_with_the_same_reply(db, client, smtp, clock, caplog):
    user = make_caregiver(db).user
    limit = settings.PASSWORD_RESET_REQUESTS_PER_HOUR
    codes = [request_code(client, smtp, user.email) for _ in range(limit)]
    assert len(smtp.collect()) == limit

    with caplog.at_level(logging.WARNING, logger=password_reset.logger.name):
        r = client.post(RECOVER, json={"email": user.email})

    assert r.status_code == 200 and r.json() == RECOVERY_REPLY
    assert len(smtp.collect()) == limit  # nothing more was sent
    assert any("throttled" in rec.getMessage() for rec in caplog.records)
    # the throttled request did not invalidate the live code
    assert reset(client, user.email, codes[-1]).status_code == 200


def test_the_throttle_is_per_address_and_resets_after_the_hour(db, client, smtp, clock):
    a, b = make_caregiver(db).user, make_patient(db).user
    for _ in range(settings.PASSWORD_RESET_REQUESTS_PER_HOUR):
        request_code(client, smtp, a.email)
    sent = len(smtp.collect())
    client.post(RECOVER, json={"email": a.email})
    assert len(smtp.collect()) == sent

    request_code(client, smtp, b.email)  # another address is unaffected
    clock.advance(hours=1, seconds=1)
    request_code(client, smtp, a.email)  # the window has passed


def test_an_unknown_address_is_throttled_too_so_the_reply_never_differs(db, client, smtp):
    for _ in range(settings.PASSWORD_RESET_REQUESTS_PER_HOUR + 2):
        assert client.post(RECOVER, json={"email": "nobody@example.com"}).json() == RECOVERY_REPLY
    rows = db.scalars(select(AdminRateLimit).where(AdminRateLimit.key.like("prr:%"))).all()
    assert len(rows) == 1 and rows[0].count == settings.PASSWORD_RESET_REQUESTS_PER_HOUR
    assert "nobody" not in rows[0].key  # the address is hashed


def test_stale_counter_rows_are_purged(db, client, smtp, clock):
    client.post(RECOVER, json={"email": "one@example.com"})
    clock.advance(hours=3)
    client.post(RECOVER, json={"email": "two@example.com"})

    keys = db.scalars(select(AdminRateLimit.key).where(AdminRateLimit.key.like("prr:%"))).all()
    assert len(keys) == 1


# --- logs and responses --------------------------------------------------------------------------------------


def test_no_code_password_or_token_ever_reaches_the_logs_or_a_response(db, client, smtp, caplog):
    user = make_caregiver(db).user
    with caplog.at_level(logging.DEBUG):
        code = request_code(client, smtp, user.email)
        wrong = wrong_code(code)
        bodies = [reset(client, user.email, wrong, password="Secret-Pass-111").text,
                  reset(client, user.email, code, password="short").text,
                  client.post(RESET, json={"email": user.email, "new_password": "Secret-Pass-222"}).text,
                  reset(client, user.email, code, password="Secret-Pass-333").text]
        for _ in range(settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS):
            reset(client, user.email, wrong)
        bodies.append(reset(client, user.email, code, password="Secret-Pass-444").text)
    logged = "\n".join(rec.getMessage() + " " + str(rec.args) for rec in caplog.records)

    for secret in (code, "Secret-Pass-111", "Secret-Pass-222", "Secret-Pass-333", "Secret-Pass-444", "short"):
        assert secret not in logged, secret
    for body in bodies:
        assert code not in body and "Secret-Pass" not in body
    assert hash_in(db, user) not in logged


def hash_in(db, user) -> str:
    db.expire_all()
    return db.get(User, user.id).password_reset_code_hash or "no-hash-stored"


def test_a_422_never_echoes_the_code_or_the_password(client):
    for body in ({"email": "a@example.com", "code": "12345"},  # no password
                 {"email": "a@example.com", "new_password": "Secret-Pass-555"},  # no code: model-level error
                 {"new_password": "Secret-Pass-555"},
                 {"token": "tok", "email": "a@example.com", "code": "12345", "new_password": "Secret-Pass-555"}):
        r = client.post(RESET, json=body)
        assert r.status_code == 422, body
        assert "Secret-Pass-555" not in r.text and "12345" not in r.text and "tok\"" not in r.text, r.text


# --- the deprecated token form ------------------------------------------------------------------------------------


def test_the_email_still_carries_the_legacy_token_in_small_print(db, client, smtp):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    [(_to, _subject, html)] = smtp.collect()
    assert code_from(html)
    assert "older version of the app" in html
    assert re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", html)


def test_the_legacy_token_form_still_works_and_also_ends_other_sessions(db, client, smtp):
    user = make_caregiver(db).user
    old = auth_headers(user)
    client.post(RECOVER, json={"email": user.email})
    legacy = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", smtp.collect()[0][2]).group(1)

    r = client.post(RESET, json={"token": legacy, "new_password": NEW_PASSWORD})

    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Password updated successfully"}
    assert sign_in(client, user, NEW_PASSWORD).status_code == 200
    assert client.get(ME, headers=old).status_code == 401
    db.expire_all()
    stored = db.get(User, user.id)
    assert stored.token_version == 1
    assert stored.password_reset_code_hash is None  # the unused code died with the reset


def test_the_legacy_token_keeps_its_old_errors_and_is_single_use(db, client, smtp):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    legacy = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", smtp.collect()[0][2]).group(1)
    assert client.post(RESET, json={"token": "not-a-token", "new_password": NEW_PASSWORD}).json() == {"detail": "Invalid token"}
    assert client.post(RESET, json={"token": legacy, "new_password": NEW_PASSWORD}).status_code == 200

    r = client.post(RESET, json={"token": legacy, "new_password": "Another-Pass-77"})

    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid or expired token"}


def test_a_weak_password_is_refused_on_the_legacy_path_too_without_using_the_token(db, client, smtp):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    legacy = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", smtp.collect()[0][2]).group(1)

    r = client.post(RESET, json={"token": legacy, "new_password": "short"})

    assert r.status_code == 400 and r.json()["detail"]["code"] == "weak_password"
    assert client.post(RESET, json={"token": legacy, "new_password": NEW_PASSWORD}).status_code == 200


def test_an_expired_legacy_token_is_refused(db, client, smtp, clock):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    legacy = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", smtp.collect()[0][2]).group(1)
    clock.advance(hours=settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS, minutes=1)

    r = client.post(RESET, json={"token": legacy, "new_password": NEW_PASSWORD})

    assert r.status_code == 400 and r.json() == {"detail": "Invalid or expired token"}


def test_with_the_legacy_form_switched_off_the_email_and_endpoint_drop_it(db, client, smtp, monkeypatch):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    legacy = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", smtp.collect()[0][2]).group(1)
    monkeypatch.setattr(settings, "PASSWORD_RESET_LEGACY_TOKEN_ENABLED", False)

    assert client.post(RESET, json={"token": legacy, "new_password": NEW_PASSWORD}).json() == {"detail": "Invalid token"}

    client.post(RECOVER, json={"email": user.email})
    assert not re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", smtp.collect()[1][2])
    assert "older version" not in smtp.collect()[1][2]


def test_the_form_must_be_one_or_the_other(client):
    assert client.post(RESET, json={"new_password": NEW_PASSWORD}).status_code == 422
    assert client.post(RESET, json={"email": "a@example.com", "new_password": NEW_PASSWORD}).status_code == 422
    assert client.post(RESET, json={"code": "12345", "new_password": NEW_PASSWORD}).status_code == 422
    assert client.post(RESET, json={"token": "t", "code": "12345", "email": "a@example.com",
                                    "new_password": NEW_PASSWORD}).status_code == 422


# --- neighbours --------------------------------------------------------------------------------------------------


def test_changing_the_password_kills_a_pending_code(db, client, smtp):
    user = make_caregiver(db).user
    code = request_code(client, smtp, user.email)
    ChangePasswordService(db).change(user.id, DEFAULT_PASSWORD, "Changed-Pass-9")

    assert reset(client, user.email, code).json()["detail"] == INVALID
    assert sign_in(client, user, "Changed-Pass-9").status_code == 200


def test_the_plain_service_helper_also_ends_sessions(db):
    from app.services.user import UserService

    user = make_caregiver(db).user
    assert UserService(db).reset_password(user.email, NEW_PASSWORD)
    db.expire_all()
    assert db.get(User, user.id).token_version == 1
