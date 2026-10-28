"""Writes the live captures behind the B3 part (medications) of the care plans section of the caregiver FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness), after the B1 and B2 captures:

    CAPTURE_CARE_PLANS=1 poetry run pytest app/tests/test_capture_care_plans.py app/tests/test_capture_care_plans_b2.py \
        app/tests/test_capture_care_plans_b3.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver`.
Files `240`-`244`. All data is placeholder; the medicines are invented names, not real products or doses.
"""

import os
from datetime import datetime, timedelta

import pytest

from app.db.models.booking import BookingStatus
from app.services.care_plans import local_today
from app.tests.care_plan_helpers import BASE, make_nurse
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.test_capture_care_plans import OUT, _save

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CARE_PLANS"), reason="capture run only")


def test_capture_b3(db, client):
    today = local_today()
    nurse = make_nurse(db, full_name="Placeholder Nurse")
    helper = make_caregiver(db, full_name="Placeholder Caregiver")
    outsider = make_caregiver(db, full_name="Placeholder Caregiver Two")
    hn, hh, ho = (auth_headers(c.user) for c in (nurse, helper, outsider))

    client_one = make_patient(db, full_name="Placeholder Client", gender="female", date_of_birth=today - timedelta(days=71 * 366))
    visit = make_booking(db, client_one, nurse, status=BookingStatus.IN_PROGRESS, booking_for="myself",
                         actual_start_time=datetime.utcnow())
    make_booking(db, client_one, helper, status=BookingStatus.ACCEPTED, booking_for="myself")
    make_booking(db, make_patient(db, full_name="Placeholder Client Two"), outsider, status=BookingStatus.IN_PROGRESS,
                 booking_for="myself")

    plan = client.post(f"{BASE}/bookings/{visit.id}/care-plan", headers=hn, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "medium", "start_date": today.isoformat(), "duration_days": 30,
        "goals": [{"title": "Placeholder goal", "frequency": "daily"}]}).json()
    pid = plan["id"]
    url = f"{BASE}/care-plans/{pid}/medications"

    added = _save("240-a-medication-add.json", client.post(url, headers=hn, json={
        "name": "Placeholder medicine A", "strength": "500 mg", "dose_amount": "1", "dose_unit": "tablet", "route": "oral",
        "reason": "Placeholder reason", "prescribed_by": "Placeholder prescriber", "note": "Placeholder note",
        "frequency": "twice_daily", "times_of_day": ["20:00", "08:00"], "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=28)).isoformat(), "expected_version": plan["version"]}),
        "`times_of_day` comes back sorted. `expected_version` is optional when adding.")
    first, version = added.json()["medication"]["id"], added.json()["plan_version"]
    minimal = _save("240-b-medication-add-minimal.json", client.post(url, headers=hn, json={
        "name": "Placeholder medicine B", "frequency": "as_needed"}),
        "Only `name` and `frequency` are required. Unset text fields are null, `times_of_day` is [] and `start_date` is today.")
    second, version = minimal.json()["medication"]["id"], minimal.json()["plan_version"]
    _save("240-c-medication-add-not-a-nurse.json", client.post(url, headers=hh, json={
        "name": "Placeholder medicine C", "frequency": "once_daily"}), "A caregiver without the nurse skill can read, not write.")
    _save("240-d-medication-add-validation.json", client.post(url, headers=hn, json={
        "name": "", "frequency": "hourly", "times_of_day": ["8:00", "8:00"]}), "The standard 422 list; no rejected value is echoed.")
    _save("240-e-medication-add-end-before-start.json", client.post(url, headers=hn, json={
        "name": "Placeholder medicine D", "frequency": "once_daily", "start_date": today.isoformat(),
        "end_date": (today - timedelta(days=1)).isoformat()}))
    _save("240-f-medication-add-version-conflict.json", client.post(url, headers=hn, json={
        "name": "Placeholder medicine E", "frequency": "once_daily", "expected_version": 1}),
        "Stale `expected_version`: the body carries the current plan (trimmed in the guide).")

    edited = _save("241-a-medication-edit.json", client.patch(f"{BASE}/care-plan-medications/{first}", headers=hn, json={
        "expected_version": version, "strength": "250 mg", "times_of_day": ["12:00"], "note": None, "frequency": "once_daily"}),
        "Only the fields sent change. Optional fields accept null to clear them (`note` here).")
    version = edited.json()["plan_version"]
    _save("241-b-medication-edit-version-conflict.json", client.patch(f"{BASE}/care-plan-medications/{first}", headers=hn, json={
        "expected_version": 1, "name": "Placeholder other"}), "Stale `expected_version`.")
    _save("241-c-medication-edit-null-name.json", client.patch(f"{BASE}/care-plan-medications/{first}", headers=hn, json={
        "expected_version": version, "name": None}), "`name`, `frequency`, `times_of_day` and `start_date` cannot be null.")

    _save("243-a-plan-with-medications.json", client.get(f"{BASE}/care-plans/{pid}", headers=hn),
          "`medications` on the full plan: active ones first, then stopped ones.")
    _save("243-b-plan-medications-read-only-caregiver.json", client.get(f"{BASE}/care-plans/{pid}", headers=hh),
          "Every caregiver with a live visit for the client sees the same list; `can_edit_plan` is false for a non-nurse.")

    stopped = _save("242-a-medication-stop.json", client.post(f"{BASE}/care-plan-medications/{first}/stop", headers=hn,
                                                              json={"expected_version": version}),
                    "The medication stays on the plan as history. `stopped_at` is UTC without a `Z`.")
    version = stopped.json()["plan_version"]
    again = _save("242-b-medication-stop-again.json", client.post(f"{BASE}/care-plan-medications/{first}/stop", headers=hn,
                                                                  json={"expected_version": version}),
                  "Stopping a stopped medication changes nothing: same body, same `plan_version`.")
    _save("242-c-medication-edit-stopped.json", client.patch(f"{BASE}/care-plan-medications/{first}", headers=hn, json={
        "expected_version": version, "name": "Placeholder other"}), "A stopped medication is history: record a new one instead.")
    _save("242-d-medication-stop-not-a-nurse.json", client.post(f"{BASE}/care-plan-medications/{second}/stop", headers=hh,
                                                                json={"expected_version": version}))
    _save("243-c-plan-medications-after-stop.json", client.get(f"{BASE}/care-plans/{pid}", headers=hn),
          "After the stop: the active medication first, the stopped one after it.")

    _save("244-a-medication-add-no-access.json", client.post(url, headers=ho, json={"name": "Placeholder", "frequency": "once_daily"}),
          "A caregiver with no live visit for this client: the same 404 as everywhere.")
    _save("244-b-medication-stop-no-access.json", client.post(f"{BASE}/care-plan-medications/{second}/stop", headers=ho,
                                                              json={"expected_version": version}))
    done = client.patch(f"{BASE}/care-plans/{pid}", headers=hn, json={"expected_version": again.json()["plan_version"], "status": "completed"})
    assert done.status_code == 200, done.text
    _save("244-c-medication-add-plan-completed.json", client.post(url, headers=hn, json={"name": "Placeholder", "frequency": "once_daily"}))
    _save("244-d-plan-completed-keeps-medications.json", client.get(f"{BASE}/care-plans/{pid}", headers=hn),
          "A completed plan keeps its medications (history); no write is accepted.")
    assert OUT.exists()
