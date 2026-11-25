"""Care alerts for out-of-range readings (slice 1c): what raises one, the debounce, who is told and what the message says.

A reading goes through the real vitals API (so the hook is the one production uses); the fake Resend and Expo catch the messages."""

import json
from datetime import datetime, timedelta

import pytest

from app.core.security import decrypt
from app.db.models.care_alert import CareAlert, CareAlertOccurrence
from app.services import care_alerts
from app.tests.care_alert_helpers import (  # noqa: F401  (outbox is a fixture)
    add_family, give_consent, outbox, patient_with_family, post_reading, self_recipient,
)
from app.tests.factories import make_admin, make_patient
from app.tests.fakes import expo  # noqa: F401


def alerts(db):
    db.expire_all()
    return db.query(CareAlert).order_by(CareAlert.created_at).all()


def detail(alert):
    return json.loads(decrypt(alert.detail_encrypted))


# --- what raises an alert -----------------------------------------------------------------------------------------------

def test_a_high_reading_raises_one_warning_alert(db, client, outbox):
    patient = make_patient(db)
    assert post_reading(client, patient, "heart_rate", "110", "bpm").status_code == 200
    [alert] = alerts(db)
    assert (alert.type, alert.severity, alert.status) == ("vital_out_of_range", "warning", "open")
    assert alert.patient_id == patient.id and alert.vital_id is not None and alert.occurrence_count == 1
    assert alert.summary == "Heart rate reading is higher than the safe range."
    assert detail(alert)["status"] == "high" and detail(alert)["value"] == "110"


def test_a_low_reading_says_lower(db, client, outbox):
    patient = make_patient(db)
    post_reading(client, patient, "temperature", "35.5", "°C")
    [alert] = alerts(db)
    assert alert.severity == "warning" and alert.summary == "Temperature reading is lower than the safe range."


def test_a_critical_reading_raises_a_critical_alert(db, client, outbox):
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "45", "bpm")
    [alert] = alerts(db)
    assert alert.severity == "critical" and alert.summary == "Heart rate reading is at a critical level."


@pytest.mark.parametrize("vital_type,value,unit", [
    ("heart_rate", "72", "bpm"), ("blood_pressure", "120/80", "mmHg"), ("temperature", "36.8", "°C"),
    ("respiratory_rate", "16", "breaths/min"), ("sleep_duration", "7", "hrs")])
def test_normal_and_unflagged_readings_raise_nothing(db, client, outbox, vital_type, value, unit):
    patient = make_patient(db)
    assert post_reading(client, patient, vital_type, value, unit).status_code == 200
    assert alerts(db) == [] and outbox.requests == []


def test_a_backfilled_old_reading_raises_nothing(db, client, outbox):
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "45", "bpm", recorded_at=datetime.utcnow() - timedelta(hours=7))
    assert alerts(db) == []


def test_editing_a_reading_into_range_raises_and_a_note_edit_does_not(db, client, outbox):
    patient = make_patient(db)
    vital_id = post_reading(client, patient, "heart_rate", "72", "bpm").json()["id"]
    from app.tests.factories import auth_headers
    headers = auth_headers(patient.user)
    assert client.put(f"/api/v1/vitals/{vital_id}", json={"notes": "after a walk"}, headers=headers).status_code == 200
    assert alerts(db) == []
    assert client.put(f"/api/v1/vitals/{vital_id}", json={"value": "120"}, headers=headers).status_code == 200
    [alert] = alerts(db)
    assert detail(alert)["status"] == "high"
    # the same edit again is the same source: still one alert, not counted twice
    client.put(f"/api/v1/vitals/{vital_id}", json={"value": "121"}, headers=headers)
    assert [a.occurrence_count for a in alerts(db)] == [1]


# --- the debounce -------------------------------------------------------------------------------------------------------

def test_a_burst_of_readings_is_one_alert_with_a_count(db, client, outbox):
    patient, _recipient, family = patient_with_family(db)
    for value in ("110", "112", "115"):
        post_reading(client, patient, "heart_rate", value, "bpm")
    [alert] = alerts(db)
    assert alert.occurrence_count == 3
    assert detail(alert)["value"] == "115"  # the latest reading is the one shown
    assert db.query(CareAlertOccurrence).count() == 3
    assert outbox.to() == [family.email]  # the family heard once, not three times


