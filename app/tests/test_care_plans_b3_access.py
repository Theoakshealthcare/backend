"""Access on the three slice B3 routes, and on the medications inside the plan detail.

Same matrix as B1 and B2: every caller that must be refused gets the one identical 404 ("Care plan not found"), whatever
exists; callers with access get through, and a caregiver who is not a nurse reads but never writes (403 ``not_a_nurse``).
"""

import uuid

import pytest

from app.db.models.booking import Booking, BookingStatus
from app.db.models.patient import Patient
from app.tests.care_plan_helpers import (
    BASE, NOT_FOUND, SECRET_MED_TEXTS, SECRET_TEXTS, add_med, colleague, h, make_nurse, med_body, started_plan,
)
from app.tests.factories import make_booking, make_patient

NOT_LIVE = (BookingStatus.PENDING, BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.DECLINED)


@pytest.fixture
def scene(db, client):
    owner, patient, booking, plan = started_plan(client, db)
    added = add_med(client, owner, plan["id"]).json()
    return {"owner": owner, "patient": patient, "booking": booking, "plan": plan, "medication": added["medication"]["id"],
            "version": added["plan_version"]}


def _routes(scene):
    plan_id, mid, v = scene["plan"]["id"], scene["medication"], scene["version"]
    return {
        "add": ("POST", f"{BASE}/care-plans/{plan_id}/medications", med_body()),
        "patch": ("PATCH", f"{BASE}/care-plan-medications/{mid}", {"expected_version": v, "name": "x"}),
        "stop": ("POST", f"{BASE}/care-plan-medications/{mid}/stop", {"expected_version": v}),
        "plan_detail": ("GET", f"{BASE}/care-plans/{plan_id}", None),
    }


def _assert_no_access(client, caregiver, scene):
    for name, (method, path, body) in _routes(scene).items():
        r = client.request(method, path, json=body, headers=h(caregiver))
        assert r.status_code == 404 and r.json() == NOT_FOUND, (name, r.status_code, r.text)
        for secret in SECRET_TEXTS + SECRET_MED_TEXTS + ("Placeholder Client",):
            assert secret not in r.text


@pytest.mark.parametrize("status", NOT_LIVE)
def test_a_booking_that_is_not_live_gives_no_access(db, client, scene, status):
    other = make_nurse(db)
    make_booking(db, scene["patient"], other, status=status, booking_for="myself")
    _assert_no_access(client, other, scene)


def test_a_nurse_with_a_live_visit_for_someone_else_sees_nothing(db, client, scene):
    stranger = make_nurse(db)
    make_booking(db, make_patient(db), stranger, status=BookingStatus.IN_PROGRESS)
    _assert_no_access(client, stranger, scene)


def test_access_ends_when_the_visit_completes(db, client, scene):
    scene["booking"].status = BookingStatus.COMPLETED
    db.commit()
    _assert_no_access(client, scene["owner"], scene)


def test_a_caregiver_who_dropped_out_loses_access(db, client, scene):
    other = colleague(db, scene["patient"], status=BookingStatus.ACCEPTED, nurse=True)
    assert client.get(f"{BASE}/care-plans/{scene['plan']['id']}", headers=h(other)).status_code == 200
    booking = db.query(Booking).filter_by(caregiver_id=other.id).one()
    booking.caregiver_id, booking.status = None, BookingStatus.PENDING
    db.commit()
    _assert_no_access(client, other, scene)


def test_an_erased_client_is_not_shown(db, client, scene):
    patient = db.get(Patient, scene["patient"].id)
    patient.full_name, patient.is_active = "Deleted user", False
    db.commit()
    _assert_no_access(client, scene["owner"], scene)


def test_unknown_ids_are_the_same_404(db, client, scene):
    nurse = scene["owner"]
    for method, path, body in (
        ("POST", f"{BASE}/care-plans/{uuid.uuid4()}/medications", med_body()),
        ("PATCH", f"{BASE}/care-plan-medications/{uuid.uuid4()}", {"expected_version": 1, "name": "x"}),
        ("POST", f"{BASE}/care-plan-medications/{uuid.uuid4()}/stop", {"expected_version": 1}),
    ):
        r = client.request(method, path, json=body, headers=h(nurse))
        assert r.status_code == 404 and r.json() == NOT_FOUND, (method, path)


def test_a_medication_id_of_another_clients_plan_is_not_reachable(db, client, scene):
    other_nurse, _, _, other_plan = started_plan(client, db)
    other_med = add_med(client, other_nurse, other_plan["id"]).json()["medication"]["id"]
    r = client.post(f"{BASE}/care-plan-medications/{other_med}/stop", headers=h(scene["owner"]), json={"expected_version": 1})
    assert r.status_code == 404 and r.json() == NOT_FOUND
    r = client.patch(f"{BASE}/care-plan-medications/{other_med}", headers=h(scene["owner"]), json={"expected_version": 1, "name": "x"})
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_a_non_nurse_with_no_access_gets_the_404_not_the_403(db, client, scene):
    """Access is checked before the nurse test, so the 403 never reveals that a plan or medication exists."""
    from app.tests.factories import make_caregiver

    outsider = make_caregiver(db)
    make_booking(db, make_patient(db), outsider, status=BookingStatus.IN_PROGRESS)
    _assert_no_access(client, outsider, scene)


def test_the_medications_travel_with_the_plan_to_every_assigned_caregiver(db, client, scene):
    for status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS):
        other = colleague(db, scene["patient"], status=status)
        got = client.get(f"{BASE}/care-plans/{scene['plan']['id']}", headers=h(other))
        assert got.status_code == 200 and [m["id"] for m in got.json()["medications"]] == [scene["medication"]]


def test_a_response_is_a_closed_list_with_no_ciphertext_or_family_flag(db, client, scene):
    import json

    r = client.get(f"{BASE}/care-plans/{scene['plan']['id']}", headers=h(scene["owner"]))
    assert "gAAAAA" not in r.text and "family_visible" not in r.text and "detail_encrypted" not in r.text
    assert "gAAAAA" not in json.dumps(r.json())
