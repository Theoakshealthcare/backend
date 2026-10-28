"""K4: the pure decision rules and the name / date-of-birth / fingerprint helpers (no database).

Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 7.1-7.3. Names and numbers are placeholders.
"""
import itertools
import random
from datetime import date
from decimal import Decimal

import pytest

from app.services import kyc_decision as kd
from app.services.kyc_decision import Evidence, Policy, Profile, decide
from app.services.kyc_matching import (
    dobs_match, id_fingerprint, name_tokens, names_match, normalise_id_number, parse_dob,
)

POLICY = Policy(face_threshold=85, face_floor=70, liveness_floor=50, max_attempts=3)
PROFILE = Profile(full_name="Amina Bello", date_of_birth=date(1990, 4, 12))


def good(**kw):
    base = dict(verdict="pass", face_confidence=Decimal("92"), liveness_confidence=Decimal("90"),
                registry_name="BELLO AMINA", registry_dob=date(1990, 4, 12))
    base.update(kw)
    return Evidence(**base)


def run(evidence=None, profile=PROFILE, *, duplicate=False, disagreement=False, used=1, policy=POLICY):
    return decide(evidence or good(), profile, duplicate=duplicate, disagreement=disagreement, attempts_used=used, policy=policy)


# --- names -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,expected", [
    ("Amina Bello", "BELLO AMINA", True),                      # order and case
    ("Amina Bello", "Bello, Amina Yusuf", True),               # the registry has a middle name
    ("Amina Yusuf Bello", "Amina Bello", True),                # the profile has one
    ("Aminá Béllo", "AMINA BELLO", True),                      # accents
    ("Amina O. Bello", "Amina Olu Bello", True),               # an initial matches its name
    ("Amina-Grace Bello", "Amina Grace Bello", True),          # punctuation
    ("Amina Bello", "Amina Okafor", False),                    # a married name is a mismatch (a human decides)
    ("Amina", "Amina Bello Yusuf", False),                     # one shared word is too weak
    ("Amina Bello", "Bello Bello", False),                     # a repeated word cannot be used twice
    ("Amina Bello", "Chinedu Okafor", False),
    ("", "Amina Bello", None),
    ("Amina Bello", None, None),
    ("  ", "Amina Bello", None),
    (None, None, None),
])
def test_names_match(a, b, expected):
    assert names_match(a, b) is expected


def test_name_tokens_are_lowercase_ascii_words():
    assert name_tokens("  Ọlúwáṣeun-Ade O'Neil ") == ["oluwaseun", "ade", "o", "neil"]
    assert name_tokens(None) == [] and name_tokens(42) == []


# --- dates of birth --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1990-04-12", date(1990, 4, 12)), ("12-04-1990", date(1990, 4, 12)), ("12/04/1990", date(1990, 4, 12)),
    ("12 Apr 1990", date(1990, 4, 12)), ("12-Apr-1990", date(1990, 4, 12)), ("12 April 1990", date(1990, 4, 12)),
    (date(1990, 4, 12), date(1990, 4, 12)), ("  1990-04-12  ", date(1990, 4, 12)),
    ("", None), ("not a date", None), ("1990-13-40", None), (None, None), (19900412, None), ("12/04/90", None),
])
def test_parse_dob(raw, expected):
    assert parse_dob(raw) == expected


def test_dobs_match():
    assert dobs_match(date(1990, 4, 12), date(1990, 4, 12)) is True
    assert dobs_match(date(1990, 4, 12), date(1990, 12, 4)) is False  # day and month swapped is a mismatch, not a match
    assert dobs_match(None, date(1990, 4, 12)) is None and dobs_match(date(1990, 4, 12), None) is None


# --- fingerprints ----------------------------------------------------------------------------------------------------
def test_the_fingerprint_is_stable_keyed_and_not_the_number():
    fp = id_fingerprint("nin", "12345678909", key="k1")
    assert fp == id_fingerprint("nin", " 1234-5678 909 ", key="k1")           # normalised
    assert fp != id_fingerprint("bvn", "12345678909", key="k1")               # the type is part of it
    assert fp != id_fingerprint("nin", "12345678909", key="k2")               # keyed
    assert "12345678909" not in fp and len(fp) == 64


