"""Access on every slice B2 route: tasks, day view, ticks, observations, progress.

One matrix, as for B1: each route is called by every kind of caller that must be refused, and the answer is the one
identical 404 ("Care plan not found") whatever exists. Callers with access get through, and the roles differ only where the
design says so (a nurse edits the task list; any assigned caregiver ticks and writes notes during their in-progress visit).
"""

import uuid

import pytest

from app.db.models.booking import Booking, BookingNotification, BookingStatus
from app.db.models.patient import Patient
from app.tests.care_plan_helpers import (
    BASE, NOT_FOUND, SECRET_NOTE, SECRET_TEXTS, colleague, daily_task, h, make_nurse, started_plan, today_iso,
)
from app.tests.factories import auth_headers, make_booking, make_patient

NOT_LIVE = (BookingStatus.PENDING, BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.DECLINED)


@pytest.fixture
def scene(db, client):
    owner, patient, booking, plan = started_plan(client, db)
    return {"owner": owner, "patient": patient, "booking": booking, "plan": plan, "task": plan["tasks"][0]["id"],
            "version": plan["version"]}


def _routes(scene):
    """Every B2 route, as (method, path, json)."""
    plan_id, task_id, v = scene["plan"]["id"], scene["task"], scene["version"]
    return {
        "add_task": ("POST", f"{BASE}/care-plans/{plan_id}/tasks", daily_task("x")),
        "patch_task": ("PATCH", f"{BASE}/care-plan-tasks/{task_id}", {"expected_version": v, "title": "x"}),
        "delete_task": ("DELETE", f"{BASE}/care-plan-tasks/{task_id}?expected_version={v}", None),
        "day_view": ("GET", f"{BASE}/care-plans/{plan_id}/tasks", None),
        "tick": ("PUT", f"{BASE}/care-plan-tasks/{task_id}/completion", {"date": today_iso(), "done": True}),
        "untick": ("PUT", f"{BASE}/care-plan-tasks/{task_id}/completion", {"date": today_iso(), "done": False}),
        "list_notes": ("GET", f"{BASE}/care-plans/{plan_id}/observations", None),
        "add_note": ("POST", f"{BASE}/care-plans/{plan_id}/observations", {"body": SECRET_NOTE}),
        "progress": ("GET", f"{BASE}/care-plans/{plan_id}/progress", None),
    }


def _call(client, caregiver, request):
    method, path, body = request
    return client.request(method, path, json=body, headers=h(caregiver))


def _assert_no_access(client, caregiver, scene):
    for name, request in _routes(scene).items():
        r = _call(client, caregiver, request)
        assert r.status_code == 404 and r.json() == NOT_FOUND, (name, r.status_code, r.text)
        for secret in SECRET_TEXTS + (SECRET_NOTE, "Placeholder Client"):
            assert secret not in r.text


@pytest.mark.parametrize("status", NOT_LIVE)
def test_a_booking_that_is_not_live_gives_no_access(db, client, scene, status):
    other = make_nurse(db)
    make_booking(db, scene["patient"], other, status=status, booking_for="myself")
    _assert_no_access(client, other, scene)


def test_a_caregiver_with_no_booking_for_the_client_sees_nothing(db, client, scene):
    stranger = make_nurse(db)
    make_booking(db, make_patient(db), stranger, status=BookingStatus.IN_PROGRESS)  # a live visit, for someone else
    _assert_no_access(client, stranger, scene)


def test_a_caregiver_only_offered_the_visit_sees_nothing(db, client, scene):
    offered = make_nurse(db)
    booking = make_booking(db, scene["patient"], None, status=BookingStatus.PENDING, booking_for="myself")
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=offered.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    _assert_no_access(client, offered, scene)


def test_a_caregiver_who_dropped_out_loses_access(db, client, scene):
    other = colleague(db, scene["patient"], status=BookingStatus.ACCEPTED)
    assert client.get(f"{BASE}/care-plans/{scene['plan']['id']}/progress", headers=h(other)).status_code == 200
    booking = db.query(Booking).filter_by(caregiver_id=other.id).one()
    booking.caregiver_id, booking.status = None, BookingStatus.PENDING
    db.commit()
    _assert_no_access(client, other, scene)


def test_access_ends_when_the_visit_completes_even_for_a_note_or_a_tick(db, client, scene):
    scene["booking"].status = BookingStatus.COMPLETED
    db.commit()
    _assert_no_access(client, scene["owner"], scene)


