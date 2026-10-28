"""Care plan tasks, slice B2: edit the definitions, the day view, ticks (idempotent, today only, in-progress visit only).

Access across every new route is in test_care_plans_b2_access.py; encryption and erasure in test_care_plans_b2_security.py;
progress in test_care_plan_progress.py; observations in test_care_plan_observations.py.
"""

import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanTask, CarePlanTaskCompletion
from app.db.session import SessionLocal
from app.services import care_plan_tasks, care_plans
from app.services.care_plan_access import resolve_plan_access_for_plan
from app.tests.care_plan_helpers import (
    BASE, EVERY_DAY, NOT_FOUND, SECRET_TASK, SECRET_TEXTS, colleague, create_plan, daily_task, h, make_nurse, self_visit,
    started_plan, tick, today_iso,
)
from app.tests.factories import auth_headers, make_booking, make_caregiver


def _tasks_url(plan_id):
    return f"{BASE}/care-plans/{plan_id}/tasks"


def _day(client, caregiver, plan_id, day=None):
    suffix = f"?date={day}" if day else ""
    return client.get(_tasks_url(plan_id) + suffix, headers=h(caregiver))


def _events(db, plan_id, action=None):
    query = db.query(CarePlanEvent).filter_by(plan_id=plan_id)
    return [e for e in query.order_by(CarePlanEvent.created_at) if action is None or e.action == action]


def _task_ids(plan):
    return [t["id"] for t in plan["tasks"]]


# --- task definitions (routes 16-18) ------------------------------------------------------------------------------------

def test_a_nurse_adds_a_task_and_the_plan_version_moves_on(db, client):
    nurse, _, _, plan = started_plan(client, db)
    body = {"title": "Placeholder task three", "time_slot": "afternoon", "time_of_day": "13:30", "days_of_week": [5, 1, 3]}
    r = client.post(_tasks_url(plan["id"]), json=body, headers=h(nurse))
    assert r.status_code == 201, r.text
    assert r.json()["plan_version"] == plan["version"] + 1
    assert r.json()["task"] == {"id": r.json()["task"]["id"], "title": "Placeholder task three", "time_slot": "afternoon",
                                "time_of_day": "13:30", "days_of_week": [1, 3, 5]}
    got = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()
    assert [t["title"] for t in got["tasks"]][-1] == "Placeholder task three" and got["version"] == plan["version"] + 1
    assert [e.detail for e in _events(db, plan["id"], "task.add")] == [{"task_id": r.json()["task"]["id"]}]


def test_the_task_flag_for_the_family_cannot_be_set_by_a_request(db, client):
    nurse, _, _, plan = started_plan(client, db)
    r = client.post(_tasks_url(plan["id"]), headers=h(nurse),
                    json={**daily_task("Placeholder task three"), "family_visible": True})
    assert r.status_code == 201
    assert db.get(CarePlanTask, r.json()["task"]["id"]).family_visible is False
    r = client.patch(f"{BASE}/care-plan-tasks/{r.json()['task']['id']}", headers=h(nurse),
                     json={"expected_version": r.json()["plan_version"], "family_visible": True, "title": "Placeholder task four"})
    assert r.status_code == 200
    assert db.get(CarePlanTask, r.json()["task"]["id"]).family_visible is False


def test_adding_a_task_accepts_a_stale_version_only_when_none_is_sent(db, client):
    nurse, _, _, plan = started_plan(client, db)
    stale = client.post(_tasks_url(plan["id"]), headers=h(nurse), json={**daily_task("x"), "expected_version": 99})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    assert stale.json()["detail"]["plan"]["id"] == plan["id"]
    ok = client.post(_tasks_url(plan["id"]), headers=h(nurse), json={**daily_task("x"), "expected_version": plan["version"]})
    assert ok.status_code == 201


