"""Writes the live captures behind the Caregiver identity review (K5) section of the admin FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_KYC_ADMIN=1 poetry run pytest app/tests/test_capture_kyc_admin.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-admin.md docs/fe-captures/admin`.
Files `600`-`606`, and a re-capture of `042` (the caregiver queue gained two counts). scripts/capture_admin_flow.py (compose
stack) rewrites the whole directory, so fold these calls into it the next time it is run.

What is real and what is not: the app, the routes, the database, the admin API responses and, since K4, the decision engine are
real: the attempts marked [engine] were decided by ``process_event`` (the code that runs in production) from a session served by
``FakePrembly``. NOTHING here has met the real Prembly (the session shapes are the documented ones, design section 15), so the
scores and reason codes are what the engine does with the fake's values. Rows nothing can produce yet (a stuck attempt) are
seeded into the database and marked [seeded]. All names are placeholders; the ID number comes from Prembly's published sandbox.
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
from app.tests.fake_prembly import SANDBOX_BVN
from app.tests.factories import auth_headers, make_admin, make_caregiver

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_KYC_ADMIN"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "admin"
ADMIN = "/api/v1/admin"
CG = f"{ADMIN}/caregivers"
APP = "/api/v1/caregiver/identity-verification"


def _save(name, r, note=None, auth="Bearer <ADMIN_ACCESS_TOKEN_JWT>"):
    sent = r.request.content
    doc = {
        "request": {"method": r.request.method,
                    "path": r.request.url.path + (f"?{r.request.url.query.decode()}" if r.request.url.query else ""),
                    "auth": auth if r.request.headers.get("authorization") else None, "body": json.loads(sent) if sent else None},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")},
                     "body": r.json()},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return r


def _caregiver(db, name, **kw):
    kw.setdefault("profile_completion_step", 2)
    kw.setdefault("identity_verification_status", "in_progress")
    kw.setdefault("date_of_birth", date(1990, 4, 12))
    return make_caregiver(db, approved=False, full_name=name, **kw)


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


def test_capture_admin_kyc(db, client, monkeypatch, fake_prembly, kyc_availability):
    OUT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PREMBLY_WIDGET_ID", "placeholder-widget-id")
    monkeypatch.setattr(settings, "PREMBLY_WIDGET_KEY", "placeholder-widget-key")
    reviewer = make_admin(db, ["verification"], password=None, full_name="Placeholder Reviewer")
    finance = make_admin(db, ["finance"], password=None, full_name="Placeholder Finance")
    analyst = make_admin(db, ["read_only_analyst"], password=None, full_name="Placeholder Analyst")
    boss = make_admin(db, ["super_admin"], password=None, full_name="Placeholder Boss")
    hr, hf, ha, hb = (auth_headers(a) for a in (reviewer, finance, analyst, boss))

    # 042 is re-captured because the queue response gained two counts (it was captured with an empty queue)
    _save("042-caregiver-queue-as-verifier.json", client.get(CG, headers=hr), "A route the Verification role may use. Since K5 the list also carries the identity badges.",
          auth="Bearer <ACCESS_TOKEN_JWT>")

    # --- the cases -----------------------------------------------------------------------------------------------------
    passed = _caregiver(db, "Placeholder Amina Okafor")
    _decided(db, fake_prembly, kyc_availability, passed)  # [engine] a clean pass: verified, holds the sandbox NIN
    duplicate = _caregiver(db, "Placeholder Bola Adeyemi")
    _decided(db, fake_prembly, kyc_availability, duplicate)  # [engine] the same NIN again: duplicate_identity -> review
    mismatch = _caregiver(db, "Placeholder Chidi Nwosu")
    _decided(db, fake_prembly, kyc_availability, mismatch, registry_name="PLACEHOLDER OTHER NAME", id_number=SANDBOX_BVN, id_type="bvn")
    grey = _caregiver(db, "Placeholder Dayo Bello")
    _decided(db, fake_prembly, kyc_availability, grey, clean=False, overall_result="passed", face_confidence=77, id_number="11111111112",
             id_type="nin", registry_name="Placeholder Dayo Bello", registry_dob="1990-04-12")  # [engine] grey band -> review
    retry = _caregiver(db, "Placeholder Efe Ojo")
    _decided(db, fake_prembly, kyc_availability, retry, clean=False, overall_result="failed", face_confidence=41)  # [engine] try again
    exhausted = _caregiver(db, "Placeholder Funmi Lawal")
    for _ in range(3):
        _decided(db, fake_prembly, kyc_availability, exhausted, clean=False, overall_result="failed", face_confidence=20)  # [engine]
    stuck = _caregiver(db, "Placeholder Gbenga Ade")
    old = datetime.utcnow() - timedelta(hours=30)
    db.add(IdentityVerification(caregiver_id=stuck.id, attempt_no=1, status="awaiting_result", created_at=old, updated_at=old))  # [seeded]
    fresh = _caregiver(db, "Placeholder Hauwa Musa", identity_verification_status="pending")
    db.commit()

    # --- the queue -----------------------------------------------------------------------------------------------------
    _save("600-a-queue-all.json", client.get(CG, headers=hr),
          "The Verification tab as before, plus `needs_review_count` and `stuck_count` for the badges. Both follow the `profile_status` tab and ignore `identity`.")
    _save("600-b-queue-needs-review.json", client.get(CG, params={"identity": "needs_review"}, headers=hr),
          "`identity=needs_review`: caregivers waiting for a person. `total` equals `needs_review_count`.")
    _save("600-c-queue-stuck.json", client.get(CG, params={"identity": "stuck"}, headers=hr),
          "[seeded] `identity=stuck`: an attempt open for longer than the session lifetime plus an hour, on a caregiver nobody decided. "
          "Nothing in the app produces one until the cron has missed it (provider outage), so this one is inserted.")
    _save("600-d-queue-failed-empty.json", client.get(CG, params={"identity": "failed"}, headers=hr),
          "`identity=failed` before any admin has failed anyone: empty. The engine never fails a caregiver by itself.")
    _save("600-e-queue-bad-filter.json", client.get(CG, params={"identity": "everything"}, headers=hr), "An unknown bucket is a 422.")
    _save("600-f-queue-as-analyst.json", client.get(CG, params={"identity": "needs_review"}, headers=ha), "No `caregivers.read`: 403 naming it.")
    _save("600-g-queue-no-token.json", client.get(CG, params={"identity": "needs_review"}), "No token: 401.")

    # --- the identity block --------------------------------------------------------------------------------------------
    _save("601-a-detail-duplicate.json", client.get(f"{CG}/{duplicate.id}", headers=hr),
          "[engine] A caregiver whose NIN already passed for another account: `review_reason` is `duplicate_identity` and "
          "`duplicate_of_caregiver_id` names the other caregiver. Reading this is an audited act. Only `identity` is shown in the guide; the rest is the usual detail.")
    _save("601-b-detail-name-mismatch.json", client.get(f"{CG}/{mismatch.id}", headers=hr), "[engine] The name on the ID differs from the profile: `name_match` is false.")
    _save("601-c-detail-grey-band.json", client.get(f"{CG}/{grey.id}", headers=hr), "[engine] Face score between the floor and the threshold.")
    _save("601-d-detail-passed.json", client.get(f"{CG}/{passed.id}", headers=hr), "[engine] Verified by a pull-confirmed pass: `decided_via` is `provider`.")
    _save("601-e-detail-try-again.json", client.get(f"{CG}/{retry.id}", headers=hr), "[engine] One clean failure; the caregiver is `in_progress` and can try again. `can_reset` is false.")
    _save("601-f-detail-exhausted.json", client.get(f"{CG}/{exhausted.id}", headers=hr),
          "[engine] Three clean failures: `needs_review` with `attempts_exhausted`; `attempts_used` equals `attempts_max`.")
    _save("601-g-detail-no-attempts.json", client.get(f"{CG}/{fresh.id}", headers=hr), "Nothing started: an empty list, not an absent key.")
    _save("601-h-detail-stuck.json", client.get(f"{CG}/{stuck.id}", headers=hr), "[seeded] One attempt still open after 30 hours.")

    # --- identity-decision ---------------------------------------------------------------------------------------------
    url = lambda c, action: f"{CG}/{c.id}/{action}"  # noqa: E731
    _save("602-a-decision-as-finance.json", client.post(url(mismatch, "identity-decision"), headers=hf, json={"decision": "verified", "notes": "Placeholder note"}),
          "Finance holds `caregivers.read` but not `caregivers.approve`: 403, nothing changes.")
    _save("602-b-decision-no-notes.json", client.post(url(mismatch, "identity-decision"), headers=hr, json={"decision": "verified"}), "`notes` is required.")
    _save("602-c-decision-unknown-caregiver.json",
          client.post(f"{CG}/00000000-0000-4000-8000-000000000001/identity-decision", headers=hr, json={"decision": "verified", "notes": "Placeholder note"}))
    _save("602-k-decision-case-changed.json", client.post(url(grey, "identity-decision"), headers=hr,
                                                          json={"decision": "verified", "notes": "Placeholder note", "expected_status": "in_progress"}),
          "`expected_status` is optional: the status the admin was looking at. The case is `needs_review` now, so nothing is decided and the answer is a 409.")
    _save("602-d-verify.json", client.post(url(mismatch, "identity-decision"), headers=hr,
                                           json={"decision": "verified", "notes": "Placeholder: married name, checked on the Prembly dashboard."}),
          "A person resolves the name mismatch. The response is the same summary as before.")
    _save("602-e-detail-after-verify.json", client.get(f"{CG}/{mismatch.id}", headers=hr),
          "The review attempt is now `passed`, `decided_via` `admin`; its reason codes stay so the override is visible; `review_reason` is null.")
    _save("602-f-fail.json", client.post(url(duplicate, "identity-decision"), headers=hr,
                                         json={"decision": "failed", "notes": "Placeholder: same person, second account."}),
          "Failing is the admin's word only; the response carries `auto_suspended` as before.")
    _save("602-g-detail-after-fail.json", client.get(f"{CG}/{duplicate.id}", headers=hr),
          "The review attempt is `failed` by the admin; the caregiver is `failed`; `can_reset` is true; the duplicate link is still shown.")
    _save("602-h-verify-with-open-attempt.json", client.post(url(stuck, "identity-decision"), headers=hr,
                                                             json={"decision": "verified", "notes": "Placeholder: verified by hand."}),
          "[seeded] A caregiver with an attempt still open (the stuck one).")
    _save("602-i-detail-after-open-attempt.json", client.get(f"{CG}/{stuck.id}", headers=hr),
          "The open attempt was closed as `expired`: it no longer counts and the provider can no longer settle it.")
    _save("602-j-queue-failed.json", client.get(CG, params={"identity": "failed"}, headers=hr), "Now one caregiver is `failed`.")

    # --- identity-reset ------------------------------------------------------------------------------------------------
    _save("603-a-reset-as-finance.json", client.post(url(exhausted, "identity-reset"), headers=hf, json={"notes": "Placeholder note"}), "403 for Finance, nothing changes.")
    _save("603-b-reset-no-notes.json", client.post(url(exhausted, "identity-reset"), headers=hr, json={}), "`notes` is required.")
    _save("603-c-reset-verified.json", client.post(url(passed, "identity-reset"), headers=hr, json={"notes": "Placeholder note"}),
          "A verified caregiver cannot be reset: 409, `unmet[0].code` `IDENTITY_RESET_NOT_ALLOWED`. Fail them first if the verification is to be undone.")
    _save("603-d-reset-in-progress.json", client.post(url(retry, "identity-reset"), headers=hr, json={"notes": "Placeholder note"}),
          "A caregiver who can still try on their own does not need a reset: the same 409.")
    _save("603-k-reset-doubtful-review.json", client.post(url(grey, "identity-reset"), headers=hr, json={"notes": "Placeholder note"}),
          "[engine] A review that is about the evidence (here a face score in the grey band) needs a verdict, not another go: the same 409. Only `failed` and an exhausted review can be reset.")
    caregiver_view = client.get(APP, headers=auth_headers(exhausted.user))
    _save("603-e-app-state-before-reset.json", caregiver_view, "What the caregiver's app sees before the reset (it never sees the reason).", auth="Bearer <ACCESS_TOKEN_JWT>")
    _save("603-f-reset.json", client.post(url(exhausted, "identity-reset"), headers=hr, json={"notes": "Placeholder: photos were dark, allow another go."}),
          "The caregiver is `pending` again with a fresh set of attempts. They are not verified, approved or reinstated.")
    _save("603-g-detail-after-reset.json", client.get(f"{CG}/{exhausted.id}", headers=hr),
          "The attempts stay as history, each now carrying `admin_reset` in `reason_codes`; `attempts_used` is 0 again.")
    _save("603-h-app-state-after-reset.json", client.get(APP, headers=auth_headers(exhausted.user)),
          "What the caregiver's app sees: `can_start` is true and `attempts_used` is 0.", auth="Bearer <ACCESS_TOKEN_JWT>")
    consent = {"consent": True, "policy_version": settings.KYC_POLICY_VERSION}
    _save("603-i-app-starts-new-attempt.json", client.post(f"{APP}/sessions", json=consent, headers=auth_headers(exhausted.user)),
          "The next attempt continues the numbering (`attempt_no` 4).", auth="Bearer <ACCESS_TOKEN_JWT>")
    _save("603-j-reset-after-fail.json", client.post(url(duplicate, "identity-reset"), headers=hr, json={"notes": "Placeholder: second account closed, allow the first."}),
          "Reset also works on a caregiver an admin failed.")

    # --- the audit trail -----------------------------------------------------------------------------------------------
    _save("604-a-audit-identity-actions.json", client.get(f"{ADMIN}/audit", params={"entity_type": "caregiver", "limit": 100}, headers=hb),
          "Every identity action is in `admin_audit_log` (`caregiver.identity_decision`, `caregiver.identity_reset`), with the notes as `reason`. "
          "Opening a caregiver is `caregiver.view`.")
