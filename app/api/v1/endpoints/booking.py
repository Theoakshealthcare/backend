from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, get_current_caregiver, get_current_patient
from app.db.models.booking import Booking, CaregiverLocation
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.user import User
from app.schemas.booking import (
    BookingCreate, BookingResponse, BookingActionRequest,
    CaregiverLocationUpdate, CaregiverLocationResponse,
    ChatMessageCreate, ChatMessageResponse,
    BookingNoteCreate, BookingNoteResponse,
    BookingNotificationResponse,
    LiveTrackingResponse
)
from app.services.booking import BookingService
from app.services.location import LocationService

router = APIRouter()


def get_booking_service(db: Session = Depends(get_db)) -> BookingService:
    return BookingService(db)


def get_location_service(db: Session = Depends(get_db)) -> LocationService:
    return LocationService(db)


# Patient Endpoints
@router.post(
    "/create", 
    response_model=BookingResponse,
    summary="Create a new booking",
    description="Create a new healthcare service booking. This endpoint allows patients to book various healthcare services including nurses, doctors, physiotherapists, caregivers, and nutritionists.",
    responses={
        200: {
            "description": "Booking created successfully",
            "model": BookingResponse
        },
        404: {
            "description": "Patient profile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Patient profile not found"}
                }
            }
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {"detail": "Error creating booking"}
                }
            }
        }
    },
    tags=["Patient Bookings"]
)
async def create_booking(
    booking_data: BookingCreate,
    current_user: User = Depends(get_current_patient),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Create a new healthcare service booking.
    
    **Required Fields:**
    - **service_type**: Type of healthcare service (nurse, doctor, physiotherapist, caregiver, nutritionist)
    - **assistance_type**: Specific type of assistance needed
    - **duration_hours**: Duration of service (1-24 hours)
    - **scheduled_date**: When the service should be provided (ISO 8601 format)
    - **patient_address**: Full address where service will be provided
    - **patient_latitude**: Latitude coordinate of service location
    - **patient_longitude**: Longitude coordinate of service location
    - **patient_city**: City where service will be provided
    - **patient_state**: State where service will be provided
    - **booking_for**: Who the booking is for
    
    **Optional Fields:**
    - **condition**: Medical condition or specific needs
    - **special_instructions**: Additional instructions for the caregiver
    
    **Process:**
    1. Validates patient profile exists
    2. Creates booking with calculated pricing
    3. Notifies nearby available caregivers
    4. Returns booking details with unique booking code
    
    **Authentication:** Requires patient authentication token.
    """
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


@router.get(
    "/patient/bookings", 
    response_model=List[BookingResponse],
    summary="Get patient's bookings",
    description="Retrieve all bookings for the authenticated patient with optional status filtering and detailed caregiver information.",
    responses={
        200: {
            "description": "List of patient bookings retrieved successfully",
            "model": List[BookingResponse]
        },
        404: {
            "description": "Patient profile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Patient profile not found"}
                }
            }
        }
    },
    tags=["Patient Bookings"]
)
def get_patient_bookings(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_patient),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get patient's bookings with detailed caregiver information.
    
    **Query Parameters:**
    - **status** (optional): Filter bookings by status
      - Available options: "pending", "accepted", "in_progress", "completed", "cancelled", "declined"
      - If not provided, returns all bookings
    
    **Response includes:**
    - Complete booking details
    - Caregiver information (name and specialization) when assigned
    - Payment and status information
    - Scheduling details
    
    **Authentication:** Requires patient authentication token.
    """
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
    
    # Enrich bookings with caregiver details including location
    enriched_bookings = []
    for booking in bookings:
        booking_dict = booking.__dict__.copy()
        
        # Add caregiver details if caregiver is assigned
        if booking.caregiver_id:
            caregiver = booking_service.db.query(Caregiver).filter(
                Caregiver.id == booking.caregiver_id
            ).first()
            
            # Get caregiver's current location
            caregiver_location = booking_service.db.query(CaregiverLocation).filter(
                CaregiverLocation.caregiver_id == booking.caregiver_id
            ).first()
            
            if caregiver:
                caregiver_details = {
                    'caregiver_id': caregiver.id,
                    'full_name': caregiver.full_name,
                    'specialization': caregiver.specialization,
                    'current_latitude': caregiver_location.latitude if caregiver_location else None,
                    'current_longitude': caregiver_location.longitude if caregiver_location else None,
                    'is_online': caregiver_location.is_active if caregiver_location else False,
                    'last_location_update': caregiver_location.updated_at if caregiver_location else None,
                    'distance_from_patient': None,
                    'estimated_arrival_time': None
                }
                
                # Calculate distance and ETA if both locations are available
                if (caregiver_location and booking.patient_latitude and booking.patient_longitude):
                    from app.services.location import LocationService
                    location_service = LocationService(booking_service.db)
                    
                    distance = location_service.calculate_distance(
                        booking.patient_latitude, booking.patient_longitude,
                        caregiver_location.latitude, caregiver_location.longitude
                    )
                    caregiver_details['distance_from_patient'] = round(distance, 2)
                    
                    # Calculate ETA (assuming 25 km/h average speed in urban areas)
                    # Minimum 5 minutes for very close distances
                    eta_minutes = max(5, int((distance / 25) * 60))
                    caregiver_details['estimated_arrival_time'] = eta_minutes
                
                booking_dict['caregiver_details'] = caregiver_details
        
        enriched_bookings.append(booking_dict)
    
    return enriched_bookings


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
@router.post(
    "/caregiver/location", 
    response_model=CaregiverLocationResponse,
    summary="Update caregiver's real-time location",
    description="Update caregiver's current GPS location for real-time tracking and matching. This endpoint should be called periodically by the caregiver mobile app.",
    responses={
        200: {
            "description": "Location updated successfully",
            "model": CaregiverLocationResponse
        },
        404: {
            "description": "Caregiver profile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Caregiver profile not found"}
                }
            }
        }
    },
    tags=["Caregiver Location"]
)
def update_caregiver_location(
    location_data: CaregiverLocationUpdate,
    current_user: User = Depends(get_current_caregiver),
    location_service: LocationService = Depends(get_location_service),
) -> Any:
    """
    Update caregiver's current GPS location for real-time tracking.
    
    **Required Data from Frontend:**
    - **latitude**: GPS latitude coordinate (-90 to 90)
    - **longitude**: GPS longitude coordinate (-180 to 180)
    - **is_available**: Whether caregiver is available for new bookings
    
    **Optional Data from Frontend:**
    - **accuracy**: GPS accuracy in meters (helps determine location reliability)
    - **heading**: Direction of movement in degrees (0-360, where 0 is North)
    - **speed**: Movement speed in meters per second (used for movement detection)
    
    **Update Frequency:**
    - **Active booking**: Every 10-15 seconds for real-time tracking
    - **Available but no booking**: Every 30-60 seconds for matching
    - **Offline**: Stop sending updates and set is_available to false
    
    **Frontend Implementation Notes:**
    - Use background location services for continuous tracking
    - Handle GPS permission requests gracefully
    - Implement exponential backoff for failed requests
    - Cache last known location for offline scenarios
    
    **Authentication:** Requires caregiver authentication token.
    """
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


