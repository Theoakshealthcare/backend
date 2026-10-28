"""Care plan progress (View Progress): ticked / scheduled task occurrences over the days that had a visit only."""

from datetime import date, datetime, time, timedelta

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan, CarePlanGoal, CarePlanObservation, CarePlanTask, CarePlanTaskCompletion
from app.services.care_plans import local_today
from app.tests.care_plan_helpers import (
    BASE, NOT_FOUND, daily_task, h, make_nurse, relative_visit, self_visit, started_plan, today_iso,
)
from app.tests.factories import make_booking, make_caregiver, make_patient

TODAY = local_today()


def _progress(client, caregiver, plan_id):
    return client.get(f"{BASE}/care-plans/{plan_id}/progress", headers=h(caregiver))


def _backdate(db, plan, days_ago):
    """Make the plan (and its tasks) start `days_ago` days back, so there is history to measure."""
    start = TODAY - timedelta(days=days_ago)
    row = db.get(CarePlan, plan["id"])
    row.start_date = start
    db.query(CarePlanTask).filter_by(plan_id=plan["id"]).update({"created_at": datetime.combine(start, time(6, 0))})
    db.commit()
    return start


def _visit(db, patient, caregiver, day, status=BookingStatus.COMPLETED, hour=10, **over):
    """A visit for the client that started on `day` (10:00 UTC is 11:00 in Lagos)."""
    over.setdefault("booking_for", "myself")
    return make_booking(db, patient, caregiver, status=status, actual_start_time=datetime.combine(day, time(hour, 0)), **over)


def _tick(db, task_id, day, caregiver, booking):
    db.add(CarePlanTaskCompletion(task_id=task_id, for_date=day, completed_by_caregiver_id=caregiver.id, booking_id=booking.id))
    db.commit()


def test_a_plan_with_no_visit_yet_has_nothing_to_measure(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse, status=BookingStatus.ACCEPTED)
    plan = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse), json={
        "diagnosis": "d", "risk_level": "low", "start_date": today_iso(), "duration_days": 30,
        "tasks": [daily_task()]}).json()
    r = _progress(client, nurse, plan["id"])
    assert r.status_code == 200, r.text
    assert r.json() == {"overall_percent": None, "scheduled": 0, "completed": 0, "visit_days": 0, "day": 1, "of_days": 30,
                        "review_due_on": None, "by_day": [], "goals": {"active": 0, "achieved": 0, "discontinued": 0},
                        "last_observation_at": None}
    assert r.headers["cache-control"] == "no-store"


