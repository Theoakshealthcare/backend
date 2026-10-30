"""The admin side of identity verification (KYC K5): what an admin sees, the queue buckets, and the two things an admin may do.

Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 7.2, 12.3 and 13.

What an admin sees (``identity_block``): the attempts with their scores, yes/no match results and reason codes, and the other
caregiver an identity number was already used by. Never an ID number, an image, a provider session id, the keyed fingerprint or
anything the provider said about the person: none of that is stored (K1), and the columns read here are the ones the design lists.

What an admin may do, both called by ``CaregiverApprovalService`` under its caregiver row lock, which then audits and commits:

* ``apply_decision`` closes the attempt the admin just resolved (the one waiting for review) and any attempt still open, and
  writes the ``identity_verification_events`` rows (``admin_audit_log`` is written by the caller).
* ``apply_reset`` gives the caregiver a fresh set of attempts. There is no column for it (no migration): every attempt that exists
  is marked with ``kyc_caregiver.RESET_MARK`` and ``attempts_used`` skips marked ones. The attempts themselves stay as history.

Lock order is the K3 rule everywhere: the caregiver row first (the caller holds it), then the caregiver's attempts by id. So an
admin action can queue behind the webhook worker or the cron, and the other way round, but never deadlock with them.

Nothing here knows about the provider: the reason codes and statuses are our own.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_EXPIRED, VERIFICATION_FAILED, VERIFICATION_NEEDS_REVIEW, VERIFICATION_PASSED,
    IdentityVerification, IdentityVerificationEvent,
)
from app.services.kyc_caregiver import RESET_MARK, attempts_used

DECISION_REASON = "admin_decision"
DONE_STEP = 3  # the identity step is finished: the app moves on to consent (the same step K4 sets)
EXHAUSTED = "attempts_exhausted"
PROVIDER_MAY_RETRY = ("pending", "in_progress")

# An attempt older than the session lifetime is normally expired by the cron within minutes; this much more before it shows as
# "stuck", so the bucket lists the ones the cron could not close (an outage, a provider that never answers) and not every session
# crossing the line.
STUCK_GRACE = timedelta(hours=1)

# What to show first when an attempt waiting for review has several reasons: the ones a person must act on, then the evidence
# strength. Anything not listed (a code a later slice adds) comes after these, in the order it was recorded.
REVIEW_REASON_PRIORITY = ("watchlisted", "duplicate_identity", "provider_disagreement", "identity_data_missing", "attempts_exhausted",
                          "face_grey_band", "name_mismatch", "dob_mismatch")


def _num(value) -> Optional[float]:
    return None if value is None else float(value)


# --- reads --------------------------------------------------------------------------------------------------------------
def review_reason(codes) -> Optional[str]:
    codes = [c for c in (codes or []) if c != RESET_MARK]
    for code in REVIEW_REASON_PRIORITY:
        if code in codes:
            return code
    return codes[0] if codes else None


def open_review_attempt(attempts: List[IdentityVerification]) -> Optional[IdentityVerification]:
    """The attempt a person has to resolve: the newest one still waiting for review that a reset has not already handed back."""
    waiting = [a for a in attempts if a.status == VERIFICATION_NEEDS_REVIEW and RESET_MARK not in (a.reason_codes or ())]
    return max(waiting, key=lambda a: a.attempt_no, default=None)


def can_reset(caregiver: Caregiver, attempts: List[IdentityVerification]) -> bool:
    """Design 7.2: a failed caregiver, or one whose case ran out of attempts (``needs_review`` with ``attempts_exhausted``). Any other
    review (watch list, duplicate, mismatch) needs a verdict, not another go: fail the caregiver first if that is what the case needs."""
    if caregiver.identity_verification_status == "failed":
        return True
    review = open_review_attempt(attempts) if caregiver.identity_verification_status == "needs_review" else None
    return review is not None and EXHAUSTED in (review.reason_codes or ())


def attempts_of(db: Session, caregiver_id: UUID) -> List[IdentityVerification]:
    return (db.query(IdentityVerification).filter(IdentityVerification.caregiver_id == caregiver_id)
            .order_by(IdentityVerification.attempt_no.desc()).all())


def _duplicate_of(db: Session, caregiver_id: UUID, attempts: List[IdentityVerification]) -> Optional[UUID]:
    prints = {a.id_fingerprint for a in attempts if a.id_fingerprint}
    if not prints:
        return None
    return db.scalar(
        select(IdentityVerification.caregiver_id)
        .where(IdentityVerification.id_fingerprint.in_(prints), IdentityVerification.status == VERIFICATION_PASSED,
               IdentityVerification.caregiver_id != caregiver_id)
        .order_by(IdentityVerification.created_at, IdentityVerification.id).limit(1))


def identity_block(db: Session, caregiver: Caregiver) -> Dict[str, Any]:
    attempts = attempts_of(db, caregiver.id)  # newest first
    review = open_review_attempt(attempts) if caregiver.identity_verification_status == "needs_review" else None
    return {
        "status": caregiver.identity_verification_status,
        "review_reason": review_reason(review.reason_codes) if review else None,
        "attempts_used": attempts_used(attempts),
        "attempts_max": settings.KYC_MAX_ATTEMPTS,
        "can_reset": can_reset(caregiver, attempts),
        "attempts": [{
            "id": a.id, "attempt_no": a.attempt_no, "status": a.status, "created_at": a.created_at, "started_at": a.started_at,
            "completed_at": a.completed_at, "id_type": a.id_type, "face_confidence": _num(a.face_confidence),
            "liveness_confidence": _num(a.liveness_confidence), "face_threshold": a.face_threshold, "name_match": a.name_match,
            "dob_match": a.dob_match, "reason_codes": list(a.reason_codes or []), "provider_reference": a.provider_reference,
            "decided_via": a.decided_via,
        } for a in attempts],
        "duplicate_of_caregiver_id": _duplicate_of(db, caregiver.id, attempts),
    }


# --- the queue ----------------------------------------------------------------------------------------------------------
def _stuck(now: Optional[datetime] = None):
    """A caregiver nobody has decided who has an unfinished attempt older than the session lifetime (design 7.3)."""
    cutoff = (now or datetime.utcnow()) - timedelta(hours=settings.KYC_SESSION_TTL_HOURS) - STUCK_GRACE
    old_open = select(IdentityVerification.id).where(
        IdentityVerification.caregiver_id == Caregiver.id, IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES),
        IdentityVerification.created_at < cutoff).exists()
    return and_(Caregiver.identity_verification_status.in_(PROVIDER_MAY_RETRY), old_open)


def queue_condition(bucket: str):
    if bucket == "needs_review":
        return Caregiver.identity_verification_status == "needs_review"
    if bucket == "failed":
        return Caregiver.identity_verification_status == "failed"
    if bucket == "stuck":
        return _stuck()
    raise ValueError(bucket)


def queue_counts(db: Session, profile_status: str) -> Dict[str, int]:
    """The badges: how many caregivers of this tab wait for an identity review, and how many have a stuck attempt. They do not depend
    on the ``identity`` filter, so each equals the ``total`` that bucket's filter returns on the same tab."""
    def count(condition) -> int:
        return db.scalar(select(func.count()).select_from(Caregiver).where(Caregiver.profile_status == profile_status, condition)) or 0
    return {"needs_review_count": count(queue_condition("needs_review")), "stuck_count": count(queue_condition("stuck"))}


