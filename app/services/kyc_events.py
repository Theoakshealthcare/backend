"""The worker side of the Dojah webhook: find the attempt, PULL the verification, record what Dojah says (design 8.3-8.4, K3).

K3 recorded what the pull said; K4 (``kyc_engine``, ``kyc_decision``) decides on it in the SAME locked section
(``confirm_and_record``): the attempt becomes ``passed`` / ``failed`` / ``needs_review``, the caregiver's identity status follows
(design 7.2), an ``identity_verification_events`` row is written, and the caregiver is told after the commit. A pulled verdict has
no column, so the decision cannot be made later from the row: it is made here or not at all (an attempt left undecided is found
again by ``kyc_reconcile``).

Trust (design 16.1): the webhook is only the trigger. Nothing in the webhook body is believed (its signature proves the sender
knows the webhook secret, nothing more). The verification is fetched with the SECRET key and the pull decides. The webhook's claim
(``Failed``) is compared with the pull's verdict; a disagreement is recorded as such and never as a result.

Binding (16.1 #5): the server MINTED the reference (``kyc_seam.mint_reference``) before the widget was launched and stored it on
the attempt. The webhook names a reference; the attempt is the one that holds EXACTLY that string, and the pulled verification
must name it back. There is no client hint and no e-mail binding: nobody but this server chooses what an attempt's reference is.

LOCK ORDER (K1 follow-up, decided in K3): **always the caregiver row first, then its attempts (by id)**. Erasure
(``AccountErasureService``) already locks the user, then the caregiver, then touches the attempts; the worker used to be specified
as "attempt, then caregiver" (design 8.3), which would deadlock against it. ``kyc_locks.lock_caregiver_then_attempt`` is the only way
this module (and the engine, and the reconcile cron) takes the locks. The Dojah HTTP call is made with NO lock and no open
transaction, and the attempt's state is re-checked after the locks are taken (an erasure or a human decision may have landed).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, IdentityVerification, KycWebhookEvent,
)
from app.services import kyc_engine
from app.services.dojah import DojahClient, DojahError, DojahVerification
from app.services import kyc_seam as seam
from app.services.kyc_hooks import KycHooks
from app.services.kyc_locks import lock_caregiver_then_attempt
from app.services.kyc_notify import Notice

logger = logging.getLogger(__name__)

# Ledger outcomes (``kyc_webhook_events.outcome``; the CHECK set is fixed by migration 0015).
APPLIED, ORPHAN, IGNORED_TERMINAL, DISAGREEMENT, ERROR = "applied", "orphan", "ignored_terminal", "disagreement", "error"
RETRY_BASE_SECONDS = 30
MAX_RETRY_SECONDS = 300


class KycRetry(Exception):
    """The pull failed in a way worth retrying (Dojah down, rate limited, timed out). The ARQ job turns it into a retry."""

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


def process_event(db: Session, job: dict, *, client: Optional[DojahClient] = None, final_try: bool = True,
                  hooks: Optional[KycHooks] = None, actor: str = "provider") -> Processed:
    """One ledger row. Idempotent: a row that already has an outcome is left alone. Raises ``KycRetry`` when the pull failed in a
    retryable way and ``final_try`` is false; on the last try the row is closed as ``error`` (the K4 reconcile cron then finds the
    attempt from the attempt's own side)."""
    hooks = hooks or KycHooks()
    event_id = uuid.UUID(job["event_id"])
    event = db.query(KycWebhookEvent).filter(KycWebhookEvent.id == event_id).first()
    if event is None or event.processed_at is not None:
        return Processed(event.outcome if event is not None else ERROR)
    reference_id = event.provider_session_id  # the signed body's reference; nulled by erasure; never taken from the job
    claim = job.get("claim")
    settled = bool(job.get("settled"))

    attempt = _locate(db, reference_id)
    if attempt is None:
        # Nobody holds this reference (never ours to bind, or erased): do not keep it in the ledger either (erasure nulled it for a reason)
        db.query(KycWebhookEvent).filter(KycWebhookEvent.id == event_id).update({"provider_session_id": None}, synchronize_session=False)
        return _close(db, event_id, ORPHAN)
    attempt_id = attempt.id
    if attempt.status not in VERIFICATION_ACTIVE_STATUSES:
        return _close(db, event_id, IGNORED_TERMINAL, attempt_id)  # no pull for a finished attempt
    if attempt.completed_at is not None:
        return _close(db, event_id, APPLIED, attempt_id)  # a result is already recorded: a replay costs no pull
    db.rollback()  # no transaction is held open across the Dojah call

    try:
        verification = (client or DojahClient.from_settings()).get_verification(reference_id)
    except DojahError as exc:
        if final_try or not exc.retryable:  # one count per event: ARQ's retries of one failing pull must not flag Dojah down alone
            hooks.on_pull_error(exc)
        return _on_pull_error(db, event_id, attempt_id, exc, final_try)
    except ValueError:  # the reference is not one we may put in a query string
        return _close(db, event_id, ERROR, attempt_id)
    hooks.on_pull_ok()

    recorded = confirm_and_record(db, attempt_id, verification, claim=claim, actor=actor)
    if (recorded.outcome == APPLIED and settled and recorded.observation is not None
            and not recorded.observation.final and not final_try):
        # The webhook says the flow ended but the pull does not show a verdict yet (a lag between the two). Do not consume the
        # event: look again.
        db.rollback()
        raise KycRetry(RETRY_BASE_SECONDS)
    processed = _close(db, event_id, recorded.outcome, attempt_id, recorded.observation, recorded.notice)
    hooks.send(db, processed.notice)  # after the commit; best effort, never raises
    return processed


def confirm_and_record(db: Session, attempt_id: uuid.UUID, verification: DojahVerification, *, claim: Optional[str] = None,
                       actor: str = "provider") -> Recorded:
    """The locked section: re-check the attempt, check the pulled verification names this attempt's reference and, when the pull is
    final, DECIDE (``kyc_engine.decide_and_apply``). Commits nothing: the caller closes the ledger row in the same transaction.

    Nothing is written unless the caregiver is still the provider's to move (``pending`` / ``in_progress``): a human decision
    (``verified`` / ``failed`` / a ``needs_review`` waiting for a person) is never overwritten by a late event (design 7.2)."""
    caregiver, attempt = lock_caregiver_then_attempt(db, attempt_id)
    if attempt is None or attempt.status not in VERIFICATION_ACTIVE_STATUSES:
        return Recorded(IGNORED_TERMINAL)  # finished or erased while we were talking to Dojah
    if not kyc_engine.may_decide(caregiver):
        return Recorded(IGNORED_TERMINAL)  # an admin (or a review already waiting for one) has the last word
    if attempt.completed_at is not None:
        return Recorded(APPLIED)  # a result is already recorded (the unlocked check in process_event is only a cost saver): never overwrite it
    # Until the PULLED answer is shown to be about this attempt's own reference nothing is written to it, not even a reason code.
    if not attempt.provider_session_id or verification.reference_id != attempt.provider_session_id:
        return Recorded(DISAGREEMENT)
    observation = seam.observe(verification)
    if not observation.final:
        return Recorded(APPLIED, observation)  # nothing to record yet: Dojah has no verdict (pending, ongoing or abandoned)
    disagreement = claim is not None and claim != observation.verdict

    attempt.face_confidence = observation.face_confidence  # no liveness score exists (D8); no price in the answer (D14)
    attempt.completed_at = datetime.utcnow()
    applied = kyc_engine.decide_and_apply(db, caregiver, attempt, observation.evidence, actor=actor, disagreement=disagreement)
    return Recorded(DISAGREEMENT if disagreement else APPLIED, observation, applied.notice if applied else None)


def settle_session(db: Session, attempt_id: uuid.UUID, verification: DojahVerification, *, actor: str, hooks: KycHooks) -> Recorded:
    """The same check-and-decide step for a verification found WITHOUT a webhook (the reconcile cron): no ledger row, no webhook
    claim, so there is nothing to compare the pull with; the pull alone decides, exactly as it does for a webhook (the webhook is
    only ever a trigger). Commits, then tells the caregiver."""
    recorded = confirm_and_record(db, attempt_id, verification, actor=actor)
    db.commit()
    hooks.send(db, recorded.notice)
    return recorded


def _locate(db: Session, reference_id: Optional[str]) -> Optional[IdentityVerification]:
    """The attempt holding EXACTLY this reference (``provider_session_id``, UNIQUE). Re-checked against the pull."""
    if not reference_id:
        return None
    return db.query(IdentityVerification).filter(IdentityVerification.provider_session_id == reference_id).first()


def _on_pull_error(db: Session, event_id: uuid.UUID, attempt_id: uuid.UUID, exc: DojahError, final_try: bool) -> Processed:
    if exc.needs_ops_alert:
        # Breaks every caregiver at once and retrying cannot fix it. The log line carries only the kind; K4 raises the alert.
        logger.error("kyc pull failed: %s (ops attention needed)", exc.kind)
    if exc.retryable and not final_try:
        raise KycRetry(min(exc.retry_after or RETRY_BASE_SECONDS, MAX_RETRY_SECONDS))
    if exc.kind == "not_found":  # a signed webhook for a reference Dojah does not know: the two disagree
        return _close(db, event_id, DISAGREEMENT, attempt_id)  # ledger only: nothing proves Dojah ever ran this attempt
    return _close(db, event_id, ERROR, attempt_id)


def _close(db: Session, event_id: uuid.UUID, outcome: str, verification_id: Optional[uuid.UUID] = None,
           observation: Optional[seam.Observation] = None, notice: Optional[Notice] = None) -> Processed:
    """Close the ledger row in the caller's transaction (the recording and the decision) and commit it."""
    _stamp(db, event_id, outcome, verification_id)
    db.commit()
    return Processed(outcome, verification_id, observation, notice)


def _stamp(db: Session, event_id: uuid.UUID, outcome: str, verification_id: Optional[uuid.UUID]) -> None:
    db.query(KycWebhookEvent).filter(KycWebhookEvent.id == event_id, KycWebhookEvent.processed_at.is_(None)).update(
        {"outcome": outcome, "processed_at": datetime.utcnow(), "verification_id": verification_id}, synchronize_session=False)
