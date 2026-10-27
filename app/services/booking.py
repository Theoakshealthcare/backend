import hmac
import logging
import math
import secrets
import string
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from uuid import UUID
from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from app.db.models.booking import (
    Booking, BookingNotification, ChatMessage, BookingNote,
    BookingStatus, PaymentStatus
)
from app.db.models.booking_settings import BookingSettings
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.schemas.booking import BookingCreate, BookingActionRequest, BookingPreviewRequest, BookingPreviewResponse
from app.services import booking_policy, earnings
from app.services.booking_cancellation import flag_refund
from app.services.location import LocationService
from app.services.notification_service import NotificationService
from app.services.onmyway import distance_m as onmyway_distance_m
from app.services.gemini import gemini_service
from app.services.paystack import PaystackService, paystack_email_for
from app.services.pricing import PriceBreakdown, compute_price, service_multiplier
# aliased: `settings` is shadowed by BookingSettings locals in this module's methods
from app.core.config import settings as app_settings
from app.core.money import kobo_to_naira
from app.core.security import decrypt, encrypt

logger = logging.getLogger(__name__)


def _naira(kobo: int) -> float:
    """Kobo -> naira for the (float-typed) response fields. Display only."""
    return float(kobo_to_naira(kobo))


class BookingActionForbidden(Exception):
    """The caregiver is not allowed to perform this action on this booking (HTTP 403)."""


LOCATION_FRESH_MINUTES = 5  # a fix older than this is not "where they were when they tapped on my way"


