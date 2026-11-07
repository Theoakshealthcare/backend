"""A patient grants, lists and revokes a caregiver's access to their own vitals; a caregiver lists who granted them access.

The endpoints compared `users.id` with `patients.id` (different ids), so every grant, revoke and list answered 403 or
empty (found by the patient-app live capture). Health data: these tests pin who may NOT do what as hard as what works."""

import uuid
from datetime import datetime

from app.db.models.booking import BookingStatus
from app.db.models.patient_caregiver_access import PatientCaregiverAccess
from app.db.models.patient_vital import PatientVital
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

GRANT = "/api/v1/vitals/access/grant"
REVOKE = "/api/v1/vitals/access/revoke"
LISTING = "/api/v1/vitals/access/list"
VITALS = "/api/v1/vitals/"


def ids(patient, caregiver):
    return {"patient_id": str(patient.id), "caregiver_id": str(caregiver.id)}


def grant(client, patient, caregiver, actor=None):
    return client.post(GRANT, json=ids(patient, caregiver), headers=auth_headers(actor or patient.user))


def revoke(client, patient, caregiver, actor=None):
    return client.post(REVOKE, json=ids(patient, caregiver), headers=auth_headers(actor or patient.user))


def active(db, patient=None, caregiver=None):
    db.expire_all()
    q = db.query(PatientCaregiverAccess).filter(PatientCaregiverAccess.revoked_at.is_(None))
    if patient is not None:
        q = q.filter(PatientCaregiverAccess.patient_id == patient.id)
    if caregiver is not None:
        q = q.filter(PatientCaregiverAccess.caregiver_id == caregiver.id)
    return q.all()


def can_read(client, patient, caregiver):
    return client.get(f"{VITALS}{patient.id}", headers=auth_headers(caregiver.user)).status_code == 200


# --- what works -------------------------------------------------------------------------------------------

