"""Care plan access: who may see or change a plan, and the one identical 404.

The caregiver rule is the client profile's rule unchanged, so one test here asserts the two endpoints agree across the whole
matrix. A caregiver with no access of any kind gets the same body whether the booking, the plan or the client exists.
"""

import uuid

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan
from app.db.models.patient import Patient
from app.tests.care_plan_helpers import (
    BASE, NOT_FOUND, SECRET_TEXTS, create_plan, h, make_nurse, relative_visit, self_visit,
)
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

LIVE = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)
NOT_LIVE = (BookingStatus.PENDING, BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.DECLINED)


@pytest.fixture
def scene(db, client):
    """A nurse with a live visit and a plan for the client, plus the pieces the routes below need."""
    owner = make_nurse(db)
    patient, booking = self_visit(db, owner)
    plan = create_plan(client, owner, booking).json()
    return {"owner": owner, "patient": patient, "booking": booking, "plan": plan, "goal": plan["goals"][0]["id"],
            "version": plan["version"]}


def _requests(scene, booking):
    """Every route that takes a plan, a goal or a booking, as (method, path, json)."""
    plan_id, goal_id, v = scene["plan"]["id"], scene["goal"], scene["version"]
    return {
        "get_plan": ("GET", f"{BASE}/care-plans/{plan_id}", None),
        "patch_plan": ("PATCH", f"{BASE}/care-plans/{plan_id}", {"expected_version": v, "risk_level": "low"}),
        "add_goal": ("POST", f"{BASE}/care-plans/{plan_id}/goals", {"title": "g", "frequency": "daily"}),
        "patch_goal": ("PATCH", f"{BASE}/care-plan-goals/{goal_id}", {"expected_version": v, "title": "x"}),
        "delete_goal": ("DELETE", f"{BASE}/care-plan-goals/{goal_id}?expected_version={v}", None),
        "visit_summary": ("GET", f"{BASE}/bookings/{booking.id}/care-plan", None),
        "create": ("POST", f"{BASE}/bookings/{booking.id}/care-plan",
                   {"diagnosis": "d", "risk_level": "low"}),
    }


def _call(client, caregiver, request):
    method, path, body = request
    return client.request(method, path, json=body, headers=h(caregiver))


def _assert_no_access(client, caregiver, scene, booking):
    for name, request in _requests(scene, booking).items():
        r = _call(client, caregiver, request)
        assert r.status_code == 404 and r.json() == NOT_FOUND, (name, r.status_code, r.text)
        for secret in SECRET_TEXTS + ("Placeholder Client",):
            assert secret not in r.text


# --- who has no access ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", NOT_LIVE)
def test_a_booking_that_is_not_live_gives_no_access(db, client, scene, status):
    other = make_nurse(db)
    booking = make_booking(db, scene["patient"], other, status=status, booking_for="myself")
    _assert_no_access(client, other, scene, booking)


def test_a_caregiver_with_no_booking_for_the_client_sees_nothing(db, client, scene):
    stranger = make_nurse(db)
    make_booking(db, make_patient(db), stranger, status=BookingStatus.IN_PROGRESS)  # a live visit, for someone else
    for name, request in _requests(scene, scene["booking"]).items():
        r = _call(client, stranger, request)
        assert r.status_code == 404 and r.json() == NOT_FOUND, name


def test_a_booking_that_is_not_yours_is_the_same_404_as_a_missing_one(db, client, scene):
    stranger = make_nurse(db)
    for name, request in _requests(scene, scene["booking"]).items():
        if "bookings/" in request[1]:  # the booking-keyed routes, called with the owner's booking id
            r = _call(client, stranger, request)
            assert r.status_code == 404 and r.json() == NOT_FOUND, name
    missing = client.get(f"{BASE}/bookings/{uuid.uuid4()}/care-plan", headers=h(stranger))
    assert missing.status_code == 404 and missing.json() == NOT_FOUND
    absent = client.get(f"{BASE}/care-plans/{uuid.uuid4()}", headers=h(stranger))
    assert absent.status_code == 404 and absent.json() == NOT_FOUND
    assert client.delete(f"{BASE}/care-plan-goals/{uuid.uuid4()}?expected_version=1", headers=h(stranger)).json() == NOT_FOUND


def test_a_caregiver_only_offered_the_visit_sees_nothing(db, client, scene):
    from app.db.models.booking import BookingNotification

    offered = make_nurse(db)
    booking = make_booking(db, scene["patient"], None, status=BookingStatus.PENDING, booking_for="myself")
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=offered.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    _assert_no_access(client, offered, scene, booking)


