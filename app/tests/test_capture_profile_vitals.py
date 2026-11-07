"""Writes the live captures behind docs/fe-integration-guide-patient-profile-vitals.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness, not the compose stack):

    CAPTURE_PROFILE_VITALS=1 poetry run pytest app/tests/test_capture_profile_vitals.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-patient-profile-vitals.md docs/fe-captures/patient-profile-vitals`.
It rewrites every file in docs/fe-captures/patient-profile-vitals. Names and emails are placeholders; ids are generated.
"""

import json
import os
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_PROFILE_VITALS"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "patient-profile-vitals"
API = "/api/v1"


class Recorder:
    def __init__(self, client):
        self.client, self.index = client, []
        for old in OUT.glob("*.json"):
            old.unlink()

    def call(self, name, method, path, user=None, body=None, note=None, **params):
        headers = auth_headers(user) if user is not None else {}
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        r = getattr(self.client, method)(f"{API}{path}", **kwargs)
        doc = {
            "request": {"method": method.upper(), "path": path if not params else path,
                        "auth": "Bearer <ACCESS_TOKEN_JWT>" if user is not None else None, "body": body},
            "response": {"status_code": r.status_code,
                         "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type",)},
                         "body": r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None},
        }
        if note:
            doc["note"] = note
        (OUT / f"{name}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        self.index.append({"file": f"{name}.json", "status": r.status_code, "method": method.upper(), "path": path})
        return r


def _named(db, person, name, email, phone):
    person.user.full_name, person.user.email, person.user.phone_number = name, email, phone
    person.full_name, person.email, person.phone_number = name, email, phone
    db.commit()
    return person


def test_capture(db, client):
    OUT.mkdir(parents=True, exist_ok=True)
    rec = Recorder(client)
    patient = _named(db, make_patient(db), "Placeholder Patient", "patient-profile@example.com", "+2340000718201")
    other = _named(db, make_patient(db), "Placeholder Other Patient", "patient-other@example.com", "+2348030000202")
    caregiver = _named(db, make_caregiver(db), "Placeholder Caregiver", "caregiver-profile@example.com", "+2340000718203")
    stranger = _named(db, make_caregiver(db), "Placeholder Stranger", "caregiver-stranger@example.com", "+2340000718204")
    p, c = patient.user, caregiver.user

    # --- profile ----------------------------------------------------------------------------------------------
    photo = "https://res.cloudinary.com/placeholder/image/upload/v1/oaklycare/profile_images/avatar.jpg"
    p.profile_image_url = photo  # as if uploaded through POST /auth/profile/upload-image (Cloudinary is not called here)
    db.commit()
    rec.call("100-profile-save", "put", "/auth/profile", p, {
        "full_name": "  Placeholder Patient Edited  ", "phone_number": "+234 000 071 8205",
        "address": "1 Placeholder Street, Lagos"},
        "Name is trimmed, the phone's spaces are removed; the photo (uploaded earlier) is untouched.")
    rec.call("101-me-after-profile-save", "get", "/auth/me", p, note="What the app re-reads: user and the patient copy agree.")
    rec.call("102-profile-only-address", "put", "/auth/profile", p, {"address": "2 Placeholder Road, Lagos"},
             "Fields left out are untouched: name, phone and photo are as saved above.")
    rec.call("103-profile-clear-phone-and-photo", "put", "/auth/profile", p, {"phone_number": None, "profile_image_url": None},
             "An explicit null clears an optional field.")
    rec.call("104-profile-email-change", "put", "/auth/profile", p, {"email": "new-address@example.com", "full_name": "Not Saved"},
             "An email change is refused and NOTHING in the request is saved.")
    rec.call("105-profile-same-email", "put", "/auth/profile", p, {"email": "PATIENT-PROFILE@example.com"},
             "Sending the current email back (any letter case) is a no-op, not an error.")
    rec.call("106-profile-blank-name", "put", "/auth/profile", p, {"full_name": "   "})
    rec.call("107-profile-null-name", "put", "/auth/profile", p, {"full_name": None})
    rec.call("108-profile-bad-phone", "put", "/auth/profile", p, {"phone_number": "call me"})
    rec.call("109-profile-blank-phone", "put", "/auth/profile", p, {"phone_number": ""},
             "A blank string is not a clear: send null.")
    rec.call("110-profile-bad-photo-url", "put", "/auth/profile", p, {"profile_image_url": "javascript:alert(1)"})
    rec.call("116-profile-photo-typed-in", "put", "/auth/profile", p,
             {"profile_image_url": "https://res.cloudinary.com/placeholder/image/upload/v1/oaklycare/profile_images/other.jpg"},
             "A photo URL cannot be typed in (it would let someone point at another person's photo): upload it instead.")
    rec.call("117-profile-phone-taken-other-spelling", "put", "/auth/profile", p, {"phone_number": "0803 000 0202"},
             "Another account holds +2348030000202; the same number written 08030000202 counts as taken.")
    rec.call("111-profile-phone-taken", "put", "/auth/profile", p, {"phone_number": other.user.phone_number},
             "The phone belongs to another account.")
    rec.call("112-profile-password", "put", "/auth/profile", p, {"password": "Placeholder-Pass-1"},
             "Not a way to change the password: use POST /auth/change-password.")
    rec.call("113-profile-save-caregiver", "put", "/auth/profile", c,
             {"full_name": "Placeholder Caregiver Edited", "phone_number": "+2340000718206"},
             "A caregiver: the same call; the caregiver row's name and phone follow.")
    rec.call("114-me-caregiver-after-save", "get", "/auth/me", c)
    rec.call("115-profile-no-token", "put", "/auth/profile", None, {"full_name": "Nobody"})

    # --- vitals: what is accepted ---------------------------------------------------------------------------------
    def vital(name, vital_type, value, unit, notes=None, who=p, who_patient=patient, note=None):
        body = {"vital_type": vital_type, "value": value, "unit": unit, "source": "manual",
                "recorded_at": "2026-11-07T10:00:00Z", "patient_id": str(who_patient.id),
                "recorded_by_user_id": str(who.id)}
        if notes:
            body["notes"] = notes
        return rec.call(name, "post", "/vitals/", who, body, note)

    vital("200-vital-blood-pressure", "blood_pressure", "120/80", "mmHg")
    vital("201-vital-heart-rate", "heart_rate", "72", "bpm")
    vital("202-vital-temperature-c", "temperature", "36.8", "°C")
    vital("203-vital-temperature-f", "temperature", "98.6", "°F")
    vital("204-vital-respiratory", "respiratory_rate", "16", "breaths/min")
    vital("205-vital-glucose-mgdl", "blood_glucose", "110", "mg/dL", "RBS", note="Glucose: the context goes in notes (RBS, FBS or PP).")
    vital("206-vital-glucose-mmol", "blood_glucose", "7.8", "mmol/L", "PP")
    vital("207-vital-sleep", "sleep_duration", "7.5", "hrs", "22:30-06:00")
    vital("208-vital-blood-pressure-spaced", "blood_pressure", "120 / 80", "mmHg",
          note="Spaces around the slash are accepted and tidied before saving.")

    # --- vitals: what is refused -------------------------------------------------------------------------------------
    vital("210-vital-unknown-type", "banana", "not a number", "kg")
    vital("211-vital-bp-absurd", "blood_pressure", "300/20", "mmHg", note="The captured DP1-16 case.")
    vital("212-vital-bp-systolic-not-above-diastolic", "blood_pressure", "80/120", "mmHg")
    vital("213-vital-bp-not-two-numbers", "blood_pressure", "120", "mmHg")
    vital("214-vital-heart-rate-too-high", "heart_rate", "300", "bpm")
    vital("215-vital-temperature-wrong-for-unit", "temperature", "98.6", "°C", note="98.6 is Fahrenheit.")
    vital("216-vital-wrong-unit", "temperature", "36.8", "kelvin")
    vital("217-vital-sleep-too-long", "sleep_duration", "25", "hrs")
    vital("218-vital-not-a-number", "heart_rate", "seventy", "bpm")
    vital("219-vital-for-another-patient", "heart_rate", "70", "bpm", who_patient=other,
          note="A valid reading for someone else's record: still 403.")

    listed = rec.call("220-vitals-list", "get", f"/vitals/{patient.id}", p)
    first = listed.json()[0]["id"]
    rec.call("221-vital-edit-ok", "put", f"/vitals/{first}", p, {"notes": "after a walk"})
    rec.call("222-vital-edit-implausible", "put", f"/vitals/{first}", p, {"value": "999"})

    # --- caregiver access to a patient's vitals ----------------------------------------------------------------------
    ids = {"patient_id": str(patient.id), "caregiver_id": str(caregiver.id)}
    rec.call("300-caregiver-reads-before-grant", "get", f"/vitals/{patient.id}", c, note="No visit, no grant.")
    rec.call("301-grant", "post", "/vitals/access/grant", p, ids)
    rec.call("302-grant-again", "post", "/vitals/access/grant", p, ids, "Granting twice returns the same grant.")
    rec.call("303-patient-lists-grants", "get", "/vitals/access/list", p)
    rec.call("304-caregiver-lists-grants", "get", "/vitals/access/list", c)
    rec.call("305-caregiver-reads-after-grant", "get", f"/vitals/{patient.id}", c)
    rec.call("306-other-caregiver-reads", "get", f"/vitals/{patient.id}", stranger.user)
    rec.call("310-grant-for-another-patient", "post", "/vitals/access/grant", p,
             {"patient_id": str(other.id), "caregiver_id": str(caregiver.id)}, "Nobody grants on behalf of someone else.")
    rec.call("311-caregiver-tries-to-grant", "post", "/vitals/access/grant", c, ids)
    rec.call("312-grant-unknown-caregiver", "post", "/vitals/access/grant", p,
             {"patient_id": str(patient.id), "caregiver_id": "00000000-0000-0000-0000-000000000000"})
    rec.call("313-grant-with-user-ids", "post", "/vitals/access/grant", p,
             {"patient_id": str(p.id), "caregiver_id": str(c.id)}, "The user ids are not the profile ids.")
    rec.call("314-caregiver-tries-to-revoke", "post", "/vitals/access/revoke", c, ids)
    rec.call("320-revoke", "post", "/vitals/access/revoke", p, ids)
    rec.call("321-caregiver-reads-after-revoke", "get", f"/vitals/{patient.id}", c, note="Access ends at once.")
    rec.call("322-revoke-again", "post", "/vitals/access/revoke", p, ids)
    rec.call("323-patient-lists-after-revoke", "get", "/vitals/access/list", p)
    rec.call("324-caregiver-lists-after-revoke", "get", "/vitals/access/list", c)
    rec.call("325-grant-no-token", "post", "/vitals/access/grant", None, ids)

    # the visit rule is separate from grants
    make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)
    rec.call("330-caregiver-reads-during-visit", "get", f"/vitals/{patient.id}", c,
             note="A visit in progress gives access with no grant, and a revoke does not end it.")

    (OUT / "000-index.json").write_text(json.dumps(rec.index, indent=2) + "\n")