@pytest.mark.parametrize("number", [None, "", "12", "!!", True, 3.5, ["1234567"], "x" * 41])
def test_no_usable_number_means_no_fingerprint(number):
    assert id_fingerprint("nin", number, key="k1") is None


def test_no_key_means_no_fingerprint():
    assert id_fingerprint("nin", "12345678909", key="") is None


def test_the_id_number_normaliser():
    assert normalise_id_number("ab-12 34") == "AB1234" and normalise_id_number(12345678909) == "12345678909"


# --- decisions -------------------------------------------------------------------------------------------------------
def test_a_clean_pass_passes():
    d = run()
    assert (d.status, d.reasons, d.name_match, d.dob_match, d.caregiver_status) == ("passed", (), True, True, "verified")


def test_a_clean_failure_lets_the_caregiver_try_again_and_never_fails_the_caregiver():
    d = run(Evidence(verdict="fail", failure_reason="document_unreadable"))
    assert (d.status, d.reasons, d.caregiver_status) == ("failed", ("document_unreadable",), "in_progress")
    assert run(Evidence(verdict="fail")).reasons == ("provider_failed",)


def test_the_last_allowed_failure_goes_to_review_not_to_failed():
    d = run(Evidence(verdict="fail", failure_reason="face_mismatch"), used=3)
    assert (d.status, d.reasons, d.caregiver_status) == ("needs_review", ("face_mismatch", "attempts_exhausted"), "needs_review")
    assert run(Evidence(verdict="fail"), used=2).status == "failed"


@pytest.mark.parametrize("face,status,reasons", [
    ("100", "passed", ()), ("85", "passed", ()),                        # the threshold itself passes
    ("84.99", "needs_review", ("face_grey_band",)), ("70", "needs_review", ("face_grey_band",)),  # the floor is grey, not low
    ("69.99", "failed", ("face_mismatch",)), ("0", "failed", ("face_mismatch",)),
])
def test_the_face_bands(face, status, reasons):
    d = run(good(face_confidence=Decimal(face)))
    assert (d.status, d.reasons) == (status, reasons)


def test_a_missing_face_score_cannot_pass():
    d = run(good(face_confidence=None))
    assert (d.status, d.reasons) == ("needs_review", ("identity_data_missing",))


def test_liveness_below_the_floor_is_a_clean_failure_and_a_missing_score_is_not():
    assert run(good(liveness_confidence=Decimal("49"))).reasons == ("liveness_failed",)
    assert run(good(liveness_confidence=Decimal("50"))).status == "passed"
    assert run(good(liveness_confidence=None)).status == "passed"


def test_name_and_dob_differences_go_to_review_with_their_reason():
    d = run(good(registry_name="CHINEDU OKAFOR"))
    assert (d.status, d.reasons, d.name_match, d.dob_match) == ("needs_review", ("name_mismatch",), False, True)
    d = run(good(registry_dob=date(1991, 4, 12)))
    assert (d.status, d.reasons, d.name_match, d.dob_match) == ("needs_review", ("dob_mismatch",), True, False)
    d = run(good(registry_name="CHINEDU OKAFOR", registry_dob=date(1991, 4, 12)))
    assert d.reasons == ("name_mismatch", "dob_mismatch")


def test_a_name_or_dob_that_could_not_be_compared_cannot_pass():
    for ev, profile in [(good(registry_name=None), PROFILE), (good(registry_dob=None), PROFILE),
                        (good(), Profile("Amina Bello", None)), (good(), Profile("", date(1990, 4, 12)))]:
        d = run(ev, profile)
        assert (d.status, d.reasons) == ("needs_review", ("identity_data_missing",))


def test_risky_signals_always_go_to_review_even_over_a_clean_failure():
    assert run(duplicate=True).reasons == ("duplicate_identity",) and run(duplicate=True).status == "needs_review"
    assert run(disagreement=True).reasons == ("provider_disagreement",)
    d = run(Evidence(verdict="fail", watchlisted=True))
    assert (d.status, d.reasons) == ("needs_review", ("watchlisted", "provider_failed"))
    d = run(good(face_confidence=Decimal("10")), duplicate=True)
    assert d.status == "needs_review" and d.reasons == ("duplicate_identity", "face_mismatch")