def test_a_caregiver_who_dropped_out_loses_access(db, client, scene):
    other = make_nurse(db)
    booking = make_booking(db, scene["patient"], other, status=BookingStatus.ACCEPTED, booking_for="myself")
    assert client.get(f"{BASE}/care-plans/{scene['plan']['id']}", headers=h(other)).status_code == 200
    booking.caregiver_id, booking.status = None, BookingStatus.PENDING
    db.commit()
    _assert_no_access(client, other, scene, booking)


def test_access_ends_when_the_visit_completes(db, client, scene):
    assert client.get(f"{BASE}/care-plans/{scene['plan']['id']}", headers=h(scene["owner"])).status_code == 200
    scene["booking"].status = BookingStatus.COMPLETED
    db.commit()
    _assert_no_access(client, scene["owner"], scene, scene["booking"])


def test_an_erased_client_is_not_shown(db, client, scene):
    patient = db.get(Patient, scene["patient"].id)
    patient.full_name, patient.is_active = "Deleted user", False
    db.commit()
    _assert_no_access(client, scene["owner"], scene, scene["booking"])


def test_a_visit_for_another_client_does_not_open_this_clients_plan(db, client, scene):
    nurse = scene["owner"]
    _, other_booking = self_visit(db, nurse)  # a second client of the same nurse
    other_plan = create_plan(client, nurse, other_booking, diagnosis="Placeholder diagnosis two").json()
    assert other_plan["id"] != scene["plan"]["id"]
    stranger = make_nurse(db)
    make_booking(db, db.get(Patient, other_booking.patient_id), stranger, status=BookingStatus.ACCEPTED, booking_for="myself")
    assert client.get(f"{BASE}/care-plans/{other_plan['id']}", headers=h(stranger)).status_code == 200
    r = client.get(f"{BASE}/care-plans/{scene['plan']['id']}", headers=h(stranger))
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_the_booker_is_not_the_relatives_client(db, client):
    """A visit for the booker herself must not open the plan of the relative she booked for, and the reverse."""
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    relative_plan = create_plan(client, nurse, booking).json()
    other = make_nurse(db)
    make_booking(db, booker, other, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    r = client.get(f"{BASE}/care-plans/{relative_plan['id']}", headers=h(other))
    assert r.status_code == 404 and r.json() == NOT_FOUND
    own = create_plan(client, other, db.query(type(booking)).filter_by(caregiver_id=other.id).one())
    assert own.status_code == 201 and own.json()["client"]["full_name"] == "Placeholder Booker"
    # and a visit for a different relative of the same booker does not open this relative's plan
    _, _, other_booking = relative_visit(db, other)
    assert client.get(f"{BASE}/care-plans/{relative_plan['id']}", headers=h(other)).status_code == 404
    assert other_booking.care_recipient_id != recipient.id


def test_an_uppercase_myself_is_still_the_client_themselves(db, client):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse, booking_for="Myself")
    r = create_plan(client, nurse, booking)
    assert r.status_code == 201 and r.json()["client"]["full_name"] == "Placeholder Client"
    other = make_nurse(db)
    make_booking(db, patient, other, status=BookingStatus.EN_ROUTE, booking_for=" myself ")
    assert client.get(f"{BASE}/care-plans/{r.json()['id']}", headers=h(other)).status_code == 200


# --- suspension -----------------------------------------------------------------------------------------------------

def test_a_suspended_caregiver_finishes_a_visit_under_way_but_starts_no_new_one(db, client):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse, status=BookingStatus.IN_PROGRESS)
    plan = create_plan(client, nurse, booking).json()
    suspended = make_nurse(db, profile_status="suspended")
    accepted = make_booking(db, patient, suspended, status=BookingStatus.ACCEPTED, booking_for="myself")
    on_way = make_booking(db, patient, suspended, status=BookingStatus.EN_ROUTE, booking_for="myself")
    for b in (accepted, on_way):
        assert client.get(f"{BASE}/bookings/{b.id}/care-plan", headers=h(suspended)).status_code == 404
    assert client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(suspended)).status_code == 404
    under_way = make_booking(db, patient, suspended, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    assert client.get(f"{BASE}/bookings/{under_way.id}/care-plan", headers=h(suspended)).status_code == 200
    r = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(suspended))
    assert r.status_code == 200 and r.json()["booking_id"] == str(under_way.id)
    assert client.get(f"{BASE}/care-plans", headers=h(suspended)).json()["items"][0]["id"] == plan["id"]


# --- the profile's rule, unchanged ----------------------------------------------------------------------------------

