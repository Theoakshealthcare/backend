"""A database with no ``booking_settings`` row (a new environment) answers preview and create with a clear 503, not a 500."""

import logging

from app.db.models.booking import Booking
from app.tests.factories import auth_headers, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1/booking"
# --- pricing settings: a clear 503 instead of a 500 -------------------------------------------------------------------
PREVIEW = {"service_type": "nurse", "duration_hours": 3}
CREATE = dict(service_type="nurse", assistance_type="x", duration_hours=3, scheduled_date="2030-01-01T10:00:00",
              patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
              patient_state="Lagos", booking_for="myself")
NOT_CONFIGURED = {"code": "booking_settings_not_configured",
                  "message": "Pricing is not available right now. Please try again later."}


def test_preview_on_a_database_with_no_pricing_is_a_503_with_a_stable_code_and_a_log_line(client, caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.booking"):
        r = client.post(f"{API}/preview", json=PREVIEW)
    assert r.status_code == 503 and r.json()["detail"] == NOT_CONFIGURED
    assert "booking_settings has no row" in caplog.text and "booking-settings" in caplog.text


def test_create_on_a_database_with_no_pricing_is_a_503_and_creates_nothing(db, client, fake_paystack, caplog):
    patient = make_patient(db)
    with caplog.at_level(logging.ERROR, logger="app.services.booking"):
        r = client.post(f"{API}/create", json=CREATE, headers=auth_headers(patient.user))
    assert r.status_code == 503 and r.json()["detail"] == NOT_CONFIGURED
    assert db.query(Booking).count() == 0 and fake_paystack.initialised == []
    assert "booking_settings has no row" in caplog.text


