import secrets
import string
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from uuid import UUID
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
from app.services.location import LocationService
from app.services.gemini import gemini_service
from app.services.paystack import PaystackService
from app.services.pricing import compute_price
from app.core.config import settings
from app.core.money import kobo_to_naira


def _naira(kobo: int) -> float:
    """Kobo -> naira for the (float-typed) response fields. Display only."""
    return float(kobo_to_naira(kobo))


class BookingActionForbidden(Exception):
    """The caregiver is not allowed to perform this action on this booking (HTTP 403)."""


class BookingStateConflict(Exception):
    """The booking is not in a state that allows this action (HTTP 409)."""


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
        settings = self.db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first()
        if not settings:
            raise Exception("Booking settings not configured.")
        
        # Price in kobo (single source of truth shared with create_booking)
        multiplier = 1.0
        if settings.multipliers and preview_data.service_type in settings.multipliers:
            multiplier = settings.multipliers[preview_data.service_type]
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
                radius_km=20,
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
        
        # Calculate end time
        scheduled_end = booking_data.scheduled_date + timedelta(hours=booking_data.duration_hours)
        
        # Fetch latest booking settings
        settings = self.db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first()
        if not settings:
            raise Exception("Booking settings not configured.")
        
        # Price in kobo. What we store is exactly what we send to Paystack and what settlement
        # later compares against.
        multiplier = 1.0
        if settings.multipliers and booking_data.service_type in settings.multipliers:
            multiplier = settings.multipliers[booking_data.service_type]
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
            duration_hours=booking_data.duration_hours,
            scheduled_date=booking_data.scheduled_date,
            scheduled_end_date=scheduled_end,
            patient_address=booking_data.patient_address,
            patient_latitude=booking_data.patient_latitude,
            patient_longitude=booking_data.patient_longitude,
            patient_city=booking_data.patient_city,
            patient_state=booking_data.patient_state,
            special_instructions=booking_data.special_instructions,
            booking_for=booking_data.booking_for,
            total_amount_kobo=price.total_kobo,
            service_fee_kobo=price.service_fee_kobo,
            platform_fee_kobo=price.platform_fee_kobo,
            paystack_reference=paystack_reference
        )
        
        self.db.add(booking)
        self.db.commit()
        self.db.refresh(booking)
        
        # Get patient email for payment
        patient = self.db.query(Patient).filter(Patient.id == patient_id).first()
        if not patient or not patient.email:
            raise Exception("Patient email not found for payment processing.")
        
        # Initialize Paystack payment
        payment_response = self.paystack_service.initialize_transaction(
            email=patient.email,
            amount_kobo=booking.total_amount_kobo,
            reference=paystack_reference,
            callback_url=f"{settings.FRONTEND_URL}/bookings/{booking.id}/payment-complete" if hasattr(settings, 'FRONTEND_URL') else None,
            metadata={
                "booking_id": str(booking.id),
                "patient_id": str(patient_id),
                "service_type": booking_data.service_type,
                "booking_code": booking_code
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

    def notify_nearby_caregivers(self, booking: Booking) -> List[BookingNotification]:
        """Send notifications to nearby caregivers using smart matching algorithm"""
        
        # Use enhanced smart matching to find best caregivers
        smart_matched_caregivers = self.smart_caregiver_matching(booking)
        
        notifications = []
        
        # Notify top 8 best-matched caregivers (reduced from 10 for better matching)
        for caregiver in smart_matched_caregivers[:8]:
            notification = BookingNotification(
                booking_id=booking.id,
                caregiver_id=caregiver['caregiver_id'],
                title="New Booking Request",
                message=f"New {booking.service_type} request - {caregiver['distance_km']}km away, ETA {caregiver.get('estimated_arrival_minutes', 'N/A')} min",
                notification_type="new_booking"
            )
            
            self.db.add(notification)
            notifications.append(notification)
        
        self.db.commit()
        return notifications

    def smart_caregiver_matching(self, booking: Booking) -> List[Dict[str, Any]]:
        """Uber-like smart matching algorithm considering multiple factors"""
        
        # Get enhanced caregivers with ETA and scoring
        base_caregivers = self.location_service.get_nearby_active_caregivers_with_eta(
            booking.patient_latitude, booking.patient_longitude,
            radius_km=20,  # Increased radius for better matching
            service_type=booking.service_type
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
            specialization = caregiver.get('specialization', '').lower()
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
                if booking.status != BookingStatus.PENDING or booking.caregiver_id is not None:
                    raise BookingStateConflict("Booking is no longer available")
                if booking.payment_status != PaymentStatus.PAID:
                    raise BookingStateConflict("Booking has not been paid for")

                booking.caregiver_id = caregiver_id
                booking.status = BookingStatus.ACCEPTED
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

                if action == "start":
                    if booking.status != BookingStatus.ACCEPTED:
                        raise BookingStateConflict("Only an accepted booking can be started")
                    booking.status = BookingStatus.IN_PROGRESS
                    booking.actual_start_time = datetime.utcnow()
                elif action == "complete":
                    if booking.status != BookingStatus.IN_PROGRESS:
                        raise BookingStateConflict("Only a booking in progress can be completed")
                    booking.status = BookingStatus.COMPLETED
                    booking.actual_end_time = datetime.utcnow()
                elif action == "cancel":
                    if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.IN_PROGRESS):
                        raise BookingStateConflict("This booking can no longer be cancelled")
                    booking.status = BookingStatus.CANCELLED

            self.db.commit()
        except Exception:
            self.db.rollback()  # also releases the row lock
            raise

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

    def auto_cancel_unpaid_bookings(self):
        """Auto-cancel unpaid bookings older than 7 days"""
        threshold = datetime.utcnow() - timedelta(days=7)
        unpaid_bookings = self.db.query(Booking).filter(
            Booking.payment_status == PaymentStatus.PENDING,
            Booking.status == BookingStatus.PENDING,
            Booking.created_at < threshold
        ).all()
        for booking in unpaid_bookings:
            booking.status = BookingStatus.CANCELLED
        self.db.commit()
        return len(unpaid_bookings)