def test_two_vital_types_in_a_window_are_still_one_alert_per_severity(db, client, outbox):
    """The key is patient + type + severity: a high heart rate then a low temperature are both 'vital_out_of_range' warnings."""
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    post_reading(client, patient, "temperature", "35.0", "°C")
    assert [(a.severity, a.occurrence_count) for a in alerts(db)] == [("warning", 2)]


def test_a_critical_reading_is_not_swallowed_by_an_open_warning(db, client, outbox):
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert sorted(a.severity for a in alerts(db)) == ["critical", "warning"]


def test_the_warning_window_expires(db, client, outbox, monkeypatch):
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    alert = alerts(db)[0]
    alert.last_occurred_at = datetime.utcnow() - timedelta(minutes=241)
    db.commit()
    post_reading(client, patient, "heart_rate", "111", "bpm")
    assert [a.occurrence_count for a in alerts(db)] == [1, 1]


def test_the_critical_window_is_shorter(db, client, outbox):
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "45", "bpm")
    alert = alerts(db)[0]
    alert.last_occurred_at = datetime.utcnow() - timedelta(minutes=31)
    db.commit()
    post_reading(client, patient, "heart_rate", "44", "bpm")
    assert len(alerts(db)) == 2


def test_a_resolved_alert_absorbs_nothing(db, client, outbox):
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    alert = alerts(db)[0]
    alert.status = "resolved"
    db.commit()
    post_reading(client, patient, "heart_rate", "111", "bpm")
    assert [(a.status, a.occurrence_count) for a in alerts(db)] == [("resolved", 1), ("open", 1)]


def test_other_patients_never_share_an_alert(db, client, outbox):
    a, b = make_patient(db), make_patient(db)
    post_reading(client, a, "heart_rate", "110", "bpm")
    post_reading(client, b, "heart_rate", "110", "bpm")
    assert len(alerts(db)) == 2


# --- who is told, and what they are told ---------------------------------------------------------------------------------

def test_a_consenting_verified_family_contact_gets_a_plain_email_with_no_reading(db, client, outbox):
    patient, _r, family = patient_with_family(db, first_name="Ada")
    post_reading(client, patient, "blood_pressure", "165/100", "mmHg")
    assert outbox.to() == [family.email]
    mail = outbox.mails[0]
    assert mail["subject"].endswith("Oakly care alert")
    body = mail["html"]
    assert "A blood pressure reading for Ada was higher than the safe range." in body
    assert "112" not in body  # a warning does not say call 112
    for leaked in ("165", "165/100", "Okafor", "mmHg", "hypertension", "diabetes"):
        assert leaked not in body
    [alert] = alerts(db)
    assert alert.family_notified_count == 1 and alert.family_notified_at is not None


def test_a_critical_reading_says_call_112(db, client, outbox):
    patient, _r, family = patient_with_family(db, first_name="Ada")
    post_reading(client, patient, "heart_rate", "45", "bpm")
    body = outbox.body(family.email)
    assert "A heart rate reading for Ada is at a dangerous level. This may be an emergency. Call 112 now." in body
    assert "45" not in body.replace("112", "")


def test_a_push_reaches_a_family_contact_with_the_app(db, client, outbox, expo):
    patient, _r, family = patient_with_family(db, push=True, email=False)
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert outbox.requests == []
    [message] = expo.messages
    assert message["title"] == "Oakly care alert"
    assert message["body"] == "A heart rate reading for Ada is at a dangerous level. This may be an emergency. Call 112 now."
    assert message["data"] == {"type": "care_alert", "severity": "critical"}
    assert alerts(db)[0].family_notified_count == 1


def test_a_contact_with_email_and_push_is_reached_once_per_channel_and_counted_once(db, client, outbox, expo):
    patient, _r, family = patient_with_family(db, push=True)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    assert outbox.to() == [family.email] and len(expo.messages) == 1
    assert alerts(db)[0].family_notified_count == 1


