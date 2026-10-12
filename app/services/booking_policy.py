"""The admin-editable booking policy: cancellation tiers and caregiver drop-out timings.

``current(db)`` is the one reader (a missing row, which only happens in a database that skipped the migration's seed,
falls back to the defaults). ``update`` is the one writer: it validates the *merged* result, stores it and writes the
audit row (old and new values, the reason) in the same transaction.

The caregiver earnings terms (visit share, hold, late-fee share) are snapshotted onto the booking the same way
(``earnings_snapshot``). The cancellation tiers are **snapshotted** onto the quote and the booking when they are made (``cancel_snapshot``), and
cancellation reads the booking's copy, so an edit here only affects bookings quoted afterwards. The drop-out timings and
the on-my-way window are read live.
"""

import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.models.booking_policy import PAYOUT_CADENCES, POLICY_DEFAULTS, BookingPolicy
from app.services.audit import AuditService

logger = logging.getLogger(__name__)

SNAPSHOT_KEYS = ("free_hours", "late_hours", "fee_percent", "late_fee_percent")


class PolicyInvalid(ValueError):
    """The policy as it would be after the change breaks a rule; the message says which."""


@dataclass(frozen=True)
class PolicyValues:
    cancel_free_hours: int
    cancel_late_hours: int
    cancel_fee_percent: int
    cancel_late_fee_percent: int
    dropout_redispatch_min_lead_minutes: int
    dropout_decision_lead_minutes: int
    onmyway_compensation_window_minutes: int
    caregiver_late_fee_share_percent: int
    caregiver_visit_share_percent: int
    earning_hold_hours: int
    payout_cadence: str
    payout_min_naira: int
    finance_refund_limit_naira: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


FIELDS = tuple(f.name for f in fields(PolicyValues))
MAX_HOURS = 168          # a week
MAX_MINUTES = 10_080     # a week
MAX_NAIRA = 100_000_000
HOUR_FIELDS = ("cancel_free_hours", "cancel_late_hours", "earning_hold_hours")
PERCENT_FIELDS = ("cancel_fee_percent", "cancel_late_fee_percent", "caregiver_late_fee_share_percent",
                  "caregiver_visit_share_percent")
NAIRA_FIELDS = ("payout_min_naira", "finance_refund_limit_naira")
TEXT_FIELDS = ("payout_cadence",)
MINUTE_FIELDS = ("dropout_redispatch_min_lead_minutes", "dropout_decision_lead_minutes",
                 "onmyway_compensation_window_minutes")


def validate(values: Dict[str, Any]) -> None:
    for name in FIELDS:
        value = values[name]
        if name in TEXT_FIELDS:
            if value not in PAYOUT_CADENCES:
                raise PolicyInvalid(f"{name} must be one of: {', '.join(PAYOUT_CADENCES)}")
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyInvalid(f"{name} must be a whole number")
        if value < 0:
            raise PolicyInvalid(f"{name} cannot be negative")
        if name in PERCENT_FIELDS and value > 100:
            raise PolicyInvalid(f"{name} must be between 0 and 100")
        if name in HOUR_FIELDS and value > MAX_HOURS:
            raise PolicyInvalid(f"{name} cannot be more than {MAX_HOURS} hours")
        if name in NAIRA_FIELDS and value > MAX_NAIRA:
            raise PolicyInvalid(f"{name} cannot be more than {MAX_NAIRA} naira")
        if name in MINUTE_FIELDS and value > MAX_MINUTES:
            raise PolicyInvalid(f"{name} cannot be more than {MAX_MINUTES} minutes")
    if values["cancel_late_hours"] >= values["cancel_free_hours"]:
        raise PolicyInvalid("The late-cancellation window (cancel_late_hours) must be shorter than the free-cancellation "
                            "window (cancel_free_hours)")
    if values["cancel_late_fee_percent"] < values["cancel_fee_percent"]:
        raise PolicyInvalid("The late fee (cancel_late_fee_percent) cannot be lower than the earlier fee (cancel_fee_percent)")
    if values["dropout_decision_lead_minutes"] >= values["dropout_redispatch_min_lead_minutes"]:
        raise PolicyInvalid("The decision lead (dropout_decision_lead_minutes) must be shorter than the re-dispatch "
                            "minimum lead (dropout_redispatch_min_lead_minutes)")


def _values(row: Optional[BookingPolicy]) -> PolicyValues:
    if row is None:
        return PolicyValues(**POLICY_DEFAULTS)
    return PolicyValues(**{name: (str if name in TEXT_FIELDS else int)(getattr(row, name)) for name in FIELDS})


def current(db: Session) -> PolicyValues:
    return _values(db.get(BookingPolicy, 1))


def cancel_snapshot(policy: PolicyValues) -> Dict[str, int]:
    """The cancellation terms copied onto a quote, then its booking."""
    return {"free_hours": policy.cancel_free_hours, "late_hours": policy.cancel_late_hours,
            "fee_percent": policy.cancel_fee_percent, "late_fee_percent": policy.cancel_late_fee_percent}


def earnings_snapshot(policy: PolicyValues) -> Dict[str, int]:
    """What a caregiver earns on a booking, copied onto it when it is created."""
    return {"visit_share_percent": policy.caregiver_visit_share_percent, "hold_hours": policy.earning_hold_hours,
            "late_fee_share_percent": policy.caregiver_late_fee_share_percent}


def get_row(db: Session) -> Optional[BookingPolicy]:
    return db.get(BookingPolicy, 1)


def update(db: Session, *, admin_id: UUID, changes: Dict[str, Any], reason: str,
           request: Optional[Request] = None) -> BookingPolicy:
    """Apply ``changes`` (field -> new value) to the policy, validated as a whole. Commits (with the audit row)."""
    row = db.query(BookingPolicy).filter(BookingPolicy.id == 1).with_for_update().first()
    before = _values(row)
    merged = {**before.as_dict(), **changes}
    validate(merged)
    if row is None:
        row = BookingPolicy(id=1)
        db.add(row)
    for name, value in merged.items():
        setattr(row, name, value)
    row.updated_at = datetime.utcnow()
    row.updated_by = admin_id
    db.flush()
    AuditService(db).record(admin_user_id=admin_id, action="booking_policy.update", entity_type="booking_policy",
                            entity_id=1, before=before.as_dict(), after=merged, reason=reason, request=request)
    db.commit()
    logger.info("booking policy updated by admin %s: %s", admin_id,
                {k: (before.as_dict()[k], v) for k, v in changes.items() if before.as_dict()[k] != v})
    return row
