"""Admin bookings operations (AD-FR-19..27): list, detail, the unmatched queue and the actions on a booking.

Every action follows one shape: lock the booking row (``SELECT ... FOR UPDATE``), check the state under the
lock, change it, write the audit row and commit once, so an audit row always matches a committed change and two
admins (or an admin and a caregiver pressing accept) can never both win.

Lock order is conversation -> quote -> booking, as everywhere else. Admin actions never touch a conversation or
a quote, so they take only the booking lock and then, when a caregiver is involved, that caregiver's row
``FOR SHARE`` (the same order ``BookingService.handle_booking_action`` uses for an accept).

Nothing here makes an HTTP call under a lock, and nothing moves money: a cancel that owes a refund only sets
``refund_required`` for the refund queue. Pushes go out after the commit and never affect the outcome.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.phone import mask_phone, normalize_e164, phone_variants
from app.core.security import decrypt, encrypt
from app.db.models.admin import AdminUser
from app.db.models.audit import AdminAuditLog
from app.db.models.booking import (
    Booking,
    BookingDispatchWave,
    BookingNote,
    BookingNotification,
    BookingStatus,
    PaymentStatus,
)
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient, PatientTransaction
from app.db.models.user import User
from app.db.models.whatsapp_booking import BookingEvent
from app.schemas.admin_booking import (
    AddressOut,
    AssignIn,
    BookingActionOut,
    BookingDetail,
    BookingList,
    BookingListItem,
    CancelIn,
    CandidateList,
    CandidateOut,
    ClinicalOut,
    ConfirmAddressIn,
    CorrectStatusIn,
    DispatchOut,
    EventOut,
    OfferOut,
    PartyOut,
    PaymentAttemptOut,
    PaymentOut,
    PriceOut,
    ReassignIn,
    RedispatchIn,
    TimelineEntry,
    UnmatchedItem,
    UnmatchedList,
    WaveOut,
)
from app.services import booking_events, dispatch
from app.services.booking_cancellation import flag_refund
from app.services.notification_service import NotificationService
from app.services import push as push_service
from app.services.audit import AuditService

logger = logging.getLogger(__name__)

ACTIVE_VISIT = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE)
CAN_BE_CANCELLED = (BookingStatus.PENDING, BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE)
FORWARD = {
    BookingStatus.ACCEPTED: (BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED),
    BookingStatus.EN_ROUTE: (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED),
    BookingStatus.IN_PROGRESS: (BookingStatus.COMPLETED,),
}


class BookingNotFound(Exception):
    pass


class CaregiverNotFound(Exception):
    pass


class Forbidden(Exception):
    """The admin is signed in but may not do this (maps to 403)."""


class Conflict(Exception):
    """The booking (or caregiver) is not in a state that allows the action (maps to 409)."""


class InvalidInput(Exception):
    """A semantically invalid request (maps to 422)."""


# --- masking -----------------------------------------------------------------------------------------


def mask_name(full_name: str) -> str:
    """"Ada Okafor" -> "Ada O."; a single name is shortened to its first letter plus dots."""
    parts = full_name.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0]}."
    return f"{full_name[:1]}..." if full_name else "-"


def _party(ident: UUID, full_name: str, phone: Optional[str], unmasked: bool) -> PartyOut:
    if unmasked:
        return PartyOut(id=ident, full_name=full_name, phone=phone, masked=False)
    return PartyOut(id=ident, full_name=mask_name(full_name), phone=mask_phone(phone) if phone else None, masked=True)


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def no_show_suspected(booking: Booking, now: datetime) -> bool:
    """An accepted booking nobody started NO_SHOW_GRACE_MINUTES after it was due (AD-FR-26)."""
    return (
        booking.status in ACTIVE_VISIT
        and booking.actual_start_time is None
        and booking.scheduled_date + timedelta(minutes=settings.NO_SHOW_GRACE_MINUTES) <= now
    )


def _snapshot(b: Booking) -> Dict[str, Any]:
    return {
        "status": b.status.value,
        "payment_status": b.payment_status.value,
        "dispatch_state": b.dispatch_state,
        "caregiver_id": b.caregiver_id,
        "needs_address_check": b.needs_address_check,
        "refund_required": b.refund_required,
        "refund_required_reason": b.refund_required_reason,
        "cancel_reason": b.cancel_reason,
        "no_show_flagged_at": b.no_show_flagged_at,
    }


# --- preconditions: shared by the actions (which raise) and available_actions (which only asks) --------


def _why_not_unassigned_paid(b: Booking) -> Optional[str]:
    if b.status != BookingStatus.PENDING or b.caregiver_id is not None:
        return "Only a pending booking without a caregiver can be offered"
    if b.payment_status != PaymentStatus.PAID:
        return "Booking has not been paid for"
    if b.needs_address_check:
        return "Confirm the address first: the pin is a city-centre placeholder"
    return None


def _why_not_reassign(b: Booking) -> Optional[str]:
    if b.status not in ACTIVE_VISIT or b.caregiver_id is None:
        return "Only an accepted booking that has not started can be reassigned"
    return None


def _why_not_cancel(b: Booking) -> Optional[str]:
    if b.status not in CAN_BE_CANCELLED:
        return f"A {b.status.value} booking cannot be cancelled"
    return None


def _why_not_confirm_address(b: Booking) -> Optional[str]:
    if not b.needs_address_check:
        return "This booking does not need an address check"
    if b.status != BookingStatus.PENDING:
        return f"A {b.status.value} booking cannot have its address confirmed"
    return None


def _why_not_flag_no_show(b: Booking, now: datetime) -> Optional[str]:
    if b.status not in ACTIVE_VISIT or b.caregiver_id is None or b.actual_start_time is not None:
        return "Only an accepted booking that has not started can be flagged as a no-show"
    if b.scheduled_date > now:
        return "The visit is not due yet"
    if b.no_show_flagged_at is not None:
        return "Already flagged as a no-show"
    return None


def _why_not_correct(b: Booking, target: BookingStatus) -> Optional[str]:
    allowed = FORWARD.get(b.status)
    if b.caregiver_id is None or allowed is None or target not in allowed:
        return f"A {b.status.value} booking cannot be corrected to {target.value}"
    return None


class AdminBookingService:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    # === reads =========================================================================================

    def _base_query(self):
        return (
            self.db.query(Booking, Patient, User.phone_number, Caregiver)
            .join(Patient, Patient.id == Booking.patient_id)
            .join(User, User.id == Patient.user_id)
            .outerjoin(Caregiver, Caregiver.id == Booking.caregiver_id)
        )

    @staticmethod
    def _patient_phone(patient: Patient, user_phone: Optional[str]) -> Optional[str]:
        return patient.phone_number or user_phone

    def _list_item(self, admin: AdminUser, row: Tuple, now: datetime) -> Dict[str, Any]:
        b, patient, user_phone, cg = row
        pii = "people.pii" in admin.permissions
        return {
            "id": b.id,
            "booking_code": b.booking_code,
            "service_type": b.service_type,
            "scheduled_date": b.scheduled_date,
            "scheduled_end_date": b.scheduled_end_date,
            "city": b.patient_city,
            "state": b.patient_state,
            "patient": _party(patient.id, patient.full_name, self._patient_phone(patient, user_phone), pii),
            "caregiver": _party(cg.id, cg.full_name, cg.phone_number, pii) if cg else None,
            "status": b.status.value,
            "payment_status": b.payment_status.value,
            "dispatch_state": b.dispatch_state,
            "total_amount_kobo": b.total_amount_kobo,
            "channel": b.channel,
            "created_at": b.created_at,
            "age_minutes": _minutes(now - b.created_at),
            "needs_address_check": b.needs_address_check,
            "refund_required": b.refund_required,
            "no_show_flagged_at": b.no_show_flagged_at,
            "no_show_suspected": no_show_suspected(b, now),
        }

    def _search_filter(self, q: str, pii: bool):
        if not pii:
            # Searching by name or phone would let a role that only sees masked values confirm who they are.
            return Booking.booking_code.icontains(q, autoescape=True)
        like = [
            Booking.booking_code.icontains(q, autoescape=True),
            Patient.full_name.icontains(q, autoescape=True),
            Caregiver.full_name.icontains(q, autoescape=True),
            Patient.phone_number.icontains(q, autoescape=True),
            User.phone_number.icontains(q, autoescape=True),
        ]
        e164 = normalize_e164(q)
        if e164:
            variants = phone_variants(e164)
            like += [Patient.phone_number.in_(variants), User.phone_number.in_(variants)]
        return or_(*like)

    def list_bookings(
        self, admin: AdminUser, *, q: Optional[str], status: Optional[str], payment_status: Optional[str],
        dispatch_state: Optional[str], channel: Optional[str], city: Optional[str], service_type: Optional[str],
        caregiver_id: Optional[UUID], scheduled_from: Optional[datetime], scheduled_to: Optional[datetime],
        created_from: Optional[datetime], created_to: Optional[datetime], needs_address_check: Optional[bool],
        no_show: Optional[bool], sort: str, order: str, limit: int, offset: int,
    ) -> BookingList:
        now = datetime.utcnow()
        query = self._base_query()
        if q and q.strip():
            query = query.filter(self._search_filter(q.strip(), "people.pii" in admin.permissions))
        if status:
            query = query.filter(Booking.status == BookingStatus(status))
        if payment_status:
            query = query.filter(Booking.payment_status == PaymentStatus(payment_status))
        if dispatch_state:
            query = query.filter(Booking.dispatch_state == dispatch_state)
        if channel:
            query = query.filter(Booking.channel == channel)
        if city:
            query = query.filter(func.lower(Booking.patient_city) == city.strip().lower())
        if service_type:
            query = query.filter(Booking.service_type == service_type)
        if caregiver_id:
            query = query.filter(Booking.caregiver_id == caregiver_id)
        if scheduled_from:
            query = query.filter(Booking.scheduled_date >= scheduled_from)
        if scheduled_to:
            query = query.filter(Booking.scheduled_date < scheduled_to)
        if created_from:
            query = query.filter(Booking.created_at >= created_from)
        if created_to:
            query = query.filter(Booking.created_at < created_to)
        if needs_address_check is not None:
            query = query.filter(Booking.needs_address_check == needs_address_check)
        if no_show is not None:
            suspected = and_(
                Booking.status.in_(ACTIVE_VISIT), Booking.actual_start_time.is_(None),
                Booking.scheduled_date <= now - timedelta(minutes=settings.NO_SHOW_GRACE_MINUTES),
            )
            flagged_or_suspected = or_(Booking.no_show_flagged_at.is_not(None), suspected)
            query = query.filter(flagged_or_suspected if no_show else ~flagged_or_suspected)

        total = query.count()
        column = {"scheduled_date": Booking.scheduled_date, "created_at": Booking.created_at,
                  "total_amount_kobo": Booking.total_amount_kobo}[sort]
        ordering = column.asc() if order == "asc" else column.desc()
        rows = query.order_by(ordering, Booking.id).limit(limit).offset(offset).all()
        items = [BookingListItem(**self._list_item(admin, r, now)) for r in rows]
        return BookingList(items=items, total=total, limit=limit, offset=offset)

    def unmatched_queue(
        self, admin: AdminUser, *, reason: Optional[str], limit: int, offset: int
    ) -> UnmatchedList:
        """Paid bookings that still need an operator: held for an address check, escalated to ``unmatched``, or
        waiting longer than UNMATCHED_SLA_MINUTES for a caregiver. Soonest visit first."""
        now = datetime.utcnow()
        waiting_since = func.coalesce(Booking.dispatch_started_at, Booking.created_at)
        held = Booking.needs_address_check.is_(True)
        unmatched = Booking.dispatch_state == dispatch.UNMATCHED
        breached = and_(
            Booking.dispatch_state.in_([dispatch.DISPATCHING]) | Booking.dispatch_state.is_(None),
            waiting_since <= now - timedelta(minutes=settings.UNMATCHED_SLA_MINUTES),
        )
        by_reason = {"address_check": held, "unmatched": and_(~held, unmatched),
                     "sla_breached": and_(~held, ~unmatched, breached)}
        condition = by_reason[reason] if reason else or_(held, unmatched, breached)

        query = self._base_query().filter(
            Booking.status == BookingStatus.PENDING,
            Booking.payment_status == PaymentStatus.PAID,
            Booking.caregiver_id.is_(None),
            condition,
        )
        total = query.count()
        rows = query.order_by(Booking.scheduled_date.asc(), Booking.id).limit(limit).offset(offset).all()
        items = []
        for row in rows:
            b = row[0]
            queue_reason = "address_check" if b.needs_address_check else (
                "unmatched" if b.dispatch_state == dispatch.UNMATCHED else "sla_breached")
            since = b.dispatch_started_at or b.created_at
            items.append(UnmatchedItem(
                **self._list_item(admin, row, now), queue_reason=queue_reason,
                waiting_minutes=_minutes(now - since)))
        return UnmatchedList(items=items, total=total, limit=limit, offset=offset)

    def candidates(self, booking_id: UUID, radius_km: Optional[float]) -> CandidateList:
        booking = self.db.get(Booking, booking_id)
        if booking is None:
            raise BookingNotFound()
        found = dispatch.find_candidates(self.db, booking, datetime.utcnow(), radius_km)
        return CandidateList(items=[
            CandidateOut(
                caregiver_id=c["caregiver_id"], full_name=c["full_name"], specialization=c.get("specialization"),
                distance_km=c["distance_km"], estimated_arrival_minutes=c.get("estimated_arrival_minutes", 0),
                match_score=c.get("final_match_score", c.get("match_score", 0)))
            for c in found
        ])

    def _clinical(self, admin: AdminUser, b: Booking) -> Tuple[ClinicalOut, List[str]]:
        """The condition and instructions, readable only with clinical.read. Returns the fields revealed so the
        caller can audit the read."""
        condition: Optional[str] = b.condition
        if b.condition_encrypted:
            try:
                condition = decrypt(b.condition_encrypted) if "clinical.read" in admin.permissions else None
            except InvalidToken:
                logger.error("Booking %s: condition cannot be decrypted (encryption key rotated away?)", b.id)
                condition = None
        has_condition = bool(b.condition or b.condition_encrypted)
        has_instructions = bool(b.special_instructions)
        if "clinical.read" not in admin.permissions:
            return ClinicalOut(masked=True, has_condition=has_condition, has_special_instructions=has_instructions), []
        revealed = [name for name, present in (("condition", has_condition),
                                               ("special_instructions", has_instructions)) if present]
        return ClinicalOut(
            masked=False, has_condition=has_condition, has_special_instructions=has_instructions,
            condition=condition, special_instructions=b.special_instructions,
        ), revealed

    def available_actions(self, admin: AdminUser, b: Booking, now: datetime) -> List[str]:
        perms = admin.permissions
        actions: List[str] = []
        unassigned_ok = _why_not_unassigned_paid(b) is None
        if "bookings.dispatch" in perms and unassigned_ok:
            actions.append("redispatch")
        if "bookings.assign" in perms and unassigned_ok:
            actions.append("assign")
            if "bookings.correct_status" in perms:
                actions.append("force_assign")
        if "bookings.assign" in perms and _why_not_reassign(b) is None:
            actions.append("reassign")
        if "bookings.cancel" in perms and _why_not_cancel(b) is None:
            actions.append("cancel")
        if "bookings.address" in perms and _why_not_confirm_address(b) is None:
            actions.append("confirm_address")
        if "bookings.dispatch" in perms and _why_not_flag_no_show(b, now) is None:
            actions.append("flag_no_show")
        if "bookings.correct_status" in perms and b.status in FORWARD:
            actions.append("correct_status")
        return actions

    def detail(self, admin: AdminUser, booking_id: UUID, request: Optional[Request]) -> BookingDetail:
        now = datetime.utcnow()
        row = self._base_query().filter(Booking.id == booking_id).first()
        if row is None:
            raise BookingNotFound()
        b, patient, user_phone, cg = row
        pii = "people.pii" in admin.permissions
        see_address = "bookings.address" in admin.permissions

        clinical, revealed = self._clinical(admin, b)
        if revealed:
            # Reading clinical text is itself an auditable act (AD-FR-73), written before the response leaves.
            self.audit.record(
                admin_user_id=admin.id, action="booking.clinical_view", entity_type="booking",
                entity_id=b.id, after={"fields": revealed}, request=request)
            self.db.commit()

        waves = (self.db.query(BookingDispatchWave).filter(BookingDispatchWave.booking_id == b.id)
                 .order_by(BookingDispatchWave.wave_no).all())
        notices = (
            self.db.query(BookingNotification, Caregiver.full_name)
            .join(Caregiver, Caregiver.id == BookingNotification.caregiver_id)
            .filter(BookingNotification.booking_id == b.id)
            .order_by(BookingNotification.created_at, BookingNotification.id).all()
        )
        offers = [
            OfferOut(caregiver_id=n.caregiver_id, caregiver_name=name if pii else mask_name(name),
                     offered_at=n.created_at, response=n.response, response_at=n.response_at)
            for n, name in notices if n.notification_type == "new_booking"
        ]
        events = (self.db.query(BookingEvent).filter(BookingEvent.booking_id == b.id)
                  .order_by(BookingEvent.created_at).all())
        attempts = (self.db.query(PatientTransaction).filter(PatientTransaction.booking_id == b.id)
                    .order_by(PatientTransaction.created_at).all())
        audit_rows = (
            self.db.query(AdminAuditLog, AdminUser.full_name)
            .join(AdminUser, AdminUser.id == AdminAuditLog.admin_user_id)
            .filter(AdminAuditLog.entity_type == "booking", AdminAuditLog.entity_id == str(b.id))
            .order_by(AdminAuditLog.created_at).all()
        )
        note_count = self.db.query(func.count(BookingNote.id)).filter(BookingNote.booking_id == b.id).scalar() or 0

        timeline = self._timeline(b, waves, notices, events, audit_rows)
        return BookingDetail(
            id=b.id, booking_code=b.booking_code, service_type=b.service_type, assistance_type=b.assistance_type,
            booking_for=b.booking_for, duration_hours=b.duration_hours, scheduled_date=b.scheduled_date,
            scheduled_end_date=b.scheduled_end_date, actual_start_time=b.actual_start_time,
            actual_end_time=b.actual_end_time, status=b.status.value, channel=b.channel,
            cancel_reason=b.cancel_reason, created_at=b.created_at, updated_at=b.updated_at,
            age_minutes=_minutes(now - b.created_at),
            patient=_party(patient.id, patient.full_name, self._patient_phone(patient, user_phone), pii),
            caregiver=_party(cg.id, cg.full_name, cg.phone_number, pii) if cg else None,
            address=AddressOut(
                city=b.patient_city, state=b.patient_state, visible=see_address,
                line=b.patient_address if see_address else None,
                latitude=b.patient_latitude if see_address else None,
                longitude=b.patient_longitude if see_address else None,
                needs_address_check=b.needs_address_check),
            clinical=clinical,
            price=PriceOut(total_kobo=b.total_amount_kobo, service_fee_kobo=b.service_fee_kobo,
                           platform_fee_kobo=b.platform_fee_kobo),
            payment=PaymentOut(
                status=b.payment_status.value, paystack_reference=b.paystack_reference,
                failure_reason=b.payment_failure_reason, expires_at=b.payment_expires_at,
                refund_required=b.refund_required, refund_required_reason=b.refund_required_reason,
                refund_amount_kobo=b.refund_amount_kobo,
                attempts=[PaymentAttemptOut(
                    id=t.id, type=t.type, status=t.status, amount_kobo=t.amount_kobo,
                    paystack_reference=t.paystack_reference, created_at=t.created_at) for t in attempts]),
            dispatch=DispatchOut(
                state=b.dispatch_state, started_at=b.dispatch_started_at,
                waves=[WaveOut(wave_no=w.wave_no, sent_at=w.sent_at, expires_at=w.expires_at,
                               notified_count=len(w.notified_caregiver_ids or [])) for w in waves],
                offers=offers),
            events=[EventOut(event=e.event, created_at=e.created_at, delivered_at=e.delivered_at,
                             attempts=e.attempts, last_error=e.last_error) for e in events],
            no_show_flagged_at=b.no_show_flagged_at,
            no_show_suspected=no_show_suspected(b, now),
            note_count=note_count,
            timeline=timeline,
            available_actions=self.available_actions(admin, b, now),
        )

    @staticmethod
    def _timeline(b: Booking, waves, notices, events, audit_rows) -> List[TimelineEntry]:
        t: List[TimelineEntry] = [TimelineEntry(
            at=b.created_at, kind="booking_created", title=f"Booking created via {b.channel}", actor="system")]
        if b.dispatch_started_at:
            t.append(TimelineEntry(at=b.dispatch_started_at, kind="dispatch_started", title="Dispatch started",
                                   actor="system"))
        for w in waves:
            t.append(TimelineEntry(
                at=w.sent_at, kind="wave_sent", title=f"Wave {w.wave_no} sent", actor="system",
                detail=f"{len(w.notified_caregiver_ids or [])} caregiver(s) offered the visit"))
        for n, name in notices:
            if n.notification_type == "new_booking":
                if n.response_at:
                    t.append(TimelineEntry(at=n.response_at, kind="offer_response", title=f"Offer {n.response}",
                                           actor=f"caregiver: {mask_name(name)}"))
            else:
                t.append(TimelineEntry(at=n.created_at, kind="notification_queued", title=n.title,
                                       actor="system", detail=f"to caregiver {mask_name(name)}"))
        for e in events:
            t.append(TimelineEntry(at=e.created_at, kind="notification_queued", title=f"Event queued: {e.event}",
                                   actor="system"))
            if e.delivered_at:
                t.append(TimelineEntry(at=e.delivered_at, kind="notification_delivered",
                                       title=f"Event delivered: {e.event}", actor="system"))
        if b.actual_start_time:
            t.append(TimelineEntry(at=b.actual_start_time, kind="visit_started", title="Visit started"))
        if b.actual_end_time:
            t.append(TimelineEntry(at=b.actual_end_time, kind="visit_completed", title="Visit completed"))
        for row, admin_name in audit_rows:
            t.append(TimelineEntry(at=row.created_at, kind="admin_action", title=row.action,
                                   actor=f"admin: {admin_name}", detail=row.reason))
        t.sort(key=lambda e: e.at)
        return t

    # === actions =======================================================================================

    def _lock(self, booking_id: UUID) -> Booking:
        booking = (
            self.db.query(Booking).filter(Booking.id == booking_id)
            .with_for_update().populate_existing().first()
        )
        if booking is None:
            self.db.rollback()
            raise BookingNotFound()
        return booking

    def _lock_caregiver(self, caregiver_id: UUID) -> Caregiver:
        """FOR SHARE, after the booking lock: it waits for an in-flight suspend and blocks a new one until this
        commits, so a caregiver is never assigned while being suspended."""
        cg = (self.db.query(Caregiver).filter(Caregiver.id == caregiver_id)
              .with_for_update(read=True).populate_existing().first())
        if cg is None:
            raise CaregiverNotFound()
        if cg.profile_status != "approved":
            raise Conflict("Caregiver is not approved")
        if not self.db.query(User.is_active).filter(User.id == cg.user_id).scalar():
            raise Conflict("Caregiver account is closed")
        return cg

    def _out(self, b: Booking, notified: int = 0) -> BookingActionOut:
        return BookingActionOut(
            id=b.id, booking_code=b.booking_code, status=b.status.value, payment_status=b.payment_status.value,
            dispatch_state=b.dispatch_state, caregiver_id=b.caregiver_id,
            needs_address_check=b.needs_address_check, refund_required=b.refund_required,
            refund_required_reason=b.refund_required_reason, refund_amount_kobo=b.refund_amount_kobo,
            no_show_flagged_at=b.no_show_flagged_at,
            notified_caregivers=notified)

    def _finish(self, booking: Booking) -> None:
        self.db.commit()
        self.db.refresh(booking)

    def _notify_caregiver(self, booking: Booking, caregiver_id: UUID, title: str, message: str,
                          kind: str = "booking_update") -> None:
        self.db.add(BookingNotification(
            booking_id=booking.id, caregiver_id=caregiver_id, title=title, message=message, notification_type=kind,
            # Informational: a non-null response keeps it out of the caregiver app's "new requests" list, which
            # is every booking with an unanswered notification of any type.
            response="info" if kind != "new_booking" else None))

    def _push_caregivers(self, notices: List[Tuple[UUID, str, str, str, UUID]]) -> None:
        """(caregiver_id, title, body, type, booking_id) after the commit. The payload names the booking only:
        never the patient, address or condition."""
        for caregiver_id, title, body, kind, booking_id in notices:
            try:
                user_id = self.db.query(Caregiver.user_id).filter(Caregiver.id == caregiver_id).scalar()
                if user_id is not None:
                    push_service.get_push_service().send_to_users(
                        self.db, [user_id], title=title, body=body,
                        data={"type": kind, "booking_id": str(booking_id)})
            except Exception:
                self.db.rollback()
                logger.warning("push: admin booking notice for booking %s failed", booking_id, exc_info=True)

    # --- re-dispatch -------------------------------------------------------------------------------

    def redispatch(self, booking_id: UUID, admin: AdminUser, body: RedispatchIn,
                   request: Optional[Request]) -> BookingActionOut:
        now = datetime.utcnow()
        booking = self._lock(booking_id)
        try:
            why = _why_not_unassigned_paid(booking)
            if why:
                raise Conflict(why)
            before = _snapshot(booking)
            latest = (self.db.query(func.max(BookingDispatchWave.wave_no))
                      .filter(BookingDispatchWave.booking_id == booking.id).scalar()) or 0
            booking.dispatch_state = dispatch.DISPATCHING
            booking.dispatch_started_at = now
            offers: List[Dict[str, Any]] = []
            sent = dispatch.send_wave(
                self.db, booking, latest + 1, now, offers, radius_km=body.radius_km,
                renotify=body.include_previously_notified)
            if not sent:
                booking.dispatch_state = dispatch.UNMATCHED
            notified = len(offers[0]["caregiver_ids"]) if offers else 0
            self.audit.record(
                admin_user_id=admin.id, action="booking.redispatch", entity_type="booking", entity_id=booking.id,
                before=before, after={**_snapshot(booking), "wave_no": latest + 1, "notified": notified,
                                      "radius_km": body.radius_km,
                                      "include_previously_notified": body.include_previously_notified},
                reason=body.reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        dispatch.push_offers(self.db, offers)
        return self._out(booking, notified)

    # --- assign / reassign -------------------------------------------------------------------------

    def _accept_effects(self, booking: Booking, caregiver_id: UUID, now: datetime, response: str) -> None:
        """What a caregiver's own accept does (``BookingService.handle_booking_action``), for a booking an admin
        assigns directly: the caregiver, status and dispatch state, a fresh visit code, and everyone else's
        unanswered offer closed."""
        booking.caregiver_id = caregiver_id
        booking.status = BookingStatus.ACCEPTED
        booking.dispatch_state = dispatch.MATCHED
        booking.visit_code_enc = encrypt(f"{secrets.randbelow(10_000):04d}")
        booking.visit_code_failed_attempts = 0
        booking.visit_code_locked_until = None
        mine = None
        for n in self.db.query(BookingNotification).filter(BookingNotification.booking_id == booking.id).all():
            if n.caregiver_id == caregiver_id and n.notification_type == "new_booking":
                mine = n
            elif n.notification_type == "new_booking" and n.response in (None, "accept"):
                n.response = "auto_declined"
                n.response_at = now
        if mine is None:
            mine = BookingNotification(
                booking_id=booking.id, caregiver_id=caregiver_id, title="Visit assigned",
                message=f"{booking.service_type.capitalize()} visit assigned to you by Oakly operations",
                notification_type="new_booking")
            self.db.add(mine)
        mine.response = response
        mine.response_at = now

    def assign(self, booking_id: UUID, admin: AdminUser, body: AssignIn,
               request: Optional[Request]) -> BookingActionOut:
        """Offer a pending booking to one caregiver, who still has to accept, or (Super Admin, ``force``) assign it
        outright. A booking someone has accepted in the meantime is a conflict, never a silent reassignment: the
        admin asked for something else."""
        now = datetime.utcnow()
        if body.force and "bookings.correct_status" not in admin.permissions:
            raise Forbidden("Only a Super Admin can force an assignment")
        booking = self._lock(booking_id)
        pushes: List[Tuple[UUID, str, str, str, UUID]] = []
        offers: List[Dict[str, Any]] = []
        try:
            why = _why_not_unassigned_paid(booking)
            if why:
                raise Conflict(why)
            before = _snapshot(booking)
            self._lock_caregiver(body.caregiver_id)
            if body.force:
                self._accept_effects(booking, body.caregiver_id, now, "assigned_by_admin")
                NotificationService(self.db).caregiver_accepted(booking)  # the booker hears who is coming + the code
                pushes.append((body.caregiver_id, "Visit assigned", "A visit was assigned to you",
                               "job_assigned", booking.id))
            else:
                # Offer the visit to them alone. The ordinary accept path (offered, approved, still pending)
                # settles it, and it races with every other offer exactly as a wave does.
                self._offer_to(booking, body.caregiver_id, now, offers)
            self.audit.record(
                admin_user_id=admin.id, action="booking.force_assign" if body.force else "booking.assign",
                entity_type="booking", entity_id=booking.id, before=before,
                after={**_snapshot(booking), "target_caregiver_id": body.caregiver_id},
                reason=body.reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        dispatch.push_offers(self.db, offers)
        self._push_caregivers(pushes)
        return self._out(booking, notified=len(offers))

    def reassign(self, booking_id: UUID, admin: AdminUser, body: ReassignIn,
                 request: Optional[Request]) -> BookingActionOut:
        """Move an accepted visit that has not started to another approved caregiver (AD-FR-24). The new caregiver
        is assigned directly (the booking was already accepted once); both are told."""
        now = datetime.utcnow()
        booking = self._lock(booking_id)
        pushes: List[Tuple[UUID, str, str, str, UUID]] = []
        try:
            why = _why_not_reassign(booking)
            if why:
                raise Conflict(why)
            previous = booking.caregiver_id
            if previous == body.caregiver_id:
                raise Conflict("The booking is already assigned to this caregiver")
            before = _snapshot(booking)
            self._lock_caregiver(body.caregiver_id)
            for n in self.db.query(BookingNotification).filter(
                    BookingNotification.booking_id == booking.id, BookingNotification.caregiver_id == previous,
                    BookingNotification.notification_type == "new_booking"):
                n.response = "reassigned"
                n.response_at = now
            self._accept_effects(booking, body.caregiver_id, now, "assigned_by_admin")
            booking.no_show_flagged_at = None
            booking.no_show_flagged_by = None
            NotificationService(self.db).caregiver_reassigned(booking)  # the booker: new professional and new code
            self._notify_caregiver(booking, previous, "Visit reassigned",
                                   "A visit you accepted was reassigned by Oakly operations")
            pushes += [
                (previous, "Visit reassigned", "A visit you accepted was reassigned", "job_reassigned", booking.id),
                (body.caregiver_id, "Visit assigned", "A visit was assigned to you", "job_assigned", booking.id),
            ]
            self.audit.record(
                admin_user_id=admin.id, action="booking.reassign", entity_type="booking", entity_id=booking.id,
                before=before, after={**_snapshot(booking), "previous_caregiver_id": previous,
                                      "target_caregiver_id": body.caregiver_id},
                reason=body.reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        self._push_caregivers(pushes)
        return self._out(booking)

    def _offer_to(self, booking: Booking, caregiver_id: UUID, now: datetime, offers: List[Dict[str, Any]]) -> None:
        existing = (self.db.query(BookingNotification).filter(
            BookingNotification.booking_id == booking.id, BookingNotification.caregiver_id == caregiver_id,
            BookingNotification.notification_type == "new_booking").first())
        if existing is not None:
            existing.response = None
            existing.response_at = None
        else:
            self._notify_caregiver(
                booking, caregiver_id, "New Booking Request",
                f"New {booking.service_type} request assigned to you by Oakly operations", kind="new_booking")
        offers.append({
            "booking_id": booking.id, "caregiver_ids": [caregiver_id], "service_type": booking.service_type,
            "area": booking.patient_city, "scheduled_date": booking.scheduled_date,
        })

    # --- cancel ------------------------------------------------------------------------------------

    def cancel(self, booking_id: UUID, admin: AdminUser, body: CancelIn,
               request: Optional[Request]) -> BookingActionOut:
        booking = self._lock(booking_id)
        pushes: List[Tuple[UUID, str, str, str, UUID]] = []
        try:
            why = _why_not_cancel(booking)
            if why:
                raise Conflict(why)
            paid = booking.payment_status == PaymentStatus.PAID
            if body.refund != "none" and not paid:
                raise InvalidInput("Nothing to refund: the booking has not been paid for")
            if body.refund == "partial" and body.refund_amount_kobo >= booking.total_amount_kobo:
                raise InvalidInput("A partial refund must be less than the amount paid; choose a full refund")
            before = _snapshot(booking)
            previous = booking.caregiver_id
            booking.status = BookingStatus.CANCELLED
            booking.cancel_reason = body.reason_code
            if body.refund != "none":
                # Money is not moved here. The refund queue (A3) reads these: the reason says which kind, and
                # ``refund_amount_kobo`` says how much (the whole payment for a full refund).
                flag_refund(
                    booking, "admin_cancelled_full" if body.refund == "full" else "admin_cancelled_partial",
                    booking.total_amount_kobo if body.refund == "full" else body.refund_amount_kobo)
            NotificationService(self.db).cancelled_by_admin(booking, caregiver_pushed=True)  # same transaction
            now = datetime.utcnow()
            for n in self.db.query(BookingNotification).filter(
                    BookingNotification.booking_id == booking.id, BookingNotification.notification_type == "new_booking",
                    BookingNotification.response.is_(None)):
                n.response = "booking_cancelled"  # the app lists unanswered offers as new requests
                n.response_at = now
            if previous is not None:
                self._notify_caregiver(booking, previous, "Visit cancelled", "A visit you accepted was cancelled")
                pushes.append((previous, "Visit cancelled", "A visit you accepted was cancelled",
                               "job_cancelled", booking.id))
            self.audit.record(
                admin_user_id=admin.id, action="booking.cancel", entity_type="booking", entity_id=booking.id,
                before=before, after={**_snapshot(booking), "reason_code": body.reason_code, "refund": body.refund,
                                      "refund_amount_kobo": body.refund_amount_kobo},
                reason=body.reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        self._push_caregivers(pushes)
        return self._out(booking)

    # --- confirm address ---------------------------------------------------------------------------

    def confirm_address(self, booking_id: UUID, admin: AdminUser, body: ConfirmAddressIn,
                        request: Optional[Request]) -> BookingActionOut:
        now = datetime.utcnow()
        booking = self._lock(booking_id)
        start = False
        try:
            why = _why_not_confirm_address(booking)
            if why:
                raise Conflict(why)
            before = {**_snapshot(booking), "latitude": booking.patient_latitude,
                      "longitude": booking.patient_longitude}
            booking.patient_latitude = body.latitude
            booking.patient_longitude = body.longitude
            if body.address:
                booking.patient_address = body.address
            booking.needs_address_check = False
            # Exactly what settlement does for a paid booking that is not held: mark it dispatching in this
            # transaction, so the tick can still send wave 1 if the send below fails.
            if booking.payment_status == PaymentStatus.PAID and booking.dispatch_state is None:
                booking.dispatch_state = dispatch.DISPATCHING
                booking.dispatch_started_at = now
                start = True
            self.audit.record(
                admin_user_id=admin.id, action="booking.confirm_address", entity_type="booking",
                entity_id=booking.id, before=before,
                after={**_snapshot(booking), "latitude": body.latitude, "longitude": body.longitude},
                reason=body.reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        if start:
            try:
                dispatch.start_dispatch(self.db, booking.id, now)
            except Exception:
                self.db.rollback()
                logger.exception("Booking %s address confirmed but starting dispatch failed; dispatch_tick will retry",
                                 booking.id)
            self.db.refresh(booking)
        wave = (self.db.query(BookingDispatchWave).filter(BookingDispatchWave.booking_id == booking.id)
                .order_by(BookingDispatchWave.wave_no.desc()).first()) if start else None
        return self._out(booking, notified=len(wave.notified_caregiver_ids) if wave else 0)

    # --- no-show -----------------------------------------------------------------------------------

    def flag_no_show(self, booking_id: UUID, admin: AdminUser, reason: str,
                     request: Optional[Request]) -> BookingActionOut:
        now = datetime.utcnow()
        booking = self._lock(booking_id)
        try:
            why = _why_not_flag_no_show(booking, now)
            if why:
                raise Conflict(why)
            before = _snapshot(booking)
            booking.no_show_flagged_at = now
            booking.no_show_flagged_by = admin.id
            self.audit.record(
                admin_user_id=admin.id, action="booking.flag_no_show", entity_type="booking", entity_id=booking.id,
                before=before, after={**_snapshot(booking), "caregiver_id": booking.caregiver_id},
                reason=reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        return self._out(booking)

    # --- status correction (Super Admin) -----------------------------------------------------------

    def correct_status(self, booking_id: UUID, admin: AdminUser, body: CorrectStatusIn,
                       request: Optional[Request]) -> BookingActionOut:
        now = datetime.utcnow()
        target = BookingStatus(body.status)
        booking = self._lock(booking_id)
        try:
            why = _why_not_correct(booking, target)
            if why:
                raise Conflict(why)
            before = _snapshot(booking)
            # The same fields the caregiver's own start / complete set. Earnings (A3) and the feedback request
            # hook onto the completed transition when they ship.
            if target in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED) and booking.actual_start_time is None:
                booking.actual_start_time = now
            if target == BookingStatus.COMPLETED:
                booking.actual_end_time = now
            booking.status = target
            self.audit.record(
                admin_user_id=admin.id, action="booking.correct_status", entity_type="booking",
                entity_id=booking.id, before=before, after=_snapshot(booking), reason=body.reason, request=request)
            self._finish(booking)
        except Exception:
            self.db.rollback()
            raise
        return self._out(booking)
