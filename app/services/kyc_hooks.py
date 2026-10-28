"""The side effects the KYC worker runs around a Prembly call, injected so tests need neither Redis, Expo nor SMTP (K4).

* ``on_pull_ok`` / ``on_pull_error``: provider health (design 7.3). Three consecutive outage-kind errors flag Prembly unavailable for
  15 minutes (the caregiver app then offers "continue without for now"); a wallet that is empty or a key that is refused flags it at
  once AND raises an ops alert (design 11.6), because retrying cannot fix it and it breaks every caregiver together. A good answer
  clears the streak and the flag.
* ``notify``: tell the caregiver what was decided, after the decision is committed.

``KycHooks()`` is inert (no health tracking, no notifications): the plain default for callers that only want the decision.
``KycHooks.production()`` is what the ARQ jobs use.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.services import kyc_notify
from app.services.kyc_availability import ProviderAvailability, get_provider_availability
from app.services.kyc_notify import Notice
from app.services.prembly import PremblyError

logger = logging.getLogger(__name__)


@dataclass
class KycHooks:
    availability: Optional[ProviderAvailability] = None
    notify: Optional[Callable[[Session, Notice], None]] = None
    alert: Optional[Callable[[str], None]] = None

    @classmethod
    def production(cls) -> "KycHooks":
        return cls(availability=get_provider_availability(), notify=kyc_notify.deliver, alert=kyc_notify.alert_ops)

    def on_pull_ok(self) -> None:
        if self.availability is not None:
            self.availability.record_success()

    def on_pull_error(self, exc: PremblyError) -> None:
        if self.availability is None or not exc.outage:
            return
        if exc.needs_ops_alert:
            self.availability.mark_unavailable()
            if self.alert is not None and self.availability.claim_alert(exc.kind):
                self.alert(exc.kind)
        else:
            self.availability.record_error()

    def send(self, db: Session, notice: Optional[Notice]) -> None:
        if notice is not None and self.notify is not None:
            self.notify(db, notice)
