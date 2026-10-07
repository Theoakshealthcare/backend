"""Pricing settings are versioned: every change inserts a new ``booking_settings`` row and leaves the old ones alone.

Quotes and bookings point at the row they were priced with (``settings_id``), so editing in place would silently
re-price history. Pricing reads the newest row (``created_at`` desc), which is why a new row is the whole "apply".
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models.booking_settings import BookingSettings
from app.schemas.booking_settings import BookingSettingsWrite
from app.services.audit import AuditService

logger = logging.getLogger(__name__)

_WRITE_LOCK_KEY = 7_204_001  # arbitrary, only has to be unique among this app's advisory locks


class SettingsNotFound(Exception):
    pass


class SettingsSuperseded(Exception):
    """The version being edited is no longer the latest: someone published a newer one."""

    def __init__(self, latest_id: UUID):
        super().__init__(str(latest_id))
        self.latest_id = latest_id


def latest(db: Session) -> Optional[BookingSettings]:
    return db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first()


def snapshot(row: Optional[BookingSettings]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": str(row.id), "base_fee": row.base_fee, "platform_fee_percent": row.platform_fee_percent,
        "multipliers": row.multipliers, "surge_pricing": row.surge_pricing,
        "surge_multiplier": row.surge_multiplier, "discount": row.discount,
    }


def publish(db: Session, *, admin_id: UUID, body: BookingSettingsWrite, supersedes: Optional[UUID] = None,
            request: Optional[Request] = None) -> BookingSettings:
    """Insert a new version and its audit row in one transaction. Commits.

    ``supersedes`` (PUT) must be the current latest version, otherwise ``SettingsSuperseded``: a stale form must not
    quietly overwrite a newer price. Writers are serialised so "latest" cannot fork.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _WRITE_LOCK_KEY})
    current = latest(db)
    if supersedes is not None:
        target = db.get(BookingSettings, supersedes)
        if target is None:
            raise SettingsNotFound()
        if current is None or current.id != target.id:
            raise SettingsSuperseded(current.id if current else target.id)

    before = snapshot(current)
    now = datetime.utcnow()
    row = BookingSettings(
        base_fee_kobo=None, platform_fee_percent=body.platform_fee_percent, multipliers=body.multipliers,
        surge_pricing=body.surge_pricing, surge_multiplier=body.surge_multiplier, discount=body.discount,
        created_at=now, updated_at=now,
    )
    row.base_fee = body.base_fee
    db.add(row)
    db.flush()
    AuditService(db).record(
        admin_user_id=admin_id, action="booking_settings.update" if supersedes else "booking_settings.create",
        entity_type="booking_settings", entity_id=row.id, before=before, after=snapshot(row),
        reason=body.reason, request=request)
    db.commit()
    db.refresh(row)
    logger.info("pricing version %s published by admin %s (supersedes %s)", row.id, admin_id,
                before["id"] if before else None)
    return row
