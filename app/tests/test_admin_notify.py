"""Admin e-mails about caregiver onboarding (app/services/admin_notify.py).

The real ``send_email`` runs; only the Resend HTTP transport is faked (``httpx.MockTransport``), as in test_password_recovery.py.
Mail is sent on the calling thread here (``_send_inline``); one test checks that in production it is handed to a worker thread.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta

import httpx
import pytest

from app.core.admin_permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS
from app.core.config import settings
from app.db.models.caregiver import Caregiver, CaregiverDocument
from app.db.models.identity_verification import IdentityVerification, KycWebhookEvent
from app.services import admin_notify, kyc_notify
from app.services import email as email_service
from app.services.kyc_events import process_event
from app.services.kyc_hooks import KycHooks
from app.tests.fake_dojah import FakeProviderAvailability, fake_dojah, new_reference  # noqa: F401
from app.tests.factories import auth_headers, make_admin, make_caregiver

CONSENT = "/api/v1/caregiver/consent"
UPLOAD = "/api/v1/caregiver/documents/upload"
ALL_CONSENTS = {"terms_accepted": True, "privacy_policy_accepted": True, "background_check_consent": True}
NAME = "Ada <b>Lovelace</b>"


class Outbox:
    """Every request the fake Resend saw. ``status`` makes it answer with an error."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"message": "provider error"})
        return httpx.Response(200, json={"id": "email-id-placeholder"})

    @property
    def mails(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    @property
    def to(self) -> list[str]:
        return sorted(m["to"][0] for m in self.mails)


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(box.handler))
    monkeypatch.setattr(settings, "RESEND_KEY", "re_placeholder_not_a_secret")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setattr(settings, "EMAILS_FROM_NAME", "Oakly")
    monkeypatch.setattr(settings, "ADMIN_NOTIFY_EMAIL", "")
    monkeypatch.setattr(settings, "ADMIN_CONSOLE_URL", "https://admin.oaklycare.com")
    monkeypatch.setattr(admin_notify, "_send_inline", True)
    return box


@pytest.fixture
def admins(db):
    """Two who may approve (Super Admin, Verification), and four who may not or are not active."""
    return {
        "super": make_admin(db, ["super_admin"], password=None, email="super@example.test"),
        "verifier": make_admin(db, ["verification"], password=None, email="Verifier@Example.test"),
        "ops": make_admin(db, ["operations"], password=None, email="ops@example.test"),
        "finance": make_admin(db, ["finance"], password=None, email="finance@example.test"),
        "disabled": make_admin(db, ["verification"], password=None, status="disabled", email="disabled@example.test"),
        "invited": make_admin(db, ["super_admin"], password=None, status="invited", email="invited@example.test"),
    }


APPROVERS = ["super@example.test", "verifier@example.test"]


def applicant(db, **kw):
    kw.setdefault("full_name", NAME)
    kw.setdefault("profile_status", "incomplete")
    return make_caregiver(db, approved=False, **kw)


def fresh(db, caregiver_id):
    db.expire_all()  # the test's session still holds the row as it was before the request
    return db.get(Caregiver, caregiver_id)