@pytest.mark.parametrize("status", list(BookingStatus))
@pytest.mark.parametrize("profile_status", ["approved", "suspended"])
@pytest.mark.parametrize("patient_active", [True, False])
@pytest.mark.parametrize("mine", [True, False])
def test_the_visit_summary_and_the_client_profile_agree_on_who_gets_in(db, client, status, profile_status, patient_active, mine):
    caller = make_nurse(db, profile_status=profile_status)
    owner = caller if mine else make_nurse(db)
    patient = make_patient(db)
    patient.is_active = patient_active
    db.commit()
    booking = make_booking(db, patient, owner, status=status, booking_for="myself")
    profile = client.get(f"{BASE}/bookings/{booking.id}/client", headers=h(caller))
    summary = client.get(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(caller))
    assert summary.status_code == profile.status_code, (summary.text, profile.text)


# --- roles ----------------------------------------------------------------------------------------------------------

def test_every_authoring_route_is_nurse_only_even_with_full_access(db, client, scene):
    other = make_caregiver(db)  # its self-declared specialization says "nurse"; there is no skill row
    make_booking(db, scene["patient"], other, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    requests = _requests(scene, scene["booking"])
    own_booking = db.query(type(scene["booking"])).filter_by(caregiver_id=other.id).one()
    requests["create"] = ("POST", f"{BASE}/bookings/{own_booking.id}/care-plan", {"diagnosis": "d", "risk_level": "low"})
    requests["visit_summary"] = ("GET", f"{BASE}/bookings/{own_booking.id}/care-plan", None)
    for name in ("patch_plan", "add_goal", "patch_goal", "delete_goal", "create"):
        r = _call(client, other, requests[name])
        assert r.status_code == 403, (name, r.status_code, r.text)
        assert r.json()["detail"] == {"code": "not_a_nurse", "message": "Only nurses can create or change a care plan."}
    assert _call(client, other, requests["get_plan"]).status_code == 200
    assert _call(client, other, requests["visit_summary"]).status_code == 200
    plan = db.get(CarePlan, scene["plan"]["id"])
    db.refresh(plan)
    assert plan.version == scene["version"] and plan.risk_level == "high"  # nothing changed


def test_a_nurse_without_access_gets_404_before_any_role_check(db, client, scene):
    non_nurse_stranger = make_caregiver(db)
    for name in ("patch_plan", "add_goal", "patch_goal", "delete_goal"):
        r = _call(client, non_nurse_stranger, _requests(scene, scene["booking"])[name])
        assert r.status_code == 404 and r.json() == NOT_FOUND, name


def test_the_nurse_test_uses_the_skill_row_not_the_typed_specialization(db, client):
    typed_only = make_caregiver(db, specialization="Registered Nurse")
    assert client.get(f"{BASE}/home", headers=h(typed_only)).json()["can_author_care_plans"] is False
    assert client.get(f"{BASE}/care-plan-templates", headers=h(typed_only)).status_code == 403
    skilled = make_nurse(db, specialization="Cook")
    assert client.get(f"{BASE}/home", headers=h(skilled)).json()["can_author_care_plans"] is True
    assert client.get(f"{BASE}/care-plan-templates", headers=h(skilled)).status_code == 200


def test_other_skills_do_not_make_an_author(db, client):
    from app.db.models.caregiver import CaregiverSkill

    cg = make_caregiver(db)
    db.add_all([CaregiverSkill(caregiver_id=cg.id, service_type=t) for t in ("doctor", "physiotherapist", "caregiver")])
    db.commit()
    assert client.get(f"{BASE}/care-plan-templates", headers=h(cg)).status_code == 403


# --- other account types and anonymous callers ----------------------------------------------------------------------

def test_a_patient_token_is_403_and_no_token_is_401_on_every_route(db, client, scene):
    for name, (method, path, body) in _requests(scene, scene["booking"]).items():
        anon = client.request(method, path, json=body)
        assert anon.status_code == 401, name
        patient = client.request(method, path, json=body, headers=auth_headers(scene["patient"].user))
        assert patient.status_code == 403, name
        assert patient.json() == {"detail": "Access denied. Caregiver account required."}
    for path in ("/care-plans", "/care-plans/candidates", "/care-plan-templates"):
        assert client.get(f"{BASE}{path}").status_code == 401
        assert client.get(f"{BASE}{path}", headers=auth_headers(scene["patient"].user)).status_code == 403


def test_bad_ids_are_422_not_a_probe(db, client, scene):
    assert client.get(f"{BASE}/care-plans/not-a-uuid", headers=h(scene["owner"])).status_code == 422


def test_a_caregiver_account_without_a_profile_is_refused_with_the_same_404(db, client):
    from app.tests.factories import make_user

    user = make_user(db, "caregiver")
    r = client.get(f"{BASE}/care-plans", headers=auth_headers(user))
    assert r.status_code == 404 and r.json() == NOT_FOUND
