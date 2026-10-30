from fastapi import APIRouter

from app.api.v1.endpoints import auth, caregiver, booking, booking_settings, payment, prescription, vitals, fallguard, patient, admin_caregivers, admin_auth, admin_users, devices, caregiver_app, admin_bookings, admin_booking_policy, admin_earnings, admin_payouts, admin_refunds, caregiver_clients, caregiver_care_plans, admin_care_plans
from app.api.v1.endpoints import auth, caregiver, booking, booking_settings, payment, prescription, vitals, fallguard, patient, admin_caregivers, devices, caregiver_app, whatsapp
from app.api.v1.endpoints import kyc_webhook

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])
api_router.include_router(caregiver.router, prefix="/caregiver", tags=["caregiver"])
api_router.include_router(caregiver_app.router, prefix="/caregiver", tags=["caregiver-app"])
api_router.include_router(caregiver_clients.router, prefix="/caregiver", tags=["caregiver-clients"])
api_router.include_router(caregiver_care_plans.router, prefix="/caregiver", tags=["caregiver-care-plans"])
api_router.include_router(devices.router, prefix="/devices", tags=["devices"])
api_router.include_router(booking.router, prefix="/booking", tags=["booking"])
api_router.include_router(booking_settings.router, prefix="/booking-settings", tags=["booking_settings"])
api_router.include_router(payment.router, prefix="/payment", tags=["payment"])
api_router.include_router(prescription.router, prefix="/prescription", tags=["prescription"])
api_router.include_router(vitals.router, prefix="/vitals", tags=["vitals"])
api_router.include_router(fallguard.router, prefix="/fallguard", tags=["fallguard"])
api_router.include_router(patient.router, prefix="/patient", tags=["patient"])
api_router.include_router(admin_caregivers.router, prefix="/admin/caregivers", tags=["admin-caregivers"])
api_router.include_router(admin_bookings.router, prefix="/admin/bookings", tags=["admin-bookings"])
api_router.include_router(admin_booking_policy.router, prefix="/admin/booking-policy", tags=["admin-booking-policy"])
api_router.include_router(admin_earnings.router, prefix="/admin/earnings", tags=["admin-earnings"])
api_router.include_router(admin_payouts.router, prefix="/admin/payouts", tags=["admin-payouts"])
api_router.include_router(admin_refunds.router, prefix="/admin/refunds", tags=["admin-refunds"])
api_router.include_router(admin_auth.router, prefix="/admin/auth", tags=["admin-auth"])
api_router.include_router(admin_users.router, prefix="/admin", tags=["admin-users"])
api_router.include_router(admin_care_plans.router, prefix="/admin", tags=["admin-care-plans"])
api_router.include_router(whatsapp.router, prefix="/whatsapp", tags=["whatsapp"])
api_router.include_router(kyc_webhook.router, prefix="/kyc/dojah", tags=["kyc"])
