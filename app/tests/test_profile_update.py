"""PUT /auth/profile: name, phone and photo URL persist (on the user AND the patient/caregiver profile copy), an
optional field is cleared with an explicit null, and an email change is refused until a verified flow exists.

Found by the patient-app live capture (DP1-33): the endpoint answered 200 and kept none of it."""

import pytest

from app.core.security import verify_password
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.user import User
from app.tests.factories import DEFAULT_PASSWORD, auth_headers, make_caregiver, make_patient

URL = "/api/v1/auth/profile"
ME = "/api/v1/auth/me"
IMAGE = "https://res.cloudinary.com/placeholder/image/upload/v1/avatar.jpg"


def put(client, user, body):
    return client.put(URL, json=body, headers=auth_headers(user))


def reload(db, user):
    db.expire_all()
    return db.get(User, user.id)


def test_requires_a_token(client):
    assert client.put(URL, json={"full_name": "Someone"}).status_code == 401


def test_a_patient_saves_name_phone_and_photo_and_the_response_says_so(db, client):
    patient = make_patient(db)
    patient.user.profile_image_url = IMAGE  # photos arrive by upload; sending the stored one back is allowed
    db.commit()
    r = put(client, patient.user, {"full_name": "  Ada Placeholder  ", "phone_number": "+2348030000001",
                                   "profile_image_url": IMAGE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["full_name"], body["phone_number"], body["profile_image_url"]) == (
        "Ada Placeholder", "+2348030000001", IMAGE)

    user = reload(db, patient.user)
    assert (user.full_name, user.phone_number, user.profile_image_url) == ("Ada Placeholder", "+2348030000001", IMAGE)
    # the patient row keeps a copy of both, and /auth/me (what the apps re-read) shows the saved values
    row = db.query(Patient).filter(Patient.user_id == user.id).one()
    assert (row.full_name, row.phone_number) == ("Ada Placeholder", "+2348030000001")
    me = client.get(ME, headers=auth_headers(user)).json()
    assert (me["full_name"], me["phone_number"], me["profile_image_url"]) == ("Ada Placeholder", "+2348030000001", IMAGE)
    assert (me["patient"]["full_name"], me["patient"]["phone_number"]) == ("Ada Placeholder", "+2348030000001")


def test_a_caregivers_phone_and_name_stay_in_sync_on_both_rows(db, client):
    caregiver = make_caregiver(db)
    r = put(client, caregiver.user, {"full_name": "Chidi Placeholder", "phone_number": "08030000002"})
    assert r.status_code == 200, r.text
    user = reload(db, caregiver.user)
    row = db.query(Caregiver).filter(Caregiver.user_id == user.id).one()
    assert (user.phone_number, row.phone_number) == ("08030000002", "08030000002")
    assert (user.full_name, row.full_name) == ("Chidi Placeholder", "Chidi Placeholder")


def test_fields_left_out_are_untouched(db, client):
    patient = make_patient(db)
    before = (patient.user.full_name, patient.user.phone_number)
    r = put(client, patient.user, {"address": "1 Placeholder Street, Lagos"})
    assert r.status_code == 200
    user = reload(db, patient.user)
    assert (user.full_name, user.phone_number) == before
    assert r.json()["address"] == "1 Placeholder Street, Lagos"


def test_phone_separators_are_removed_so_the_number_can_be_used_to_sign_in(db, client):
    patient = make_patient(db)
    r = put(client, patient.user, {"phone_number": "+234 (803) 000-0003"})
    assert r.status_code == 200, r.text
    assert r.json()["phone_number"] == "+2348030000003"


@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_blank_or_null_name_is_refused_and_nothing_is_saved(db, client, name):
    patient = make_patient(db)
    before = patient.user.full_name
    r = put(client, patient.user, {"full_name": name, "phone_number": "+2348030000004"})
    assert r.status_code == 422, r.text
    user = reload(db, patient.user)
    assert user.full_name == before and user.phone_number != "+2348030000004"


@pytest.mark.parametrize("phone", ["abc", "12345", "+234803000000123456", "080-ABC-0000", "", "   ", "+"])
def test_an_implausible_or_blank_phone_is_refused(db, client, phone):
    patient = make_patient(db)
    before = patient.user.phone_number
    r = put(client, patient.user, {"phone_number": phone})
    assert r.status_code == 422, r.text
    assert reload(db, patient.user).phone_number == before


def test_a_phone_another_account_uses_is_refused_with_a_code(db, client):
    patient, other = make_patient(db), make_patient(db)
    r = put(client, patient.user, {"full_name": "Should Not Save", "phone_number": other.user.phone_number})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "phone_number_taken"
    assert reload(db, patient.user).full_name != "Should Not Save"


def test_keeping_your_own_phone_is_not_a_conflict(db, client):
    patient = make_patient(db)
    r = put(client, patient.user, {"phone_number": patient.user.phone_number})
    assert r.status_code == 200, r.text


def test_an_explicit_null_clears_phone_and_photo_on_the_user_and_the_profile_row(db, client):
    caregiver = make_caregiver(db)
    caregiver.user.profile_image_url = IMAGE
    db.commit()
    r = put(client, caregiver.user, {"phone_number": None, "profile_image_url": None})
    assert r.status_code == 200, r.text
    assert r.json()["phone_number"] is None and r.json()["profile_image_url"] is None
    user = reload(db, caregiver.user)
    assert user.phone_number is None and user.profile_image_url is None
    assert db.query(Caregiver).filter(Caregiver.user_id == user.id).one().phone_number is None


def test_a_patients_phone_can_be_cleared_too(db, client):
    patient = make_patient(db)
    assert put(client, patient.user, {"phone_number": None}).status_code == 200
    user = reload(db, patient.user)
    assert user.phone_number is None
    assert db.query(Patient).filter(Patient.user_id == user.id).one().phone_number is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "not a url", "ftp://example.com/a.jpg", "https://",
                                 "https://example.com/" + "a" * 2100, ""])
