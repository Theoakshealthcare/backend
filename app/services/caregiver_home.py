"""Data behind the caregiver app's home screen and availability switch."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.money import kobo_to_naira
from app.db.models.booking import Booking, BookingNotification, BookingStatus, CaregiverLocation, PaymentStatus
from app.db.models.caregiver import Caregiver, CaregiverWallet
from app.db.models.patient import Patient

HOME_LIST_LIMIT = 50
ACTIVE_STATUSES = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)


class NotApproved(Exception):
    """Only approved caregivers may change availability."""


class LocationRequired(Exception):
    """Going available needs a location on file, otherwise matching could never see the caregiver."""


def set_availability(db: Session, caregiver: Caregiver, is_available: bool) -> bool:
    """Flip ``caregiver_locations.is_available``, the flag matching reads.

    Only the flag changes: the timestamp is left alone, so a toggle can't make a stale position look
    fresh. Already-sent offers and accepted bookings are untouched. Locks the row so two taps in a
    row (or a location ping racing the toggle) apply in order.
    """
    if caregiver.profile_status != "approved":
        raise NotApproved()
    location = (
        db.query(CaregiverLocation)
        .filter(CaregiverLocation.caregiver_id == caregiver.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if location is None:
        db.rollback()
        if is_available:
            raise LocationRequired()
        return False  # nothing to switch off: never matchable anyway
    # Naming updated_at in the SET (to itself) stops the column's onupdate from refreshing it, which
    # would make a stale position count as fresh for matching.
    db.query(CaregiverLocation).filter(CaregiverLocation.id == location.id).update(
        {"is_available": is_available, "updated_at": CaregiverLocation.updated_at},
        synchronize_session=False,
    )
    db.commit()
    return is_available


def _local_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    """Start of tomorrow in HOME_TIMEZONE, as naive UTC (every timestamp in this schema is naive UTC)."""
    tz = ZoneInfo(settings.HOME_TIMEZONE)
    local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)
    tomorrow_start_local = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_now, tomorrow_start_local.astimezone(timezone.utc).replace(tzinfo=None)


def _visit(booking: Booking, patient: Optional[Patient]) -> Dict[str, Any]:
    return {
        "id": booking.id,
        "booking_code": booking.booking_code,
        "service_type": booking.service_type,
        "status": booking.status,
        "dispatch_state": booking.dispatch_state,
        "scheduled_date": booking.scheduled_date,
        "scheduled_end_date": booking.scheduled_end_date,
        "duration_hours": booking.duration_hours,
        "patient_name": patient.full_name if patient else None,
        "patient_address": booking.patient_address,
        "patient_city": booking.patient_city,
        "patient_state": booking.patient_state,
    }


def build_home(db: Session, caregiver: Caregiver, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.utcnow()
    _, tomorrow_start = _local_day_bounds_utc(now)

    location = db.query(CaregiverLocation).filter(CaregiverLocation.caregiver_id == caregiver.id).first()
    wallet = db.query(CaregiverWallet).filter(CaregiverWallet.caregiver_id == caregiver.id).first()

    assigned = (
        db.query(Booking, Patient)
        .outerjoin(Patient, Patient.id == Booking.patient_id)
        .filter(Booking.caregiver_id == caregiver.id, Booking.status.in_(ACTIVE_STATUSES))
    )
    # Today = anything still to do or underway that is due before tomorrow starts (overdue included,
    # so a missed visit doesn't vanish). Upcoming = accepted and due from tomorrow on.
    today = (
        assigned.filter(Booking.scheduled_date < tomorrow_start)
        .order_by(Booking.scheduled_date).limit(HOME_LIST_LIMIT).all()
    )
    upcoming = (
        assigned.filter(Booking.status == BookingStatus.ACCEPTED, Booking.scheduled_date >= tomorrow_start)
        .order_by(Booking.scheduled_date).limit(HOME_LIST_LIMIT).all()
    )

    offers: List[Dict[str, Any]] = []
    if caregiver.profile_status == "approved":
        rows = (
            db.query(BookingNotification, Booking)
            .join(Booking, Booking.id == BookingNotification.booking_id)
            .filter(
                and_(
                    BookingNotification.caregiver_id == caregiver.id,
                    BookingNotification.notification_type == "new_booking",
                    BookingNotification.response.is_(None),
                    Booking.status == BookingStatus.PENDING,
                    Booking.caregiver_id.is_(None),
                    Booking.payment_status == PaymentStatus.PAID,
                )
            )
            .order_by(BookingNotification.created_at.desc())
            .limit(HOME_LIST_LIMIT)
            .all()
        )
        # Minimal on purpose: an offer is not yet this caregiver's job, so no patient details.
        offers = [
            {
                "booking_id": b.id,
                "service_type": b.service_type,
                "scheduled_date": b.scheduled_date,
                "scheduled_end_date": b.scheduled_end_date,
                "duration_hours": b.duration_hours,
                "area": b.patient_city,
                "state": b.patient_state,
                "offered_at": n.created_at,
            }
            for n, b in rows
        ]

    return {
        "profile_status": caregiver.profile_status,
        "is_available": bool(location and location.is_available),
        "location_updated_at": location.updated_at if location else None,
        "today_visits": [_visit(b, p) for b, p in today],
        "upcoming_visits": [_visit(b, p) for b, p in upcoming],
        "open_offers": offers,
        "wallet_balance_naira": float(kobo_to_naira(wallet.balance or 0)) if wallet else 0.0,
        "currency": "NGN",
    }
