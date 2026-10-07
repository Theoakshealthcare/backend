"""Shared setup for the W3 tests: an onboarded WhatsApp contact with bookings of every status."""

from datetime import datetime, timedelta
from typing import Optional

from app.core.security import encrypt
from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.patient import Patient
from app.db.models.whatsapp_booking import BookingEvent
from app.schemas.booking import BookingActionRequest
from app.services import dispatch
from app.services.booking import BookingService
from app.tests.factories import make_booking, make_caregiver
from app.tests.fake_meta import fake_meta  # noqa: F401  (fixture)
from app.tests.test_whatsapp_processor import Chat, onboarded  # noqa: F401  (Chat, fixture helper)

CONDITION = "SENTINEL-CONDITION-7788 post-surgery"
INSTRUCTIONS = "SENTINEL-NOTE-5511 gate code"


def contact_patient(db, chat) -> Patient:
    return db.query(Patient).filter_by(user_id=chat.contact.user_id).one()


def wa_booking(db, chat, caregiver=None, *, hours_ahead: float = 48, status=BookingStatus.PENDING,
               payment_status=PaymentStatus.PAID, recipient: Optional[CareRecipient] = None, **overrides) -> Booking:
    """A paid WhatsApp booking made by the chat's contact, with a sentinel condition that must never be sent."""
    patient = contact_patient(db, chat)
    start = datetime.utcnow() + timedelta(hours=hours_ahead)
    fields = dict(channel="whatsapp", booked_by_user_id=chat.contact.user_id, scheduled_date=start,
                  scheduled_end_date=start + timedelta(hours=4), condition_encrypted=encrypt(CONDITION),
                  special_instructions=INSTRUCTIONS, care_recipient_id=recipient.id if recipient else None)
    fields.update(overrides)
    return make_booking(db, patient, caregiver, status=status, payment_status=payment_status, **fields)


def offer(db, booking, caregiver) -> None:
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="New Booking Request",
                               message="x", notification_type="new_booking"))
    db.commit()


def act(db, booking, caregiver, action: str, **extra) -> Booking:
    return BookingService(db).handle_booking_action(booking.id, caregiver.id, BookingActionRequest(action=action, **extra))


def internal_dropout(db, booking, caregiver) -> Booking:
    """A caregiver drop-out that does not come through the caregiver's cancel button (which refuses a WhatsApp booking
    close to the visit): the same code path an internal or admin-initiated drop-out uses, with no time-window rule."""
    service = BookingService(db)
    locked = db.query(Booking).filter(Booking.id == booking.id).with_for_update().populate_existing().one()
    offers: list = []
    service.apply_caregiver_dropout(locked, caregiver.id, offers)
    db.commit()
    dispatch.push_offers(db, offers)
    db.expire_all()
    return db.get(Booking, booking.id)


def accepted_booking(db, chat, **kw):
    """A paid WhatsApp booking, offered to and accepted by a caregiver through the real transition."""
    caregiver = make_caregiver(db, full_name="Ngozi Okafor", years_of_experience=6)
    booking = wa_booking(db, chat, **kw)
    offer(db, booking, caregiver)
    act(db, booking, caregiver, "accept")
    db.expire_all()
    return db.get(Booking, booking.id), caregiver


def make_recipient(db, user_id, name="Folake Adeyemi", role="family") -> CareRecipient:
    recipient = CareRecipient(full_name=name, created_by_user_id=user_id)
    db.add(recipient)
    db.flush()
    db.add(CareRelationship(contact_user_id=user_id, recipient_id=recipient.id, role=role, clinical_access=False))
    db.commit()
    return recipient


def events_of(db, booking) -> list:
    db.expire_all()
    return [(e.event, e.dedupe_key) for e in db.query(BookingEvent).filter_by(booking_id=booking.id)
            .order_by(BookingEvent.created_at, BookingEvent.event)]


def issuance(booking, caregiver) -> str:
    from app.services.notification_service import issuance_key
    assert booking.caregiver_id == caregiver.id
    return issuance_key(booking)


def body_text(body: dict) -> str:
    interactive = body.get("interactive") or {}
    return body.get("text", {}).get("body") or interactive.get("body", {}).get("text") or ""


def template_params(body: dict) -> list:
    component = next(c for c in body["template"]["components"] if c["type"] == "body")
    return [p["text"] for p in component["parameters"]]
