"""Writes the live captures behind the B2 part (ticks, notes, progress) of the care plans section of the caregiver FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness), after the B1 capture:

    CAPTURE_CARE_PLANS=1 poetry run pytest app/tests/test_capture_care_plans.py app/tests/test_capture_care_plans_b2.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver`.
Files `230`-`238`. All data is placeholder. Progress history (the `236-a` capture) is set up with direct inserts because
the API cannot tick a past day; the response itself is real.
"""

import os
from datetime import date, datetime, time, timedelta

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan, CarePlanTask, CarePlanTaskCompletion
from app.services.care_plans import local_today
from app.tests.care_plan_helpers import BASE, make_nurse
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.test_capture_care_plans import OUT, _save

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CARE_PLANS"), reason="capture run only")


def test_capture_b2(db, client):
    today = local_today()
    nurse = make_nurse(db, full_name="Placeholder Nurse")
    helper = make_caregiver(db, full_name="Placeholder Caregiver")
    waiting = make_caregiver(db, full_name="Placeholder Caregiver Two")
    hn, hh, hw = (auth_headers(c.user) for c in (nurse, helper, waiting))

    client_one = make_patient(db, full_name="Placeholder Client", gender="female",
                              date_of_birth=today - timedelta(days=71 * 366))
    visit = make_booking(db, client_one, nurse, status=BookingStatus.IN_PROGRESS, booking_for="myself",
                         actual_start_time=datetime.utcnow())
    make_booking(db, client_one, helper, status=BookingStatus.IN_PROGRESS, booking_for="myself",
                 actual_start_time=datetime.utcnow())
    make_booking(db, client_one, waiting, status=BookingStatus.ACCEPTED, booking_for="myself")

    plan = client.post(f"{BASE}/bookings/{visit.id}/care-plan", headers=hn, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "medium", "start_date": today.isoformat(), "duration_days": 30,
        "review_every_days": 14,
        "goals": [{"title": "Placeholder goal", "frequency": "daily"}],
        "tasks": [{"title": "Placeholder morning task", "time_slot": "morning", "time_of_day": "08:00", "days_of_week": [1, 2, 3, 4, 5, 6, 7]},
                  {"title": "Placeholder evening task", "time_slot": "evening", "time_of_day": "18:00", "days_of_week": [1, 2, 3, 4, 5, 6, 7]}],
    }).json()
    pid = plan["id"]
    morning, evening = (t["id"] for t in plan["tasks"])
    tasks_url = f"{BASE}/care-plans/{pid}/tasks"

    _save("237-a-get-plan-with-capabilities.json", client.get(f"{BASE}/care-plans/{pid}", headers=hn),
          "`capabilities` now has three flags. `can_tick_tasks` and `can_add_observation` are true only while the caller has "
          "a visit for this client in progress and the plan is active.")
    _save("237-b-get-plan-waiting-caregiver.json", client.get(f"{BASE}/care-plans/{pid}", headers=hw),
          "A caregiver whose visit is only accepted: can read, cannot tick or write notes yet.")

    # --- task definitions
    added = _save("230-a-task-add.json", client.post(tasks_url, headers=hn, json={
        "title": "Placeholder afternoon task", "time_slot": "afternoon", "time_of_day": "13:00", "days_of_week": [1, 3, 5],
        "expected_version": plan["version"]}), "`expected_version` is optional when adding.")
    afternoon, version = added.json()["task"]["id"], added.json()["plan_version"]
    _save("230-b-task-add-not-a-nurse.json", client.post(tasks_url, headers=hh, json={
        "title": "Placeholder task", "time_slot": "morning", "days_of_week": [1]}))
    _save("230-c-task-add-validation.json", client.post(tasks_url, headers=hn, json={
        "title": "", "time_slot": "noon", "days_of_week": [1, 1]}), "Field limits: the standard 422 list, no value echoed.")
    edited = _save("231-a-task-edit.json", client.patch(f"{BASE}/care-plan-tasks/{afternoon}", headers=hn, json={
        "expected_version": version, "title": "Placeholder afternoon walk", "days_of_week": [1, 2, 3, 4, 5], "time_of_day": None}),
        "`time_of_day: null` clears the display time. Only the fields sent change.")
    _save("231-b-task-edit-version-conflict.json", client.patch(f"{BASE}/care-plan-tasks/{afternoon}", headers=hn, json={
        "expected_version": version, "title": "Placeholder other title"}),
        "Stale `expected_version`: the body carries the current plan (trimmed in the guide).")
    version = edited.json()["plan_version"]

    # --- the day view and ticks
    _save("233-a-day-view.json", client.get(tasks_url, headers=hn), "Today, nothing ticked yet.")
    tomorrow = (today + timedelta(days=1)).isoformat()
    _save("233-b-day-view-tomorrow.json", client.get(f"{tasks_url}?date={tomorrow}", headers=hn),
          "Another day can be read but not ticked: `is_today` and `can_tick` are false.")
    _save("233-c-day-view-waiting-caregiver.json", client.get(tasks_url, headers=hw), "`can_tick` is false: the visit is not in progress.")
    _save("234-a-tick.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hn,
                                        json={"date": today.isoformat(), "done": True}))
    _save("234-b-tick-again.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hh,
                                              json={"date": today.isoformat(), "done": True}),
          "Idempotent: another caregiver ticking the same task changes nothing; the first tick (who, when) is returned.")
    _save("233-d-day-view-after-tick.json", client.get(tasks_url, headers=hh), "Seen by the other caregiver: `done_by` is the first ticker.")
    _save("234-c-untick.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hh,
                                          json={"date": today.isoformat(), "done": False}))
    _save("234-d-untick-again.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hh,
                                                json={"date": today.isoformat(), "done": False}), "Unticking what is not ticked is a no-op.")
    client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hn, json={"date": today.isoformat(), "done": True})
    _save("234-e-tick-yesterday.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hn, json={
        "date": (today - timedelta(days=1)).isoformat(), "done": True}))
    _save("234-f-tick-visit-not-in-progress.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hw,
                                                              json={"date": today.isoformat(), "done": True}))
    off_days = [d for d in range(1, 8) if d != today.isoweekday()]
    weekly = client.post(tasks_url, headers=hn, json={"title": "Placeholder other-day task", "time_slot": "night",
                                                      "days_of_week": off_days}).json()
    _save("234-g-tick-not-scheduled.json", client.put(f"{BASE}/care-plan-tasks/{weekly['task']['id']}/completion", headers=hn,
                                                      json={"date": today.isoformat(), "done": True}),
          "A task that is not due today (its days exclude today).")
    removed = _save("232-a-task-remove.json", client.delete(
        f"{BASE}/care-plan-tasks/{weekly['task']['id']}?expected_version={weekly['plan_version']}", headers=hn),
        "Removing deactivates the task; it leaves `tasks` and the day view, its ticks are kept.")
    _save("232-b-task-removed-is-404.json", client.patch(f"{BASE}/care-plan-tasks/{weekly['task']['id']}", headers=hn, json={
        "expected_version": removed.json()["plan_version"], "title": "x"}), "A removed task is the plain 404.")

    # --- notes
    first = _save("235-a-note-add.json", client.post(f"{BASE}/care-plans/{pid}/observations", headers=hn, json={
        "body": "Placeholder observation: ate well."}))
    _save("235-b-special-note-add.json", client.post(f"{BASE}/care-plans/{pid}/observations", headers=hh, json={
        "body": "Placeholder special note.", "category": "special_note"}), "Any caregiver on the visit may add a note, not only a nurse.")
    _save("235-c-notes-list.json", client.get(f"{BASE}/care-plans/{pid}/observations", headers=hn), "Newest first.")
    page = _save("235-d-notes-page-1.json", client.get(f"{BASE}/care-plans/{pid}/observations?limit=1", headers=hn),
                 "`limit=1`: `next_before` and `next_before_id` are the cursor for the next, older page.")
    _save("235-e-notes-page-2.json", client.get(f"{BASE}/care-plans/{pid}/observations", headers=hn,
                                                params={"limit": 1, "before": page.json()["next_before"], "before_id": page.json()["next_before_id"]}))
    _save("235-f-notes-special-only.json", client.get(f"{BASE}/care-plans/{pid}/observations?category=special_note", headers=hn))
    _save("235-g-note-visit-not-in-progress.json", client.post(f"{BASE}/care-plans/{pid}/observations", headers=hw, json={
        "body": "Placeholder note"}))
    _save("235-h-note-validation.json", client.post(f"{BASE}/care-plans/{pid}/observations", headers=hn, json={"body": ""}))
    assert first.status_code == 201

    # --- progress
    _save("236-a-progress-today-only.json", client.get(f"{BASE}/care-plans/{pid}/progress", headers=hn),
          "Only today has a visit so far, so only today is counted.")
    start = today - timedelta(days=8)
    row = db.get(CarePlan, pid)
    row.start_date = start
    db.query(CarePlanTask).filter_by(plan_id=pid).update({"created_at": datetime.combine(start, time(6, 0))})
    for offset in (1, 2, 5):
        past = make_booking(db, client_one, nurse, status=BookingStatus.COMPLETED, booking_for="myself",
                            actual_start_time=datetime.combine(start + timedelta(days=offset), time(10, 0)))
        for task_id in (morning, evening) if offset != 5 else (morning,):
            db.add(CarePlanTaskCompletion(task_id=task_id, for_date=start + timedelta(days=offset),
                                          completed_by_caregiver_id=nurse.id, booking_id=past.id))
    db.commit()
    _save("236-b-progress-with-history.json", client.get(f"{BASE}/care-plans/{pid}/progress", headers=hn),
          "Setup note: three earlier visit days were inserted directly (the API cannot tick a past day). Days with no visit "
          "do not count, whatever their weekday.")

    quiet = make_nurse(db, full_name="Placeholder Nurse Two")
    other_client = make_patient(db, full_name="Placeholder Client Two")
    quiet_visit = make_booking(db, other_client, quiet, status=BookingStatus.ACCEPTED, booking_for="myself")
    quiet_plan = client.post(f"{BASE}/bookings/{quiet_visit.id}/care-plan", headers=auth_headers(quiet.user), json={
        "diagnosis": "Placeholder diagnosis two", "risk_level": "low", "start_date": today.isoformat(), "duration_days": 30,
        "tasks": [{"title": "Placeholder task", "time_slot": "morning", "days_of_week": [1, 2, 3, 4, 5, 6, 7]}]}).json()
    _save("236-c-progress-no-visit-yet.json", client.get(f"{BASE}/care-plans/{quiet_plan['id']}/progress", headers=auth_headers(quiet.user)),
          "No visit has started: `overall_percent` is null (show 'No visits yet').")

    # --- access and closed plans
    _save("238-a-tick-no-access.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=auth_headers(quiet.user),
                                                  json={"date": today.isoformat(), "done": True}),
          "A caregiver with no live visit for this client: the same 404 as everywhere.")
    done = client.patch(f"{BASE}/care-plans/{pid}", headers=hn, json={"expected_version": removed.json()["plan_version"], "status": "completed"})
    assert done.status_code == 200, done.text
    _save("238-b-tick-plan-completed.json", client.put(f"{BASE}/care-plan-tasks/{morning}/completion", headers=hn,
                                                       json={"date": today.isoformat(), "done": False}))
    _save("238-c-note-plan-completed.json", client.post(f"{BASE}/care-plans/{pid}/observations", headers=hn, json={"body": "Placeholder late note"}))
    _save("238-d-notes-list-completed-plan.json", client.get(f"{BASE}/care-plans/{pid}/observations?limit=1", headers=hn),
          "A completed plan keeps its notes as history.")
    assert OUT.exists()