def _minutes_words(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" + ("" if hours == 1 else "s")
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


class BookingStateConflict(Exception):
    """The booking is not in a state that allows this action (HTTP 409)."""


class BookingVisitCodeError(Exception):
    """The visit code was missing or wrong (HTTP 400), or is locked after too many wrong tries
    (HTTP 423, with ``retry_after`` seconds)."""

    def __init__(self, detail: str, status_code: int, retry_after: Optional[int] = None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class BookingService:
    def __init__(self, db: Session):
        self.db = db
        self.location_service = LocationService(db)
        self.paystack_service = PaystackService()

    def generate_booking_code(self) -> str:
        """Generate unique booking code"""
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = self.db.query(Booking).filter(Booking.booking_code == code).first()
            if not existing:
                return code

    def calculate_booking_preview(self, preview_data: BookingPreviewRequest) -> BookingPreviewResponse:
        """Calculate booking pricing and details for preview without creating booking"""
        
        # Fetch latest booking settings
        settings = self.db.query(BookingSettings).order_by(BookingSettings.created_at.desc(), BookingSettings.id.desc()).first()
        if not settings:
            raise Exception("Booking settings not configured.")
        
        # Price in kobo (single source of truth shared with create_booking)
        multiplier = service_multiplier(settings, preview_data.service_type, channel="app")
        surge_active = bool(settings.surge_pricing and settings.surge_multiplier)
        price = compute_price(
            base_fee_kobo=settings.base_fee_kobo,
            quantity=preview_data.duration_hours,
            multiplier=multiplier,
            surge_multiplier=settings.surge_multiplier if surge_active else None,
            discount_percent=settings.discount,
            platform_fee_percent=settings.platform_fee_percent,
        )

        # Detailed breakdown (naira, 2dp, for the client)
        pricing_breakdown = {
            "base_calculation": _naira(price.base_calculation_kobo),
            "surge_adjustment": _naira(price.surge_adjustment_kobo),
            "discount_savings": _naira(price.discount_savings_kobo),
            "service_fee_final": _naira(price.service_fee_kobo),
            "platform_fee": _naira(price.platform_fee_kobo),
            "total": _naira(price.total_kobo),
        }

        # Check available caregivers if location provided
        estimated_caregivers = None
        if preview_data.patient_latitude and preview_data.patient_longitude:
            nearby_caregivers = self.location_service.get_nearby_active_caregivers_with_eta(
                preview_data.patient_latitude,
                preview_data.patient_longitude,
                radius_km=app_settings.MATCH_RADIUS_KM,
                service_type=preview_data.service_type
            )
            estimated_caregivers = len(nearby_caregivers) if nearby_caregivers else 0
        
        # Peak hours warning
        current_hour = datetime.now().hour
        peak_hours_warning = None
        if 17 <= current_hour <= 21:  # 5-9 PM peak hours
            peak_hours_warning = "Peak hours (5-9 PM): Higher demand may affect caregiver availability"
        elif settings.surge_pricing and settings.surge_multiplier > 1.0:
            peak_hours_warning = f"Surge pricing active: {int((settings.surge_multiplier - 1) * 100)}% increase due to high demand"
        
        return BookingPreviewResponse(
            service_type=preview_data.service_type,
            duration_hours=preview_data.duration_hours,
            base_fee_per_hour=_naira(settings.base_fee_kobo),
            service_multiplier=multiplier,
            service_fee=_naira(price.service_fee_kobo),
            platform_fee=_naira(price.platform_fee_kobo),
            platform_fee_percent=settings.platform_fee_percent,
            total_amount=_naira(price.total_kobo),
            currency="NGN",
            
            # Pricing factors
            surge_pricing_active=bool(settings.surge_pricing and settings.surge_multiplier > 1.0),
            surge_multiplier=settings.surge_multiplier if settings.surge_pricing else None,
            discount_active=bool(settings.discount and settings.discount > 0),
            discount_percent=settings.discount if settings.discount else None,
            
            # Detailed breakdown
            pricing_breakdown=pricing_breakdown,
            
            # Additional info
            estimated_caregivers_available=estimated_caregivers,
            peak_hours_warning=peak_hours_warning
        )

    def create_booking(self, patient_id: UUID, booking_data: BookingCreate) -> Dict[str, Any]:
        """Create a new booking with payment initiation"""
        booking = self.insert_booking(patient_id, booking_data, commit=True)
        return self.start_payment(
            booking, callback_url=f"{app_settings.FRONTEND_URL}/bookings/{booking.id}/payment-complete"
        )

    def insert_booking(
        self,
        patient_id: UUID,
        booking_data: BookingCreate,
        *,
        commit: bool = True,
        locked_price: Optional[PriceBreakdown] = None,
        channel: str = "app",
        booked_by_user_id: Optional[UUID] = None,
        care_recipient_id: Optional[UUID] = None,
        condition_encrypted: Optional[str] = None,
        needs_address_check: bool = False,
    ) -> Booking:
        """Price and insert the booking row. ``commit=False`` only flushes, so a caller (the WhatsApp channel)
        can link it to its quote in the same transaction. ``locked_price`` is a price that was shown to the
        person and has not been recomputed since (WA-FR-20); without it the current settings decide."""

        # Calculate end time
        scheduled_end = booking_data.scheduled_date + timedelta(hours=booking_data.duration_hours)

        if locked_price is not None:
            price = locked_price
        else:
            # Fetch latest booking settings
            settings = self.db.query(BookingSettings).order_by(BookingSettings.created_at.desc(), BookingSettings.id.desc()).first()
            if not settings:
                raise Exception("Booking settings not configured.")

            # Price in kobo. What we store is exactly what we send to Paystack and what settlement
            # later compares against.
            multiplier = service_multiplier(settings, booking_data.service_type, channel="app")
            price = compute_price(
                base_fee_kobo=settings.base_fee_kobo,
                quantity=booking_data.duration_hours,
                multiplier=multiplier,
                surge_multiplier=settings.surge_multiplier if settings.surge_pricing else None,
                discount_percent=settings.discount,
                platform_fee_percent=settings.platform_fee_percent,
            )

        # Generate unique payment reference
        booking_code = self.generate_booking_code()
        paystack_reference = f"BKG_{booking_code}_{patient_id.hex[:8]}"

        booking = Booking(
            booking_code=booking_code,
            patient_id=patient_id,
            service_type=booking_data.service_type,
            assistance_type=booking_data.assistance_type,
            condition=booking_data.condition,
            condition_encrypted=condition_encrypted,
            duration_hours=booking_data.duration_hours,
            scheduled_date=booking_data.scheduled_date,
            scheduled_end_date=scheduled_end,
            patient_address=booking_data.patient_address,
            patient_latitude=booking_data.patient_latitude,
            patient_longitude=booking_data.patient_longitude,
            patient_city=booking_data.patient_city,
            patient_state=booking_data.patient_state,
            needs_address_check=needs_address_check,
            special_instructions=booking_data.special_instructions,
            booking_for=booking_data.booking_for,
            channel=channel,
            booked_by_user_id=booked_by_user_id,
            care_recipient_id=care_recipient_id,
            earnings_terms=booking_policy.earnings_snapshot(booking_policy.current(self.db)),
            total_amount_kobo=price.total_kobo,
            service_fee_kobo=price.service_fee_kobo,
            platform_fee_kobo=price.platform_fee_kobo,
            paystack_reference=paystack_reference,
            payment_expires_at=datetime.utcnow() + timedelta(minutes=app_settings.PAYMENT_LINK_TTL_MINUTES),
        )

        self.db.add(booking)
        if commit:
            self.db.commit()
            self.db.refresh(booking)
        else:
            self.db.flush()
        return booking

    def start_payment(self, booking: Booking, *, callback_url: str) -> Dict[str, Any]:
        """Initialise the Paystack transaction for a booking that already exists."""
        patient = self.db.query(Patient).filter(Patient.id == booking.patient_id).first()
        if not patient:
            raise Exception("Patient not found for payment processing.")
        patient_id = booking.patient_id

        # Initialize Paystack payment
        payment_response = self.paystack_service.initialize_transaction(
            email=paystack_email_for(patient),
            amount_kobo=booking.total_amount_kobo,
            reference=booking.paystack_reference,
            callback_url=callback_url,
            metadata={
                "booking_id": str(booking.id),
                "patient_id": str(patient_id),
                "service_type": booking.service_type,
                "booking_code": booking.booking_code
            }
        )

        if not payment_response.get("status"):
            # If payment initialization fails, mark booking as cancelled
            booking.status = BookingStatus.CANCELLED
            booking.payment_status = PaymentStatus.FAILED
            self.db.commit()
            self.db.refresh(booking)

            return {
                "booking": booking,
                "payment_url": None,
                "payment_error": payment_response.get("message", "Payment initialization failed")
            }

        return {
            "booking": booking,
            "payment_url": payment_response.get("authorization_url"),
            "access_code": payment_response.get("access_code")
        }

    def smart_caregiver_matching(self, booking: Booking, now: Optional[datetime] = None,
                                 radius_km: Optional[float] = None) -> List[Dict[str, Any]]:
        """Uber-like smart matching algorithm considering multiple factors.

        Eligibility (approved, not suspended, skill for the service type, fresh available
        location, within the radius) is decided in LocationService; this only orders the result.
        """
        
        # Get enhanced caregivers with ETA and scoring
        base_caregivers = self.location_service.get_nearby_active_caregivers_with_eta(
            booking.patient_latitude, booking.patient_longitude,
            radius_km=radius_km if radius_km is not None else app_settings.MATCH_RADIUS_KM,
            service_type=booking.service_type,
            now=now,
        )
        
        if not base_caregivers:
            return []
        
        # Enhanced scoring with additional factors
        scored_caregivers = []
        
        for caregiver in base_caregivers:
            # Start with base match score from location service
            score = caregiver.get('match_score', 100)
            
            # Time-based bonus (better availability during certain hours)
            current_hour = datetime.now().hour
            if 8 <= current_hour <= 20:  # Regular working hours
                score += 10
            elif 6 <= current_hour <= 22:  # Extended hours
                score += 5
            
            # Service type specialization bonus
            specialization = (caregiver.get('specialization') or '').lower()
            service_type = booking.service_type.lower()
            if service_type in specialization:
                score += 20  # Perfect match
            elif any(keyword in specialization for keyword in [service_type[:4], 'general', 'all']):
                score += 10  # Partial match
            
            # Urgency factor (for future implementation)
            # if booking has urgent flag, prioritize closer caregivers more heavily
            
            # Recent activity bonus (caregivers who updated location recently)
            if caregiver.get('last_location_update'):
                # This would need to be calculated in the location service
                pass
            
            # Update caregiver with final score
            caregiver_copy = caregiver.copy()
            caregiver_copy['final_match_score'] = round(score, 2)
            scored_caregivers.append(caregiver_copy)
        
        # Sort by final match score (highest first)
        scored_caregivers.sort(key=lambda x: x['final_match_score'], reverse=True)
        
        return scored_caregivers

    def get_booking_demand_surge_multiplier(self, patient_city: str, service_type: str) -> float:
        """Calculate surge pricing multiplier based on current demand (Uber-like)"""
        
        # Check recent bookings in the area (last 2 hours)
        recent_bookings = self.db.query(Booking).filter(
            and_(
                Booking.patient_city == patient_city,
                Booking.service_type == service_type,
                Booking.created_at >= datetime.utcnow() - timedelta(hours=2)
            )
        ).count()
        
        # Check available caregivers in the area
        available_caregivers = len(self.location_service.find_nearby_caregivers(
            # We would need patient coordinates here - this is simplified
            0, 0,  # Placeholder coordinates
            radius_km=15,
            service_type=service_type
        ))
        
        if available_caregivers == 0:
            return 2.5  # Maximum surge when no caregivers available
        
        # Calculate demand-to-supply ratio
        demand_ratio = recent_bookings / max(1, available_caregivers)
        
        # Surge multiplier based on demand ratio
        if demand_ratio > 4:
            return 2.5  # 2.5x surge (very high demand)
        elif demand_ratio > 3:
            return 2.0  # 2x surge
        elif demand_ratio > 2:
            return 1.5  # 1.5x surge
        elif demand_ratio > 1:
            return 1.25  # 1.25x surge
        else:
            return 1.0  # No surge

    def _check_visit_code(self, booking: Booking, code: Optional[str]) -> None:
        """Verify the visit code for `start`, under the booking row lock the caller holds.

        A wrong guess is counted and saved *before* raising (the caller rolls back on errors, which
        would otherwise throw the counter away). Three wrong guesses lock the code for
        VISIT_CODE_LOCK_MINUTES; while locked even the right code is refused and nothing is counted.
        """
        now = datetime.utcnow()
        if booking.visit_code_enc is None:
            # accepted before codes existed: fail closed rather than skip the check
            raise BookingStateConflict("This booking has no visit code. Contact support")
        locked_until = booking.visit_code_locked_until
        if locked_until is not None and locked_until > now:
            raise self._locked_error(locked_until, now)
        if not code:
            raise BookingVisitCodeError("A visit code is required to start the visit", status_code=400)
        try:
            expected = decrypt(booking.visit_code_enc)
        except InvalidToken:
            logger.error("Booking %s: visit code cannot be decrypted (encryption key rotated away?)", booking.id)
            raise BookingStateConflict("This booking has no visit code. Contact support")
        if hmac.compare_digest(expected.encode(), code.encode()):
            return

        attempts = (booking.visit_code_failed_attempts or 0) + 1
        if attempts >= app_settings.VISIT_CODE_MAX_ATTEMPTS:
            booking.visit_code_failed_attempts = 0
            booking.visit_code_locked_until = now + timedelta(minutes=app_settings.VISIT_CODE_LOCK_MINUTES)
            error: BookingVisitCodeError = self._locked_error(booking.visit_code_locked_until, now)
        else:
            booking.visit_code_failed_attempts = attempts
            left = app_settings.VISIT_CODE_MAX_ATTEMPTS - attempts
            error = BookingVisitCodeError(
                f"Incorrect visit code. {left} attempt{'s' if left != 1 else ''} left before the code is locked",
                status_code=400,
            )
        self.db.commit()
        logger.warning("Booking %s: wrong visit code (attempt %d)", booking.id, attempts)
        raise error

    @staticmethod
    def _locked_error(locked_until: datetime, now: datetime) -> "BookingVisitCodeError":
        seconds = max(1, math.ceil((locked_until - now).total_seconds()))
        minutes = max(1, math.ceil(seconds / 60))
        return BookingVisitCodeError(
            f"Too many incorrect attempts. The visit code is locked for {minutes} minute{'s' if minutes != 1 else ''}",
            status_code=423, retry_after=seconds,
        )

    def _remember_on_my_way(self, booking: Booking, caregiver_id: UUID) -> None:
        """When the caregiver tapped, and how far from the patient they were, so a late cancellation can later tell
        whether they really set out (app/services/onmyway.py). No fix on record: no distance, so no compensation."""
        booking.on_my_way_at = datetime.utcnow()
        booking.on_my_way_distance_m = None
        fix = self.location_service.get_caregiver_location(str(caregiver_id))
        # A fix from long ago says nothing about where they are now; pings are client-reported, so this is best-effort.
        fresh = fix is not None and getattr(fix, "updated_at", None) is not None and (
            booking.on_my_way_at - fix.updated_at) <= timedelta(minutes=LOCATION_FRESH_MINUTES)
        if fresh and booking.patient_latitude is not None and booking.patient_longitude is not None:
            booking.on_my_way_distance_m = int(round(onmyway_distance_m(
                fix.latitude, fix.longitude, booking.patient_latitude, booking.patient_longitude)))

    def _after_caregiver_cancel(self, booking: Booking) -> None:
        """WA-FR-37, for a visit that had already *started* (a drop-out before that never gets here from WhatsApp: it is
        reopened for a replacement, see app/services/dropout.py). A paid booking is flagged for a refund and queued for
        approval: a WhatsApp one with the amount left to a person when the visit had started, an app one in full (RF-D8, it
        used to be left unrefunded). The WhatsApp booker is told in the same transaction."""
        paid = booking.payment_status == PaymentStatus.PAID
        if booking.channel != "whatsapp":
            if paid:
                flag_refund(booking, "caregiver_cancelled", int(booking.total_amount_kobo))
            return
        if paid:
            started = booking.actual_start_time is not None
            flag_refund(booking, "caregiver_cancelled", None if started else int(booking.total_amount_kobo))  # kept for safety
        NotificationService(self.db).cancelled_by_caregiver(booking)

    def apply_caregiver_dropout(self, booking: Booking, caregiver_id: UUID, offers: List[Dict[str, Any]]) -> None:
        """The caregiver is out of this booking: reopen it for a replacement (paid WhatsApp booking, before the visit
        started) or cancel it as it always was. No time-window rule here: the caregiver-facing cancel enforces that
        before calling this, while an internal or admin-initiated drop-out (and a late one, which goes to the family's
        choice) comes straight here. Caller holds the booking lock and owns the transaction."""
        from app.services import dropout
        before_visit = booking.status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE)
        reopened = before_visit and dropout.on_caregiver_cancel(
            self.db, booking, caregiver_id, booking.status, datetime.utcnow(), offers)
        if not reopened:
            booking.status = BookingStatus.CANCELLED
            booking.cancel_reason = "caregiver_cancelled"
            self._after_caregiver_cancel(booking)

    def handle_booking_action(self, booking_id: UUID, caregiver_id: UUID,
                            action_data: BookingActionRequest) -> Booking:
        """Handle a caregiver action on a booking.

        Raises BookingActionForbidden (403) when this caregiver may not act on the booking,
        BookingStateConflict (409) when the booking is not in a state that allows the action,
        ValueError (404) when the booking does not exist.
        """
        action = action_data.action

        # Row lock: concurrent accepts serialise here, so exactly one sees PENDING.
        # populate_existing makes sure we read the locked row, not a stale identity-map copy.
        booking = (
            self.db.query(Booking)
            .filter(Booking.id == booking_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not booking:
            raise ValueError("Booking not found")

        offers: List[Dict[str, Any]] = []  # job-offer pushes owed by a drop-out re-dispatch: sent after the commit
        try:
            notification = self.db.query(BookingNotification).filter(
                and_(
                    BookingNotification.booking_id == booking_id,
                    BookingNotification.caregiver_id == caregiver_id
                )
            ).first()
            is_assigned = booking.caregiver_id == caregiver_id

            if action == "accept":
                # FOR SHARE, taken after the booking lock (suspend never locks bookings, so no cycle):
                # it waits for an in-flight suspend and blocks a new one until this commits, so an
                # accepted booking always appears in suspend's affected_bookings.
                caregiver_row = (
                    self.db.query(Caregiver)
                    .filter(Caregiver.id == caregiver_id)
                    .with_for_update(read=True)
                    .populate_existing()
                    .first()
                )
                if caregiver_row is None or caregiver_row.profile_status != "approved":
                    # e.g. suspended after the offer was sent
                    raise BookingActionForbidden("Your account is not approved to accept bookings")
                if notification is None:
                    raise BookingActionForbidden("You were not offered this booking")
                from app.services import dropout  # local: dropout -> dispatch -> this module
                if dropout.has_dropped(self.db, booking.id, caregiver_id):
                    raise BookingActionForbidden("You cancelled this booking")
                if booking.status != BookingStatus.PENDING or booking.caregiver_id is not None:
                    raise BookingStateConflict("Booking is no longer available")
                if booking.payment_status != PaymentStatus.PAID:
                    raise BookingStateConflict("Booking has not been paid for")

                booking.caregiver_id = caregiver_id
                booking.status = BookingStatus.ACCEPTED
                # Allowed from any dispatch state: an offer from an earlier wave stays valid, and so
                # does one from a booking already escalated to 'unmatched'.
                booking.dispatch_state = "matched"
                dropout.clear_on_assignment(booking)  # a replacement was found: no drop-out is in progress any more
                booking.visit_code_enc = encrypt(f"{secrets.randbelow(10_000):04d}")
                booking.visit_code_failed_attempts = 0
                booking.visit_code_locked_until = None
                NotificationService(self.db).caregiver_accepted(booking)  # same transaction: who is coming + the code
                notification.response = "accept"
                notification.response_at = datetime.utcnow()

                # Mark other notifications as declined
                other_notifications = self.db.query(BookingNotification).filter(
                    and_(
                        BookingNotification.booking_id == booking_id,
                        BookingNotification.caregiver_id != caregiver_id
                    )
                ).all()
                for notif in other_notifications:
                    notif.response = "auto_declined"
                    notif.response_at = datetime.utcnow()

            elif action == "decline":
                if notification is None:
                    raise BookingActionForbidden("You were not offered this booking")
                notification.response = "declined"
                notification.response_at = datetime.utcnow()

            else:  # start / complete / cancel: assigned caregiver only
                if not is_assigned:
                    raise BookingActionForbidden("This booking is not assigned to you")
                if action in ("on_my_way", "start"):
                    # A suspended caregiver may still finish a visit already under way (they're with
                    # the patient), but must not set out for or begin a new one.
                    status_row = (
                        self.db.query(Caregiver.profile_status)
                        .filter(Caregiver.id == caregiver_id)
                        .with_for_update(read=True)
                        .scalar()
                    )
                    if status_row != "approved":
                        raise BookingActionForbidden("Your account is not approved to start visits")

                if action == "on_my_way":
                    if booking.status != BookingStatus.ACCEPTED:
                        raise BookingStateConflict("Only an accepted booking can be marked on the way")
                    from app.services import booking_policy
                    window = booking_policy.current(self.db).onmyway_compensation_window_minutes
                    if datetime.utcnow() < booking.scheduled_date - timedelta(minutes=window):
                        raise BookingStateConflict(
                            f"You can mark yourself on the way from {_minutes_words(window)} before the visit")
                    booking.status = BookingStatus.EN_ROUTE
                    self._remember_on_my_way(booking, caregiver_id)
                    NotificationService(self.db).caregiver_on_the_way(booking)
                elif action == "start":
                    # on_my_way is optional: a caregiver who never pressed it can still start.
                    if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE):
                        raise BookingStateConflict("Only an accepted booking can be started")
                    self._check_visit_code(booking, action_data.visit_code)
                    booking.status = BookingStatus.IN_PROGRESS
                    booking.actual_start_time = datetime.utcnow()
                    booking.visit_code_failed_attempts = 0
                    booking.visit_code_locked_until = None
                    NotificationService(self.db).visit_started(booking)
                elif action == "complete":
                    if booking.status != BookingStatus.IN_PROGRESS:
                        raise BookingStateConflict("Only a booking in progress can be completed")
                    booking.status = BookingStatus.COMPLETED
                    booking.actual_end_time = datetime.utcnow()
                    earnings.credit_visit(self.db, booking, booking.actual_end_time)  # same transaction, under the lock
                    NotificationService(self.db).visit_completed(booking)
                elif action == "cancel":
                    if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS):
                        raise BookingStateConflict("This booking can no longer be cancelled")
                    from app.services import booking_policy
                    if booking.channel == "whatsapp":
                        # The family is relying on this visit: the app lets a caregiver drop it only while it is still
                        # ACCEPTED and further out than the re-dispatch lead (same whole-minute floor and naive-UTC
                        # clock as dropout.on_caregiver_cancel, so "allowed" always means "a replacement is searched
                        # for"). Later, or once under way, they contact support, who reassign or cancel from the admin.
                        lead = booking_policy.current(self.db).dropout_redispatch_min_lead_minutes
                        minutes = int((booking.scheduled_date - datetime.utcnow()).total_seconds() // 60)
                        if booking.status != BookingStatus.ACCEPTED or minutes <= lead:
                            raise BookingStateConflict(
                                "It's too close to the visit to cancel in the app. Please contact support.")
                    self.apply_caregiver_dropout(booking, caregiver_id, offers)

            self.db.commit()
        except Exception:
            self.db.rollback()  # also releases the row lock
            raise

        if offers:
            from app.services import dispatch  # local: dispatch imports this module
            dispatch.push_offers(self.db, offers)  # after the commit: never an HTTP call under the lock

        self.db.refresh(booking)
        return booking

    def get_caregiver_bookings(self, caregiver_id: UUID, status: str = None) -> List[Booking]:
        """Get bookings for a caregiver"""
        query = self.db.query(Booking).filter(Booking.caregiver_id == caregiver_id)
        
        if status == "new_requests":
            # Get bookings where caregiver has pending notifications
            notifications = self.db.query(BookingNotification).filter(
                and_(
                    BookingNotification.caregiver_id == caregiver_id,
                    BookingNotification.response.is_(None)
                )
            ).all()
            booking_ids = [n.booking_id for n in notifications]
            query = self.db.query(Booking).filter(Booking.id.in_(booking_ids))
        
        elif status:
            query = query.filter(Booking.status == status)
        
        return query.order_by(Booking.created_at.desc()).all()

    def get_booking_notifications(self, caregiver_id: UUID, unread_only: bool = False) -> List[BookingNotification]:
        """Get notifications for a caregiver"""
        query = self.db.query(BookingNotification).filter(
            BookingNotification.caregiver_id == caregiver_id
        )
        
        if unread_only:
            query = query.filter(BookingNotification.is_read == False)
        
        return query.order_by(BookingNotification.created_at.desc()).all()

    def send_chat_message(self, booking_id: UUID, sender_id: UUID, message: str) -> ChatMessage:
        """Send a chat message"""
        chat_message = ChatMessage(
            booking_id=booking_id,
            sender_id=sender_id,
            message=message
        )
        
        self.db.add(chat_message)
        self.db.commit()
        self.db.refresh(chat_message)
        return chat_message

    def get_chat_messages(self, booking_id: UUID) -> List[ChatMessage]:
        """Get chat messages for a booking"""
        return self.db.query(ChatMessage).filter(
            ChatMessage.booking_id == booking_id
        ).order_by(ChatMessage.created_at.asc()).all()

    async def create_booking_note(self, booking_id: UUID, caregiver_id: UUID, 
                                title: str, content: str = None, 
                                audio_transcript: str = None) -> BookingNote:
        """Create a booking note with AI analysis"""
        
        # Get booking details for context
        booking = self.db.query(Booking).filter(Booking.id == booking_id).first()
        
        note = BookingNote(
            booking_id=booking_id,
            caregiver_id=caregiver_id,
            title=title,
            content=content,
            audio_transcript=audio_transcript
        )
        
        # If we have audio transcript, analyze it with AI
        if audio_transcript and booking:
            patient_info = {
                'service_type': booking.service_type,
                'assistance_type': booking.assistance_type,
                'condition': booking.condition,
                'special_instructions': booking.special_instructions
            }
            
            ai_analysis = await gemini_service.transcribe_and_analyze_audio(
                audio_transcript, patient_info
            )
            
            note.ai_summary = ai_analysis.get('summary')
            note.ai_keywords = ai_analysis.get('keywords')
            note.ai_recommendations = '\n'.join(ai_analysis.get('recommendations', []))
        
        self.db.add(note)
        self.db.commit()
        self.db.refresh(note)
        return note

    def get_booking_notes(self, booking_id: UUID) -> List[BookingNote]:
        """Get notes for a booking"""
        return self.db.query(BookingNote).filter(
            BookingNote.booking_id == booking_id
        ).order_by(BookingNote.created_at.desc()).all()
