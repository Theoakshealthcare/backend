"""Writes the live captures behind the identity-verification section of the caregiver FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_KYC_CAREGIVER=1 poetry run pytest app/tests/test_capture_kyc_caregiver.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver`.
It only writes the 2xx files; scripts/capture_caregiver_flow.py (compose stack) rewrites the whole directory, so fold these
calls into it the next time it is run.

What is real and what is not: the app, the routes, the database and every response are real, and since K4 so is the decision
engine: the attempts below marked [engine] were decided by ``process_event`` / ``reconcile`` (the code that runs in production) from a
session served by ``FakePrembly``. NOTHING here has met the real Prembly: the session shapes are the documented ones and the fake's
guesses (design section 15). The "is Prembly up" flag is an in-memory stand-in for Redis (the engine wrote it, through the same
``KycHooks`` the worker uses); the widget id and key are placeholders. Attempt rows nothing can produce yet are seeded straight into
the database and marked [seeded].
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.db.models.identity_verification import IdentityVerification, KycWebhookEvent
from app.services.kyc_events import process_event
from app.services.kyc_hooks import KycHooks
from app.services.kyc_reconcile import reconcile
from app.tests.factories import auth_headers, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_KYC_CAREGIVER"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "caregiver"
BASE = "/api/v1/caregiver/identity-verification"


def _save(name, r, note=None):
    body = r.request.content
    doc = {
        "request": {"method": r.request.method, "path": r.request.url.path,
                    "auth": "Bearer <ACCESS_TOKEN_JWT>" if r.request.headers.get("authorization") else None,
                    "body": json.loads(body) if body else None},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")},
                     "body": r.json()},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _caregiver(db, **kw):
    kw.setdefault("profile_completion_step", 2)
    kw.setdefault("full_name", "Placeholder Caregiver")
    kw.setdefault("date_of_birth", date(1990, 4, 12))
    return make_caregiver(db, approved=False, **kw)


def _decided(db, fake_prembly, availability, caregiver, *, clean=True, **session):
    """[engine] One attempt for ``caregiver`` decided by the real worker code from a fake Prembly session."""
    n = db.query(IdentityVerification).filter_by(caregiver_id=caregiver.id).count() + 1
    attempt = IdentityVerification(caregiver_id=caregiver.id, attempt_no=n, status="awaiting_result")
    db.add(attempt)
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id=f"placeholder-session-{uuid.uuid4().hex[:8]}", signature_ok=True)
    db.add(event)
    db.commit()
    if clean:
        fake_prembly.script_clean_pass(event.provider_session_id, caregiver, **session)
    else:
        fake_prembly.script_session(event.provider_session_id, email=caregiver.email, **session)
    hooks = KycHooks(availability=availability, notify=lambda db_, notice: None, alert=lambda kind: None)
    process_event(db, {"event_id": str(event.id), "oakly_ref": str(attempt.reference)}, hooks=hooks)
    db.expire_all()
    return attempt


def _seed(db, caregiver, status, **kw):
    n = db.query(IdentityVerification).filter_by(caregiver_id=caregiver.id).count() + 1
    db.add(IdentityVerification(caregiver_id=caregiver.id, attempt_no=n, status=status, **kw))
    db.commit()


def test_capture(db, client, monkeypatch, fake_prembly, kyc_availability):
    monkeypatch.setattr(settings, "PREMBLY_WIDGET_ID", "placeholder-widget-id")
    monkeypatch.setattr(settings, "PREMBLY_WIDGET_KEY", "placeholder-widget-key")
    OUT.mkdir(parents=True, exist_ok=True)
    consent = {"consent": True, "policy_version": settings.KYC_POLICY_VERSION}

    # The happy path up to "checking": everything here is driven through the API.
    cg = _caregiver(db)
    h = auth_headers(cg.user)
    _save("200-a-kyc-state-fresh.json", client.get(BASE, headers=h), "Step 2 done, nothing started.")
    _save("201-a-kyc-sessions-no-consent.json", client.post(f"{BASE}/sessions", json={"consent": False, "policy_version": settings.KYC_POLICY_VERSION}, headers=h),
          "The tick is required; nothing is recorded.")
    _save("202-a-kyc-sessions-stale-policy.json", client.post(f"{BASE}/sessions", json={"consent": True, "policy_version": "kyc-2020-01"}, headers=h),
          "The consent text version the app showed is not the current one; nothing is recorded.")
    created = client.post(f"{BASE}/sessions", json=consent, headers=h)
    _save("203-a-kyc-sessions-create.json", created, "First call: 201, a new attempt; the consent is recorded (append-only).")
    vid = created.json()["verification_id"]
    _save("204-a-kyc-sessions-again.json", client.post(f"{BASE}/sessions", json=consent, headers=h),
          "Second call: 200, the same attempt and widget config (reopening the widget).")
    ev = f"{BASE}/{vid}/client-event"
    _save("205-a-kyc-client-event-opened.json", client.post(ev, json={"event": "opened"}, headers=h))
    _save("206-a-kyc-client-event-completed.json",
          client.post(ev, json={"event": "completed", "session_id": "placeholder-session-001"}, headers=h),
          "A hint only: the response shows the attempt waiting for the provider; the caregiver status is still in_progress.")
    _save("207-a-kyc-state-checking.json", client.get(BASE, headers=h), "What the app polls while it waits for the provider.")
    _save("208-a-kyc-client-event-bad-session-id.json", client.post(ev, json={"event": "completed", "session_id": "../x"}, headers=h))
    other = _caregiver(db)
    _save("209-a-kyc-client-event-someone-elses-attempt.json",
          client.post(ev, json={"event": "completed"}, headers=auth_headers(other.user)),
          "Another caregiver's attempt: the same 404 as for an id that does not exist.")

    # States only the webhook and decision engine (K3/K4) can reach: attempts seeded into the database.
    retry = _caregiver(db, identity_verification_status="in_progress")
    _decided(db, fake_prembly, kyc_availability, retry, clean=False, overall_result="failed", face_confidence=41)
    _save("210-a-kyc-state-try-again.json", client.get(BASE, headers=auth_headers(retry.user)),
          "[engine] One clean failure (face mismatch, decided from a fake Prembly session): a coarse tip and a retry. The scores and reason are not returned.")

    capped = _caregiver(db, identity_verification_status="in_progress")
    for _ in range(3):
        _seed(db, capped, "failed", reason_codes=["face_mismatch"])
    ch = auth_headers(capped.user)
    _save("211-a-kyc-state-attempts-exhausted.json", client.get(BASE, headers=ch),
          "[seeded] Three failed attempts with the caregiver left in_progress. The engine never leaves this state (it moves the caregiver to needs_review at the third failure, capture 228); it is only reachable by hand.")
    _save("212-a-kyc-sessions-attempts-exhausted.json", client.post(f"{BASE}/sessions", json=consent, headers=ch))

    review = _caregiver(db, identity_verification_status="in_progress")
    _decided(db, fake_prembly, kyc_availability, review, registry_name="PLACEHOLDER OTHER NAME")
    rh = auth_headers(review.user)
    _save("213-a-kyc-state-under-review.json", client.get(BASE, headers=rh),
          "[engine] A case with a person (here the name on the ID differs from the profile). The reason is not returned.")
    _save("214-a-kyc-sessions-under-review.json", client.post(f"{BASE}/sessions", json=consent, headers=rh))

    done = _caregiver(db, identity_verification_status="in_progress")
    _decided(db, fake_prembly, kyc_availability, done)
    dh2 = auth_headers(done.user)
    _save("215-a-kyc-sessions-already-verified.json", client.post(f"{BASE}/sessions", json=consent, headers=dh2),
          "[engine] Identity already verified by a pull-confirmed pass.")
    _save("226-a-kyc-state-verified.json", client.get(BASE, headers=dh2), "[engine] What the app sees once the pass is decided.")
    _save("227-a-kyc-verification-status-verified.json", client.get("/api/v1/caregiver/verification-status", headers=dh2),
          "[engine] identity_verification_status is verified and profile_completion_step moved to 3.")

    early = _caregiver(db, profile_completion_step=1)
    _save("216-a-kyc-sessions-profile-incomplete.json", client.post(f"{BASE}/sessions", json=consent, headers=auth_headers(early.user)))

    # Three clean failures: the engine ends in review, never in a failed caregiver.
    third = _caregiver(db, identity_verification_status="in_progress")
    for _ in range(3):
        _decided(db, fake_prembly, kyc_availability, third, clean=False, overall_result="failed", face_confidence=20)
    th = auth_headers(third.user)
    _save("228-a-kyc-state-attempts-used-up-review.json", client.get(BASE, headers=th),
          "[engine] The third clean failure hands the case to a person: needs_review, attempts_used 3, can_start false.")
    _save("229-a-kyc-sessions-after-attempts-used-up.json", client.post(f"{BASE}/sessions", json=consent, headers=th),
          "[engine] UNDER_REVIEW, not ATTEMPTS_EXHAUSTED: the caregiver is in review.")

    # An attempt nobody answered is expired by the cron.
    stale = _caregiver(db, identity_verification_status="in_progress")
    old = datetime.utcnow() - timedelta(hours=25)
    db.add(IdentityVerification(caregiver_id=stale.id, attempt_no=1, status="awaiting_result", created_at=old, updated_at=old))
    db.commit()
    reconcile(db, hooks=KycHooks(availability=kyc_availability, notify=lambda db_, n: None, alert=lambda k: None))
    _save("230-a-kyc-state-expired.json", client.get(BASE, headers=auth_headers(stale.user)),
          "[engine] The reconcile cron expired the 25-hour-old attempt: not counted, the caregiver can start again.")

    # A wallet that is empty: found by the engine, which flags the provider and the app sees it.
    walled = _caregiver(db, identity_verification_status="in_progress")
    fake_prembly.wallet_empty()
    _decided(db, fake_prembly, kyc_availability, walled)
    _save("231-a-kyc-state-wallet-empty.json", client.get(BASE, headers=auth_headers(walled.user)),
          "[engine] A pull answered 'wallet empty': the engine flagged the provider unavailable at once (and alerts ops).")
    fake_prembly.recover()
    kyc_availability.available = True

    # Outage path.
    down = _caregiver(db)
    dh = auth_headers(down.user)
    _save("217-a-kyc-defer-while-available.json", client.post(f"{BASE}/defer", headers=dh))
    kyc_availability.available = False
    _save("218-a-kyc-state-provider-down.json", client.get(BASE, headers=dh),
          "The provider flag is an in-memory stand-in for the Redis flag the engine sets.")
    _save("219-a-kyc-sessions-provider-down.json", client.post(f"{BASE}/sessions", json=consent, headers=dh))
    _save("220-a-kyc-defer.json", client.post(f"{BASE}/defer", headers=dh))
    _save("221-a-kyc-verification-status-after-defer.json", client.get("/api/v1/caregiver/verification-status", headers=dh),
          "profile_completion_step is 3 and identity_verification_status is still pending.")
    _save("222-a-kyc-defer-profile-incomplete.json", client.post(f"{BASE}/defer", headers=auth_headers(early.user)))
    kyc_availability.available = True

    # The old endpoint and the guards.
    _save("223-a-kyc-old-result-endpoint.json",
          client.post(f"{BASE}/result", json={"type": "face", "result": {"status": "verified"}}, headers=h),
          "Removed: 410 whatever is posted; nothing is read or stored.")
    patient = make_patient(db)
    _save("224-a-kyc-state-patient-token.json", client.get(BASE, headers=auth_headers(patient.user)))
    no_token = client.get(BASE)
    _save("225-a-kyc-state-no-token.json", no_token)