def test_a_provider_failure_with_a_grey_band_face_is_ambiguous_so_it_goes_to_review():
    d = run(Evidence(verdict="fail", face_confidence=Decimal("78")))
    assert (d.status, d.reasons) == ("needs_review", ("face_grey_band",))


def test_a_hard_failure_wins_over_soft_mismatches_and_reports_only_itself():
    d = run(good(face_confidence=Decimal("30"), registry_name="CHINEDU OKAFOR"))
    assert (d.status, d.reasons) == ("failed", ("face_mismatch",))


def test_a_duplicate_only_counts_when_the_provider_passed():
    assert run(Evidence(verdict="fail"), duplicate=True).reasons == ("provider_failed",)


def test_the_evidence_repr_hides_the_registry_data():
    text = repr(good(id_number="12345678909"))
    assert "12345678909" not in text and "BELLO" not in text and "1990" not in text


# --- fuzz: the invariants hold for any input ---------------------------------------------------------------------------
def _random_case(rng: random.Random):
    verdict = rng.choice(["pass", "fail"])
    score = lambda: rng.choice([None, Decimal(rng.choice([0, 30, 69, 70, 84, 85, 99, 100]))])  # noqa: E731
    evidence = Evidence(
        verdict=verdict, face_confidence=score(), liveness_confidence=score(), watchlisted=rng.random() < 0.15,
        failure_reason=rng.choice([None, "document_unreadable", "record_not_found", "face_mismatch"]),
        registry_name=rng.choice([None, "BELLO AMINA", "OKAFOR CHINEDU", "Amina Bello Yusuf", ""]),
        registry_dob=rng.choice([None, date(1990, 4, 12), date(1991, 4, 12)]))
    profile = Profile(rng.choice(["Amina Bello", "", None, "Chinedu Okafor"]), rng.choice([None, date(1990, 4, 12)]))
    return evidence, profile, rng.random() < 0.2, rng.random() < 0.2, rng.randint(1, 4)


def test_fuzz_a_pass_needs_every_check_and_the_caregiver_is_never_failed():
    rng = random.Random(20261028)
    seen = set()
    for _ in range(4000):
        evidence, profile, duplicate, disagreement, used = _random_case(rng)
        d = decide(evidence, profile, duplicate=duplicate, disagreement=disagreement, attempts_used=used, policy=POLICY)
        seen.add(d.status)
        assert d.caregiver_status in ("verified", "in_progress", "needs_review")  # K-C4: never ``failed``
        if d.status == "passed":
            assert evidence.verdict == "pass" and not evidence.watchlisted and not duplicate and not disagreement
            assert evidence.face_confidence is not None and evidence.face_confidence >= POLICY.face_threshold
            assert d.name_match is True and d.dob_match is True and d.reasons == ()
            assert evidence.liveness_confidence is None or evidence.liveness_confidence >= POLICY.liveness_floor
        else:
            assert d.reasons  # every non-pass says why
        if d.status == "failed":
            assert used < POLICY.max_attempts and not (disagreement or evidence.watchlisted)
            assert not (duplicate and evidence.verdict == "pass")
    assert seen == {"passed", "failed", "needs_review"}  # the generator reaches every outcome


def test_every_reason_the_engine_can_emit_is_a_known_reason_code():
    from app.db.models.identity_verification import REASON_CODES

    emitted = {getattr(kd, name) for name in dir(kd) if name.isupper() and isinstance(getattr(kd, name), str)}
    reasons = emitted & {"face_mismatch", "liveness_failed", "face_grey_band", "name_mismatch", "dob_mismatch", "duplicate_identity",
                         "watchlisted", "provider_disagreement", "provider_failed", "attempts_exhausted", "identity_data_missing"}
    assert reasons == {"face_mismatch", "liveness_failed", "face_grey_band", "name_mismatch", "dob_mismatch", "duplicate_identity",
                       "watchlisted", "provider_disagreement", "provider_failed", "attempts_exhausted", "identity_data_missing"}
    assert reasons - set(REASON_CODES) == {"identity_data_missing"}  # the one code K4 adds (no migration: the set is not a CHECK)
    assert list(itertools.islice(REASON_CODES, 1))
