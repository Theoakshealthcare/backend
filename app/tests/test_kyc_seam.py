"""KD1: the Dojah seam (app/services/kyc_seam.py) without a database: how a pulled verification becomes an Observation, and how a
webhook body becomes a WebhookEvent. The fixtures follow the shapes in Weysure's and Timp's ported parsers (docs/plans/
2026-10-30-caregiver-kyc-dojah-delta.md); every field marked UNCONFIRMED in the seam stays a guess until the K8 live pass.
Names, ids and numbers are placeholders."""
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.services import kyc_seam as seam
from app.services.dojah import DojahClient, DojahVerification, _extract
from app.tests.fake_dojah import SANDBOX_BVN, SANDBOX_NIN, FakeDojah, fake_dojah, new_reference  # noqa: F401

REF = "OKL-" + "ab" * 16

# The shape documented in Weysure's parse.py (confirmed live on Timp 2026-07-11), with placeholder values.
WEYSURE_BVN_ENTITY = {
    "reference_id": REF, "verification_status": "Completed", "verification_type": "BVN",
    "data": {"government_data": {"status": True, "data": {"bvn": {"entity": {
        "bvn": SANDBOX_BVN, "first_name": "AMINA", "middle_name": "NGOZI", "last_name": "BELLO", "date_of_birth": "01-Jun-1982"}}}},
             "selfie": {"status": True, "confidence_value": 88.4}}}


def observe(entity: dict, reference: str = REF):
    return seam.observe(_extract({"entity": entity}, reference))


# --- pass, fail, not final -----------------------------------------------------------------------------------------
def test_a_completed_bvn_verification_with_both_steps_true_is_a_pass_with_registry_evidence():
    o = observe(WEYSURE_BVN_ENTITY)
    assert (o.final, o.verdict, o.face_confidence, o.abandoned) == (True, "pass", Decimal("88.40"), False)
    e = o.evidence
    assert (e.verdict, e.liveness_passed, e.watchlisted, e.id_type, e.id_number) == ("pass", True, False, "bvn", SANDBOX_BVN)
    assert (e.registry_name, e.registry_dob, e.failure_reason) == ("AMINA NGOZI BELLO", date(1982, 6, 1), None)


def test_a_completed_nin_verification_reads_the_nin_key():
    o = observe(FakeDojah.entity(REF, id_type="nin", id_number=SANDBOX_NIN))
    assert o.verdict == "pass" and (o.evidence.id_type, o.evidence.id_number) == ("nin", SANDBOX_NIN)


def test_completed_is_not_a_pass_when_the_government_record_failed():
    o = observe(FakeDojah.entity(REF, government=False))
    assert (o.final, o.verdict) == (True, "fail")


def test_completed_is_not_a_pass_when_the_selfie_failed_whatever_the_number():
    o = observe(FakeDojah.entity(REF, selfie=False, confidence=99.0))
    assert o.verdict == "fail" and o.evidence.liveness_passed is False


@pytest.mark.parametrize("confidence, verdict", [(70.0, "pass"), (69.99, "fail"), (0, "fail"), (100, "pass")])
def test_the_face_number_is_compared_with_the_dojah_threshold(confidence, verdict):
    assert settings.DOJAH_FACE_MATCH_THRESHOLD == 70
    assert observe(FakeDojah.entity(REF, confidence=confidence)).verdict == verdict


