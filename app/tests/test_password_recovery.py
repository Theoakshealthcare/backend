"""Forgot-password flow: POST /auth/password-recovery -> email -> POST /auth/reset-password -> sign in.

These cover the email transport and the deprecated long-token form; the emailed code is in test_password_reset_code.py.

The real ``send_password_reset_email`` and ``send_email`` run in these tests; only the Resend HTTP transport (an
``httpx.MockTransport``) is faked. A signature mismatch between them (the original ``subject_template`` bug, which made
every recovery request a 500) therefore fails here, which the ``sent_emails`` fixture (it replaces the whole sender) cannot catch.
"""

import json
import logging
import re

import httpx
import pytest

from app.core.config import settings
from app.services import email as email_service
from app.tests.factories import DEFAULT_PASSWORD, make_caregiver, make_patient

RECOVER = "/api/v1/auth/password-recovery"
RESET = "/api/v1/auth/reset-password"
LOGIN = "/api/v1/auth/login/mobile"
NEW_PASSWORD = "Brand-New-Pass-42"
RECOVERY_REPLY = {"message": "Password recovery email sent"}


class FakeResend:
    """Stands in for the Resend API. Records every request; ``status`` makes it answer with an error."""

    requests: list = []
    status: int = 200

    @classmethod
    def handler(cls, request: httpx.Request) -> httpx.Response:
        cls.requests.append(request)
        if cls.status >= 400:
            return httpx.Response(cls.status, json={"name": "validation_error", "message": "The domain is not verified"})
        return httpx.Response(200, json={"id": "email-id-placeholder"})


@pytest.fixture
def smtp(monkeypatch):
    """Resend configured, transport faked. Returns the outbox: ``collect()`` gives (to, subject, html) per email sent."""
    FakeResend.requests, FakeResend.status = [], 200
    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(FakeResend.handler))
    monkeypatch.setattr(settings, "RESEND_KEY", "re_placeholder_not_a_secret")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setattr(settings, "EMAILS_FROM_NAME", "Oakly")

    class Outbox(list):
        @property
        def connections(self):
            return len(FakeResend.requests)

    outbox = Outbox()

    def collect():
        outbox.clear()
        for request in FakeResend.requests:
            body = json.loads(request.content)
            assert request.headers["authorization"] == "Bearer re_placeholder_not_a_secret"
            assert body["from"] == "Oakly <noreply@example.test>"
            [recipient] = body["to"]
            outbox.append((recipient, body["subject"], body["html"]))
        return outbox

    outbox.collect = collect
    return outbox


def token_from(html: str) -> str:
    """The deprecated long token (what the shipped caregiver app pastes): printed in small type, no longer a link."""
    match = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", html)
    assert match, "the email carries no legacy reset token"
    return match.group(1)


def sign_in(client, user, password):
    return client.post(LOGIN, json={"email_or_phone": user.email, "password": password})


def test_recovery_sends_the_reset_email_through_the_real_sender(db, client, smtp):
    user = make_caregiver(db).user

    r = client.post(RECOVER, json={"email": user.email})

    assert r.status_code == 200, r.text
    assert r.json() == RECOVERY_REPLY
    [(recipient, subject, html)] = smtp.collect()
    assert recipient == user.email
    assert "password reset code" in subject
    assert token_from(html)  # still there for the shipped caregiver app (deprecated)
    assert re.search(r">\s*[0-9]{5}\s*</p>", html)  # the 5-digit code, shown on its own


def test_the_whole_reset_flow_ends_with_the_new_password_working(db, client, smtp):
    user = make_patient(db).user
    client.post(RECOVER, json={"email": user.email})
    token = token_from(smtp.collect()[0][2])

    r = client.post(RESET, json={"token": token, "new_password": NEW_PASSWORD})

    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Password updated successfully"}
    assert sign_in(client, user, NEW_PASSWORD).status_code == 200
    assert sign_in(client, user, DEFAULT_PASSWORD).status_code == 401


def test_a_reset_link_works_once(db, client, smtp):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    token = token_from(smtp.collect()[0][2])
    assert client.post(RESET, json={"token": token, "new_password": NEW_PASSWORD}).status_code == 200

    r = client.post(RESET, json={"token": token, "new_password": "Another-Pass-77"})

    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid or expired token"
    assert sign_in(client, user, NEW_PASSWORD).status_code == 200


def test_an_older_link_stops_working_when_a_newer_one_is_requested(db, client, smtp):
    user = make_caregiver(db).user
    client.post(RECOVER, json={"email": user.email})
    first = token_from(smtp.collect()[0][2])
    # tokens carry second-resolution timestamps; make the second one differ from the first
    import time

    time.sleep(1.1)
    client.post(RECOVER, json={"email": user.email})
    second = token_from(smtp.collect()[1][2])
    assert first != second

    assert client.post(RESET, json={"token": first, "new_password": NEW_PASSWORD}).status_code == 400
    assert client.post(RESET, json={"token": second, "new_password": NEW_PASSWORD}).status_code == 200


def test_a_garbage_token_is_refused(db, client):
    make_caregiver(db)
    r = client.post(RESET, json={"token": "not-a-token", "new_password": NEW_PASSWORD})
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid token"


def test_an_unknown_email_gets_the_same_reply_and_no_email(client, smtp):
    r = client.post(RECOVER, json={"email": "nobody@example.com"})

    assert r.status_code == 200
    assert r.json() == RECOVERY_REPLY
    assert smtp.connections == 0


def test_with_the_resend_key_unset_the_endpoint_still_succeeds_and_logs(db, client, smtp, monkeypatch, caplog):
    user = make_caregiver(db).user
    monkeypatch.setattr(settings, "RESEND_KEY", "")

    with caplog.at_level(logging.WARNING, logger=email_service.logger.name):
        r = client.post(RECOVER, json={"email": user.email})

    assert r.status_code == 200, r.text
    assert r.json() == RECOVERY_REPLY
    assert smtp.connections == 0  # nothing is dialled when email is not configured
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Email is not configured" in logged
    assert user.email not in logged  # the address is not written to the log


def test_a_failing_mail_service_does_not_turn_recovery_into_a_500(db, client, smtp, caplog):
    user = make_caregiver(db).user
    FakeResend.status = 403

    with caplog.at_level(logging.WARNING, logger=email_service.logger.name):
        r = client.post(RECOVER, json={"email": user.email})

    assert r.status_code == 200, r.text
    assert r.json() == RECOVERY_REPLY
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Failed to send email" in logged and "403" in logged
    assert "re_placeholder_not_a_secret" not in logged  # the key is never written to the log


def test_a_network_error_is_logged_not_raised(db, client, smtp, monkeypatch, caplog):
    user = make_caregiver(db).user

    def boom(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(boom))
    with caplog.at_level(logging.WARNING, logger=email_service.logger.name):
        r = client.post(RECOVER, json={"email": user.email})

    assert r.status_code == 200, r.text
    assert any("Failed to send email" in rec.getMessage() for rec in caplog.records)


def test_the_other_senders_match_the_transport_signature_too(db, smtp):
    """Every function in services/email.py that sends goes through send_email with the right arguments."""
    email_service.send_verification_email("someone@example.test", "123456")
    email_service.send_password_reset_email("someone@example.test", "12345", "a-token")
    assert email_service.send_caregiver_decision_email(
        email_to="someone@example.test", full_name="Ada", decision="approved"
    ) is True

    recipients = [to for to, _subject, _html in smtp.collect()]
    assert recipients == ["someone@example.test"] * 3
