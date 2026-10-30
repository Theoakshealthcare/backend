"""The KYC decision rules (design 7.1-7.3, K4): what a pull-confirmed provider result means for ONE attempt.

A pure function over plain values, so the rules can be read, tested exhaustively and fuzzed without a database. Nothing here
knows the provider: ``kyc_seam`` turns a pulled verification into an ``Evidence`` and this module never sees the provider's own words.

The rules, in order of precedence:

1. RISKY (watch-listed, duplicate identity, webhook and pull disagreeing) -> ``needs_review``. A person looks; nothing is rejected.
2. HARD FAILURE (face below the provider floor, liveness failed, the provider itself says "failed" without a grey-band face) ->
   ``failed``: the caregiver may try again, or, when the attempts are used up, ``needs_review`` (``attempts_exhausted``). A machine
   never rejects and never marks the CAREGIVER ``failed`` (K-C4): that is an admin-only caregiver status.
3. SOFT problems (face in the grey band, name or date of birth differing, something that could not be compared) ->
   ``needs_review``. Never auto-fail: a married name or a typo in Step 1 is common.
4. Otherwise ``passed``.

Anything that could not be checked counts against a pass, never for it: a missing face score, name or date of birth makes the
result ``needs_review`` (``identity_data_missing``), because the alternative is verifying someone on evidence we did not see.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from app.services.kyc_matching import dobs_match, names_match

PASS, FAIL = "pass", "fail"

# Attempt statuses the engine can decide (the values of ``identity_verifications.status``).
PASSED, FAILED, NEEDS_REVIEW = "passed", "failed", "needs_review"
# The caregiver-level status each attempt outcome produces (design 7.2). ``failed`` is never produced here (K-C4).
CAREGIVER_STATUS = {PASSED: "verified", FAILED: "in_progress", NEEDS_REVIEW: "needs_review"}

# Reason codes the engine adds (the K1 set plus ``identity_data_missing``; ``kyc_caregiver.FAILURE_HINTS`` maps the retryable ones).
FACE_MISMATCH, LIVENESS_FAILED, FACE_GREY_BAND = "face_mismatch", "liveness_failed", "face_grey_band"
NAME_MISMATCH, DOB_MISMATCH, DUPLICATE_IDENTITY = "name_mismatch", "dob_mismatch", "duplicate_identity"
WATCHLISTED, PROVIDER_DISAGREEMENT, PROVIDER_FAILED = "watchlisted", "provider_disagreement", "provider_failed"
ATTEMPTS_EXHAUSTED, IDENTITY_DATA_MISSING = "attempts_exhausted", "identity_data_missing"


@dataclass(frozen=True)
class Evidence:
    """What the pulled session says, reduced to what the rules need. The registry name, date of birth and number are in memory
    only: ``repr`` hides them and nothing stores them (design 10.1)."""
    verdict: str  # PASS / FAIL
    face_confidence: Optional[Decimal] = None  # 0..100
    liveness_passed: Optional[bool] = None  # a boolean only (Dojah gives no liveness score); None = the provider did not say
    watchlisted: bool = False
    failure_reason: Optional[str] = None  # a canonical reason code for a clean failure, when the provider says why
    id_type: Optional[str] = None
    id_number: Optional[str] = field(default=None, repr=False)
    registry_name: Optional[str] = field(default=None, repr=False)
    registry_dob: Optional[date] = field(default=None, repr=False)
    incomplete: bool = False  # a finished flow whose government or selfie result was absent or unreadable: a person looks, never a pass


@dataclass(frozen=True)
class Profile:
    """The caregiver's own Step 1 answers (self-declared)."""
    full_name: Optional[str]
    date_of_birth: Optional[date]


@dataclass(frozen=True)
class Policy:
    face_threshold: int
    face_floor: int
    max_attempts: int


@dataclass(frozen=True)
class Decision:
    status: str  # PASSED / FAILED / NEEDS_REVIEW
    reasons: tuple
    name_match: Optional[bool]
    dob_match: Optional[bool]

    @property
    def caregiver_status(self) -> str:
        return CAREGIVER_STATUS[self.status]


def face_band(face: Optional[Decimal], policy: Policy) -> str:
    if face is None:
        return "missing"
    if face < policy.face_floor:
        return "low"
    return "grey" if face < policy.face_threshold else "ok"


def decide(evidence: Evidence, profile: Profile, *, duplicate: bool, disagreement: bool, attempts_used: int,
           policy: Policy) -> Decision:
    """``attempts_used`` counts this attempt. ``duplicate`` is whether the NIN/BVN fingerprint already belongs to a passed attempt of
    another caregiver; ``disagreement`` whether the webhook and the pull contradicted each other."""
    passing = evidence.verdict == PASS
    band = face_band(evidence.face_confidence, policy)
    name_ok = names_match(profile.full_name, evidence.registry_name) if passing else None
    dob_ok = dobs_match(profile.date_of_birth, evidence.registry_dob) if passing else None
    liveness_failed = evidence.liveness_passed is False

    risky: list[str] = []
    if disagreement:
        risky.append(PROVIDER_DISAGREEMENT)
    if evidence.watchlisted:
        risky.append(WATCHLISTED)
    if duplicate and passing:
        risky.append(DUPLICATE_IDENTITY)

    hard: list[str] = []
    if band == "low":
        hard.append(FACE_MISMATCH)
    if liveness_failed:
        hard.append(LIVENESS_FAILED)
    if not passing and band != "grey" and not hard:
        hard.append(evidence.failure_reason or PROVIDER_FAILED)

    soft: list[str] = []
    if band == "grey":
        soft.append(FACE_GREY_BAND)
    if passing:
        if band == "missing" or evidence.incomplete:
            soft.append(IDENTITY_DATA_MISSING)
        if name_ok is False:
            soft.append(NAME_MISMATCH)
        if dob_ok is False:
            soft.append(DOB_MISMATCH)
        if (name_ok is None or dob_ok is None) and IDENTITY_DATA_MISSING not in soft:
            soft.append(IDENTITY_DATA_MISSING)

    if risky:
        status, reasons = NEEDS_REVIEW, [*risky, *hard, *soft]
    elif hard:
        status, reasons = FAILED, hard
        if attempts_used >= policy.max_attempts:
            status, reasons = NEEDS_REVIEW, [*hard, ATTEMPTS_EXHAUSTED]
    elif soft:
        status, reasons = NEEDS_REVIEW, soft
    else:
        status, reasons = PASSED, []
    return Decision(status=status, reasons=tuple(dict.fromkeys(reasons)), name_match=name_ok, dob_match=dob_ok)