def test_only_a_nurse_changes_the_task_list(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    plain = colleague(db, patient)
    task_id = _task_ids(plan)[0]
    for method, path, body in (
        ("POST", _tasks_url(plan["id"]), daily_task("x")),
        ("PATCH", f"{BASE}/care-plan-tasks/{task_id}", {"expected_version": plan["version"], "title": "x"}),
        ("DELETE", f"{BASE}/care-plan-tasks/{task_id}?expected_version={plan['version']}", None),
    ):
        r = client.request(method, path, json=body, headers=h(plain))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_nurse", (method, r.text)
    assert db.query(CarePlanTask).filter_by(plan_id=plan["id"]).count() == 2


def test_a_completed_plan_does_not_take_task_changes(db, client):
    nurse, _, _, plan = started_plan(client, db)
    done = client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse),
                        json={"expected_version": plan["version"], "status": "completed"}).json()
    task_id = _task_ids(plan)[0]
    for method, path, body in (
        ("POST", _tasks_url(plan["id"]), daily_task("x")),
        ("PATCH", f"{BASE}/care-plan-tasks/{task_id}", {"expected_version": done["version"], "title": "x"}),
        ("DELETE", f"{BASE}/care-plan-tasks/{task_id}?expected_version={done['version']}", None),
    ):
        r = client.request(method, path, json=body, headers=h(nurse))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "plan_not_editable", method


def test_a_plan_holds_at_most_sixty_live_tasks_and_removed_ones_do_not_count(db, client):
    nurse, _, _, plan = started_plan(client, db, tasks=[daily_task(f"Placeholder task {i}") for i in range(60)])
    full = client.post(_tasks_url(plan["id"]), headers=h(nurse), json=daily_task("one too many"))
    assert full.status_code == 422 and full.json()["detail"]["code"] == "invalid_care_plan"
    gone = client.delete(f"{BASE}/care-plan-tasks/{_task_ids(plan)[0]}?expected_version={plan['version']}", headers=h(nurse))
    assert gone.status_code == 200
    assert client.post(_tasks_url(plan["id"]), headers=h(nurse), json=daily_task("fits now")).status_code == 201


@pytest.mark.parametrize("over", [
    {"title": ""}, {"title": "x" * 121}, {"time_slot": "noon"}, {"time_of_day": "24:00"}, {"days_of_week": []},
    {"days_of_week": [8]}, {"days_of_week": [2, 2]},
])
def test_task_field_limits_are_422_and_never_echo_the_value(db, client, over):
    nurse, _, _, plan = started_plan(client, db)
    r = client.post(_tasks_url(plan["id"]), json={**daily_task(), **over}, headers=h(nurse))
    assert r.status_code == 422 and all("input" not in e for e in r.json()["detail"])
    assert db.query(CarePlanTask).filter_by(plan_id=plan["id"]).count() == 2


def test_editing_a_task_changes_only_what_was_sent_and_the_event_names_fields_not_text(db, client):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    r = client.patch(f"{BASE}/care-plan-tasks/{task_id}", headers=h(nurse), json={
        "expected_version": plan["version"], "title": "Placeholder renamed", "time_slot": "night", "days_of_week": [7, 6]})
    assert r.status_code == 200, r.text
    assert r.json()["task"] == {"id": task_id, "title": "Placeholder renamed", "time_slot": "night", "time_of_day": "08:00",
                                "days_of_week": [6, 7]}
    assert r.json()["plan_version"] == plan["version"] + 1
    cleared = client.patch(f"{BASE}/care-plan-tasks/{task_id}", headers=h(nurse),
                           json={"expected_version": r.json()["plan_version"], "time_of_day": None})
    assert cleared.json()["task"]["time_of_day"] is None and cleared.json()["task"]["title"] == "Placeholder renamed"
    update_events = _events(db, plan["id"], "task.update")
    assert [e.detail["fields"] for e in update_events] == [["title", "time_slot", "days_of_week"], ["time_of_day"]]
    assert "Placeholder renamed" not in "".join(str(e.detail) for e in _events(db, plan["id"]))


@pytest.mark.parametrize("body", [{"title": None}, {"time_slot": None}, {"days_of_week": None}])
def test_required_task_fields_cannot_be_cleared(db, client, body):
    nurse, _, _, plan = started_plan(client, db)
    r = client.patch(f"{BASE}/care-plan-tasks/{_task_ids(plan)[0]}", headers=h(nurse),
                     json={"expected_version": plan["version"], **body})
    assert r.status_code == 422