def test_only_days_with_a_visit_count_and_any_caregivers_visit_is_a_visit(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    start = _backdate(db, plan, 6)
    other = make_caregiver(db)
    first, second = [t.id for t in db.query(CarePlanTask).filter_by(plan_id=plan["id"]).order_by(CarePlanTask.sort_order)]
    day1, day3 = start + timedelta(days=1), start + timedelta(days=3)   # visits on two of the six days
    done_1 = _visit(db, patient, other, day1)
    done_3 = _visit(db, patient, nurse, day3)
    _tick(db, first, day1, other, done_1)                                # day 1: 1 of 2
    _tick(db, first, day3, nurse, done_3)
    _tick(db, second, day3, nurse, done_3)                               # day 3: 2 of 2
    booking.actual_start_time = datetime.utcnow()                        # today's visit is in progress: 0 of 2 so far
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["visit_days"] == 3 and body["scheduled"] == 6 and body["completed"] == 3 and body["overall_percent"] == 50
    assert body["by_day"] == [{"date": day1.isoformat(), "scheduled": 2, "completed": 1},
                              {"date": day3.isoformat(), "scheduled": 2, "completed": 2},
                              {"date": TODAY.isoformat(), "scheduled": 2, "completed": 0}]
    assert body["day"] == 7 and body["of_days"] == 30


def test_another_clients_visits_do_not_count(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    _backdate(db, plan, 4)
    booking.actual_start_time = datetime.utcnow()
    _visit(db, make_patient(db), nurse, TODAY - timedelta(days=2))                     # a different client
    _visit(db, patient, nurse, TODAY - timedelta(days=3), booking_for="mother")        # this patient booking for someone else
    db.commit()
    assert _progress(client, nurse, plan["id"]).json()["visit_days"] == 1


def test_a_relatives_plan_counts_visits_for_that_recipient_only(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse), json={
        "diagnosis": "d", "risk_level": "low", "start_date": today_iso(), "duration_days": 30, "tasks": [daily_task()]}).json()
    booking.actual_start_time = datetime.utcnow()
    make_booking(db, booker, nurse, status=BookingStatus.COMPLETED, booking_for="myself",   # the booker's own visit
                 actual_start_time=datetime.utcnow())
    db.commit()
    assert _progress(client, nurse, plan["id"]).json()["visit_days"] == 1


def test_visits_that_never_started_do_not_count(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    _backdate(db, plan, 5)
    booking.actual_start_time = datetime.utcnow()
    for status in (BookingStatus.CANCELLED, BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.PENDING):
        make_booking(db, patient, nurse, status=status, booking_for="myself",
                     actual_start_time=datetime.combine(TODAY - timedelta(days=2), time(10, 0)))
    db.commit()
    assert _progress(client, nurse, plan["id"]).json()["visit_days"] == 1


def test_the_visit_day_is_the_lagos_date_not_the_utc_date(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    start = _backdate(db, plan, 3)
    booking.actual_start_time = datetime.utcnow()
    # 23:30 UTC on `start + 1` is 00:30 in Lagos on `start + 2`
    _visit(db, patient, nurse, start + timedelta(days=1), hour=23).actual_start_time = datetime.combine(
        start + timedelta(days=1), time(23, 30))
    db.commit()
    days = [d["date"] for d in _progress(client, nurse, plan["id"]).json()["by_day"]]
    assert (start + timedelta(days=2)).isoformat() in days and (start + timedelta(days=1)).isoformat() not in days


def test_visits_outside_the_plan_dates_do_not_count(db, client):
    nurse, patient, booking, plan = started_plan(client, db, duration_days=7)
    start = _backdate(db, plan, 10)                     # the plan ran from 10 days ago to 4 days ago
    db.get(CarePlan, plan["id"]).duration_days = 7
    booking.actual_start_time = datetime.utcnow()       # today is after the plan's last day
    _visit(db, patient, nurse, start - timedelta(days=1))   # before it started
    _visit(db, patient, nurse, start + timedelta(days=2))   # inside
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["visit_days"] == 1 and body["day"] == 7 and body["of_days"] == 7  # day is capped at the plan's length


def test_a_task_counts_from_the_day_it_was_added(db, client):
    nurse, patient, booking, plan = started_plan(client, db, tasks=[daily_task()])
    start = _backdate(db, plan, 5)
    booking.actual_start_time = datetime.utcnow()
    for offset in (1, 4):
        _visit(db, patient, nurse, start + timedelta(days=offset))
    late = db.get(CarePlanTask, client.post(f"{BASE}/care-plans/{plan['id']}/tasks", headers=h(nurse),
                                            json=daily_task("Placeholder late task")).json()["task"]["id"])
    late.created_at = datetime.combine(start + timedelta(days=3), time(9, 0))
    db.commit()
    by_day = {d["date"]: d["scheduled"] for d in _progress(client, nurse, plan["id"]).json()["by_day"]}
    assert by_day[(start + timedelta(days=1)).isoformat()] == 1      # only the original task
    assert by_day[(start + timedelta(days=4)).isoformat()] == 2      # both
    assert by_day[TODAY.isoformat()] == 2


def test_a_task_only_counts_on_its_weekdays(db, client):
    nurse, patient, booking, plan = started_plan(client, db, tasks=[daily_task(days=[1])])  # Mondays
    start = _backdate(db, plan, 13)
    booking.actual_start_time = datetime.utcnow()
    for offset in range(0, 13):
        _visit(db, patient, nurse, start + timedelta(days=offset))
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    mondays = [d for d in (start + timedelta(days=o) for o in range(0, 14)) if d.isoweekday() == 1 and d <= TODAY]
    assert body["scheduled"] == len(mondays)
    assert all(d["scheduled"] == (1 if date.fromisoformat(d["date"]).isoweekday() == 1 else 0) for d in body["by_day"])


def test_removed_tasks_leave_both_sides_of_the_fraction(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    start = _backdate(db, plan, 2)
    first, second = [t for t in db.query(CarePlanTask).filter_by(plan_id=plan["id"]).order_by(CarePlanTask.sort_order)]
    visit = _visit(db, patient, nurse, start + timedelta(days=1))
    booking.actual_start_time = datetime.utcnow()
    _tick(db, first.id, start + timedelta(days=1), nurse, visit)
    _tick(db, second.id, start + timedelta(days=1), nurse, visit)
    second.is_active = False
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["scheduled"] == 2 and body["completed"] == 1  # the two visit days x the one live task; one was ticked


def test_a_tick_on_a_day_the_task_is_not_due_cannot_push_the_percentage_over_a_hundred(db, client):
    nurse, patient, booking, plan = started_plan(client, db, tasks=[daily_task(days=[1, 2, 3, 4, 5, 6, 7])])
    start = _backdate(db, plan, 2)
    visit = _visit(db, patient, nurse, start + timedelta(days=1))
    booking.actual_start_time = datetime.utcnow()
    task = db.query(CarePlanTask).filter_by(plan_id=plan["id"]).one()
    _tick(db, task.id, start + timedelta(days=1), nurse, visit)
    task.days_of_week = [d for d in range(1, 8) if d != (start + timedelta(days=1)).isoweekday()]  # edited after the tick
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["completed"] <= body["scheduled"] and body["overall_percent"] <= 100


def test_a_plan_with_no_live_tasks_has_no_percentage_even_with_visits(db, client):
    nurse, patient, booking, plan = started_plan(client, db, tasks=[])
    booking.actual_start_time = datetime.utcnow()
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["visit_days"] == 1 and body["scheduled"] == 0 and body["overall_percent"] is None


def test_only_the_last_fourteen_visit_days_are_listed_oldest_first(db, client):
    nurse, patient, booking, plan = started_plan(client, db, tasks=[daily_task()], duration_days=60)
    start = _backdate(db, plan, 20)
    booking.actual_start_time = datetime.utcnow()
    for offset in range(0, 20):
        _visit(db, patient, nurse, start + timedelta(days=offset))
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["visit_days"] == 21 and len(body["by_day"]) == 14
    dates = [d["date"] for d in body["by_day"]]
    assert dates == sorted(dates) and dates[-1] == TODAY.isoformat()
    assert body["scheduled"] == 21  # the headline number is over all visit days, not just the listed ones


def test_goal_counts_and_the_last_note_time(db, client):
    nurse, _, _, plan = started_plan(client, db)
    for title, status in (("g1", "active"), ("g2", "active"), ("g3", "achieved"), ("g4", "discontinued")):
        db.add(CarePlanGoal(plan_id=plan["id"], title_encrypted="x", frequency="daily", status=status))
    db.commit()
    older = client.post(f"{BASE}/care-plans/{plan['id']}/observations", headers=h(nurse), json={"body": "Placeholder one"}).json()
    newer = client.post(f"{BASE}/care-plans/{plan['id']}/observations", headers=h(nurse), json={"body": "Placeholder two"}).json()
    body = _progress(client, nurse, plan["id"]).json()
    assert body["goals"] == {"active": 3, "achieved": 1, "discontinued": 1}  # the plan's own starter goal is active too
    assert body["last_observation_at"] == newer["created_at"] and older["created_at"] <= newer["created_at"]


def test_review_due_and_the_day_number_before_the_plan_starts(db, client):
    nurse, _, _, plan = started_plan(client, db, start_date=(TODAY + timedelta(days=4)).isoformat(), review_every_days=14)
    body = _progress(client, nurse, plan["id"]).json()
    assert body["day"] == 0 and body["review_due_on"] == (TODAY + timedelta(days=18)).isoformat()
    assert body["visit_days"] == 0 and body["overall_percent"] is None


def test_progress_is_readable_for_a_completed_plan_and_closed_to_strangers(db, client):
    nurse, _, _, plan = started_plan(client, db)
    client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse), json={"expected_version": plan["version"], "status": "completed"})
    assert _progress(client, nurse, plan["id"]).status_code == 200
    stranger = make_nurse(db)
    r = _progress(client, stranger, plan["id"])
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_the_response_is_a_closed_list(db, client):
    nurse, _, _, plan = started_plan(client, db)
    body = _progress(client, nurse, plan["id"]).json()
    assert set(body) == {"overall_percent", "scheduled", "completed", "visit_days", "day", "of_days", "review_due_on", "by_day",
                         "goals", "last_observation_at"}


def test_a_visit_that_runs_past_midnight_makes_both_days_visit_days(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    start = _backdate(db, plan, 4)
    booking.actual_start_time = datetime.utcnow()
    night = _visit(db, patient, nurse, start + timedelta(days=1), hour=21)       # 22:00 in Lagos
    night.actual_start_time = datetime.combine(start + timedelta(days=1), time(21, 0))
    night.actual_end_time = datetime.combine(start + timedelta(days=2), time(5, 0))  # 06:00 the next day
    db.commit()
    days = [d["date"] for d in _progress(client, nurse, plan["id"]).json()["by_day"]]
    assert days == [(start + timedelta(days=1)).isoformat(), (start + timedelta(days=2)).isoformat(), TODAY.isoformat()]


def test_a_visit_never_closed_counts_for_at_most_a_day(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    start = _backdate(db, plan, 6)
    booking.actual_start_time = datetime.utcnow()
    forgotten = _visit(db, patient, nurse, start + timedelta(days=1), status=BookingStatus.IN_PROGRESS, hour=9)
    forgotten.actual_start_time = datetime.combine(start + timedelta(days=1), time(9, 0))
    db.commit()
    days = [d["date"] for d in _progress(client, nurse, plan["id"]).json()["by_day"]]
    # 24 hours from 09:00 UTC reaches the next Lagos day; it does not run on for the days after
    assert days == [(start + timedelta(days=1)).isoformat(), (start + timedelta(days=2)).isoformat(), TODAY.isoformat()]


def test_a_finished_plan_stops_counting_the_day_it_was_closed(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    start = _backdate(db, plan, 6)
    booking.actual_start_time = datetime.utcnow()
    _visit(db, patient, nurse, start + timedelta(days=1))
    _visit(db, patient, nurse, start + timedelta(days=4))
    db.get(CarePlan, plan["id"]).completed_at = datetime.combine(start + timedelta(days=2), time(12, 0))
    db.get(CarePlan, plan["id"]).status = "completed"
    db.commit()
    body = _progress(client, nurse, plan["id"]).json()
    assert [d["date"] for d in body["by_day"]] == [(start + timedelta(days=1)).isoformat()]
