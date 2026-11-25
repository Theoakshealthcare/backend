"""Nudge an available caregiver whose location has gone stale ("Update your location").

The app only reports a location while it is open, and matching uses the last known fix for
``AVAILABLE_LOCATION_MAX_HOURS``. Past that the caregiver silently stops receiving offers, so a cron job
(``location_nudge_job`` in app/worker.py) pushes them to open the app. Who gets one:

* approved, not suspended, an active account, and marked available (the same people matching would use);
* last fix older than ``AVAILABLE_LOCATION_MAX_HOURS``;
* at least one push token (no token, no push; Expo dead tokens are pruned by the push service);
* not nudged within ``LOCATION_NUDGE_MIN_GAP_HOURS`` (``caregiver_locations.location_nudged_at``);
* no visit en route or in progress right now (they are busy and the app is open);
* never between ``LOCATION_NUDGE_QUIET_START_HOUR`` and ``LOCATION_NUDGE_QUIET_END_HOUR`` in ``HOME_TIMEZONE``.

Best effort: a failed push never raises and is not recorded, so the next run tries again. Recording the nudge never
touches ``updated_at`` (that column is the fix time), see ``_mark_nudged``.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, CaregiverLocation
from app.db.models.caregiver import Caregiver
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.services import push as push_service

logger = logging.getLogger(__name__)

NUDGE_TYPE = "location_stale"
NUDGE_TITLE = "Update your location"
NUDGE_BODY = "We have not seen your location for a while. Open Oakly so we can keep offering you visits near you."
BATCH_LIMIT = 500  # per run; the rest wait for the next one
BUSY = (BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)


def in_quiet_hours(now: datetime) -> bool:
    """``now`` is naive UTC. True from QUIET_START (inclusive) to QUIET_END (exclusive) on the HOME_TIMEZONE clock; the span may
    cross midnight (21 to 7)."""
    hour = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.HOME_TIMEZONE)).hour
    start, end = settings.LOCATION_NUDGE_QUIET_START_HOUR, settings.LOCATION_NUDGE_QUIET_END_HOUR
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _due(db: Session, now: datetime) -> List[Tuple[UUID, UUID]]:
    stale_before = now - timedelta(hours=settings.AVAILABLE_LOCATION_MAX_HOURS)
    gap_start = now - timedelta(hours=settings.LOCATION_NUDGE_MIN_GAP_HOURS)
    rows = (
        db.query(CaregiverLocation.id, Caregiver.user_id)
        .join(Caregiver, Caregiver.id == CaregiverLocation.caregiver_id)
        .join(User, User.id == Caregiver.user_id)
        .filter(
            CaregiverLocation.is_active.is_(True),
            CaregiverLocation.is_available.is_(True),
            CaregiverLocation.updated_at < stale_before,
            or_(CaregiverLocation.location_nudged_at.is_(None), CaregiverLocation.location_nudged_at <= gap_start),
            Caregiver.profile_status == "approved",
            Caregiver.suspended_at.is_(None),
            User.is_active.is_(True),
            exists().where(PushToken.user_id == Caregiver.user_id),
            ~exists().where(and_(Booking.caregiver_id == Caregiver.id, Booking.status.in_(BUSY))),
        )
        .order_by(CaregiverLocation.updated_at.desc(), CaregiverLocation.id)
        .limit(BATCH_LIMIT)
        .all()
    )
    return [(r[0], r[1]) for r in rows]


def _mark_nudged(db: Session, location_id: UUID, now: datetime) -> None:
    # Naming updated_at in the SET (to itself) stops the column's onupdate from refreshing it: updated_at is the time of the
    # last fix, and a nudge must not make a stale position look fresh.
    db.query(CaregiverLocation).filter(CaregiverLocation.id == location_id).update(
        {"location_nudged_at": now, "updated_at": CaregiverLocation.updated_at}, synchronize_session=False)
    db.commit()


def nudge_stale_locations(db: Session, now: Optional[datetime] = None) -> int:
    """Push every due caregiver once. Returns how many pushes Expo accepted. ``now`` is naive UTC (injectable for tests)."""
    now = now or datetime.utcnow()
    if in_quiet_hours(now):
        return 0
    sent = 0
    service = push_service.get_push_service()
    for location_id, user_id in _due(db, now):
        try:
            accepted = service.send_to_users(db, [user_id], title=NUDGE_TITLE, body=NUDGE_BODY, data={"type": NUDGE_TYPE})
            if accepted:
                _mark_nudged(db, location_id, now)
                sent += 1
        except Exception:
            logger.exception("location nudge failed for one caregiver; carrying on")
            try:
                db.rollback()
            except Exception:
                logger.exception("location nudge: rollback failed")
    return sent
