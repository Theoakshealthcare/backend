"""W2: where a WhatsApp booking's encrypted condition may and may not surface (the app and caregiver API views)."""

import json

from app.api.v1.endpoints.booking import enrich_booking_with_details
from app.core.security import encrypt
from app.tests.factories import make_booking, make_caregiver, make_patient, make_user

CONDITION = "post-surgery wound dressing, SENTINEL-CONDITION-1188"


def whatsapp_booking(db, caregiver=None):
    booker = make_user(db, "patient")
    return make_booking(db, make_patient(db), caregiver, channel="whatsapp", booked_by_user_id=booker.id,
                        condition_encrypted=encrypt(CONDITION), needs_address_check=True)


def test_the_ciphertext_never_leaves_and_the_assigned_caregiver_reads_the_condition(db):
    caregiver = make_caregiver(db)
    booking = whatsapp_booking(db, caregiver)
    view = enrich_booking_with_details(booking, db, viewer_caregiver_id=caregiver.id)
    assert "condition_encrypted" not in view and view["condition"] == CONDITION
    assert view["channel"] == "whatsapp" and view["needs_address_check"] is True


def test_the_patient_and_admin_view_has_the_plain_condition_and_no_ciphertext(db):
    booking = whatsapp_booking(db)
    view = enrich_booking_with_details(booking, db)
    assert view["condition"] == CONDITION and "condition_encrypted" not in view
    assert "gAAAA" not in json.dumps(view, default=str)  # no Fernet token anywhere in the dict


def test_a_caregiver_who_only_holds_an_offer_sees_no_condition_and_no_identity(db):
    assigned, other = make_caregiver(db), make_caregiver(db)
    booking = whatsapp_booking(db, assigned)
    view = enrich_booking_with_details(booking, db, viewer_caregiver_id=other.id)
    assert view["condition"] is None and "condition_encrypted" not in view
    assert view["booked_by_user_id"] is None and view["care_recipient_id"] is None
    assert "SENTINEL" not in json.dumps(view, default=str) and "gAAAA" not in json.dumps(view, default=str)


def test_an_app_bookings_plain_condition_still_comes_through_unchanged(db):
    booking = make_booking(db, make_patient(db), condition="Diabetes management")
    assert enrich_booking_with_details(booking, db)["condition"] == "Diabetes management"
