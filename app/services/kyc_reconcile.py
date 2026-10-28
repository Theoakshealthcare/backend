"""The ``kyc_reconcile_job`` body (design 6, 7.1, 7.3, 8.3; K3 follow-ups): the backstop for everything the webhook path can lose.

Every ~10 minutes, in this order and inside a time budget (the ARQ job must finish inside its ``job_timeout``):

1. Webhook rows that were never processed (the job was lost, or raised something that is not retried): run them again.
2. Active attempts that already carry a session id (the widget's hint, or a bound session) and have been quiet for
   ``MIN_AGE``: PULL the session and decide, exactly as the webhook path does. The pull decides; there is nothing to "claim".
3. Active attempts with NO session id (the hint never arrived, and Prembly's webhook was lost): drain Prembly's undelivered-session
   list, pull each finished session and bind it to the attempt whose reference Prembly echoes (the strong binding only: nobody
   named the session, so an e-mail match would let a stranger use up someone's attempts). The same ownership check as the
   webhook path (``kyc_events.confirm_and_record``) still applies.
4. Attempts older than ``KYC_SESSION_TTL_HOURS`` with no result: ``expired`` (not a failed attempt, design 7.1), or ``error`` when
   Prembly kept failing for them this run. The caregiver is untouched and can start a new attempt.

A provider outage stops the pulls for the run after the first outage-kind error (no hammering a down service) and feeds the
availability flag through ``KycHooks``. Everything here goes through ``kyc_events`` and ``kyc_engine``: no decision logic and
nothing Prembly-specific lives in this module.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_ERROR, VERIFICATION_EXPIRED, IdentityVerification, KycWebhookEvent,
)
from app.services import kyc_engine
from app.services import kyc_seam as seam
from app.services.kyc_events import process_event, settle_session
from app.services.kyc_hooks import KycHooks
from app.services.prembly import PremblyClient, PremblyError, PremblySession

logger = logging.getLogger(__name__)

MIN_AGE = timedelta(minutes=5)  # a webhook normally lands within seconds; do not race it
BATCH = 20  # per step per run
UNDELIVERED_PAGE_SIZE = 20
MAX_UNDELIVERED_PAGES = 3
DEFAULT_BUDGET_SECONDS = 35.0  # the ARQ job_timeout is 55 s and a single Prembly call can take up to 10 s


@dataclass
class ReconcileStats:
    reprocessed: int = 0
    pulled: int = 0
    decided: int = 0
    bound: int = 0
    expired: int = 0
    errored: int = 0
    provider_errors: int = 0
    out_of_time: bool = False
    erroring_attempts: set = field(default_factory=set, repr=False)  # a pull for it failed this run
    answered_attempts: set = field(default_factory=set, repr=False)  # Prembly answered a pull for it this run (final or not)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("erroring_attempts", "answered_attempts")}


def reconcile(db: Session, *, client: Optional[PremblyClient] = None, hooks: Optional[KycHooks] = None,
              now: Optional[datetime] = None, budget_seconds: float = DEFAULT_BUDGET_SECONDS) -> ReconcileStats:
    now = now or datetime.utcnow()
    hooks = hooks or KycHooks()
    client = client or PremblyClient.from_settings()
    run = _Run(db, client, hooks, now, time.monotonic() + budget_seconds)
    run.reprocess_lost_jobs()
    run.pull_attempts_with_a_session()
    run.discover_attempts_without_a_session()
    run.close_stale_attempts()
    if any((run.stats.reprocessed, run.stats.pulled, run.stats.decided, run.stats.bound, run.stats.expired, run.stats.errored,
            run.stats.provider_errors)):
        logger.info("kyc_reconcile: %s", run.stats.as_dict())
    return run.stats


class _Run:
    def __init__(self, db: Session, client: PremblyClient, hooks: KycHooks, now: datetime, deadline: float):
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

    def _pull(self, session_id: str, *, attempt_id: Optional[uuid.UUID] = None) -> Optional[PremblySession]:
        """One pull with health tracking. None when it failed (the caller moves on); an outage-kind failure ends the run's pulls."""
        try:
            session = self.client.get_session(session_id, include_payload=True)
        except PremblyError as exc:
            self.hooks.on_pull_error(exc)
            self.stats.provider_errors += 1
            if attempt_id is not None:
                self.stats.erroring_attempts.add(attempt_id)
            self.provider_down = exc.outage
            if attempt_id is not None and not exc.outage:
                self.stats.answered_attempts.add(attempt_id)  # Prembly answered, with a "no"
                self._debounce(attempt_id)  # Prembly answered "no" (not found, rejected): ask again later, not every run
            return None
        except ValueError:  # a session id we may not put in a URL (a bad client hint): never pulled, expires at its TTL
            return None
        self.hooks.on_pull_ok()
        self.stats.pulled += 1
        if attempt_id is not None:
            self.stats.answered_attempts.add(attempt_id)
        return session

    def _settle(self, attempt_id: uuid.UUID, session: PremblySession) -> None:
        recorded = settle_session(self.db, attempt_id, session, actor="reconcile", hooks=self.hooks)
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

    # --- 2. attempts that carry a session id -----------------------------------------------------------------
    def pull_attempts_with_a_session(self) -> None:
        rows = (self.db.query(IdentityVerification.id, IdentityVerification.provider_session_id)
                .filter(IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES),
                        IdentityVerification.provider_session_id.isnot(None), IdentityVerification.updated_at < self.now - MIN_AGE)
                .order_by(IdentityVerification.updated_at).limit(BATCH).all())
        self.db.rollback()
        for attempt_id, session_id in rows:
            if not self._can_call_provider():
                return
            session = self._pull(session_id, attempt_id=attempt_id)
            if session is not None:
                self._guarded("attempt", self._settle, attempt_id, session)

    # --- 3. attempts with no session id: look through Prembly's undelivered list -----------------------------
    def discover_attempts_without_a_session(self) -> None:
        waiting = (self.db.query(func.count(IdentityVerification.id))
                   .filter(IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES),
                           IdentityVerification.provider_session_id.is_(None),
                           IdentityVerification.created_at < self.now - MIN_AGE).scalar())
        self.db.rollback()
        if not waiting:
            return  # nothing to bind to: no call to Prembly while idle
        for page_no in range(1, MAX_UNDELIVERED_PAGES + 1):
            if not self._can_call_provider():
                return
            try:
                page = self.client.list_undelivered(page=page_no, page_size=UNDELIVERED_PAGE_SIZE)
            except PremblyError as exc:
                self.hooks.on_pull_error(exc)
                self.stats.provider_errors += 1
                self.provider_down = exc.outage
                return
            self.hooks.on_pull_ok()
            for summary in page.sessions:
                if not self._can_call_provider():
                    return
                if seam.observe(summary).final and not self._session_known(summary.session_id):
                    self._guarded("discovered session", self._bind_discovered, summary.session_id)
            if not page.has_more:
                return

    def _session_known(self, session_id: str) -> bool:
        known = self.db.query(IdentityVerification.id).filter(IdentityVerification.provider_session_id == session_id).first()
        self.db.rollback()
        return known is not None

    def _bind_discovered(self, session_id: str) -> None:
        session = self._pull(session_id)
        if session is None:
            return
        attempt_id = self._attempt_for(seam.observe(session))
        if attempt_id is None:
            return  # nobody's: left for the day an attempt claims it (or never)
        self.stats.bound += 1
        self._settle(attempt_id, session)

    def _attempt_for(self, observation: seam.Observation) -> Optional[uuid.UUID]:
        """The active attempt a session found without a webhook is for: ONLY the one whose reference Prembly echoes. No webhook and no
        client hint named this session, so nothing but our own opaque ``oakly_ref`` in Prembly's record is strong enough: anyone with
        the (public) widget key can start a session under any e-mail address, and an e-mail match here would let a stranger's
        deliberately failed session use up a victim's attempts. A session whose ref is not echoed (U6) is left for the webhook or
        the client hint, both of which name it. The pull-side ownership check in ``confirm_and_record`` still applies."""
        if observation.echoed_ref is None:
            return None
        found = (self.db.query(IdentityVerification.id)
                 .filter(IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES),
                         IdentityVerification.reference == observation.echoed_ref).first())
        self.db.rollback()
        return found[0] if found else None

    # --- 4. nothing answered in time -------------------------------------------------------------------------
    def close_stale_attempts(self) -> None:
        """Expire only what we KNOW has no result: an attempt with a session id must have been pulled and answered this run (a result may
        exist that a webhook never delivered, and a billed result must not be thrown away because the provider was down, the budget ran
        out or the debounce was still running); one with no session id only after the discovery step ran to its end. During an outage
        attempts stay as they are (design 7.3). ``error`` is for an attempt whose pull Prembly refused or could not answer."""
        if self.provider_down:
            return
        cutoff = kyc_engine.session_cutoff(self.now)
        rows = [(r[0], r[1]) for r in self.db.query(IdentityVerification.id, IdentityVerification.provider_session_id)
                .filter(IdentityVerification.status.in_(VERIFICATION_ACTIVE_STATUSES), IdentityVerification.created_at < cutoff)
                .order_by(IdentityVerification.created_at).limit(100).all()]
        self.db.rollback()
        for attempt_id, session_id in rows:
            if session_id is not None and attempt_id not in self.stats.answered_attempts:
                continue
            if session_id is None and self.stats.out_of_time:
                continue
            errored = attempt_id in self.stats.erroring_attempts
            closed = self._guarded("expiry", kyc_engine.close_unanswered, self.db, attempt_id, older_than=cutoff,
                                   to_status=VERIFICATION_ERROR if errored else VERIFICATION_EXPIRED)
            if closed:
                self.stats.errored += errored
                self.stats.expired += not errored
