from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, get_current_caregiver, get_current_patient
from app.db.models.booking import Booking
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.user import User
from app.schemas.booking import (
    BookingCreate, BookingResponse, BookingActionRequest,
    CaregiverLocationUpdate, CaregiverLocationResponse,
    ChatMessageCreate, ChatMessageResponse,
    BookingNoteCreate, BookingNoteResponse,
    BookingNotificationResponse, NearbyCaregiverResponse
)
from app.services.booking import BookingService
from app.services.location import LocationService

router = APIRouter()


def get_booking_service(db: Session = Depends(get_db)) -> BookingService:
    return BookingService(db)


def get_location_service(db: Session = Depends(get_db)) -> LocationService:
    return LocationService(db)


# Patient Endpoints
@router.post("/create", response_model=BookingResponse)
async def create_booking(
    booking_data: BookingCreate,
    current_user: User = Depends(get_current_patient),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Create a new booking (Patient)"""
    try:
        # Get patient record
        patient = booking_service.db.query(Patient).filter(
            Patient.user_id == current_user.id
        ).first()
        
        if not patient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient profile not found"
            )
        
        # Create booking
        booking = booking_service.create_booking(patient.id, booking_data)
        
        # Notify nearby caregivers
        notifications = booking_service.notify_nearby_caregivers(booking)
        
        return booking
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("/patient/bookings", response_model=List[BookingResponse])
def get_patient_bookings(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_patient),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Get patient's bookings"""
    patient = booking_service.db.query(Patient).filter(
        Patient.user_id == current_user.id
    ).first()
    
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found"
        )
    
    query = booking_service.db.query(Booking).filter(Booking.patient_id == patient.id)
    
    if status:
        query = query.filter(Booking.status == status)
    
    bookings = query.order_by(Booking.created_at.desc()).all()
    return bookings


# Caregiver Endpoints
@router.get("/caregiver/bookings", response_model=List[BookingResponse])
def get_caregiver_bookings(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Get caregiver's bookings"""
    caregiver = booking_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    bookings = booking_service.get_caregiver_bookings(caregiver.id, status)
    return bookings


@router.get("/caregiver/notifications", response_model=List[BookingNotificationResponse])
def get_caregiver_notifications(
    unread_only: bool = False,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Get caregiver's booking notifications"""
    caregiver = booking_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    notifications = booking_service.get_booking_notifications(caregiver.id, unread_only)
    return notifications


@router.post("/caregiver/action/{booking_id}")
async def handle_booking_action(
    booking_id: UUID,
    action_data: BookingActionRequest,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Handle caregiver action on booking (accept, decline, start, complete)"""
    caregiver = booking_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    try:
        booking = booking_service.handle_booking_action(
            booking_id, caregiver.id, action_data
        )
        return {"message": f"Booking {action_data.action} successfully", "booking": booking}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/booking/{booking_id}", response_model=BookingResponse)
def get_booking_details(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Get booking details"""
    booking = booking_service.db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    # Check if user has access to this booking
    if current_user.user_type == "patient":
        patient = booking_service.db.query(Patient).filter(
            Patient.user_id == current_user.id
        ).first()
        if not patient or booking.patient_id != patient.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )
    elif current_user.user_type == "caregiver":
        caregiver = booking_service.db.query(Caregiver).filter(
            Caregiver.user_id == current_user.id
        ).first()
        if not caregiver or booking.caregiver_id != caregiver.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )
    
    return booking


# Location Endpoints
@router.post("/caregiver/location", response_model=CaregiverLocationResponse)
def update_caregiver_location(
    location_data: CaregiverLocationUpdate,
    current_user: User = Depends(get_current_caregiver),
    location_service: LocationService = Depends(get_location_service),
) -> Any:
    """Update caregiver's current location"""
    caregiver = location_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    location = location_service.update_caregiver_location(
        caregiver.id,
        location_data.latitude,
        location_data.longitude,
        accuracy=location_data.accuracy,
        heading=location_data.heading,
        speed=location_data.speed,
        is_available=location_data.is_available
    )
    
    return location


@router.get("/caregiver/location", response_model=CaregiverLocationResponse)
def get_caregiver_location(
    current_user: User = Depends(get_current_caregiver),
    location_service: LocationService = Depends(get_location_service),
) -> Any:
    """Get caregiver's current location"""
    caregiver = location_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    location = location_service.get_caregiver_location(caregiver.id)
    
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )
    
    return location


# Chat Endpoints
@router.post("/chat/{booking_id}/send", response_model=ChatMessageResponse)
def send_chat_message(
    booking_id: UUID,
    message_data: ChatMessageCreate,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Send a chat message"""
    # Verify user has access to this booking
    booking = booking_service.db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    # Check access permissions
    has_access = False
    if current_user.user_type == "patient":
        patient = booking_service.db.query(Patient).filter(
            Patient.user_id == current_user.id
        ).first()
        has_access = patient and booking.patient_id == patient.id
    elif current_user.user_type == "caregiver":
        caregiver = booking_service.db.query(Caregiver).filter(
            Caregiver.user_id == current_user.id
        ).first()
        has_access = caregiver and booking.caregiver_id == caregiver.id
    
    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    message = booking_service.send_chat_message(
        booking_id, current_user.id, message_data.message
    )
    
    return message


@router.get("/chat/{booking_id}/messages", response_model=List[ChatMessageResponse])
def get_chat_messages(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Get chat messages for a booking"""
    # Verify access (same logic as send_chat_message)
    booking = booking_service.db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    # Check access permissions
    has_access = False
    if current_user.user_type == "patient":
        patient = booking_service.db.query(Patient).filter(
            Patient.user_id == current_user.id
        ).first()
        has_access = patient and booking.patient_id == patient.id
    elif current_user.user_type == "caregiver":
        caregiver = booking_service.db.query(Caregiver).filter(
            Caregiver.user_id == current_user.id
        ).first()
        has_access = caregiver and booking.caregiver_id == caregiver.id
    
    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    messages = booking_service.get_chat_messages(booking_id)
    return messages


# Notes Endpoints
@router.post("/notes/{booking_id}/create", response_model=BookingNoteResponse)
async def create_booking_note(
    booking_id: UUID,
    note_data: BookingNoteCreate,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Create a booking note with AI analysis"""
    caregiver = booking_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    # Verify caregiver has access to this booking
    booking = booking_service.db.query(Booking).filter(
        and_(Booking.id == booking_id, Booking.caregiver_id == caregiver.id)
    ).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found or access denied"
        )
    
    note = await booking_service.create_booking_note(
        booking_id,
        caregiver.id,
        note_data.title,
        note_data.content,
        note_data.audio_transcript
    )
    
    return note


@router.get("/notes/{booking_id}", response_model=List[BookingNoteResponse])
def get_booking_notes(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """Get notes for a booking"""
    # Verify access to booking
    booking = booking_service.db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    # Check access permissions
    has_access = False
    if current_user.user_type == "patient":
        patient = booking_service.db.query(Patient).filter(
            Patient.user_id == current_user.id
        ).first()
        has_access = patient and booking.patient_id == patient.id
    elif current_user.user_type == "caregiver":
        caregiver = booking_service.db.query(Caregiver).filter(
            Caregiver.user_id == current_user.id
        ).first()
        has_access = caregiver and booking.caregiver_id == caregiver.id
    
    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    notes = booking_service.get_booking_notes(booking_id)
    return notes