@pytest.mark.parametrize("why,kwargs", [
    ("an unverified account", {"verified": False}),
    ("no clinical access", {"clinical_access": False}),
    ("an inactive account", {"active": False}),
    ("a role that is not family or guardian", {"role": "self"}),
    ("no e-mail and no app", {"email": False}),
])
def test_contacts_who_are_not_allowed_or_not_verified_are_not_told(db, client, outbox, expo, why, kwargs):
    patient, _r, _family = patient_with_family(db, **kwargs)
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert outbox.to() == [] and expo.messages == [], why
    [alert] = alerts(db)  # the admin queue still has it
    assert alert.family_notified_count == 0 and alert.family_notified_at is not None


def test_no_consent_means_nobody_in_the_family_is_told(db, client, outbox):
    patient, _r, _family = patient_with_family(db, consent=False)
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert outbox.to() == [] and len(alerts(db)) == 1


def test_a_withdrawn_consent_stops_the_messages(db, client, outbox):
    patient, recipient, _family = patient_with_family(db)
    give_consent(db, recipient, "withdrawn")  # the latest decision wins
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert outbox.to() == []


def test_a_patient_with_no_recipient_row_has_no_family_to_tell(db, client, outbox):
    patient = make_patient(db)  # an app patient who never used the WhatsApp channel
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert outbox.to() == [] and len(alerts(db)) == 1


def test_only_the_alerts_own_patient_family_is_told(db, client, outbox):
    patient, _r, family = patient_with_family(db)
    _other, _or, other_family = patient_with_family(db, first_name="Bola")
    post_reading(client, patient, "heart_rate", "110", "bpm")
    assert outbox.to() == [family.email]


# --- the admin e-mail for critical ----------------------------------------------------------------------------------------

def test_a_critical_alert_mails_the_support_inbox_admins_and_a_warning_does_not(db, client, outbox, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_NOTIFY_EMAIL", "oncall@example.test")
    make_admin(db, ["support"], password=None, email="support@example.test")
    make_admin(db, ["finance"], password=None, email="finance@example.test")  # no support.inbox
    patient = make_patient(db)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    assert outbox.to() == []
    post_reading(client, patient, "heart_rate", "45", "bpm")
    assert outbox.to() == ["oncall@example.test", "support@example.test"]
    body = outbox.body("support@example.test")
    assert "https://admin.oaklycare.com/care-alerts" in body
    assert patient.full_name not in body and "45" not in body  # no name and no reading in the admin mail either
    critical = [a for a in alerts(db) if a.severity == "critical"][0]
    assert critical.admin_notified_at is not None


def test_a_repeat_inside_the_window_sends_nothing_more(db, client, outbox):
    patient, _r, family = patient_with_family(db)
    make_admin(db, ["support"], password=None, email="support@example.test")
    post_reading(client, patient, "heart_rate", "45", "bpm")
    sent = len(outbox.requests)
    post_reading(client, patient, "heart_rate", "44", "bpm")
    assert len(outbox.requests) == sent and alerts(db)[0].occurrence_count == 2


# --- never into the caregiver's request ------------------------------------------------------------------------------------

def test_a_failure_in_the_alert_path_never_fails_the_reading(db, client, outbox, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("alert path is down")

    monkeypatch.setattr(care_alerts, "alert_for_reading", boom)
    patient = make_patient(db)
    r = post_reading(client, patient, "heart_rate", "45", "bpm")
    assert r.status_code == 200 and r.json()["flag"]["status"] == "critical"
    assert "care alert for reading failed" in caplog.text


def test_a_failing_mail_provider_never_fails_the_reading(db, client, outbox, monkeypatch):
    import httpx
    from app.services import email as email_service
    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(lambda request: httpx.Response(500, json={})))
    patient, _r, _family = patient_with_family(db)
    assert post_reading(client, patient, "heart_rate", "45", "bpm").status_code == 200
    [alert] = alerts(db)
    assert alert.family_notified_count == 0 and alert.family_notified_at is not None
