"""The ``kyc_reconcile_job`` body (design 6, 7.1, 7.3, 8.3; K3 follow-ups): the backstop for everything the webhook path can lose.

Every ~10 minutes, in this order and inside a time budget (the ARQ job must finish inside its ``job_timeout``):

1. Webhook rows that were never processed (the job was lost, or raised something that is not retried): run them again.
2. Active attempts that have been quiet for ``MIN_AGE``: PULL the verification by the attempt's own reference and decide, exactly as
   the webhook path does. Since the server mints the reference before the widget opens, EVERY attempt has one: there is nothing to
   discover and nothing to bind. This is what settles a run that never produced a webhook (Dojah's retry behaviour is unknown,
   and a person who finishes the selfie on another device never tells the app). The pull decides.
3. Attempts older than ``KYC_SESSION_TTL_HOURS`` with no result: ``expired`` (not a failed attempt, design 7.1), or ``error`` when
   Dojah kept failing for them this run. The caregiver is untouched and can start a new attempt. An abandoned run ends here too.

A provider outage stops the pulls for the run after the first outage-kind error (no hammering a down service) and feeds the
availability flag through ``KycHooks``. Everything here goes through ``kyc_events`` and ``kyc_engine``: no decision logic and
nothing Dojah-specific lives in this module.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_ERROR, VERIFICATION_EXPIRED, IdentityVerification, KycWebhookEvent,
)
from app.services import kyc_engine
from app.services import kyc_seam as seam
from app.services.kyc_events import process_event, settle_session
from app.services.kyc_hooks import KycHooks
from app.services.dojah import NOT_FOUND, DojahClient, DojahError, DojahVerification

logger = logging.getLogger(__name__)

MIN_AGE = timedelta(minutes=5)  # a webhook normally lands within seconds; do not race it
BATCH = 20  # per step per run
DEFAULT_BUDGET_SECONDS = 20.0  # the ARQ job_timeout is 55 s and one Dojah call can take ~31 s (3 attempts of up to 10 s, plus back-off)


@dataclass
class ReconcileStats:
    reprocessed: int = 0
    pulled: int = 0
    decided: int = 0
    expired: int = 0
    errored: int = 0
    provider_errors: int = 0
    out_of_time: bool = False
    erroring_attempts: set = field(default_factory=set, repr=False)  # a pull for it failed this run
    answered_attempts: set = field(default_factory=set, repr=False)  # Dojah answered a pull for it this run (final or not)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("erroring_attempts", "answered_attempts")}


def reconcile(db: Session, *, client: Optional[DojahClient] = None, hooks: Optional[KycHooks] = None,
              now: Optional[datetime] = None, budget_seconds: float = DEFAULT_BUDGET_SECONDS) -> ReconcileStats:
    now = now or datetime.utcnow()
    hooks = hooks or KycHooks()
    client = client or DojahClient.from_settings()
    run = _Run(db, client, hooks, now, time.monotonic() + budget_seconds)
    run.reprocess_lost_jobs()
    run.pull_quiet_attempts()
    run.close_stale_attempts()
    if any((run.stats.reprocessed, run.stats.pulled, run.stats.decided, run.stats.expired, run.stats.errored,
            run.stats.provider_errors)):
        logger.info("kyc_reconcile: %s", run.stats.as_dict())
    return run.stats


class _Run:
    def __init__(self, db: Session, client: DojahClient, hooks: KycHooks, now: datetime, deadline: float):
        self.db, self.client, self.hooks, self.now, self.deadline = db, client, hooks, now, deadline
        self.stats = ReconcileStats()
        self.provider_down = False

    # --- plumbing --------------------------------------------------------------------------------------------
    def _guarded(self, what: str, fn, *args, **kwargs):
        """One row's trouble must not stop the rest of the run: log the kind (never a body), roll back, carry on."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("kyc_reconcile: %s failed (%s)", what, type(exc).__name__)
            self.db.rollback()
            return None

    def _can_call_provider(self) -> bool:
        if self.provider_down:
            return False
        if time.monotonic() >= self.deadline:
            self.stats.out_of_time = True
            return False
        return True

    def _pull(self, reference_id: str, *, attempt_id: uuid.UUID) -> Optional[DojahVerification]:
        """One pull with health tracking. None when it failed (the caller moves on); an outage-kind failure ends the run's pulls."""
        try:
            verification = self.client.get_verification(reference_id)
        except DojahError as exc:
            if exc.kind == NOT_FOUND:
                # The expected answer for a run nobody has opened (the reference is minted before the widget launches, so Dojah may
                # never have heard of it): an answer, not a failure. The attempt expires at its TTL; ask again later, not every run.
                self.hooks.on_pull_ok()
                self.stats.answered_attempts.add(attempt_id)
                self._debounce(attempt_id)
                return None
            self.hooks.on_pull_error(exc)
            self.stats.provider_errors += 1
            self.stats.erroring_attempts.add(attempt_id)
            self.provider_down = exc.outage
            if not exc.outage:
                self.stats.answered_attempts.add(attempt_id)  # Dojah answered, with a "no" (rejected)
                self._debounce(attempt_id)  # ask again later, not every run
            return None
        except ValueError:  # a reference we may not put in a query string: never pulled, expires at its TTL
            return None
        self.hooks.on_pull_ok()
        self.stats.pulled += 1
        self.stats.answered_attempts.add(attempt_id)
        return verification

    def _settle(self, attempt_id: uuid.UUID, verification: DojahVerification) -> None:
        recorded = settle_session(self.db, attempt_id, verification, actor="reconcile", hooks=self.hooks)
        if recorded.notice is not None:
            self.stats.decided += 1
        else:
            self._debounce(attempt_id)  # no verdict yet, or the caregiver is no longer the provider's to move

    def _debounce(self, attempt_id: uuid.UUID) -> None:
        """Do not look at this attempt again for ``MIN_AGE``: ``updated_at`` is the debounce (a finished attempt is unaffected)."""
        self.db.query(IdentityVerification).filter(IdentityVerification.id == attempt_id,
                                                   IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES)).update(
            {"updated_at": datetime.utcnow()}, synchronize_session=False)
        self.db.commit()

    # --- 1. webhook rows nobody processed --------------------------------------------------------------------
    def reprocess_lost_jobs(self) -> None:
        rows = (self.db.query(KycWebhookEvent.id).filter(KycWebhookEvent.processed_at.is_(None),
                                                         KycWebhookEvent.received_at < self.now - MIN_AGE)
                .order_by(KycWebhookEvent.received_at).limit(BATCH).all())
        self.db.rollback()
        for (event_id,) in rows:
            if not self._can_call_provider():
                return
            self._guarded("webhook row", process_event, self.db, {"event_id": str(event_id)}, client=self.client, final_try=True,
                          hooks=self.hooks, actor="reconcile")
            self.stats.reprocessed += 1

    # --- 2. quiet attempts: pull by their own reference ------------------------------------------------------
    def pull_quiet_attempts(self) -> None:
        rows = (self.db.query(IdentityVerification.id, IdentityVerification.provider_session_id)
                .filter(IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES),
                        IdentityVerification.provider_session_id.like("OKL-%"), IdentityVerification.updated_at < self.now - MIN_AGE)
                .order_by(IdentityVerification.updated_at).limit(BATCH).all())
        self.db.rollback()
        for attempt_id, reference_id in rows:
            if not seam.is_our_reference(reference_id):
                continue  # only a reference this server minted is ever pulled (an older, client-chosen id is not)
            if not self._can_call_provider():
                return
            verification = self._pull(reference_id, attempt_id=attempt_id)
            if verification is not None:
                self._guarded("attempt", self._settle, attempt_id, verification)

    # --- 3. nothing answered in time -------------------------------------------------------------------------
    def close_stale_attempts(self) -> None:
        """Expire only what we KNOW has no result: an attempt must have been pulled and answered this run (a result may exist that a
        webhook never delivered, and a billed result must not be thrown away because the provider was down, the budget ran out or the
        debounce was still running). During an outage attempts stay as they are (design 7.3). ``error`` is for an attempt whose
        pull Dojah refused or could not answer. An attempt with no reference at all (none is created that way) is expired once
        the provider is up."""
        if self.provider_down:
            return
        cutoff = kyc_engine.session_cutoff(self.now)
        rows = [(r[0], r[1]) for r in self.db.query(IdentityVerification.id, IdentityVerification.provider_session_id)
                .filter(IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES), IdentityVerification.created_at < cutoff)
                .order_by(IdentityVerification.created_at).limit(100).all()]
        self.db.rollback()
        for attempt_id, reference_id in rows:
            if seam.is_our_reference(reference_id) and attempt_id not in self.stats.answered_attempts:
                continue
            if not seam.is_our_reference(reference_id) and self.stats.out_of_time:
                continue
            errored = attempt_id in self.stats.erroring_attempts
            closed = self._guarded("expiry", kyc_engine.close_unanswered, self.db, attempt_id, older_than=cutoff,
                                   to_status=VERIFICATION_ERROR if errored else VERIFICATION_EXPIRED)
            if closed:
                self.stats.errored += errored
                self.stats.expired += not errored
