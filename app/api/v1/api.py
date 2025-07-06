from fastapi import APIRouter

from app.api.v1.endpoints import auth, caregiver, booking, booking_settings, payment, prescription, vitals

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])
api_router.include_router(caregiver.router, prefix="/caregiver", tags=["caregiver"])
api_router.include_router(booking.router, prefix="/booking", tags=["booking"])
api_router.include_router(booking_settings.router, prefix="/booking-settings", tags=["booking_settings"])
api_router.include_router(payment.router, prefix="/payment", tags=["payment"])
api_router.include_router(prescription.router, prefix="/prescription", tags=["prescription"])
api_router.include_router(vitals.router, prefix="/vitals", tags=["vitals"])