"""Admin bookings operations API (AD-FR-19..26). Every route needs an admin; every action writes an audit row."""

from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.db.models.admin import AdminUser
from app.schemas.admin_booking import (
    AssignIn,
    BookingActionOut,
    BookingDetail,
    BookingList,
    BookingStatusName,
    CancelIn,
    CandidateList,
    ChannelName,
    ConfirmAddressIn,
    CorrectStatusIn,
    DispatchStateName,
    PaymentStatusName,
    QueueReason,
    ReasonIn,
    ReassignIn,
    RedispatchIn,
    ServiceTypeName,
    SortField,
    SortOrder,
    UnmatchedList,
)
from app.services.admin_bookings import (
    AdminBookingService,
    BookingNotFound,
    CaregiverNotFound,
    Conflict,
    Forbidden,
    InvalidInput,
)

router = APIRouter()


def get_service(db: Session = Depends(get_db)) -> AdminBookingService:
    return AdminBookingService(db)


def _run(fn: Callable[[], Any]) -> Any:
    """Call a service method and translate its errors into the API's {"detail": ...} responses."""
    try:
        return fn()
    except BookingNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found")
    except CaregiverNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Caregiver not found")
    except Forbidden as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))
    except InvalidInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("", response_model=BookingList)
@router.get("/", include_in_schema=False, response_model=BookingList)
def list_bookings(
    q: Optional[str] = Query(None, max_length=100, description="booking code, patient or caregiver name, phone"),
    status_: Optional[BookingStatusName] = Query(None, alias="status"),
    payment_status: Optional[PaymentStatusName] = None,
    dispatch_state: Optional[DispatchStateName] = None,
    channel: Optional[ChannelName] = None,
    city: Optional[str] = Query(None, max_length=100),
    service_type: Optional[ServiceTypeName] = None,
    caregiver_id: Optional[UUID] = None,
    scheduled_from: Optional[datetime] = None,
    scheduled_to: Optional[datetime] = None,
    created_from: Optional[datetime] = None,
    created_to: Optional[datetime] = None,
    needs_address_check: Optional[bool] = None,
    no_show: Optional[bool] = Query(None, description="true: flagged or suspected no-shows only"),
    sort: SortField = "scheduled_date",
    order: SortOrder = "desc",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: AdminUser = Depends(require_permission("bookings.read")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return service.list_bookings(
        admin, q=q, status=status_, payment_status=payment_status, dispatch_state=dispatch_state, channel=channel,
        city=city, service_type=service_type, caregiver_id=caregiver_id, scheduled_from=scheduled_from,
        scheduled_to=scheduled_to, created_from=created_from, created_to=created_to,
        needs_address_check=needs_address_check, no_show=no_show, sort=sort, order=order, limit=limit,
        offset=offset)


@router.get("/unmatched", response_model=UnmatchedList)
def unmatched_queue(
    reason: Optional[QueueReason] = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: AdminUser = Depends(require_permission("bookings.read")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return service.unmatched_queue(admin, reason=reason, limit=limit, offset=offset)


@router.get("/{booking_id}", response_model=BookingDetail)
def get_booking(
    booking_id: UUID, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.read")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.detail(admin, booking_id, request))


@router.get("/{booking_id}/candidates", response_model=CandidateList)
def candidates(
    booking_id: UUID,
    radius_km: Optional[float] = Query(None, ge=1, le=100),
    _admin: AdminUser = Depends(require_permission("bookings.assign")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.candidates(booking_id, radius_km))


@router.post("/{booking_id}/redispatch", response_model=BookingActionOut)
def redispatch(
    booking_id: UUID, body: RedispatchIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.dispatch")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.redispatch(booking_id, admin, body, request))


@router.post("/{booking_id}/assign", response_model=BookingActionOut)
def assign(
    booking_id: UUID, body: AssignIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.assign")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    """Offer a pending booking to one caregiver (they still accept), or force it (Super Admin)."""
    return _run(lambda: service.assign(booking_id, admin, body, request))


@router.post("/{booking_id}/reassign", response_model=BookingActionOut)
def reassign(
    booking_id: UUID, body: ReassignIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.assign")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    """Move an accepted visit that has not started to another approved caregiver."""
    return _run(lambda: service.reassign(booking_id, admin, body, request))


@router.post("/{booking_id}/cancel", response_model=BookingActionOut)
def cancel(
    booking_id: UUID, body: CancelIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.cancel")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.cancel(booking_id, admin, body, request))


@router.post("/{booking_id}/confirm-address", response_model=BookingActionOut)
def confirm_address(
    booking_id: UUID, body: ConfirmAddressIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.address")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.confirm_address(booking_id, admin, body, request))


@router.post("/{booking_id}/no-show", response_model=BookingActionOut)
def flag_no_show(
    booking_id: UUID, body: ReasonIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.dispatch")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.flag_no_show(booking_id, admin, body.reason, request))


@router.post("/{booking_id}/correct-status", response_model=BookingActionOut)
def correct_status(
    booking_id: UUID, body: CorrectStatusIn, request: Request,
    admin: AdminUser = Depends(require_permission("bookings.correct_status")),
    service: AdminBookingService = Depends(get_service),
) -> Any:
    return _run(lambda: service.correct_status(booking_id, admin, body, request))
