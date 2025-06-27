from fastapi import APIRouter

from app.api.v1.endpoints import auth, caregiver, booking

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])
api_router.include_router(caregiver.router, prefix="/caregiver", tags=["caregiver"])
api_router.include_router(booking.router, prefix="/booking", tags=["booking"])