def test_an_erased_client_is_not_shown(db, client, scene):
    patient = db.get(Patient, scene["patient"].id)
    patient.full_name, patient.is_active = "Deleted user", False
    db.commit()
    _assert_no_access(client, scene["owner"], scene)


def test_a_suspended_caregiver_is_limited_to_the_visit_in_progress(db, client, scene):
    nurse = scene["owner"]
    nurse.profile_status = "suspended"
    db.commit()
    assert client.get(f"{BASE}/care-plans/{scene['plan']['id']}/progress", headers=h(nurse)).status_code == 200  # in progress
    scene["booking"].status = BookingStatus.EN_ROUTE
    db.commit()
    _assert_no_access(client, nurse, scene)


def test_unknown_ids_are_the_same_404_as_a_forbidden_one(db, client, scene):
    nurse = scene["owner"]
    for method, path, body in (
        ("GET", f"{BASE}/care-plans/{uuid.uuid4()}/tasks", None),
        ("POST", f"{BASE}/care-plans/{uuid.uuid4()}/tasks", daily_task("x")),
        ("PATCH", f"{BASE}/care-plan-tasks/{uuid.uuid4()}", {"expected_version": 1, "title": "x"}),
        ("DELETE", f"{BASE}/care-plan-tasks/{uuid.uuid4()}?expected_version=1", None),
        ("PUT", f"{BASE}/care-plan-tasks/{uuid.uuid4()}/completion", {"date": today_iso(), "done": True}),
        ("GET", f"{BASE}/care-plans/{uuid.uuid4()}/observations", None),
        ("POST", f"{BASE}/care-plans/{uuid.uuid4()}/observations", {"body": "x"}),
        ("GET", f"{BASE}/care-plans/{uuid.uuid4()}/progress", None),
    ):
        r = client.request(method, path, json=body, headers=h(nurse))
        assert r.status_code == 404 and r.json() == NOT_FOUND, (method, path)


def test_a_task_of_another_clients_plan_is_not_reachable_through_a_plan_you_can_see(db, client, scene):
    """The task id decides the plan, not the URL: a task of a plan you cannot see stays hidden."""
    other_nurse, _, _, other_plan = started_plan(client, db, tasks=[daily_task("Placeholder other task")])
    other_task = other_plan["tasks"][0]["id"]
    for method, body in (("PATCH", {"expected_version": 1, "title": "x"}), ("DELETE", None)):
        suffix = "?expected_version=1" if method == "DELETE" else ""
        r = client.request(method, f"{BASE}/care-plan-tasks/{other_task}{suffix}", json=body, headers=h(scene["owner"]))
        assert r.status_code == 404 and r.json() == NOT_FOUND
    r = client.put(f"{BASE}/care-plan-tasks/{other_task}/completion", json={"date": today_iso(), "done": True},
                   headers=h(scene["owner"]))
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_a_patient_token_is_403_and_no_token_is_401(db, client, scene):
    patient_headers = auth_headers(scene["patient"].user)
    for name, (method, path, body) in _routes(scene).items():
        assert client.request(method, path, json=body, headers=patient_headers).status_code == 403, name
        assert client.request(method, path, json=body).status_code == 401, name


def test_who_can_do_what_through_a_live_visit(db, client, scene):
    plain_waiting = colleague(db, scene["patient"], status=BookingStatus.ACCEPTED)
    plain_active = colleague(db, scene["patient"], status=BookingStatus.IN_PROGRESS)
    outcome = {}
    for who, caregiver in (("owner", scene["owner"]), ("plain_waiting", plain_waiting), ("plain_active", plain_active)):
        outcome[who] = {name: _call(client, caregiver, request).status_code for name, request in _routes(scene).items()
                        if name in ("add_task", "day_view", "tick", "untick", "list_notes", "add_note", "progress")}
    reads = {"day_view": 200, "list_notes": 200, "progress": 200}
    assert outcome["owner"] == {**reads, "add_task": 201, "tick": 200, "untick": 200, "add_note": 201}
    assert outcome["plain_active"] == {**reads, "add_task": 403, "tick": 200, "untick": 200, "add_note": 201}
    assert outcome["plain_waiting"] == {**reads, "add_task": 403, "tick": 409, "untick": 409, "add_note": 409}