@router.get(
    "/caregiver/location", 
    response_model=CaregiverLocationResponse,
    summary="Get caregiver's current location",
    description="Retrieve the current GPS location and status of the authenticated caregiver.",
    responses={
        200: {
            "description": "Current location retrieved successfully",
            "model": CaregiverLocationResponse
        },
        404: {
            "description": "Caregiver profile or location not found",
            "content": {
                "application/json": {
                    "examples": {
                        "no_profile": {"value": {"detail": "Caregiver profile not found"}},
                        "no_location": {"value": {"detail": "Location not found"}}
                    }
                }
            }
        }
    },
    tags=["Caregiver Location"]
)
def get_caregiver_location(
    current_user: User = Depends(get_current_caregiver),
    location_service: LocationService = Depends(get_location_service),
) -> Any:
    """
    Get caregiver's current GPS location and status.
    
    **Returns:**
    - Current GPS coordinates
    - Availability status
    - Location accuracy and timestamp
    - Additional GPS metadata (heading, speed if available)
    
    **Use Cases:**
    - Caregiver app checking own location status
    - Debugging location tracking issues
    - Verifying successful location updates
    
    **Authentication:** Requires caregiver authentication token.
    """
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


# Real-time Tracking Endpoints
@router.get(
    "/booking/{booking_id}/live-tracking",
    response_model=LiveTrackingResponse,
    summary="Get live caregiver tracking",
    description="Get real-time caregiver location and ETA for active booking",
    responses={
        200: {
            "description": "Live tracking data retrieved successfully",
            "model": LiveTrackingResponse
        },
        404: {
            "description": "Booking not found or no tracking data available",
            "content": {
                "application/json": {
                    "example": {"detail": "No tracking data available"}
                }
            }
        },
        403: {
            "description": "Access denied",
            "content": {
                "application/json": {
                    "example": {"detail": "Access denied"}
                }
            }
        }
    },
    tags=["Live Tracking"]
)
def get_live_booking_tracking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
    location_service: LocationService = Depends(get_location_service),
) -> Any:
    """
    Get real-time caregiver tracking for an active booking.
    
    **Features:**
    - Real-time caregiver location
    - Live distance calculation
    - Dynamic ETA updates
    - Movement detection
    - Booking status tracking
    
    **Access:** Both patients and caregivers can access their booking tracking data.
    
    **Authentication:** Requires user authentication token.
    """
    # Get booking and verify access
    booking = booking_service.db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    # Verify user has access to this booking
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
    
    # Check if booking has assigned caregiver
    if not booking.caregiver_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No caregiver assigned to this booking yet"
        )
    
    # Get caregiver and location data
    caregiver = booking_service.db.query(Caregiver).filter(
        Caregiver.id == booking.caregiver_id
    ).first()
    
    caregiver_location = booking_service.db.query(CaregiverLocation).filter(
        CaregiverLocation.caregiver_id == booking.caregiver_id
    ).first()
    
    if not caregiver_location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No tracking data available"
        )
    
    # Calculate distance and ETA
    distance = location_service.calculate_distance(
        booking.patient_latitude, booking.patient_longitude,
        caregiver_location.latitude, caregiver_location.longitude
    )
    
    # Calculate ETA (25 km/h average speed, minimum 5 minutes)
    eta_minutes = max(5, int((distance / 25) * 60))
    
    # Determine if caregiver is moving (speed > 0.5 m/s)
    is_moving = caregiver_location.speed and caregiver_location.speed > 0.5
    
    return LiveTrackingResponse(
        booking_id=booking.id,
        caregiver_id=caregiver.id,
        caregiver_name=caregiver.full_name,
        current_latitude=caregiver_location.latitude,
        current_longitude=caregiver_location.longitude,
        distance_km=round(distance, 2),
        estimated_arrival_minutes=eta_minutes,
        last_updated=caregiver_location.updated_at,
        is_moving=is_moving,
        booking_status=booking.status
    )


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