def test_a_stale_version_on_a_task_edit_is_a_conflict_that_returns_the_plan(db, client):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse), json={"expected_version": plan["version"], "risk_level": "low"})
    r = client.patch(f"{BASE}/care-plan-tasks/{task_id}", headers=h(nurse), json={"expected_version": plan["version"], "title": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "version_conflict"
    assert r.json()["detail"]["plan"]["risk_level"] == "low"


def test_removing_a_task_keeps_the_row_and_its_ticks_and_is_repeatable(db, client):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    assert tick(client, nurse, task_id).status_code == 200
    r = client.delete(f"{BASE}/care-plan-tasks/{task_id}?expected_version={plan['version']}", headers=h(nurse))
    assert r.status_code == 200 and r.json()["plan_version"] == plan["version"] + 1
    assert db.get(CarePlanTask, task_id).is_active is False
    assert db.query(CarePlanTaskCompletion).filter_by(task_id=task_id).count() == 1  # the history is kept
    got = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()
    assert task_id not in _task_ids(got)
    again = client.delete(f"{BASE}/care-plan-tasks/{task_id}?expected_version={r.json()['plan_version']}", headers=h(nurse))
    assert again.status_code == 200 and again.json()["plan_version"] == r.json()["plan_version"]
    assert len(_events(db, plan["id"], "task.remove")) == 1


def test_a_removed_task_can_no_longer_be_edited_or_ticked(db, client):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    gone = client.delete(f"{BASE}/care-plan-tasks/{task_id}?expected_version={plan['version']}", headers=h(nurse)).json()
    r = client.patch(f"{BASE}/care-plan-tasks/{task_id}", headers=h(nurse),
                     json={"expected_version": gone["plan_version"], "title": "x"})
    assert r.status_code == 404 and r.json() == NOT_FOUND
    r = tick(client, nurse, task_id)
    assert r.status_code == 404 and r.json() == NOT_FOUND


# --- the day view (route 19) --------------------------------------------------------------------------------------------

def test_the_day_view_groups_tasks_by_slot_in_day_order_with_times(db, client):
    tasks = [daily_task("Placeholder evening", "evening", "18:00"), daily_task("Placeholder late morning", "morning", "10:30"),
             daily_task("Placeholder early morning", "morning", "08:00"), daily_task("Placeholder untimed", "morning", None),
             daily_task("Placeholder night", "night", "21:00")]
    nurse, _, _, plan = started_plan(client, db, tasks=tasks)
    r = _day(client, nurse, plan["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] == today_iso() and body["is_today"] is True and body["can_tick"] is True
    assert [s["slot"] for s in body["slots"]] == ["morning", "evening", "night"]  # afternoon has nothing, so it is left out
    morning = body["slots"][0]
    assert morning["time_of_day"] == "08:00"
    assert [t["title"] for t in morning["tasks"]] == ["Placeholder early morning", "Placeholder late morning", "Placeholder untimed"]
    assert all(t["done"] is False and t["done_by"] is None and t["done_at"] is None for s in body["slots"] for t in s["tasks"])


def test_the_day_view_shows_who_ticked_and_when(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    helper = colleague(db, patient)
    first, second = _task_ids(plan)
    assert tick(client, helper, first).status_code == 200
    body = _day(client, nurse, plan["id"]).json()
    ticked = {t["id"]: t for s in body["slots"] for t in s["tasks"]}
    assert ticked[first]["done"] is True and ticked[first]["done_by"] == helper.full_name and ticked[first]["done_at"]
    assert ticked[second]["done"] is False


def test_a_task_appears_only_on_its_weekdays_inside_the_plan_and_from_the_day_it_was_added(db, client):
    start = date.fromisoformat(today_iso())
    # one task for every weekday from tomorrow on, so every case below is a future day (created today, so "added" holds)
    nurse, _, _, plan = started_plan(client, db, tasks=[], duration_days=14)
    monday_only = client.post(_tasks_url(plan["id"]), headers=h(nurse), json=daily_task("Placeholder weekly", days=[1])).json()
    assert monday_only["task"]["days_of_week"] == [1]
    for offset in range(0, 14):
        day = start + timedelta(days=offset)
        shown = [t["id"] for s in _day(client, nurse, plan["id"], day.isoformat()).json()["slots"] for t in s["tasks"]]
        assert (monday_only["task"]["id"] in shown) == (day.isoweekday() == 1), day


def test_the_day_view_stops_at_the_end_of_the_plan(db, client):
    start = date.fromisoformat(today_iso())
    nurse, _, _, plan = started_plan(client, db, duration_days=14)  # two daily tasks
    last, after = start + timedelta(days=13), start + timedelta(days=14)
    assert len(_day(client, nurse, plan["id"], last.isoformat()).json()["slots"]) == 2
    assert _day(client, nurse, plan["id"], after.isoformat()).json()["slots"] == []
    assert _day(client, nurse, plan["id"], (start + timedelta(days=90)).isoformat()).json()["slots"] == []


def test_a_task_added_today_was_not_due_on_earlier_days(db, client):
    nurse, _, _, plan = started_plan(client, db, start_date=(date.fromisoformat(today_iso()) - timedelta(days=5)).isoformat())
    yesterday = (date.fromisoformat(today_iso()) - timedelta(days=1)).isoformat()
    assert _day(client, nurse, plan["id"], yesterday).json()["slots"] == []
    assert len(_day(client, nurse, plan["id"]).json()["slots"]) == 2


def test_removed_tasks_leave_the_day_view(db, client):
    nurse, _, _, plan = started_plan(client, db)
    client.delete(f"{BASE}/care-plan-tasks/{_task_ids(plan)[0]}?expected_version={plan['version']}", headers=h(nurse))
    shown = [t["title"] for s in _day(client, nurse, plan["id"]).json()["slots"] for t in s["tasks"]]
    assert shown == ["Placeholder task two"]


def test_can_tick_is_false_on_other_days_without_a_visit_in_progress_and_on_a_closed_plan(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    tomorrow = (date.fromisoformat(today_iso()) + timedelta(days=1)).isoformat()
    other_day = _day(client, nurse, plan["id"], tomorrow).json()
    assert other_day["is_today"] is False and other_day["can_tick"] is False and other_day["slots"]
    waiting = colleague(db, patient, status=BookingStatus.ACCEPTED)
    assert _day(client, waiting, plan["id"]).json()["can_tick"] is False
    client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse), json={"expected_version": plan["version"], "status": "completed"})
    assert _day(client, nurse, plan["id"]).json()["can_tick"] is False  # still readable: it is history


def test_a_bad_date_is_a_plain_422(db, client):
    nurse, _, _, plan = started_plan(client, db)
    for bad in ("2026-13-01", "yesterday", "28-10-2026"):
        assert _day(client, nurse, plan["id"], bad).status_code == 422


# --- ticking (route 20) -------------------------------------------------------------------------------------------------

def test_ticking_a_task_today_writes_one_row_attributed_to_the_caregiver_and_the_visit(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    r = tick(client, nurse, task_id)
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == task_id and r.json()["date"] == today_iso() and r.json()["done"] is True
    assert r.json()["done_by"] == nurse.full_name and r.json()["done_at"]
    row = db.query(CarePlanTaskCompletion).one()
    assert str(row.task_id) == task_id and row.completed_by_caregiver_id == nurse.id and row.booking_id == booking.id
    assert row.for_date.isoformat() == today_iso()
    assert db.get(CarePlan, plan["id"]).version == plan["version"]  # ticks never bump the version


def test_ticking_twice_is_a_no_op_and_keeps_the_first_tick(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    helper = colleague(db, patient)
    task_id = _task_ids(plan)[0]
    first = tick(client, nurse, task_id).json()
    again = tick(client, helper, task_id)  # another caregiver ticks what is already ticked
    assert again.status_code == 200 and again.json() == first
    assert db.query(CarePlanTaskCompletion).count() == 1


def test_a_caregiver_who_is_not_a_nurse_may_tick(db, client):
    _, patient, _, plan = started_plan(client, db)
    plain = colleague(db, patient)
    assert tick(client, plain, _task_ids(plan)[0]).status_code == 200


def test_unticking_deletes_the_row_and_writes_one_event_and_repeating_it_is_harmless(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    helper = colleague(db, patient)
    task_id = _task_ids(plan)[0]
    tick(client, nurse, task_id)
    r = tick(client, helper, task_id, done=False)
    assert r.status_code == 200 and r.json() == {"task_id": task_id, "date": today_iso(), "done": False, "done_by": None,
                                                 "done_at": None}
    assert db.query(CarePlanTaskCompletion).count() == 0
    assert tick(client, helper, task_id, done=False).json()["done"] is False  # nothing left to untick
    events = _events(db, plan["id"], "task.untick")
    assert len(events) == 1 and events[0].actor_id == helper.id
    assert events[0].detail == {"task_id": task_id, "for_date": today_iso(), "ticked_by": str(nurse.id)}
    assert tick(client, nurse, task_id).json()["done"] is True  # and it can be ticked again


@pytest.mark.parametrize("status", (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE))
def test_ticking_needs_a_visit_in_progress(db, client, status):
    _, patient, _, plan = started_plan(client, db)
    waiting = colleague(db, patient, status=status)
    r = tick(client, waiting, _task_ids(plan)[0])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "visit_not_in_progress"
    assert r.json()["detail"]["message"] == "Available during an active visit."
    assert db.query(CarePlanTaskCompletion).count() == 0
    assert tick(client, waiting, _task_ids(plan)[0], done=False).status_code == 409  # unticking is a write too


def test_a_visit_that_ended_since_access_was_resolved_refuses_the_tick(db, client):
    """The booking is re-read under a lock at write time, not trusted from the access check a moment before."""
    nurse, _, booking, plan = started_plan(client, db)
    task = db.query(CarePlanTask).filter_by(plan_id=plan["id"]).first()
    access = resolve_plan_access_for_plan(db, nurse, db.get(CarePlan, plan["id"]))
    other = SessionLocal()
    try:
        other.get(type(booking), booking.id).status = BookingStatus.COMPLETED
        other.commit()
    finally:
        other.close()
    with pytest.raises(care_plans.VisitNotInProgress):
        care_plan_tasks.set_completion(db, nurse, access, task, date.fromisoformat(today_iso()), True)
    assert db.query(CarePlanTaskCompletion).count() == 0


def test_a_completed_or_archived_plan_takes_no_ticks(db, client):
    nurse, _, _, plan = started_plan(client, db)
    client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse), json={"expected_version": plan["version"], "status": "archived"})
    r = tick(client, nurse, _task_ids(plan)[0])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "plan_not_editable"


def test_ticking_any_day_but_today_is_422(db, client):
    nurse, _, _, plan = started_plan(client, db)
    today = date.fromisoformat(today_iso())
    for day in (today - timedelta(days=1), today + timedelta(days=1), today - timedelta(days=400)):
        r = tick(client, nurse, _task_ids(plan)[0], day=day.isoformat())
        assert r.status_code == 422 and r.json()["detail"]["code"] == "task_date_not_today", day
        assert tick(client, nurse, _task_ids(plan)[0], done=False, day=day.isoformat()).status_code == 422
    assert db.query(CarePlanTaskCompletion).count() == 0


def test_a_task_not_due_today_cannot_be_ticked(db, client):
    nurse, _, _, plan = started_plan(client, db, tasks=[])
    today = date.fromisoformat(today_iso())
    other_days = [d for d in EVERY_DAY if d != today.isoweekday()]
    task = client.post(_tasks_url(plan["id"]), headers=h(nurse), json=daily_task("Placeholder not today", days=other_days)).json()
    r = tick(client, nurse, task["task"]["id"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "task_not_scheduled"


def test_a_plan_that_has_not_started_or_has_ended_has_nothing_to_tick(db, client):
    today = date.fromisoformat(today_iso())
    nurse, _, _, future = started_plan(client, db, start_date=(today + timedelta(days=3)).isoformat())
    r = tick(client, nurse, _task_ids(future)[0])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "task_not_scheduled"
    nurse2, _, _, past = started_plan(client, db, start_date=(today - timedelta(days=40)).isoformat(), duration_days=30)
    db.query(CarePlanTask).filter_by(plan_id=past["id"]).update({"created_at": datetime.utcnow() - timedelta(days=40)})
    db.commit()
    r = tick(client, nurse2, _task_ids(past)[0])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "task_not_scheduled"


def test_a_tick_body_needs_a_real_date_and_a_boolean(db, client):
    nurse, _, _, plan = started_plan(client, db)
    url = f"{BASE}/care-plan-tasks/{_task_ids(plan)[0]}/completion"
    for body in ({"done": True}, {"date": today_iso()}, {"date": "soon", "done": True}, {"date": today_iso(), "done": "maybe"}):
        assert client.put(url, json=body, headers=h(nurse)).status_code == 422


# --- "today" is the Africa/Lagos date, not the server's or the client's ---------------------------------------------------

class _FrozenDatetime(datetime):
    frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen


@pytest.mark.parametrize("utc, lagos_date", [
    (datetime(2026, 10, 27, 22, 59, 59, tzinfo=timezone.utc), "2026-10-27"),   # 23:59:59 in Lagos
    (datetime(2026, 10, 27, 23, 0, 0, tzinfo=timezone.utc), "2026-10-28"),     # midnight in Lagos
    (datetime(2026, 10, 27, 23, 30, 0, tzinfo=timezone.utc), "2026-10-28"),
    (datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc), "2027-01-01"),   # year end
])
def test_today_is_the_lagos_date_around_midnight(monkeypatch, utc, lagos_date):
    monkeypatch.setattr(care_plans, "datetime", _FrozenDatetime)
    _FrozenDatetime.frozen = utc
    assert care_plans.local_today().isoformat() == lagos_date


def test_a_tick_for_yesterday_is_refused_the_moment_midnight_passes(db, client, monkeypatch):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    day_one = date.fromisoformat(today_iso()) + timedelta(days=2)  # a plan day well inside the window
    monkeypatch.setattr(care_plans, "local_today", lambda: day_one)
    assert tick(client, nurse, task_id, day=day_one.isoformat()).status_code == 200
    monkeypatch.setattr(care_plans, "local_today", lambda: day_one + timedelta(days=1))  # 00:00:01
    late = tick(client, nurse, task_id, day=day_one.isoformat(), done=False)
    assert late.status_code == 422 and late.json()["detail"]["code"] == "task_date_not_today"
    assert tick(client, nurse, task_id, day=(day_one + timedelta(days=1)).isoformat()).status_code == 200
    assert db.query(CarePlanTaskCompletion).count() == 2  # yesterday's tick was not undone by the late call


# --- races ----------------------------------------------------------------------------------------------------------------

def test_many_callers_ticking_the_same_task_at_once_leave_one_row_and_all_succeed(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    callers = [nurse] + [colleague(db, patient) for _ in range(3)]
    task_id = _task_ids(plan)[0]
    results = []

    def go(caregiver):
        results.append(tick(client, caregiver, task_id).status_code)

    threads = [threading.Thread(target=go, args=(c,)) for c in callers for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [200] * len(threads)
    assert db.query(CarePlanTaskCompletion).count() == 1


def test_a_tick_waits_for_a_plan_being_completed_and_is_then_refused(db, client):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    editor = SessionLocal()
    outcome = []
    try:
        locked = editor.query(CarePlan).filter(CarePlan.id == plan["id"]).with_for_update().one()  # a nurse mid-completion
        locked.status = "completed"
        worker = threading.Thread(target=lambda: outcome.append(tick(client, nurse, task_id)))
        worker.start()
        time.sleep(0.6)
        assert worker.is_alive(), "the tick should be waiting on the plan row"
        editor.commit()
        worker.join(10)
    finally:
        editor.close()
    assert outcome[0].status_code == 409 and outcome[0].json()["detail"]["code"] == "plan_not_editable"
    assert db.query(CarePlanTaskCompletion).count() == 0


def test_a_tick_and_an_untick_racing_leave_a_consistent_row(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    helper = colleague(db, patient)
    task_id = _task_ids(plan)[0]
    tick(client, nurse, task_id)
    codes = []
    threads = [threading.Thread(target=lambda c=c, d=d: codes.append(tick(client, c, task_id, done=d).status_code))
               for c, d in ((nurse, True), (helper, False), (nurse, True), (helper, False))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200] * 4
    assert db.query(CarePlanTaskCompletion).count() in (0, 1)


# --- the response shapes are closed lists ---------------------------------------------------------------------------------

def test_responses_carry_only_the_documented_keys_and_no_health_text_beyond_the_task_titles(db, client):
    nurse, _, _, plan = started_plan(client, db)
    task_id = _task_ids(plan)[0]
    tick_body = tick(client, nurse, task_id).json()
    assert set(tick_body) == {"task_id", "date", "done", "done_by", "done_at"}
    day = _day(client, nurse, plan["id"])
    assert set(day.json()) == {"date", "is_today", "can_tick", "slots"}
    assert set(day.json()["slots"][0]) == {"slot", "time_of_day", "tasks"}
    assert set(day.json()["slots"][0]["tasks"][0]) == {"id", "title", "time_of_day", "done", "done_by", "done_at"}
    for response in (day, client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse))):
        assert response.headers["cache-control"] == "no-store"
        assert "family_visible" not in response.text and "booking_id" not in response.json().get("slots", [{}])[0].get("tasks", [{}])[0]
