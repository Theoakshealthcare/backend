import logging
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, get_current_caregiver, get_current_patient, get_current_admin, user_is_admin, authenticate_websocket
from app.core.money import kobo_to_naira
from app.db.models.booking import Booking, BookingStatus, PaymentStatus, CaregiverLocation
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.user import User
from app.schemas.booking import (
    BookingCreate, BookingResponse, BookingActionRequest,
    BookingPreviewRequest, BookingPreviewResponse,
    CaregiverLocationUpdate, CaregiverLocationResponse,
    ChatMessageCreate, ChatMessageResponse,
    BookingNoteCreate, BookingNoteResponse,
    BookingNotificationResponse,
    LiveTrackingResponse
)
from app.services.booking import BookingActionForbidden, BookingService, BookingStateConflict
from app.services.booking_payment import SettleOutcome, settle_booking_payment
from app.services.location import LocationService
from app.services.websocket_manager import websocket_manager
from app.services.paystack import PaystackService

log = logging.getLogger(__name__)

router = APIRouter()


def get_booking_service(db: Session = Depends(get_db)) -> BookingService:
    return BookingService(db)


def get_location_service(db: Session = Depends(get_db)) -> LocationService:
    return LocationService(db)


def enrich_booking_with_details(booking: Booking, db: Session) -> dict:
    """Helper function to enrich booking with patient and caregiver details"""
    booking_dict = booking.__dict__.copy()
    booking_dict.pop("_sa_instance_state", None)
    # Money is stored as kobo; responses keep the naira field names mobile clients already use.
    booking_dict["total_amount"] = booking.total_amount
    booking_dict["service_fee"] = booking.service_fee
    booking_dict["platform_fee"] = booking.platform_fee

    # Add patient details
    patient = db.query(Patient).filter(Patient.id == booking.patient_id).first()
    if patient:
        patient_details = {
            'patient_id': patient.id,
            'full_name': patient.full_name,
            'phone_number': patient.phone_number,
            'profile_image_url': patient.user.profile_image_url if patient.user else None,
            'emergency_contact_name': patient.emergency_contact_name,
            'emergency_contact_phone': patient.emergency_contact_phone
        }
        booking_dict['patient_details'] = patient_details
    
    # Add caregiver details if caregiver is assigned
    if booking.caregiver_id:
        caregiver = db.query(Caregiver).filter(
            Caregiver.id == booking.caregiver_id
        ).first()
        
        # Get caregiver's current location
        caregiver_location = db.query(CaregiverLocation).filter(
            CaregiverLocation.caregiver_id == booking.caregiver_id
        ).first()
        
        if caregiver:
            caregiver_details = {
                'caregiver_id': caregiver.id,
                'full_name': caregiver.full_name,
                'specialization': caregiver.specialization,
                'profile_image_url': caregiver.user.profile_image_url if caregiver.user else None,
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
                location_service = LocationService(db)
                
                distance = location_service.calculate_distance(
                    booking.patient_latitude, booking.patient_longitude,
                    caregiver_location.latitude, caregiver_location.longitude
                )
                caregiver_details['distance_from_patient'] = round(distance, 2)
                
                # Calculate ETA (assuming 25 km/h average speed in urban areas)
                eta_minutes = max(5, int((distance / 25) * 60))
                caregiver_details['estimated_arrival_time'] = eta_minutes
            
            booking_dict['caregiver_details'] = caregiver_details
    
    return booking_dict


def ensure_booking_read_access(db: Session, user: User, booking: Booking) -> None:
    """Allow the booking's patient, its assigned caregiver, or an admin; refuse everyone else."""
    if user_is_admin(db, user):
        return
    if user.user_type == "patient":
        patient = db.query(Patient).filter(Patient.user_id == user.id).first()
        if patient and booking.patient_id == patient.id:
            return
    elif user.user_type == "caregiver":
        caregiver = db.query(Caregiver).filter(Caregiver.user_id == user.id).first()
        if caregiver and booking.caregiver_id == caregiver.id:
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


# Patient Endpoints
@router.post(
    "/preview",
    response_model=BookingPreviewResponse,
    summary="Get booking price preview",
    description="Calculate booking pricing and availability without creating a booking. Use this to show pricing transparency to patients before they commit to booking.",
    responses={
        200: {
            "description": "Booking preview generated successfully",
            "model": BookingPreviewResponse
        },
        400: {
            "description": "Invalid input data",
            "content": {
                "application/json": {
                    "example": {"detail": "Duration must be between 1 and 24 hours"}
                }
            }
        },
        500: {
            "description": "Booking settings not configured",
            "content": {
                "application/json": {
                    "example": {"detail": "Booking settings not configured"}
                }
            }
        }
    },
    tags=["Booking Preview"]
)
def get_booking_preview(
    preview_data: BookingPreviewRequest,
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get booking price preview and availability estimate.
    
    **This endpoint provides:**
    - Detailed pricing breakdown (service fee, platform fee, total)
    - Applied surge pricing and discounts
    - Estimated number of available caregivers (if location provided)
    - Peak hours warnings
    - Service type multipliers
    
    **Use this endpoint to:**
    - Show transparent pricing to patients before booking
    - Update pricing when service type or duration changes
    - Display availability estimates in the UI
    - Warn about surge pricing or peak hours
    
    **Optional location data:**
    - Include patient_latitude and patient_longitude to get caregiver availability estimates
    - Location is not required for basic pricing calculation
    
    **No authentication required** - this is a public pricing endpoint.
    """
    try:
        preview = booking_service.calculate_booking_preview(preview_data)
        return preview
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


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
        
        # Create booking with payment initiation
        booking_result = booking_service.create_booking(patient.id, booking_data)
        booking = booking_result["booking"]
        
        # DO NOT notify caregivers yet - wait for payment confirmation
        # Notifications will be sent after successful payment via webhook
        
        # Enrich booking with patient and caregiver details
        enriched_booking = enrich_booking_with_details(booking, booking_service.db)
        
        # Add payment information to response
        enriched_booking["payment_url"] = booking_result.get("payment_url")
        enriched_booking["payment_access_code"] = booking_result.get("access_code")
        
        # If payment initialization failed, mark booking as failed
        if booking_result.get("payment_error"):
            enriched_booking["payment_error"] = booking_result.get("payment_error")
            enriched_booking["status"] = BookingStatus.CANCELLED
            enriched_booking["payment_status"] = PaymentStatus.FAILED
        
        return enriched_booking
        
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
    
    # Enrich bookings with both patient and caregiver details
    enriched_bookings = [enrich_booking_with_details(booking, booking_service.db) for booking in bookings]
    
    return enriched_bookings


# Caregiver Endpoints
@router.get(
    "/caregiver/bookings", 
    response_model=List[BookingResponse],
    summary="Get caregiver's bookings",
    description="Retrieve all bookings assigned to the authenticated caregiver with optional status filtering.",
    tags=["Caregiver Bookings"]
)
def get_caregiver_bookings(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get caregiver's assigned bookings with optional status filtering.
    
    **Query Parameters:**
    - **status** (optional): Filter by booking status
      - Available options: "pending", "accepted", "in_progress", "completed", "cancelled", "declined"
      - Special option: "new_requests" - shows bookings with pending notifications
    
    **Authentication:** Requires caregiver authentication token.
    """
    caregiver = booking_service.db.query(Caregiver).filter(
        Caregiver.user_id == current_user.id
    ).first()
    
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    bookings = booking_service.get_caregiver_bookings(caregiver.id, status)
    
    # Enrich bookings with patient and caregiver details
    enriched_bookings = []
    for booking in bookings:
        enriched_booking = enrich_booking_with_details(booking, booking_service.db)
        enriched_bookings.append(enriched_booking)
    
    return enriched_bookings


@router.get(
    "/caregiver/notifications", 
    response_model=List[BookingNotificationResponse],
    summary="Get caregiver's booking notifications",
    description="Retrieve booking notifications for the authenticated caregiver with optional filtering for unread notifications.",
    tags=["Caregiver Bookings"]
)
def get_caregiver_notifications(
    unread_only: bool = False,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get caregiver's booking notifications.
    
    **Query Parameters:**
    - **unread_only** (optional): If true, returns only unread notifications
    
    **Authentication:** Requires caregiver authentication token.
    """
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


@router.post(
    "/caregiver/action/{booking_id}",
    summary="Handle caregiver booking actions",
    description="Allow caregiver to perform actions on bookings: accept, decline, start service, or complete service.",
    tags=["Caregiver Bookings"]
)
async def handle_booking_action(
    booking_id: UUID,
    action_data: BookingActionRequest,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Handle caregiver actions on bookings.
    
    **Available Actions:**
    - **accept**: Accept a booking request
    - **decline**: Decline a booking request  
    - **start**: Start providing service (booking must be accepted)
    - **complete**: Mark service as completed
    - **cancel**: Cancel the booking
    
    **Authentication:** Requires caregiver authentication token.
    """
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
        # Enrich booking with patient and caregiver details
        enriched_booking = enrich_booking_with_details(booking, booking_service.db)
        return {"message": f"Booking {action_data.action} successfully", "booking": enriched_booking}
    except BookingActionForbidden as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except BookingStateConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get(
    "/booking/{booking_id}", 
    response_model=BookingResponse,
    summary="Get booking details",
    description="Retrieve detailed information about a specific booking. Available to both patients and caregivers who have access.",
    tags=["Booking Details"]
)
def get_booking_details(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get detailed information about a specific booking.
    
    **Access Control:**
    - Patients can access their own bookings
    - Caregivers can access bookings assigned to them
    
    **Authentication:** Requires user authentication token (patient or caregiver).
    """
    booking = booking_service.db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    ensure_booking_read_access(booking_service.db, current_user, booking)

    # Enrich booking with patient and caregiver details
    enriched_booking = enrich_booking_with_details(booking, booking_service.db)
    return enriched_booking


def _refund_required_response(booking: Booking) -> dict:
    """Paid, but the booking stays cancelled (the payment link had expired and the visit is too
    close to reinstate). Distinct shape so the app can tell the patient a refund is coming."""
    return {
        "status": "success",
        "message": "Your payment was received after the booking was cancelled. "
                   "The booking will not go ahead and the payment will be refunded.",
        "booking_id": booking.id,
        "payment_status": "paid",
        "booking_status": booking.status.value,
        "refund_required": True,
    }


@router.post(
    "/booking/{booking_id}/verify-payment",
    summary="Verify booking payment",
    description="Verify the payment status of a booking and update the booking accordingly.",
    tags=["Booking Payments"]
)
def verify_booking_payment(
    booking_id: UUID,
    current_user: User = Depends(get_current_patient),
    booking_service: BookingService = Depends(get_booking_service),
    db: Session = Depends(get_db)
) -> Any:
    """
    Verify payment status for a booking.
    
    **Process:**
    1. Retrieves booking by ID
    2. Verifies payment with Paystack
    3. Updates booking payment status
    4. Returns payment verification result
    
    **Authentication:** Requires patient authentication token.
    """
    # Get booking
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    # Verify user owns this booking
    patient = db.query(Patient).filter(
        Patient.user_id == current_user.id
    ).first()
    
    if not patient or booking.patient_id != patient.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    # Already settled (webhook got there first, or a repeat call): nothing to re-verify or re-notify
    if booking.payment_status == PaymentStatus.PAID:
        if booking.refund_required:
            return _refund_required_response(booking)
        return {
            "status": "success",
            "message": "Payment verified successfully",
            "booking_id": booking_id,
            "payment_status": "paid",
            "amount_paid": kobo_to_naira(booking.total_amount_kobo),
            "paid_at": None
        }

    if not booking.paystack_reference:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Booking has no payment reference"
        )

    # Verify payment with Paystack
    paystack_service = PaystackService()
    verification_result = paystack_service.verify_transaction(booking.paystack_reference)
    
    if verification_result.get("status"):
        payment_data = verification_result.get("data", {})
        
        # Update booking payment status based on Paystack response
        if payment_data.get("status") == "success":
            outcome = settle_booking_payment(db, booking.id, payment_data.get("amount_kobo"))
            if outcome == SettleOutcome.AMOUNT_MISMATCH:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Payment amount does not match booking total"
                )
            
            if outcome == SettleOutcome.PAID_NOT_DISPATCHED:
                db.refresh(booking)
                return _refund_required_response(booking)

            return {
                "status": "success",
                "message": "Payment verified successfully",
                "booking_id": booking_id,
                "payment_status": "paid",
                "amount_paid": kobo_to_naira(payment_data["amount_kobo"]) if payment_data.get("amount_kobo") is not None else None,
                "paid_at": payment_data.get("paid_at")
            }
        else:
            return {
                "status": "pending",
                "message": "Payment not yet completed",
                "booking_id": booking_id,
                "payment_status": payment_data.get("status")
            }
    else:
        return {
            "status": "error",
            "message": verification_result.get("message", "Payment verification failed"),
            "booking_id": booking_id
        }


# Location Endpoints
@router.post(
    "/caregiver/location", 
    response_model=CaregiverLocationResponse,
    summary="Update caregiver's real-time location (REST - Legacy)",
    description="Update caregiver's current GPS location for real-time tracking and matching. **⚠️ DEPRECATED: Consider using WebSocket endpoint `/ws/caregiver/location` for better scalability and real-time performance.**",
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
    tags=["Caregiver Location (Legacy REST)"]
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
    
    **⚠️ PERFORMANCE WARNING:**
    - This REST endpoint creates significant server load with many users
    - Each caregiver polling every 10s = massive API requests
    - **RECOMMENDED: Use WebSocket `/ws/caregiver/location` instead**
    
    **Update Frequency (if using REST):**
    - **Active booking**: Every 10-15 seconds for real-time tracking
    - **Available but no booking**: Every 30-60 seconds for matching
    - **Offline**: Stop sending updates and set is_available to false
    
    **Migration Path:**
    1. **Phase 1**: Implement WebSocket alongside REST (current)
    2. **Phase 2**: Migrate mobile apps to use WebSocket
    3. **Phase 3**: Deprecate this REST endpoint
    
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
    tags=["Caregiver Location (Legacy REST)"]
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
    
    ensure_booking_read_access(booking_service.db, current_user, booking)

    # The location is the caregiver's latest global position, so it must stop being visible
    # once the job is over (same rule as the tracking WebSocket). Admins keep access.
    if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.IN_PROGRESS) and not user_is_admin(
        booking_service.db, current_user
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Live tracking is only available while the booking is accepted or in progress",
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
    is_moving = bool(caregiver_location.speed and caregiver_location.speed > 0.5)
    
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


# WebSocket Endpoints for Real-time Location Tracking
@router.websocket("/ws/caregiver/location")
async def caregiver_location_websocket(
    websocket: WebSocket,
    db: Session = Depends(get_db)
):
    """
    WebSocket endpoint for caregiver real-time location updates.
    
    **Scalable Alternative to REST Polling:**
    - Persistent connection instead of polling every 10-15 seconds
    - Real-time bidirectional communication
    - Automatic broadcasting to interested parties
    - Much lower server load with high user base
    
    **Message Types Supported:**
    - `location_update`: Send GPS location data
    - `heartbeat`: Keep connection alive
    
    **Authentication (required, caregiver accounts only):**
    - A JWT *access* token, as `?token=<jwt>` or in the `Sec-WebSocket-Protocol` header
      (`bearer, <jwt>`). Missing/invalid token closes with 4401; a non-caregiver with 4403.
    
    **Example Usage:**
    ```javascript
    const ws = new WebSocket('wss://api.example.com/ws/caregiver/location?token=...');
    
    // Send location update
    ws.send(JSON.stringify({
      type: "location_update",
      data: {
        latitude: 6.5244,
        longitude: 3.3792,
        accuracy: 10.5,
        heading: 45.0,
        speed: 2.3,
        is_available: true
      }
    }));
    ```
    """
    auth = await authenticate_websocket(websocket, db, required_user_type="caregiver")
    if auth is None:
        return
    user, subprotocol = auth
    user_id = str(user.id)

    try:
        await websocket_manager.connect_caregiver(websocket, user_id, subprotocol)

        try:
            while True:
                message = await websocket.receive_text()
                await websocket_manager.handle_caregiver_message(websocket, user_id, message, db)
        except WebSocketDisconnect:
            websocket_manager.disconnect_caregiver(user_id)

    except Exception:
        log.exception("caregiver location websocket failed")
        websocket_manager.disconnect_caregiver(user_id)
        await websocket.close(code=4000, reason="Server error")


@router.websocket("/ws/patient/tracking")
async def patient_tracking_websocket(
    websocket: WebSocket,
    db: Session = Depends(get_db)
):
    """
    WebSocket endpoint for patient real-time booking tracking.
    
    **Features:**
    - Subscribe to specific booking tracking
    - Receive real-time caregiver location updates
    - Automatic ETA recalculation
    - Live movement detection
    
    **Message Types Supported:**
    - `tracking_subscribe`: Subscribe to booking tracking
    - `tracking_unsubscribe`: Stop tracking a booking
    - `heartbeat`: Keep connection alive
    
    **Authentication (required, patient accounts only):**
    - A JWT *access* token, as `?token=<jwt>` or in the `Sec-WebSocket-Protocol` header
      (`bearer, <jwt>`). Missing/invalid token closes with 4401; a non-patient with 4403.
    - A patient can only subscribe to their own booking while it is accepted or in progress.
    
    **Example Usage:**
    ```javascript
    const ws = new WebSocket('wss://api.example.com/ws/patient/tracking?token=...');
    
    // Subscribe to booking tracking
    ws.send(JSON.stringify({
      type: "tracking_subscribe",
      booking_id: "booking-uuid-here"
    }));
    
    // Listen for location updates
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'location_broadcast') {
        // Update UI with live tracking data
        updateCaregiverLocationOnMap(data.data);
      }
    };
    ```
    """
    auth = await authenticate_websocket(websocket, db, required_user_type="patient")
    if auth is None:
        return
    user, subprotocol = auth
    user_id = str(user.id)

    try:
        await websocket_manager.connect_patient(websocket, user_id, subprotocol)

        try:
            while True:
                message = await websocket.receive_text()
                await websocket_manager.handle_patient_message(websocket, user_id, message, db)
        except WebSocketDisconnect:
            websocket_manager.disconnect_patient(user_id)

    except Exception:
        log.exception("patient tracking websocket failed")
        websocket_manager.disconnect_patient(user_id)
        await websocket.close(code=4000, reason="Server error")


@router.get(
    "/ws/stats",
    summary="Get WebSocket connection statistics", 
    description="Get current WebSocket connection and subscription statistics for monitoring",
    tags=["WebSocket Monitoring"]
)
def get_websocket_stats(current_admin: User = Depends(get_current_admin)) -> dict:
    """
    Get WebSocket connection statistics for monitoring and debugging.
    
    **Returns:**
    - Active caregiver connections count
    - Active patient connections count  
    - Active tracking subscriptions count
    - Total subscribers count
    
    **Use Cases:**
    - System monitoring and health checks
    - Debugging connection issues
    - Scaling decisions based on connection load
    """
    return websocket_manager.get_connection_stats()


# Chat Endpoints
@router.post(
    "/chat/{booking_id}/send", 
    response_model=ChatMessageResponse,
    summary="Send chat message",
    description="Send a chat message in a booking conversation between patient and caregiver.",
    tags=["Chat & Communication"]
)
def send_chat_message(
    booking_id: UUID,
    message_data: ChatMessageCreate,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Send a chat message in a booking conversation.
    
    **Access Control:**
    - Only patients and caregivers involved in the booking can send messages
    
    **Authentication:** Requires user authentication token.
    """
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


@router.get(
    "/chat/{booking_id}/messages", 
    response_model=List[ChatMessageResponse],
    summary="Get chat messages",
    description="Retrieve all chat messages for a booking conversation between patient and caregiver.",
    tags=["Chat & Communication"]
)
def get_chat_messages(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get chat messages for a booking conversation.
    
    **Access Control:**
    - Only patients and caregivers involved in the booking can view messages
    
    **Authentication:** Requires user authentication token.
    """
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
@router.post(
    "/notes/{booking_id}/create", 
    response_model=BookingNoteResponse,
    summary="Create booking note with AI analysis",
    description="Create a detailed note for a booking with optional AI-powered analysis of audio transcripts and content.",
    tags=["Notes & Documentation"]
)
async def create_booking_note(
    booking_id: UUID,
    note_data: BookingNoteCreate,
    current_user: User = Depends(get_current_caregiver),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Create a booking note with AI analysis.
    
    **Features:**
    - Rich text notes with title and content
    - Audio transcript support
    - AI-powered analysis including summary, keywords, and recommendations
    
    **Access:** Only caregivers assigned to the booking can create notes
    
    **Authentication:** Requires caregiver authentication token.
    """
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


@router.get(
    "/notes/{booking_id}", 
    response_model=List[BookingNoteResponse],
    summary="Get booking notes",
    description="Retrieve all notes for a booking including AI-generated insights and caregiver observations.",
    tags=["Notes & Documentation"]
)
def get_booking_notes(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    booking_service: BookingService = Depends(get_booking_service),
) -> Any:
    """
    Get all notes for a booking.
    
    **Access Control:**
    - Patients can view notes for their bookings
    - Caregivers can view notes for their assigned bookings
    
    **Authentication:** Requires user authentication token.
    """
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
