"""Regression: Dojah's answers in the REAL shape seen in the live sandbox run (2026-10-30), with fake values (app/tests/dojah_real_shapes.py).
Pins what the parser, the seam and the rules do with them: a sandbox person with no face number and a true selfie flag passes when the
name and date of birth match; the flat webhook body is read; nothing outside the whitelist is kept."""
import json
from datetime import date

import httpx
import pytest

from app.services import kyc_seam as seam
from app.services.dojah import DojahClient, _extract
from app.services.kyc_decision import Policy, Profile, decide
from app.services.kyc_events import process_event
from app.db.models.identity_verification import IdentityVerification, KycWebhookEvent
from app.tests.dojah_real_shapes import FAKE_DOB, FAKE_FIRST, FAKE_LAST, FAKE_NUMBER, FAKE_URL, real_body, real_pull
from app.tests.factories import make_caregiver
from app.tests.fake_dojah import fake_dojah, kyc_queue, new_reference, signed_webhook  # noqa: F401

REF = "OKL-" + "cd" * 16
POLICY = Policy(face_threshold=85, face_floor=70, max_attempts=3)


def test_the_real_pull_shape_parses_to_a_pass_with_the_registry_identity():
    v = _extract(real_pull(REF), REF)
    assert (v.verification_status, v.verification_type, v.registry_type) == ("Completed", "BVN", "bvn")
    assert (v.government_status, v.selfie_status, v.selfie_confidence, v.selfie_confidence_given) == (True, True, None, False)
    assert v.registry == {"bvn": FAKE_NUMBER, "first_name": FAKE_FIRST, "middle_name": "", "last_name": FAKE_LAST, "date_of_birth": FAKE_DOB}
    o = seam.observe(v)
    assert (o.final, o.verdict, o.face_confidence) == (True, "pass", None)
    e = o.evidence
    assert (e.face_flag_only, e.liveness_passed, e.incomplete, e.id_type, e.id_number) == (True, True, False, "bvn", FAKE_NUMBER)
    assert (e.registry_name, e.registry_dob) == (f"{FAKE_FIRST} {FAKE_LAST}", date(1971, 3, 5))  # an empty middle name is skipped, DD-Mon-YYYY read


def test_matching_name_and_dob_with_no_face_number_decides_passed():
    e = seam.observe(_extract(real_pull(REF), REF)).evidence
    d = decide(e, Profile(f"{FAKE_FIRST} {FAKE_LAST}", date(1971, 3, 5)), duplicate=False, disagreement=False, attempts_used=1, policy=POLICY)
    assert (d.status, d.reasons, d.name_match, d.dob_match) == ("passed", (), True, True)


def test_a_different_person_with_no_face_number_goes_to_review_for_name_and_dob_only():
    e = seam.observe(_extract(real_pull(REF), REF)).evidence
    d = decide(e, Profile("Someone Else", date(1990, 1, 1)), duplicate=False, disagreement=False, attempts_used=1, policy=POLICY)
    assert (d.status, d.reasons) == ("needs_review", ("name_mismatch", "dob_mismatch"))  # not identity_data_missing


def test_nothing_outside_the_whitelist_is_kept():
    v = _extract(real_pull(REF), REF)
    blob = repr(v) + json.dumps(v.registry) + str(v.message)
    for private in ("fake@example.invalid", "+2340000000001", "1 Fake Street", "FAKE NAME", "10000000098", FAKE_URL, "192.0.2.1", "Fake ISP"):
        assert private not in blob
    assert set(v.registry) == {"bvn", "first_name", "middle_name", "last_name", "date_of_birth"}


def test_a_passing_blocks_success_message_is_never_read_as_a_failure_reason():
    """Seen live: the selfie message is "Successfully validated your liveness". With the government check false, that text must not make
    the reason ``liveness_failed``."""
    v = _extract(real_pull(REF, government_status=False), REF)
    assert "liveness" not in (v.message or "").lower()
    o = seam.observe(v)
    assert (o.verdict, o.evidence.failure_reason) == ("fail", None)


def test_a_failed_selfie_still_names_its_own_message():
    o = seam.observe(_extract(real_pull(REF, selfie_status=False), REF))
    assert (o.verdict, o.evidence.liveness_passed, o.evidence.failure_reason) == ("fail", False, "liveness_failed")


def test_the_real_flat_webhook_body_is_read_and_it_can_never_claim_a_pass():
    body = real_body(REF)
    assert "entity" not in body  # the webhook is the pull's entity, flat
    event = seam.parse_event(body)
    assert (event.reference_id, event.verification_status, event.claim, event.settled) == (REF, "completed", None, True)
    assert seam.is_our_reference(event.reference_id)


def test_the_real_webhook_is_signed_in_hex_and_the_v2_header_is_not_needed():
    raw, headers = signed_webhook(real_body(REF), "fake-secret")
    assert seam.verify_signature(raw, headers["x-dojah-signature"], "fake-secret")
    assert not seam.verify_signature(raw, headers["x-dojah-signature"], "another-secret")


def test_end_to_end_the_real_shape_settles_a_matching_caregiver_as_passed(client, db, kyc_queue, fake_dojah):  # noqa: F811
    caregiver = make_caregiver(db, approved=False, profile_completion_step=2, identity_verification_status="in_progress",
                               full_name=f"{FAKE_FIRST} {FAKE_LAST}", date_of_birth=date(1971, 3, 5))
    attempt = IdentityVerification(caregiver_id=caregiver.id, attempt_no=1, status="awaiting_result", provider_session_id=new_reference())
    db.add(attempt)
    db.commit()
    ref = attempt.provider_session_id
    fake_dojah.verifications[ref] = real_pull(ref)["entity"]
    raw, headers = signed_webhook(real_body(ref))
    assert client.post("/api/v1/kyc/dojah/webhook", content=raw, headers=headers).status_code == 200
    assert process_event(db, kyc_queue.jobs[-1]).outcome == "applied"
    db.expire_all()
    a = db.get(IdentityVerification, attempt.id)
    assert (a.status, a.reason_codes, a.face_confidence, a.name_match, a.dob_match) == ("passed", [], None, True, True)
    assert caregiver_status(db, caregiver) == "verified"
    stored = json.dumps([[str(getattr(r, c.name)) for c in r.__table__.columns] for r in db.query(KycWebhookEvent).all()])
    assert FAKE_NUMBER not in stored and FAKE_URL not in stored


def caregiver_status(db, caregiver):
    from app.db.models.caregiver import Caregiver

    return db.get(Caregiver, caregiver.id).identity_verification_status