# --- actions (the caller holds the caregiver row lock, audits and commits) ---------------------------------------------
def _lock_attempts(db: Session, caregiver_id: UUID) -> List[IdentityVerification]:
    return (db.query(IdentityVerification).filter(IdentityVerification.caregiver_id == caregiver_id)
            .order_by(IdentityVerification.id).populate_existing().with_for_update().all())


def _event(db: Session, attempt: IdentityVerification, admin_id: UUID, before: str, after: str, reasons: List[str]) -> None:
    db.add(IdentityVerificationEvent(verification_id=attempt.id, caregiver_id=attempt.caregiver_id, actor=f"admin:{admin_id}",
                                     from_status=before, to_status=after, reason_codes=reasons))


def _close_open(db: Session, attempts: List[IdentityVerification], admin_id: UUID, reason: str, now: datetime) -> List[IdentityVerification]:
    """An attempt still waiting on the provider is of no use once an admin has spoken. ``expired`` is the status for "nothing was
    decided about the person" and does not count against the cap; the provider can no longer settle it (K4 only decides active ones)."""
    closed = []
    for attempt in attempts:
        if attempt.status in VERIFICATION_ACTIVE_STATUSES:
            before, attempt.status, attempt.updated_at = attempt.status, VERIFICATION_EXPIRED, now
            _event(db, attempt, admin_id, before, VERIFICATION_EXPIRED, [reason])
            closed.append(attempt)
    return closed


def apply_decision(db: Session, caregiver: Caregiver, admin_id: UUID, decision: str) -> Dict[str, Any]:
    """The admin's verdict (``verified`` / ``failed``) reaches the attempts. Returns what was closed, for the audit row."""
    now = datetime.utcnow()
    attempts = _lock_attempts(db, caregiver.id)
    review = open_review_attempt(attempts)
    if review is not None:
        outcome = VERIFICATION_PASSED if decision == "verified" else VERIFICATION_FAILED
        review.status, review.decided_via, review.updated_at = outcome, "admin", now
        review.completed_at = review.completed_at or now
        _event(db, review, admin_id, VERIFICATION_NEEDS_REVIEW, outcome, [DECISION_REASON])
    overridden = None
    if review is None:  # an admin overriding a provider pass: the pass is the attempt the audit row has to name (design 7.2)
        overridden = max((a for a in attempts if a.status == VERIFICATION_PASSED and a.decided_via != "admin"),
                         key=lambda a: a.attempt_no, default=None)
        if overridden is not None:
            _event(db, overridden, admin_id, VERIFICATION_PASSED, VERIFICATION_PASSED, [DECISION_REASON])
    closed = _close_open(db, attempts, admin_id, DECISION_REASON, now)
    if decision == "verified":
        caregiver.profile_completion_step = max(caregiver.profile_completion_step or 0, DONE_STEP)
    return {"identity_verification_id": review.id if review else None,
            "overridden_verification_id": overridden.id if overridden else None, "closed_attempt_ids": [a.id for a in closed]}


def apply_reset(db: Session, caregiver: Caregiver, admin_id: UUID) -> Dict[str, Any]:
    """A fresh set of attempts. Returns the attempts that were wiped, for the audit row. The caller sets the caregiver ``pending``: the
    status is written only by the files the structural test in test_kyc_engine.py lists, and this is not one of them."""
    now = datetime.utcnow()
    attempts = _lock_attempts(db, caregiver.id)
    _close_open(db, attempts, admin_id, RESET_MARK, now)
    wiped = [a for a in attempts if RESET_MARK not in (a.reason_codes or ())]
    for attempt in wiped:
        attempt.reason_codes = [*(attempt.reason_codes or []), RESET_MARK]
        attempt.updated_at = now
    if attempts:  # the trail of the reset itself, on the newest attempt (the table needs an attempt to hang a row on)
        newest = max(attempts, key=lambda a: a.attempt_no)
        _event(db, newest, admin_id, newest.status, newest.status, [RESET_MARK])
    return {"attempts_reset": [a.id for a in wiped]}