def test_a_photo_url_must_be_a_normal_http_url(db, client, url):
    patient = make_patient(db)
    r = put(client, patient.user, {"profile_image_url": url})
    assert r.status_code == 422, r.text
    assert reload(db, patient.user).profile_image_url is None


def test_changing_the_email_is_refused_and_nothing_else_in_the_request_is_saved(db, client):
    patient = make_patient(db)
    old_email, old_name = patient.user.email, patient.user.full_name
    r = put(client, patient.user, {"email": "new-address@example.com", "full_name": "Should Not Save"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "email_change_not_supported"
    assert "email" in r.json()["detail"]["message"].lower()
    user = reload(db, patient.user)
    assert (user.email, user.full_name) == (old_email, old_name)


def test_sending_the_current_email_back_is_a_harmless_no_op(db, client):
    caregiver = make_caregiver(db)
    r = put(client, caregiver.user, {"email": caregiver.user.email.upper(), "full_name": "Chidi Same Email"})
    assert r.status_code == 200, r.text
    user = reload(db, caregiver.user)
    assert user.full_name == "Chidi Same Email" and user.email == caregiver.user.email


def test_a_null_email_changes_nothing(db, client):
    patient = make_patient(db)
    r = put(client, patient.user, {"email": None, "full_name": "Ada Null Email"})
    assert r.status_code == 200, r.text
    assert reload(db, patient.user).email == patient.user.email


@pytest.mark.parametrize("make", [make_patient, make_caregiver])
def test_the_profile_side_effects_are_kept(db, client, make):
    person = make(db)
    r = put(client, person.user, {"address": "2 Placeholder Road", "gender": "female", "date_of_birth": "1980-02-03",
                                  "emergency_contact_name": "Placeholder Contact",
                                  "emergency_contact_phone": "+2348030000009",
                                  "emergency_contact_address": "3 Placeholder Close"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["address"], body["gender"], body["date_of_birth"]) == ("2 Placeholder Road", "female", "1980-02-03")
    assert body["emergency_contact_phone"] == "+2348030000009"
    # blank profile fields are still ignored, as before
    r = put(client, person.user, {"address": ""})
    assert r.status_code == 200 and r.json()["address"] == "2 Placeholder Road"


def test_a_password_in_the_body_is_refused_and_nothing_changes(db, client):
    """It used to be dropped without a word; saved as is it would let a stolen access token set a password without
    knowing the current one and without ending other sessions. POST /auth/change-password is the way."""
    patient = make_patient(db)
    r = put(client, patient.user, {"password": "Another-Pass-42", "full_name": "Should Not Save"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "use_change_password"
    user = reload(db, patient.user)
    assert verify_password(DEFAULT_PASSWORD, user.hashed_password) and user.full_name != "Should Not Save"


def test_a_blank_password_is_ignored_as_before(db, client):
    patient = make_patient(db)
    assert put(client, patient.user, {"password": "", "full_name": "Ada Blank Password"}).status_code == 200


def test_a_photo_url_cannot_be_typed_in_only_cleared_or_resent(db, client):
    """Erasure and the upload route delete the stored URL's file at Cloudinary: a URL a user types in could name
    someone else's photo, so only an uploaded one (already stored) may be sent back."""
    victim_photo = "https://res.cloudinary.com/placeholder/image/upload/v1/oaklycare/profile_images/someone-else.jpg"
    patient = make_patient(db)
    r = put(client, patient.user, {"profile_image_url": victim_photo, "full_name": "Should Not Save"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "photo_upload_only", r.text
    user = reload(db, patient.user)
    assert user.profile_image_url is None and user.full_name != "Should Not Save"


def test_a_number_another_account_holds_in_any_spelling_is_taken(db, client):
    patient, other = make_patient(db), make_patient(db)
    other.user.phone_number = "+2348030000011"
    db.commit()
    for spelling in ("08030000011", "2348030000011", "+234 803 000 0011"):
        r = put(client, patient.user, {"phone_number": spelling})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "phone_number_taken", (spelling, r.text)


def test_digits_from_other_scripts_are_not_a_phone_number(db, client):
    patient = make_patient(db)
    r = put(client, patient.user, {"phone_number": "٠٨٠٣٠٠٠٠٠١٢"})
    assert r.status_code == 422, r.text


def test_a_phone_only_account_cannot_remove_the_phone_it_signs_in_with(db, client):
    patient = make_patient(db)
    patient.user.email = None
    db.commit()
    r = put(client, patient.user, {"phone_number": None})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "phone_number_required", r.text
    assert reload(db, patient.user).phone_number is not None