def doc(db, caregiver, *, minutes_ago=0, name="Licence.pdf"):
    row = CaregiverDocument(caregiver_id=caregiver.id, document_type="license", document_name=name, file_path=f"k/{uuid.uuid4()}",
                            uploaded_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
    db.add(row)
    db.commit()
    return row


# --- who is told -----------------------------------------------------------------------------------------------------
def test_the_permission_is_a_real_one_held_by_the_approving_roles_only():
    assert admin_notify.APPROVE_PERMISSION in ALL_PERMISSIONS
    holders = {role for role, perms in ROLE_PERMISSIONS.items() if admin_notify.APPROVE_PERMISSION in perms}
    assert holders == {"super_admin", "verification"}


def test_recipients_are_exactly_the_active_admins_who_may_approve(db, outbox, admins):
    cg = applicant(db)
    admin_notify.notify("submitted", caregiver_id=cg.id)
    assert outbox.to == APPROVERS  # not operations, not finance, not disabled, not invited


def test_the_optional_extra_address_is_added_once(db, outbox, admins, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_NOTIFY_EMAIL", "  Owner@Example.test ")
    cg = applicant(db)
    admin_notify.notify("submitted", caregiver_id=cg.id)
    assert outbox.to == sorted([*APPROVERS, "owner@example.test"])

    outbox.requests.clear()
    monkeypatch.setattr(settings, "ADMIN_NOTIFY_EMAIL", "SUPER@example.test")  # already an admin: not sent twice
    admin_notify.notify("submitted", caregiver_id=cg.id)
    assert outbox.to == APPROVERS


def test_with_nobody_to_tell_nothing_is_sent(db, outbox):
    admin_notify.notify("submitted", caregiver_id=applicant(db).id)
    assert outbox.requests == []


def test_the_extra_address_alone_is_enough(db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_NOTIFY_EMAIL", "owner@example.test")
    admin_notify.notify("submitted", caregiver_id=applicant(db).id)
    assert outbox.to == ["owner@example.test"]


# --- what they are told ----------------------------------------------------------------------------------------------
def test_the_mail_has_the_name_and_the_link_and_nothing_private(db, outbox, admins):
    cg = applicant(db, phone_number="+2348000000001", address="12 Placeholder Street", state="Lagos",
                   emergency_contact_phone="+2348000000002", emergency_contact_name="Placeholder Kin",
                   date_of_birth=datetime(1990, 4, 12).date())
    for event in ("submitted", "resubmitted", "identity_review"):
        outbox.requests.clear()
        admin_notify.notify(event, caregiver_id=cg.id)
        assert len(outbox.mails) == 2
        for mail in outbox.mails:
            html = mail["html"]
            assert "Ada &lt;b&gt;Lovelace&lt;/b&gt;" in html  # the name, escaped
            assert f"https://admin.oaklycare.com/caregivers/applications/{cg.id}" in html
            assert mail["subject"].startswith(f"{settings.PROJECT_NAME} - ")
            private = [cg.phone_number, cg.address, cg.email, cg.emergency_contact_phone, cg.emergency_contact_name, "1990-04-12", "12 April",
                       "Lagos", "reason", "mismatch", "BVN", "NIN", str(cg.user_id)]
            assert not [p for p in private if p in html or p in mail["subject"]], mail["subject"]


def test_the_link_is_built_from_the_console_setting(db, outbox, admins, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_CONSOLE_URL", "https://admin.staging.example.test/")
    cg = applicant(db)
    admin_notify.notify("submitted", caregiver_id=cg.id)
    assert f'href="https://admin.staging.example.test/caregivers/applications/{cg.id}"' in outbox.mails[0]["html"]


def test_a_document_mail_has_the_name_and_the_link_but_not_the_document(db, outbox, admins):
    cg = applicant(db, profile_status="under_review")
    admin_notify.on_document_uploaded(cg.id, doc(db, cg, name="Secret-Licence.pdf").id)
    html = outbox.mails[0]["html"]
    assert "Ada &lt;b&gt;Lovelace&lt;/b&gt;" in html and f"/caregivers/applications/{cg.id}" in html
    assert "Secret-Licence" not in json.dumps(outbox.mails) and "license" not in html.lower()


def test_each_event_says_its_own_thing(db, outbox, admins):
    cg = applicant(db, profile_status="under_review")
    headings = {}
    for event in ("submitted", "resubmitted", "identity_review"):
        outbox.requests.clear()
        admin_notify.notify(event, caregiver_id=cg.id)
        headings[event] = outbox.mails[0]["subject"]
    assert len(set(headings.values())) == 3


# --- trigger 1: the profile enters under_review ----------------------------------------------------------------------
def put_consent(client, cg, **body):
    return client.put(CONSENT, json={**ALL_CONSENTS, **body}, headers=auth_headers(cg.user))


def test_submitting_the_profile_mails_once_and_not_on_a_repeat(db, client, outbox, admins):
    cg = applicant(db)
    assert put_consent(client, cg).status_code == 200
    assert fresh(db, cg.id).profile_status == "under_review"
    assert len(outbox.requests) == 2 and "submitted" in outbox.mails[0]["subject"]

    assert put_consent(client, cg).status_code == 200  # the same state again
    assert len(outbox.requests) == 2


def test_a_resubmission_after_changes_mails_again(db, client, outbox, admins):
    cg = applicant(db, profile_status="changes_requested")
    assert put_consent(client, cg).status_code == 200
    assert fresh(db, cg.id).profile_status == "under_review"
    assert len(outbox.requests) == 2 and "resubmitted" in outbox.mails[0]["subject"]


@pytest.mark.parametrize("status", ["approved", "rejected", "suspended"])
def test_a_decided_caregiver_sending_consent_mails_no_one(db, client, outbox, admins, status):
    cg = applicant(db, profile_status=status)
    assert put_consent(client, cg).status_code == 200
    assert outbox.requests == []


def test_incomplete_consent_mails_no_one(db, client, outbox, admins):
    cg = applicant(db)
    assert put_consent(client, cg, terms_accepted=False).status_code == 200
    assert fresh(db, cg.id).profile_status == "incomplete" and outbox.requests == []


# --- trigger 2: an identity check ends as needs_review ---------------------------------------------------------------
class Engine:
    """The KYC worker with the production notifier (``kyc_notify.deliver``) instead of a recorder."""

    def __init__(self, db, fake):
        self.db, self.fake = db, fake
        self.hooks = KycHooks(availability=FakeProviderAvailability(), notify=kyc_notify.deliver, alert=lambda kind: None,)

    def run(self, caregiver, **script):
        attempt = IdentityVerification(caregiver_id=caregiver.id, attempt_no=1, status="awaiting_result",
                                       provider_session_id=new_reference())
        self.db.add(attempt)
        self.db.commit()
        self.fake.script_clean_pass(attempt.provider_session_id, caregiver, **script)
        return attempt, self.replay(attempt)

    def replay(self, attempt):
        event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id=attempt.provider_session_id, signature_ok=True)
        self.db.add(event)
        self.db.commit()
        return process_event(self.db, {"event_id": str(event.id), "claim": None, "settled": True}, hooks=self.hooks)


@pytest.fixture
def engine(db, fake_dojah, monkeypatch):
    monkeypatch.setattr(kyc_notify.push_service, "get_push_service", lambda: type("P", (), {"send_to_users": lambda *a, **k: None})())
    return Engine(db, fake_dojah)


def kyc_caregiver(db):
    return applicant(db, date_of_birth=datetime(1990, 4, 12).date(), profile_completion_step=2, identity_verification_status="in_progress",
                     profile_status="incomplete")


def test_a_check_that_needs_review_mails_the_admins_once(db, engine, outbox, admins):
    cg = kyc_caregiver(db)
    attempt, _ = engine.run(cg, first_name="CHINEDU", middle_name=None, last_name="OKAFOR")  # a name mismatch
    assert fresh(db, cg.id).identity_verification_status == "needs_review"
    to_admins = [m for m in outbox.mails if m["to"][0] in APPROVERS]
    assert len(to_admins) == 2 and "identity" in to_admins[0]["subject"].lower()
    assert "mismatch" not in json.dumps(outbox.mails).lower()  # the reason never leaves the server

    sent = len(outbox.requests)
    engine.replay(attempt)  # the same webhook again: a decided check is not decided twice
    assert len(outbox.requests) == sent


def test_a_pass_or_a_failure_mails_no_admin(db, engine, outbox, admins):
    passed = kyc_caregiver(db)
    engine.run(passed)
    failed = kyc_caregiver(db)
    attempt = IdentityVerification(caregiver_id=failed.id, attempt_no=1, status="awaiting_result", provider_session_id=new_reference())
    db.add(attempt)
    db.commit()
    engine.fake.script_verification(attempt.provider_session_id, status="Failed", confidence=20, selfie=False)
    engine.replay(attempt)
    assert fresh(db, passed.id).identity_verification_status == "verified"
    assert fresh(db, failed.id).identity_verification_status == "in_progress"
    assert [m for m in outbox.mails if m["to"][0] in APPROVERS] == []


# --- trigger 3: a document while the application is open -------------------------------------------------------------
@pytest.mark.parametrize("status,sent", [("under_review", True), ("changes_requested", True), ("incomplete", False),
                                         ("approved", False), ("rejected", False), ("suspended", False)])
def test_a_document_mails_only_while_the_application_is_open(db, outbox, admins, status, sent):
    cg = applicant(db, profile_status=status)
    admin_notify.on_document_uploaded(cg.id, doc(db, cg).id)
    assert len(outbox.requests) == (2 if sent else 0)


def test_documents_within_thirty_minutes_send_one_mail(db, outbox, admins):
    cg = applicant(db, profile_status="under_review")
    first, second, third = doc(db, cg, minutes_ago=20), doc(db, cg, minutes_ago=10), doc(db, cg, minutes_ago=0)
    for row in (first, second, third):
        admin_notify.on_document_uploaded(cg.id, row.id)
    assert len(outbox.requests) == 2  # one mail, two admins: the first of the burst


def test_a_document_after_a_quiet_half_hour_mails_again(db, outbox, admins):
    cg = applicant(db, profile_status="under_review")
    old = doc(db, cg, minutes_ago=31)
    admin_notify.on_document_uploaded(cg.id, old.id)
    admin_notify.on_document_uploaded(cg.id, doc(db, cg, minutes_ago=0).id)
    assert len(outbox.requests) == 4


def test_the_debounce_is_per_caregiver(db, outbox, admins):
    one, two = applicant(db, profile_status="under_review"), applicant(db, profile_status="under_review")
    admin_notify.on_document_uploaded(one.id, doc(db, one).id)
    admin_notify.on_document_uploaded(two.id, doc(db, two).id)
    assert len(outbox.requests) == 4


def test_the_upload_endpoint_mails_the_admins_once(db, client, outbox, admins, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    cg = applicant(db, profile_status="changes_requested")
    for _ in range(2):
        r = client.post(UPLOAD, files={"file": ("l.pdf", b"%PDF-1.4 x", "application/pdf")}, data={"document_type": "license"},
                        headers=auth_headers(cg.user))
        assert r.status_code == 200, r.text
    assert len(outbox.requests) == 2
    assert "Licence" not in json.dumps(outbox.mails) and "l.pdf" not in json.dumps(outbox.mails)  # no document name


# --- never breaks the request ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("failure", ["status", "connect"])
def test_a_failing_mail_transport_does_not_break_the_caregivers_request(db, client, outbox, admins, monkeypatch, caplog, failure):
    if failure == "status":
        outbox.status = 503
    else:
        def boom(request):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(boom))
    cg = applicant(db)
    with caplog.at_level(logging.WARNING):
        assert put_consent(client, cg).status_code == 200
    assert fresh(db, cg.id).profile_status == "under_review"  # committed before any mail


def test_a_crash_inside_the_notifier_is_logged_and_swallowed(db, client, outbox, admins, monkeypatch, caplog, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(admin_notify, "_recipients", boom)
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    cg = applicant(db)
    with caplog.at_level(logging.WARNING, logger=admin_notify.logger.name):
        assert put_consent(client, cg).status_code == 200
        r = client.post(UPLOAD, files={"file": ("l.pdf", b"%PDF-1.4 x", "application/pdf")}, data={"document_type": "license"},
                        headers=auth_headers(cg.user))
        assert r.status_code == 200
    assert sum("admin notify failed" in rec.getMessage() for rec in caplog.records) == 2
    assert fresh(db, cg.id).profile_status == "under_review" and outbox.requests == []


def test_the_notifier_never_raises_on_bad_input(db, outbox, admins):
    admin_notify.notify("submitted", caregiver_id=uuid.uuid4())  # no such caregiver
    admin_notify.notify("no_such_event", caregiver_id=uuid.uuid4())
    admin_notify.notify("submitted")
    assert outbox.requests == []


def test_in_production_the_send_runs_on_a_worker_not_on_the_request(db, outbox, admins, monkeypatch):
    submitted = []

    class Pool:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(admin_notify, "_send_inline", False)
    monkeypatch.setattr(admin_notify, "_executor", Pool())
    admin_notify.notify("submitted", caregiver_id=applicant(db).id)
    assert outbox.requests == [] and len(submitted) == 1  # nothing sent on this thread
    fn, args = submitted[0]
    fn(*args)
    assert outbox.to == APPROVERS


def test_a_kyc_notice_for_an_unknown_user_is_harmless(db, outbox, admins, monkeypatch):
    monkeypatch.setattr(kyc_notify.push_service, "get_push_service", lambda: type("P", (), {"send_to_users": lambda *a, **k: None})())
    kyc_notify.deliver(db, kyc_notify.Notice(kind="under_review", user_id=uuid.uuid4()))
    assert outbox.requests == []
