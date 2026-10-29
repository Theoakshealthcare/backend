"""The worker side of the Prembly webhook: find the attempt, PULL the session, record what Prembly says (design 8.3-8.4, K3).

K3 recorded what the pull said; K4 (``kyc_engine``, ``kyc_decision``) now decides on it in the SAME locked section
(``confirm_and_record``): the attempt becomes ``passed`` / ``failed`` / ``needs_review``, the caregiver's identity status follows
(design 7.2), an ``identity_verification_events`` row is written, and the caregiver is told after the commit. A pulled verdict has
no column, so the decision cannot be made later from the row: it is made here or not at all (an attempt left undecided is found
again by ``kyc_reconcile``).

Trust (design 16.1): the webhook is only the trigger. The signature may have been made with a key that ships inside the app (U1 in
``kyc_seam``), so nothing in the webhook body is believed. The session is fetched with the SECRET key and the pull decides. The
webhook's verdict is compared with the pull's; a disagreement is recorded as such and never as a result.

Binding (16.1 #5): the attempt comes from the signed body's ``oakly_ref`` or, failing that, the client's session-id hint, and in
both cases the pulled session must belong to it: its echoed ``oakly_ref`` equals the attempt's, or (when Prembly echoes none) the
e-mail Prembly recorded is the caregiver's. A hint alone, or a ref alone, is never enough.

LOCK ORDER (K1 follow-up, decided in K3): **always the caregiver row first, then its attempts (by id)**. Erasure
(``AccountErasureService``) already locks the user, then the caregiver, then touches the attempts; the worker used to be specified
as "attempt, then caregiver" (design 8.3), which would deadlock against it. ``kyc_locks.lock_caregiver_then_attempt`` is the only way
this module (and the engine, and the reconcile cron) takes the locks. The Prembly HTTP call is made with NO lock and no open
transaction, and the attempt's state is re-checked after the locks are taken (an erasure or a human decision may have landed).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, IdentityVerification, KycWebhookEvent,
)
from app.services import kyc_engine
from app.services import kyc_seam as seam
from app.services.kyc_hooks import KycHooks
from app.services.kyc_locks import lock_caregiver_then_attempt
from app.services.kyc_notify import Notice
from app.services.prembly import PremblyClient, PremblyError, PremblySession

logger = logging.getLogger(__name__)

# Ledger outcomes (``kyc_webhook_events.outcome``; the CHECK set is fixed by migration 0015).
APPLIED, ORPHAN, IGNORED_TERMINAL, DISAGREEMENT, ERROR = "applied", "orphan", "ignored_terminal", "disagreement", "error"
RETRY_BASE_SECONDS = 30
MAX_RETRY_SECONDS = 300


class KycRetry(Exception):
    """The pull failed in a way worth retrying (Prembly down, rate limited, timed out). The ARQ job turns it into a retry."""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        super().__init__(f"retry in {delay_seconds:.0f}s")


@dataclass(frozen=True)
class Processed:
    outcome: str
    verification_id: Optional[uuid.UUID] = None
    observation: Optional[seam.Observation] = None  # set when the pull was recorded
    notice: Optional[Notice] = None  # what the caregiver is to be told (set only when the decision was committed)


@dataclass(frozen=True)
class Recorded:
    """The result of the locked section. ``notice`` is delivered by the caller once the transaction has committed."""
    outcome: str
    observation: Optional[seam.Observation] = None
    notice: Optional[Notice] = None


def process_event(db: Session, job: dict, *, client: Optional[PremblyClient] = None, final_try: bool = True,
                  hooks: Optional[KycHooks] = None, actor: str = "provider") -> Processed:
    """One ledger row. Idempotent: a row that already has an outcome is left alone. Raises ``KycRetry`` when the pull failed in a
    retryable way and ``final_try`` is false; on the last try the row is closed as ``error`` (the K4 reconcile cron then finds the
    attempt from Prembly's side)."""
    hooks = hooks or KycHooks()
    event_id = uuid.UUID(job["event_id"])
    event = db.query(KycWebhookEvent).filter(KycWebhookEvent.id == event_id).first()
    if event is None or event.processed_at is not None:
        return Processed(event.outcome if event is not None else ERROR)
    session_id = event.provider_session_id  # nulled by erasure; never taken from the job again
    ref = _uuid(job.get("oakly_ref"))
    claim = job.get("claim")

    attempt = _locate(db, ref, session_id)
    if attempt is None:
        return _close(db, event_id, ORPHAN)
    attempt_id = attempt.id
    if attempt.status not in VERIFICATION_ACTIVE_STATUSES:
        return _close(db, event_id, IGNORED_TERMINAL, attempt_id)  # no pull for a finished attempt
    if attempt.completed_at is not None:
        return _close(db, event_id, APPLIED, attempt_id)  # a result is already recorded: a replay costs no pull
    db.rollback()  # no transaction is held open across the Prembly call

    try:
        session = (client or PremblyClient.from_settings()).get_session(session_id, include_payload=True)
    except PremblyError as exc:
        if final_try or not exc.retryable:  # one count per event: ARQ's retries of one failing pull must not flag Prembly down alone
            hooks.on_pull_error(exc)
        return _on_pull_error(db, event_id, attempt_id, exc, final_try)
    except ValueError:  # the session id is not one we may put in a URL
        return _close(db, event_id, ERROR, attempt_id)
    hooks.on_pull_ok()

    recorded = confirm_and_record(db, attempt_id, session, claim=claim, actor=actor)
    if (recorded.outcome == APPLIED and claim is not None and recorded.observation is not None
            and not recorded.observation.final and not final_try):
        # The webhook says Prembly finished but the session read does not show a verdict yet (a lag between the two). Do not
        # consume the event: undo the binding and look again.
        db.rollback()
        raise KycRetry(RETRY_BASE_SECONDS)
    processed = _close(db, event_id, recorded.outcome, attempt_id, recorded.observation, recorded.notice)
    hooks.send(db, processed.notice)  # after the commit; best effort, never raises
    return processed


def confirm_and_record(db: Session, attempt_id: uuid.UUID, session: PremblySession, *, claim: Optional[str] = None,
                       actor: str = "provider") -> Recorded:
    """The locked section: re-check the attempt, bind the pulled session to it, record the scores and, when the pull is final, DECIDE
    (``kyc_engine.decide_and_apply``). Commits nothing: the caller closes the ledger row in the same transaction.

    Nothing is written unless the caregiver is still the provider's to move (``pending`` / ``in_progress``): a human decision
    (``verified`` / ``failed`` / a ``needs_review`` waiting for a person) is never overwritten by a late event (design 7.2)."""
    caregiver, attempt = lock_caregiver_then_attempt(db, attempt_id)
    if attempt is None or attempt.status not in VERIFICATION_ACTIVE_STATUSES:
        return Recorded(IGNORED_TERMINAL)  # finished or erased while we were talking to Prembly
    if not kyc_engine.may_decide(caregiver):
        return Recorded(IGNORED_TERMINAL)  # an admin (or a review already waiting for one) has the last word
    if attempt.completed_at is not None:
        return Recorded(APPLIED)  # a result is already recorded (the unlocked check in process_event is only a cost saver): never overwrite it
    observation = seam.observe(session)

    # The attempt was found from attacker-controllable input (the signed body's ref, a client hint), so until the PULLED session is
    # shown to belong to it nothing is written to it, not even a reason code: a forged event must not taint a victim's attempt.
    # The ledger row (outcome ``disagreement``) is the only record then.
    if not _belongs_to(attempt, caregiver, session, observation) or not _claim_session(db, attempt, session, observation):
        return Recorded(DISAGREEMENT)
    disagreement = claim is not None and observation.final and claim != observation.verdict

    # Bound to this attempt by Prembly's own record, so it replaces the client's earlier hint (the caregiver may have opened the
    # widget again: the hint is only ever the latest thing the app reported).
    attempt.provider_session_id = session.session_id
    try:
        db.flush()  # a concurrent hint on the same session id surfaces HERE, not in an autoflush of some later query
    except IntegrityError:
        db.rollback()
        return Recorded(DISAGREEMENT)
    if not observation.final:
        return Recorded(APPLIED, observation)  # bound only: Prembly has no verdict yet
    attempt.face_confidence, attempt.liveness_confidence = observation.face_confidence, observation.liveness_confidence
    attempt.provider_reference = observation.provider_reference
    attempt.provider_cost = session.total_cost
    attempt.completed_at = session.completed_at or datetime.utcnow()
    applied = kyc_engine.decide_and_apply(db, caregiver, attempt, observation.evidence, actor=actor, disagreement=disagreement)
    return Recorded(DISAGREEMENT if disagreement else APPLIED, observation, applied.notice if applied else None)


def settle_session(db: Session, attempt_id: uuid.UUID, session: PremblySession, *, actor: str, hooks: KycHooks) -> Recorded:
    """The same bind-and-decide step for a session found WITHOUT a webhook (the reconcile cron): no ledger row, no webhook claim,
    so there is nothing to compare the pull with; the pull alone decides, exactly as it does for a webhook (the webhook is only ever
    a trigger). Commits, then tells the caregiver."""
    recorded = confirm_and_record(db, attempt_id, session, actor=actor)
    try:
        db.commit()
    except IntegrityError:  # two attempts claiming one session: the decision rolls back with the binding
        db.rollback()
        return Recorded(DISAGREEMENT)
    hooks.send(db, recorded.notice)
    return recorded


def _locate(db: Session, ref: Optional[uuid.UUID], session_id: Optional[str]) -> Optional[IdentityVerification]:
    """The signed body's ``oakly_ref`` first; the client's session-id hint otherwise. Both are re-checked against the pull."""
    if ref is not None:
        found = db.query(IdentityVerification).filter(IdentityVerification.reference == ref).first()
        if found is not None:
            return found
    if session_id:
        return db.query(IdentityVerification).filter(IdentityVerification.provider_session_id == session_id).first()
    return None


def _belongs_to(attempt: IdentityVerification, caregiver: Caregiver, session: PremblySession, obs: seam.Observation) -> bool:
    if obs.echoed_ref is not None:
        return obs.echoed_ref == attempt.reference
    return seam.same_email(session.end_user_email, caregiver.email)


def _claim_session(db: Session, attempt: IdentityVerification, session: PremblySession, obs: seam.Observation) -> bool:
    """May ``attempt`` take this session id (it is UNIQUE)? Yes when no other attempt holds it. When another one does, that is either a
    squatted client hint (the K2 follow-up: someone who learned a session id stored it first) or a real clash. The caller has just
    shown the pulled session belongs to ``attempt``; ONLY when Prembly echoed our own ``oakly_ref`` for it (the strong binding) and the
    other holder is an active attempt with no recorded result, the other's hint is cleared and ``attempt`` takes the id. Anything
    else (an e-mail-only binding, a holder that is finished or has a result, a holder another worker has locked) is refused, and the
    event is recorded as a disagreement. The other row is taken with SKIP LOCKED so this path can never wait on a lock and so never
    deadlock against that holder's own worker."""
    holder = db.query(IdentityVerification).filter(
        IdentityVerification.provider_session_id == session.session_id, IdentityVerification.id != attempt.id).first()
    if holder is None:
        return True
    if obs.echoed_ref != attempt.reference:
        return False
    locked = (db.query(IdentityVerification).filter(IdentityVerification.id == holder.id).populate_existing()
              .with_for_update(skip_locked=True).first())
    if (locked is None or locked.provider_session_id != session.session_id or locked.status not in VERIFICATION_ACTIVE_STATUSES
            or locked.completed_at is not None):
        return False
    locked.provider_session_id = None
    db.flush()  # frees the unique key for the binding below
    logger.info("kyc cleared a squatted session hint holder=%s attempt=%s", locked.id, attempt.id)
    return True


def _on_pull_error(db: Session, event_id: uuid.UUID, attempt_id: uuid.UUID, exc: PremblyError, final_try: bool) -> Processed:
    if exc.needs_ops_alert:
        # Breaks every caregiver at once and retrying cannot fix it. The log line carries only the kind; K4 raises the alert.
        logger.error("kyc pull failed: %s (ops attention needed)", exc.kind)
    if exc.retryable and not final_try:
        raise KycRetry(min(exc.retry_after or RETRY_BASE_SECONDS, MAX_RETRY_SECONDS))
    if exc.kind == "not_found":  # a signed webhook for a session Prembly does not know: the two disagree
        return _close(db, event_id, DISAGREEMENT, attempt_id)  # ledger only: nothing proves the session was ever this attempt's
    return _close(db, event_id, ERROR, attempt_id)


def _close(db: Session, event_id: uuid.UUID, outcome: str, verification_id: Optional[uuid.UUID] = None,
           observation: Optional[seam.Observation] = None, notice: Optional[Notice] = None) -> Processed:
    """Close the ledger row in the caller's transaction and commit it. A unique-key race on the session id (two attempts claiming
    one session) rolls the recording AND the decision back and closes the row as a disagreement instead (no notice then)."""
    try:
        _stamp(db, event_id, outcome, verification_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        outcome, observation, notice = DISAGREEMENT, None, None
        _stamp(db, event_id, outcome, verification_id)
        db.commit()
    return Processed(outcome, verification_id, observation, notice)


def _stamp(db: Session, event_id: uuid.UUID, outcome: str, verification_id: Optional[uuid.UUID]) -> None:
    db.query(KycWebhookEvent).filter(KycWebhookEvent.id == event_id, KycWebhookEvent.processed_at.is_(None)).update(
        {"outcome": outcome, "processed_at": datetime.utcnow(), "verification_id": verification_id}, synchronize_session=False)


def _uuid(value) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None