def test_the_threshold_is_a_setting(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_FACE_MATCH_THRESHOLD", 90)
    assert observe(FakeDojah.entity(REF, confidence=85.0)).verdict == "fail"


def test_without_a_number_the_face_step_follows_the_selfie_flag_and_no_score_is_recorded():
    o = observe(FakeDojah.entity(REF, confidence=None))
    assert (o.verdict, o.face_confidence, o.evidence.face_flag_only) == ("pass", None, True)


def test_a_number_that_is_present_but_unusable_is_not_the_flag_standing_alone():
    for raw in (500.0, -3.0):
        o = observe(FakeDojah.entity(REF, confidence=raw))
        assert o.face_confidence is None and o.evidence.face_flag_only is False
    assert observe(FakeDojah.entity(REF, confidence=88.0)).evidence.face_flag_only is False


def test_the_sandbox_shape_has_no_confidence_value_and_null_scores_under_selfie_data():
    """What the live sandbox returned (2026-10-30): selfie.status true, selfie.data.match_score / liveness_score null, no confidence_value."""
    entity = FakeDojah.entity(REF, confidence=None)
    entity["data"]["selfie"]["data"] = {"match_score": None, "liveness_score": None, "human_reviewed": False}
    o = observe(entity)
    assert (o.verdict, o.face_confidence, o.evidence.face_flag_only, o.evidence.liveness_passed) == ("pass", None, True, True)


@pytest.mark.parametrize("confidence", [-1, 100.01, 1e9])
def test_a_confidence_outside_0_100_is_dropped_never_clamped(confidence):
    o = observe(FakeDojah.entity(REF, confidence=confidence))
    assert o.face_confidence is None


@pytest.mark.parametrize("field", ["government", "selfie"])
def test_a_missing_step_is_incomplete_evidence_for_a_person_not_a_pass_and_not_a_failed_try(field):
    """Dojah's shape drifting must not cost every caregiver an attempt, and must never verify anyone: the rules send it to review."""
    o = observe(FakeDojah.entity(REF, **{field: None}))
    assert o.final and o.evidence.incomplete is True


def test_an_explicit_false_beats_a_missing_step():
    assert observe(FakeDojah.entity(REF, government=False, selfie=None)).verdict == "fail"


def test_a_complete_pass_is_not_marked_incomplete():
    assert observe(FakeDojah.entity(REF)).evidence.incomplete is False


def test_an_entity_with_no_data_block_is_incomplete():
    o = observe(FakeDojah.entity(REF, data={}))
    assert o.final and o.evidence.incomplete is True


def test_failed_is_a_final_fail_with_its_reason_mapped():
    o = observe(FakeDojah.entity(REF, status="Failed", selfie=False, confidence=20, message="Liveness check failed"))
    assert (o.final, o.verdict, o.evidence.failure_reason) == (True, "fail", "liveness_failed")


@pytest.mark.parametrize("message, code", [
    ("Liveness check failed", "liveness_failed"), ("Record not found", "record_not_found"), ("Document is blurry", "document_unreadable"),
    ("Face does not match", "face_mismatch"), ("Something else", None), (None, None),
])
def test_failure_reasons_are_keyword_mapped_and_unknown_text_is_left_unset(message, code):
    o = observe(FakeDojah.entity(REF, status="Failed", government=False, selfie=None, message=message))
    assert o.evidence.failure_reason == code


@pytest.mark.parametrize("status", ["Pending", "Ongoing", "pending", "ONGOING", "Whatever", None, ""])
def test_pending_ongoing_and_unknown_words_are_not_final_and_carry_no_evidence(status):
    o = observe(FakeDojah.entity(REF, status=status, confidence=95))
    assert (o.final, o.verdict, o.evidence, o.face_confidence, o.abandoned) == (False, None, None, None, False)


def test_abandoned_is_not_final_and_says_so():
    o = observe(FakeDojah.entity(REF, status="Abandoned"))
    assert (o.final, o.verdict, o.evidence, o.abandoned) == (False, None, None, True)


def test_a_status_word_is_case_and_space_insensitive():
    assert observe(FakeDojah.entity(REF, status=" completed ")).final is True
    assert observe(FakeDojah.entity(REF, status="FAILED")).verdict == "fail"


# --- registry identity ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("01-Jun-1982", date(1982, 6, 1)), ("1982-06-01", date(1982, 6, 1)), ("01/06/1982", date(1982, 6, 1)),
    ("01-06-1982", date(1982, 6, 1)), ("not a date", None), ("", None), (None, None),
])
def test_the_date_of_birth_formats_dojah_may_send(raw, expected):
    assert observe(FakeDojah.entity(REF, date_of_birth=raw)).evidence.registry_dob == expected


def test_name_parts_are_joined_and_missing_parts_skipped():
    assert observe(FakeDojah.entity(REF, first_name="Amina", middle_name=None, last_name="Bello")).evidence.registry_name == "Amina Bello"
    assert observe(FakeDojah.entity(REF, first_name=None, middle_name=None, last_name=None)).evidence.registry_name is None


def test_no_number_means_none_and_an_unknown_id_type_is_left_null():
    assert observe(FakeDojah.entity(REF, id_number=None)).evidence.id_number is None
    odd = observe(FakeDojah.entity(REF, id_type="passport", id_number="A00000000", verification_type="PASSPORT")).evidence
    assert (odd.id_type, odd.id_number, odd.registry_name) == (None, None, None)