def test_a_patient_grants_access_and_the_caregiver_can_read_without_a_visit(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    assert not can_read(client, patient, caregiver)

    r = grant(client, patient, caregiver)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_id"] == str(patient.id) and body["caregiver_id"] == str(caregiver.id)
    assert body["caregiver_name"] == caregiver.full_name
    assert body["granted_by_patient"] is True and body["revoked_at"] is None and body["id"]
    assert can_read(client, patient, caregiver)


def test_granting_twice_keeps_one_active_grant(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    first = grant(client, patient, caregiver).json()
    second = grant(client, patient, caregiver)
    assert second.status_code == 200 and second.json()["id"] == first["id"]
    assert len(active(db, patient, caregiver)) == 1


def test_the_patient_lists_their_active_grants_with_the_caregivers_name(db, client):
    patient, other_patient = make_patient(db), make_patient(db)
    mine, revoked, theirs = make_caregiver(db), make_caregiver(db), make_caregiver(db)
    grant(client, patient, mine)
    grant(client, patient, revoked)
    revoke(client, patient, revoked)
    grant(client, other_patient, theirs)

    r = client.get(LISTING, headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(x["caregiver_id"], x["caregiver_name"]) for x in rows] == [(str(mine.id), mine.full_name)]
    assert rows[0]["patient_id"] == str(patient.id)


def test_revoking_ends_the_caregivers_access_at_once_on_every_vitals_route(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    grant(client, patient, caregiver)
    headers = auth_headers(caregiver.user)
    reading = client.post(VITALS, json={"patient_id": str(patient.id), "recorded_by_user_id": str(caregiver.user_id),
                                        "vital_type": "heart_rate", "value": "70", "unit": "bpm"}, headers=headers)
    assert reading.status_code == 200, reading.text
    vital_id = reading.json()["id"]

    r = revoke(client, patient, caregiver)
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"] is not None and r.json()["caregiver_name"] == caregiver.full_name

    assert client.get(f"{VITALS}{patient.id}", headers=headers).status_code == 403
    assert client.get(f"{VITALS}{patient.id}/download", headers=headers).status_code == 403
    assert client.put(f"{VITALS}{vital_id}", json={"notes": "x"}, headers=headers).status_code == 403
    assert client.delete(f"{VITALS}{vital_id}", headers=headers).status_code == 403
    assert client.post(VITALS, json={"patient_id": str(patient.id), "recorded_by_user_id": str(caregiver.user_id),
                                     "vital_type": "heart_rate", "value": "71", "unit": "bpm"},
                       headers=headers).status_code == 403
    assert client.get(LISTING, headers=auth_headers(patient.user)).json() == []
    assert db.query(PatientVital).filter(PatientVital.id == vital_id).count() == 1  # the patient's record is kept


def test_a_grant_can_be_given_again_after_a_revoke(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    first = grant(client, patient, caregiver).json()
    revoke(client, patient, caregiver)
    again = grant(client, patient, caregiver)
    assert again.status_code == 200 and again.json()["id"] != first["id"]
    assert can_read(client, patient, caregiver)


def test_revoke_ends_every_active_row_even_if_a_race_left_two(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    db.add_all([PatientCaregiverAccess(patient_id=patient.id, caregiver_id=caregiver.id) for _ in range(2)])
    db.commit()
    assert revoke(client, patient, caregiver).status_code == 200
    assert active(db, patient, caregiver) == [] and not can_read(client, patient, caregiver)


def test_revoking_what_was_never_granted_or_already_revoked_is_404(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    assert revoke(client, patient, caregiver).status_code == 404
    grant(client, patient, caregiver)
    assert revoke(client, patient, caregiver).status_code == 200
    r = revoke(client, patient, caregiver)
    assert r.status_code == 404 and r.json() == {"detail": "Access not found or already revoked."}


# --- who may NOT -------------------------------------------------------------------------------------------

def test_nobody_can_grant_on_behalf_of_another_patient(db, client):
    victim, attacker, caregiver = make_patient(db), make_patient(db), make_caregiver(db)
    r = client.post(GRANT, json=ids(victim, caregiver), headers=auth_headers(attacker.user))
    assert r.status_code == 403, r.text
    assert active(db, victim) == [] and not can_read(client, victim, caregiver)


def test_a_caregiver_cannot_grant_themselves_access(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    r = grant(client, patient, caregiver, actor=caregiver.user)
    assert r.status_code == 403 and r.json() == {"detail": "Only the patient can grant access."}
    assert active(db) == [] and not can_read(client, patient, caregiver)


def test_a_caregiver_cannot_revoke_a_grant_and_another_patient_cannot_either(db, client):
    patient, other, caregiver = make_patient(db), make_patient(db), make_caregiver(db)
    grant(client, patient, caregiver)
    assert revoke(client, patient, caregiver, actor=caregiver.user).status_code == 403
    assert client.post(REVOKE, json=ids(patient, caregiver), headers=auth_headers(other.user)).status_code == 403
    assert len(active(db, patient, caregiver)) == 1 and can_read(client, patient, caregiver)


def test_the_old_mix_up_of_ids_does_not_work_either_way(db, client):
    """users.id is not patients.id: neither is accepted in the other's place."""
    patient, caregiver = make_patient(db), make_caregiver(db)
    as_user = {"patient_id": str(patient.user_id), "caregiver_id": str(caregiver.id)}
    assert client.post(GRANT, json=as_user, headers=auth_headers(patient.user)).status_code == 403
    caregiver_user = {"patient_id": str(patient.id), "caregiver_id": str(caregiver.user_id)}
    r = client.post(GRANT, json=caregiver_user, headers=auth_headers(patient.user))
    assert r.status_code == 404 and active(db) == []


def test_a_caregiver_who_does_not_exist_or_is_deactivated_cannot_be_granted(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    nobody = client.post(GRANT, json={"patient_id": str(patient.id), "caregiver_id": str(uuid.uuid4())},
                         headers=auth_headers(patient.user))
    assert nobody.status_code == 404 and nobody.json() == {"detail": "Caregiver not found."}
    caregiver.user.is_active = False
    db.commit()
    assert grant(client, patient, caregiver).status_code == 404 and active(db) == []


def test_a_user_without_a_patient_profile_and_anonymous_calls_are_refused(db, client):
    from app.tests.factories import make_user
    caregiver, patient = make_caregiver(db), make_patient(db)
    profileless = make_user(db, "patient")
    r = client.post(GRANT, json=ids(patient, caregiver), headers=auth_headers(profileless))
    assert r.status_code == 403
    for method, url in (("post", GRANT), ("post", REVOKE), ("get", LISTING)):
        assert getattr(client, method)(url, **({"json": ids(patient, caregiver)} if method == "post" else {})).status_code == 401


def test_a_grant_for_one_patient_gives_no_access_to_another(db, client):
    patient, other, caregiver = make_patient(db), make_patient(db), make_caregiver(db)
    grant(client, patient, caregiver)
    assert can_read(client, patient, caregiver) and not can_read(client, other, caregiver)


def test_another_caregivers_grant_does_not_help(db, client):
    patient, granted, other = make_patient(db), make_caregiver(db), make_caregiver(db)
    grant(client, patient, granted)
    assert can_read(client, patient, granted) and not can_read(client, patient, other)


# --- the caregiver's view ------------------------------------------------------------------------------------

def test_a_caregiver_lists_only_the_patients_who_granted_them_access(db, client):
    caregiver = make_caregiver(db)
    granted, revoked, never, other_cg = make_patient(db), make_patient(db), make_patient(db), make_caregiver(db)
    grant(client, granted, caregiver)
    grant(client, revoked, caregiver)
    revoke(client, revoked, caregiver)
    grant(client, never, other_cg)

    r = client.get(LISTING, headers=auth_headers(caregiver.user))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(x["patient_id"], x["patient_name"]) for x in rows] == [(str(granted.id), granted.full_name)]
    assert rows[0]["caregiver_id"] == str(caregiver.id) and rows[0]["caregiver_name"] == caregiver.full_name


def test_a_caregiver_without_a_profile_lists_nothing(db, client):
    from app.tests.factories import make_user
    r = client.get(LISTING, headers=auth_headers(make_user(db, "caregiver")))
    assert r.status_code == 200 and r.json() == []


# --- the existing visit rule is untouched ----------------------------------------------------------------------

def test_an_in_progress_visit_still_gives_access_and_a_revoke_does_not_end_it(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)
    assert can_read(client, patient, caregiver)  # no grant needed
    grant(client, patient, caregiver)
    assert revoke(client, patient, caregiver).status_code == 200
    assert can_read(client, patient, caregiver)  # the visit is what gives access now
    booking.status = BookingStatus.COMPLETED
    db.commit()
    assert not can_read(client, patient, caregiver)


def test_accepted_and_completed_visits_still_give_no_access_without_a_grant(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED)
    make_booking(db, patient, caregiver, status=BookingStatus.COMPLETED)
    assert not can_read(client, patient, caregiver)
    grant(client, patient, caregiver)
    assert can_read(client, patient, caregiver)
