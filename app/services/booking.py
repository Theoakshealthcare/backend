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
from app.schemas.booking import BookingCreate, BookingActionRequest
from app.services.location import LocationService
from app.services.gemini import gemini_service


class BookingService:
    def __init__(self, db: Session):
        self.db = db
        self.location_service = LocationService(db)

    def generate_booking_code(self) -> str:
        """Generate unique booking code"""
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = self.db.query(Booking).filter(Booking.booking_code == code).first()
            if not existing:
                return code

    def create_booking(self, patient_id: UUID, booking_data: BookingCreate) -> Booking:
        """Create a new booking"""
        
        # Calculate end time
        scheduled_end = booking_data.scheduled_date + timedelta(hours=booking_data.duration_hours)
        
        # Calculate pricing (this would be more complex in real implementation)
        service_fee = booking_data.duration_hours * 5000  # ₦5,000 per hour base rate
        platform_fee = service_fee * 0.1  # 10% platform fee
        total_amount = service_fee + platform_fee
        
        booking = Booking(
            booking_code=self.generate_booking_code(),
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
            total_amount=total_amount,
            service_fee=service_fee,
            platform_fee=platform_fee
        )
        
        self.db.add(booking)
        self.db.commit()
        self.db.refresh(booking)
        
        return booking

    def notify_nearby_caregivers(self, booking: Booking) -> List[BookingNotification]:
        """Send notifications to nearby caregivers"""
        
        # Find nearby caregivers
        nearby_caregivers = self.location_service.find_nearby_caregivers(
            booking.patient_latitude,
            booking.patient_longitude,
            radius_km=15,  # 15km radius
            service_type=booking.service_type
        )
        
        notifications = []
        
        for caregiver in nearby_caregivers[:10]:  # Limit to 10 nearest caregivers
            notification = BookingNotification(
                booking_id=booking.id,
                caregiver_id=caregiver['caregiver_id'],
                title="New Booking Request",
                message=f"New {booking.service_type} request for {booking.assistance_type} - {caregiver['distance_km']}km away",
                notification_type="new_booking"
            )
            
            self.db.add(notification)
            notifications.append(notification)
        
        self.db.commit()
        return notifications

    def handle_booking_action(self, booking_id: UUID, caregiver_id: UUID, 
                            action_data: BookingActionRequest) -> Booking:
        """Handle caregiver actions on booking"""
        
        booking = self.db.query(Booking).filter(Booking.id == booking_id).first()
        if not booking:
            raise ValueError("Booking not found")
        
        # Update notification response
        notification = self.db.query(BookingNotification).filter(
            and_(
                BookingNotification.booking_id == booking_id,
                BookingNotification.caregiver_id == caregiver_id
            )
        ).first()
        
        if notification:
            notification.response = action_data.action
            notification.response_at = datetime.utcnow()
        
        # Handle different actions
        if action_data.action == "accept":
            if booking.status == BookingStatus.PENDING:
                booking.caregiver_id = caregiver_id
                booking.status = BookingStatus.ACCEPTED
                
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
        
        elif action_data.action == "decline":
            if notification:
                notification.response = "declined"
        
        elif action_data.action == "start":
            if booking.caregiver_id == caregiver_id and booking.status == BookingStatus.ACCEPTED:
                booking.status = BookingStatus.IN_PROGRESS
                booking.actual_start_time = datetime.utcnow()
        
        elif action_data.action == "complete":
            if booking.caregiver_id == caregiver_id and booking.status == BookingStatus.IN_PROGRESS:
                booking.status = BookingStatus.COMPLETED
                booking.actual_end_time = datetime.utcnow()
        
        elif action_data.action == "cancel":
            booking.status = BookingStatus.CANCELLED
        
        self.db.commit()
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
