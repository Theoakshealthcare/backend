"""The KYC decision engine, transactional half (design 7.2, 7.3, 8.3, K4): turn a pull-confirmed result into the attempt's outcome and
the caregiver's identity status, in the caller's locked section.

``kyc_decision`` holds the rules (pure); this module applies them to rows. It is provider-neutral: it reads an ``Evidence`` from
the seam and never looks at anything provider-shaped.

TRUST (design 16.1 #2, #4): the ONLY place a provider result can set ``caregivers.identity_verification_status`` is
``decide_and_apply``, and its only caller is ``kyc_events.confirm_and_record``, after

* the pulled session was fetched with the secret key and shown to belong to this attempt,
* the pull itself is final (``observation.evidence`` exists only for a pass or a fail verdict), and
* the caller holds the caregiver and attempt row locks (caregiver first: ``kyc_locks``).

So a caregiver is never ``verified`` without a pull-confirmed pass (the webhook is only the trigger), a provider event never
overwrites a human decision (``verified`` / ``failed`` set by an admin, or ``needs_review``, which only an admin resolves), and the
caregiver is never ``failed`` by a machine (K-C4): a clean failure is ``in_progress`` ("try again"), the last one ``needs_review``.

``needs_review`` is the only way out of anything doubtful: nothing here rejects or suspends anyone (K-D3).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_ERROR, VERIFICATION_EXPIRED, IdentityVerification, IdentityVerificationEvent,
)
from app.services import kyc_decision as rules
from app.services.kyc_caregiver import attempts_used
from app.services.kyc_decision import Decision, Evidence, Policy, Profile
from app.services.kyc_locks import lock_caregiver_then_attempt
from app.services.kyc_matching import id_fingerprint
from app.services.kyc_notify import TRY_AGAIN, UNDER_REVIEW, VERIFIED, Notice

logger = logging.getLogger(__name__)

# Only a caregiver the provider may still move: ``pending`` (after a reset) or ``in_progress``. ``verified`` / ``failed`` are human
# or already-final; ``needs_review`` waits for a person (design 7.2).
PROVIDER_MAY_DECIDE = ("pending", "in_progress")
ADVISORY_NAMESPACE = 7_204_101  # first key of pg_advisory_xact_lock(int, int): unique among this app's advisory locks
DONE_STEP = 3  # the identity step is finished (verified, or handed to a person): the app moves on to consent
NOTICE_KIND = {rules.PASSED: VERIFIED, rules.FAILED: TRY_AGAIN, rules.NEEDS_REVIEW: UNDER_REVIEW}


@dataclass(frozen=True)
class Applied:
    decision: Decision
    notice: Notice


def policy() -> Policy:
    return Policy(face_threshold=settings.KYC_FACE_THRESHOLD, face_floor=settings.KYC_FACE_FLOOR,
                  max_attempts=settings.KYC_MAX_ATTEMPTS)


def may_decide(caregiver: Caregiver) -> bool:
    return caregiver.identity_verification_status in PROVIDER_MAY_DECIDE


def decide_and_apply(db: Session, caregiver: Caregiver, attempt: IdentityVerification, evidence: Evidence, *, actor: str,
                     disagreement: bool = False) -> Optional[Applied]:
    """Decide ``attempt`` from ``evidence`` and apply it to the attempt and the caregiver. The caller holds both row locks, has bound
    the pulled session to this attempt and commits. Returns None (nothing written) when the caregiver is no longer the provider's to
    move or the attempt is already finished."""
    if attempt.status not in VERIFICATION_ACTIVE_STATUSES or not may_decide(caregiver):
        return None
    fingerprint = id_fingerprint(evidence.id_type, evidence.id_number)
    duplicate = False
    if fingerprint is not None and evidence.verdict == rules.PASS:
        duplicate = _claim_identity(db, caregiver.id, fingerprint)
    decision = rules.decide(
        evidence, Profile(full_name=caregiver.full_name, date_of_birth=caregiver.date_of_birth),
        duplicate=duplicate, disagreement=disagreement, attempts_used=attempts_used(_attempts(db, caregiver)), policy=policy())

    before = attempt.status
    attempt.status = decision.status
    attempt.reason_codes = list(dict.fromkeys([*(attempt.reason_codes or []), *decision.reasons]))
    attempt.name_match, attempt.dob_match = decision.name_match, decision.dob_match
    attempt.id_type, attempt.id_fingerprint = evidence.id_type, fingerprint
    attempt.face_threshold = settings.KYC_FACE_THRESHOLD
    attempt.decided_via = "provider"
    attempt.updated_at = datetime.utcnow()
    db.add(IdentityVerificationEvent(verification_id=attempt.id, caregiver_id=caregiver.id, actor=actor, from_status=before,
                                     to_status=decision.status, reason_codes=list(decision.reasons)))

    caregiver.identity_verification_status = decision.caregiver_status
    if decision.status in (rules.PASSED, rules.NEEDS_REVIEW):
        caregiver.profile_completion_step = max(caregiver.profile_completion_step or 0, DONE_STEP)
    caregiver.updated_at = datetime.utcnow()
    logger.info("kyc decided attempt=%s outcome=%s reasons=%s", attempt.id, decision.status, ",".join(decision.reasons) or "-")
    return Applied(decision, Notice(kind=NOTICE_KIND[decision.status], user_id=caregiver.user_id, email=caregiver.email,
                                    full_name=caregiver.full_name or ""))


def _claim_identity(db: Session, caregiver_id: uuid.UUID, fingerprint: str) -> bool:
    """Is this NIN/BVN already a PASSED identity of ANOTHER caregiver? The advisory lock is held to the end of the transaction, so two
    caregivers presenting one number at the same time are serialised: the second one sees the first's ``passed`` row and goes to
    review (design 9.1). It is taken after the caregiver and attempt row locks and nothing waits for a row lock while holding it, so
    it cannot deadlock with them."""
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:fp))"), {"ns": ADVISORY_NAMESPACE, "fp": fingerprint})
    return db.query(IdentityVerification.id).filter(
        IdentityVerification.id_fingerprint == fingerprint, IdentityVerification.status == rules.PASSED,
        IdentityVerification.caregiver_id != caregiver_id).first() is not None


def _attempts(db: Session, caregiver: Caregiver) -> list[IdentityVerification]:
    return db.query(IdentityVerification).filter(IdentityVerification.caregiver_id == caregiver.id).all()


def close_unanswered(db: Session, attempt_id: uuid.UUID, *, older_than: datetime, to_status: str = VERIFICATION_EXPIRED,
                     actor: str = "reconcile") -> bool:
    """Expire an attempt nothing answered (``expired``) or whose provider fetch kept failing (``error``). Neither counts against the
    caregiver's attempts and neither touches the caregiver: they simply start a new one (design 7.1). Locks like every KYC writer,
    re-checks under the lock that it is still active and still old, commits."""
    if to_status not in (VERIFICATION_EXPIRED, VERIFICATION_ERROR):
        raise ValueError(to_status)
    caregiver, attempt = lock_caregiver_then_attempt(db, attempt_id)
    if attempt is None or attempt.status not in VERIFICATION_ACTIVE_STATUSES or attempt.created_at >= older_than:
        db.rollback()
        return False
    before = attempt.status
    attempt.status = to_status
    attempt.updated_at = datetime.utcnow()
    db.add(IdentityVerificationEvent(verification_id=attempt.id, caregiver_id=attempt.caregiver_id, actor=actor, from_status=before,
                                     to_status=to_status, reason_codes=[]))
    db.commit()
    return True


def session_cutoff(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.utcnow()) - timedelta(hours=settings.KYC_SESSION_TTL_HOURS)
