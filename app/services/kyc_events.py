"""The worker side of the Prembly webhook: find the attempt, PULL the session, record what Prembly says (design 8.3-8.4, K3).

K3 stops at recording. It changes NO caregiver status and no attempt status: turning an outcome into ``passed`` / ``failed`` /
``needs_review`` and into ``caregivers.identity_verification_status`` is the K4 decision engine, which builds on ``confirm_and_record``
(the locked section below) and the ``Observation`` it returns.

Trust (design 16.1): the webhook is only the trigger. The signature may have been made with a key that ships inside the app (U1 in
``kyc_seam``), so nothing in the webhook body is believed. The session is fetched with the SECRET key and the pull decides. The
webhook's verdict is compared with the pull's; a disagreement is recorded as such and never as a result.

Binding (16.1 #5): the attempt comes from the signed body's ``oakly_ref`` or, failing that, the client's session-id hint, and in
both cases the pulled session must belong to it: its echoed ``oakly_ref`` equals the attempt's, or (when Prembly echoes none) the
e-mail Prembly recorded is the caregiver's. A hint alone, or a ref alone, is never enough.

LOCK ORDER (K1 follow-up, decided in K3): **always the caregiver row first, then its attempts (by id)**. Erasure
(``AccountErasureService``) already locks the user, then the caregiver, then touches the attempts; the worker used to be specified
as "attempt, then caregiver" (design 8.3), which would deadlock against it. ``lock_caregiver_then_attempt`` is the only way this
module takes the locks; K4 and the reconcile cron must use it. The Prembly HTTP call is made with NO lock and no open
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
from app.services import kyc_seam as seam
from app.services.prembly import PremblyClient, PremblyError, PremblySession

logger = logging.getLogger(__name__)

# Ledger outcomes (``kyc_webhook_events.outcome``; the CHECK set is fixed by migration 0015).
APPLIED, ORPHAN, IGNORED_TERMINAL, DISAGREEMENT, ERROR = "applied", "orphan", "ignored_terminal", "disagreement", "error"
DISAGREEMENT_REASON = "provider_disagreement"
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
    observation: Optional[seam.Observation] = None  # set when the pull was recorded (K4 decides from it)


def lock_caregiver_then_attempt(db: Session, attempt_id: uuid.UUID) -> tuple[Optional[Caregiver], Optional[IdentityVerification]]:
    """The only way KYC code takes these locks (see the module docstring). Returns fresh, locked rows, or (None, None) if the
    attempt does not exist. ``caregiver_id`` never changes, so reading it before the locks is safe."""
    caregiver_id = db.query(IdentityVerification.caregiver_id).filter(IdentityVerification.id == attempt_id).scalar()
    if caregiver_id is None:
        return None, None
    caregiver = db.query(Caregiver).filter(Caregiver.id == caregiver_id).populate_existing().with_for_update().one()
    attempt = (db.query(IdentityVerification).filter(IdentityVerification.id == attempt_id)
               .populate_existing().with_for_update().one())
    return caregiver, attempt


def process_event(db: Session, job: dict, *, client: Optional[PremblyClient] = None, final_try: bool = True) -> Processed:
    """One ledger row. Idempotent: a row that already has an outcome is left alone. Raises ``KycRetry`` when the pull failed in a
    retryable way and ``final_try`` is false; on the last try the row is closed as ``error`` (the K4 reconcile cron then finds the
    attempt from Prembly's side)."""
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
        return _on_pull_error(db, event_id, attempt_id, exc, final_try)
    except ValueError:  # the session id is not one we may put in a URL
        return _close(db, event_id, ERROR, attempt_id)

    outcome, observation = confirm_and_record(db, attempt_id, session, claim=claim)
    if outcome == APPLIED and claim is not None and not observation.final and not final_try:
        # The webhook says Prembly finished but the session read does not show a verdict yet (a lag between the two). Do not
        # consume the event: undo the binding and look again.
        db.rollback()
        raise KycRetry(RETRY_BASE_SECONDS)
    return _close(db, event_id, outcome, attempt_id, observation)


def confirm_and_record(db: Session, attempt_id: uuid.UUID, session: PremblySession, *, claim: Optional[str] = None,
                       ) -> tuple[str, Optional[seam.Observation]]:
    """The locked section: re-check the attempt, bind the pulled session to it, record the scores. Returns (ledger outcome,
    observation). Commits nothing: the caller closes the ledger row in the same transaction."""
    caregiver, attempt = lock_caregiver_then_attempt(db, attempt_id)
    if attempt is None or attempt.status not in VERIFICATION_ACTIVE_STATUSES:
        return IGNORED_TERMINAL, None  # finished or erased while we were talking to Prembly
    observation = seam.observe(session)

    # The attempt was found from attacker-controllable input (the signed body's ref, a client hint), so until the PULLED session is
    # shown to belong to it nothing is written to it, not even a reason code: a forged event must not taint a victim's attempt.
    # The ledger row (outcome ``disagreement``) is the only record then.
    if not _belongs_to(attempt, caregiver, session, observation) or _session_taken_by_another(db, attempt, session.session_id):
        return DISAGREEMENT, None
    if claim is not None and observation.final and claim != observation.verdict:
        return _disagree(attempt), None  # bound, but the webhook and the pull contradict each other

    # Bound to this attempt by Prembly's own record, so it replaces the client's earlier hint (the caregiver may have opened the
    # widget again: the hint is only ever the latest thing the app reported).
    attempt.provider_session_id = session.session_id
    if observation.final:
        attempt.face_confidence, attempt.liveness_confidence = observation.face_confidence, observation.liveness_confidence
        attempt.provider_reference = observation.provider_reference
        attempt.provider_cost = session.total_cost
        attempt.completed_at = session.completed_at or datetime.utcnow()
    return APPLIED, observation


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


def _session_taken_by_another(db: Session, attempt: IdentityVerification, session_id: str) -> bool:
    return db.query(IdentityVerification.id).filter(
        IdentityVerification.provider_session_id == session_id, IdentityVerification.id != attempt.id).first() is not None


def _disagree(attempt: IdentityVerification) -> str:
    if DISAGREEMENT_REASON not in (attempt.reason_codes or []):
        attempt.reason_codes = [*(attempt.reason_codes or []), DISAGREEMENT_REASON]
    return DISAGREEMENT


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
           observation: Optional[seam.Observation] = None) -> Processed:
    """Close the ledger row in the caller's transaction and commit it. A unique-key race on the session id (two attempts claiming
    one session) rolls the recording back and closes the row as a disagreement instead."""
    try:
        _stamp(db, event_id, outcome, verification_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        outcome, observation = DISAGREEMENT, None
        _stamp(db, event_id, outcome, verification_id)
        db.commit()
    return Processed(outcome, verification_id, observation)


def _stamp(db: Session, event_id: uuid.UUID, outcome: str, verification_id: Optional[uuid.UUID]) -> None:
    db.query(KycWebhookEvent).filter(KycWebhookEvent.id == event_id).update(
        {"outcome": outcome, "processed_at": datetime.utcnow(), "verification_id": verification_id}, synchronize_session=False)


def _uuid(value) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None