def test_the_photo_and_the_image_links_never_reach_the_evidence():
    e = observe(FakeDojah.entity(REF)).evidence
    assert "SENTINEL" not in repr(e) + str(e.registry_name) + str(e.id_number)
    assert "base64" not in str(e.__dict__) and "images.dojah.invalid" not in str(e.__dict__)


def test_the_evidence_hides_the_person_from_repr():
    text = repr(observe(FakeDojah.entity(REF)))
    for hidden in (SANDBOX_NIN, "Test Person", "1990", "12-Apr"):
        assert hidden not in text


# --- malformed answers ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body", [None, [], "x", 7, {}, {"entity": None}, {"entity": []}, {"entity": {"reference_id": 7}},
                                  {"entity": {"reference_id": ""}}])
def test_a_malformed_answer_is_refused_not_guessed_at(body):
    with pytest.raises(Exception) as excinfo:
        _extract(body, REF)
    assert excinfo.type.__name__ == "DojahError" and excinfo.value.kind == "reference_mismatch"


@pytest.mark.parametrize("data", [None, [], "x", {"government_data": []}, {"government_data": {"data": []}}, {"selfie": 7},
                                  {"government_data": {"status": True, "data": {"bvn": None}}}])
def test_malformed_inner_blocks_degrade_to_incomplete_not_a_crash_or_a_pass(data):
    entity = {"reference_id": REF, "verification_status": "Completed", "verification_type": "BVN", "data": data}
    o = observe(entity)
    assert o.final and o.evidence.incomplete is True


def test_the_pull_answer_with_a_boolean_for_confidence_is_dropped(fake_dojah):
    entity = FakeDojah.entity(REF)
    entity["data"]["selfie"]["confidence_value"] = True
    fake_dojah.script(httpx.Response(200, json={"entity": entity}))
    v = DojahClient.from_settings().get_verification(REF)
    assert v.selfie_confidence is None


# --- the webhook body ------------------------------------------------------------------------------------------------
def test_parse_event_reads_the_reference_and_the_status_word():
    e = seam.parse_event({"reference_id": REF, "verification_status": "Completed", "status": True, "data": {"selfie": {"status": True}}})
    assert (e.reference_id, e.verification_status, e.claim, e.settled) == (REF, "completed", None, True)


@pytest.mark.parametrize("body", [None, [], "x", 7, {}, {"reference_id": ""}, {"reference_id": 7}, {"reference_id": "a b"},
                                  {"reference_id": "x" * 129}, {"data": {"reference_id": 7}}, {"data": []}])
def test_parse_event_refuses_a_body_without_a_usable_reference(body):
    assert seam.parse_event(body) is None


def test_the_webhook_reference_is_read_from_the_top_level_first():
    other = "OKL-" + "cd" * 16
    e = seam.parse_event({"reference_id": REF, "referenceId": other, "data": {"reference_id": other}})
    assert e.reference_id == REF


@pytest.mark.parametrize("value, ours", [(REF, True), ("OKL-" + "0" * 32, True), ("okl-" + "ab" * 16, False),
                                         ("OKL-" + "AB" * 16, False), ("OKL-" + "ab" * 15, False), ("OKL-" + "ab" * 17, False),
                                         ("KYC-BVN-123", False), (None, False), (7, False), (REF + "\n", False)])
def test_is_our_reference(value, ours):
    assert seam.is_our_reference(value) is ours


def test_a_minted_reference_is_ours_unique_and_unguessable():
    refs = {seam.mint_reference() for _ in range(50)}
    assert len(refs) == 50 and all(seam.is_our_reference(r) for r in refs)


def test_the_webhook_body_is_never_kept_on_the_event():
    e = seam.parse_event(json.loads(json.dumps(FakeDojah.entity(REF, first_name="SENTINEL"))))
    assert "SENTINEL" not in repr(e)


def test_a_verification_object_hides_what_it_carries():
    v = DojahVerification(reference_id=REF, registry={"nin": SANDBOX_NIN, "first_name": "SENTINEL"}, message="SENTINEL text")
    assert "SENTINEL" not in repr(v) and SANDBOX_NIN not in repr(v)
