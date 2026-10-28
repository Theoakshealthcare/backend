"""Writes the live captures behind the Care plans (B4) section of the admin FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CARE_PLANS=1 poetry run pytest app/tests/test_capture_care_plans_admin.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-admin.md docs/fe-captures/admin`.
Files `500`-`546`. All data is placeholder; the clinical text is invented, not a real person's.
"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care import CareRecipient
from app.db.models.care_plan import CarePlanTemplate
from app.tests.care_plan_helpers import BASE, make_nurse
from app.tests.factories import auth_headers, make_admin, make_booking, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CARE_PLANS"), reason="capture run only")

OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "admin"
ADMIN = "/api/v1/admin"


def _save(name, r, note=None, auth="Bearer <ADMIN_ACCESS_TOKEN_JWT>"):
    sent = r.request.content
    doc = {
        "request": {"method": r.request.method,
                    "path": r.request.url.path + (f"?{r.request.url.query.decode()}" if r.request.url.query else ""),
                    "auth": auth if r.request.headers.get("authorization") else None, "body": json.loads(sent) if sent else None},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")},
                     "body": r.json()},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return r


def test_capture_admin_care_plans(db, client):
    today = date.today()
    reviewer = make_admin(db, ["clinical_reviewer"], password=None, full_name="Placeholder Reviewer")
    support = make_admin(db, ["support"], password=None, full_name="Placeholder Support")
    finance = make_admin(db, ["finance"], password=None, full_name="Placeholder Finance")
    hr, hs, hf = (auth_headers(a) for a in (reviewer, support, finance))

    client_one = make_patient(db, full_name="Placeholder Client One", gender="female", date_of_birth=today - timedelta(days=71 * 366))
    client_two = make_patient(db, full_name="Placeholder Client Two", gender="male", date_of_birth=today - timedelta(days=64 * 366))
    booker = make_patient(db, full_name="Placeholder Booker")
    relative = CareRecipient(full_name="Placeholder Relative", gender="male", date_of_birth=date(1950, 3, 4),
                             created_by_user_id=booker.user_id)
    db.add(relative)
    db.commit()

    _save("500-a-list-empty.json", client.get(f"{ADMIN}/care-plans", headers=hr), "No plans yet. The list is `{items, total, limit, offset}`.")
    _save("500-b-list-no-token.json", client.get(f"{ADMIN}/care-plans"), "No admin token: 401.")
    _save("500-c-list-as-support.json", client.get(f"{ADMIN}/care-plans", headers=hs),
          "A role without `careplans.read` (here Support): 403 naming the permission.")

    plan_body = {
        "patient_id": str(client_one.id), "diagnosis": "Placeholder diagnosis", "risk_level": "high",
        "start_date": today.isoformat(), "duration_days": 30, "review_every_days": 14,
        "goals": [{"title": "Placeholder goal", "target": "Placeholder target", "frequency": "daily"}],
        "tasks": [{"title": "Placeholder task", "time_slot": "morning", "time_of_day": "08:00", "days_of_week": [1, 3, 5]}],
        "allergies": ["Placeholder allergen"], "special_instructions": ["Placeholder instruction"]}
    created = _save("501-a-create.json", client.post(f"{ADMIN}/care-plans", headers=hr, json=plan_body),
                    "Exactly one of `patient_id` or `care_recipient_id`. 201 with the full plan. `author.kind` is `admin`; there is "
                    "no `booking_id` and no `capabilities`.")
    plan = created.json()
    pid = plan["id"]
    _save("501-b-create-duplicate.json", client.post(f"{ADMIN}/care-plans", headers=hr, json=plan_body),
          "One active plan per client: 409 `care_plan_exists` with the existing `plan_id`.")
    _save("501-c-create-both-ids.json", client.post(f"{ADMIN}/care-plans", headers=hr, json={
        **plan_body, "care_recipient_id": str(relative.id)}), "Both ids: 422; the error names the rule, never a value.")
    _save("501-d-create-no-id.json", client.post(f"{ADMIN}/care-plans", headers=hr, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "low"}), "Neither id: the same 422.")
    _save("501-e-create-unknown-client.json", client.post(f"{ADMIN}/care-plans", headers=hr, json={
        **plan_body, "patient_id": "00000000-0000-4000-8000-000000000001"}), "No such (active) client: 404 `client_not_found`.")
    _save("501-f-create-as-support.json", client.post(f"{ADMIN}/care-plans", headers=hs, json=plan_body), "`careplans.write` is missing: 403.")

    template = db.query(CarePlanTemplate).filter(CarePlanTemplate.key == "stroke_recovery").one()
    from_template = _save("502-a-create-from-template-for-a-recipient.json", client.post(f"{ADMIN}/care-plans", headers=hr, json={
        "care_recipient_id": str(relative.id), "template_id": str(template.id), "diagnosis": "Placeholder diagnosis two",
        "start_date": today.isoformat()}),
        "A relative the booker made visits for. The template fills the goals, tasks, instructions, duration and risk the body leaves "
        "out (trimmed in the guide).")
    _save("502-b-create-unknown-template.json", client.post(f"{ADMIN}/care-plans", headers=hr, json={
        "patient_id": str(client_two.id), "template_id": "00000000-0000-4000-8000-000000000003", "diagnosis": "x",
        "risk_level": "low"}), "404 `template_not_found`.")

    _save("503-a-list.json", client.get(f"{ADMIN}/care-plans", headers=hr),
          "Newest change first. No diagnosis or other plan text in the rows; open a plan for that.")
    _save("503-b-list-search.json", client.get(f"{ADMIN}/care-plans?q=relative&status=active&limit=10&offset=0", headers=hr),
          "`q` matches the client's name only (the diagnosis is encrypted); `status` is active, completed or archived.")
    _save("503-c-list-bad-status.json", client.get(f"{ADMIN}/care-plans?status=bogus", headers=hr))

    _save("504-a-detail.json", client.get(f"{ADMIN}/care-plans/{pid}", headers=hr),
          "Opening a plan is audited (`care_plan.view`, see the audit capture at the end).")
    _save("504-b-detail-unknown.json", client.get(f"{ADMIN}/care-plans/00000000-0000-4000-8000-000000000009", headers=hr),
          "One 404 for every id that does not exist, on every route.")

    edited = _save("505-a-patch.json", client.patch(f"{ADMIN}/care-plans/{pid}", headers=hr, json={
        "expected_version": plan["version"], "risk_level": "medium", "allergies": ["Placeholder allergen", "Placeholder allergen two"]}),
        "Only the fields sent change. `version` goes up by one.")
    version = edited.json()["version"]
    _save("505-b-patch-stale.json", client.patch(f"{ADMIN}/care-plans/{pid}", headers=hr, json={
        "expected_version": plan["version"], "risk_level": "low"}),
        "Stale `expected_version`: 409 `version_conflict`; the body carries the current plan (trimmed in the guide).")

    goal = _save("506-a-goal-add.json", client.post(f"{ADMIN}/care-plans/{pid}/goals", headers=hr, json={
        "title": "Placeholder goal two", "frequency": "weekly"}), "`expected_version` is optional when adding.")
    gid, version = goal.json()["goal"]["id"], goal.json()["plan_version"]
    goal_edit = _save("506-b-goal-edit.json", client.patch(f"{ADMIN}/care-plan-goals/{gid}", headers=hr, json={
        "expected_version": version, "status": "achieved"}))
    version = goal_edit.json()["plan_version"]
    goal_gone = _save("506-c-goal-remove.json", client.delete(f"{ADMIN}/care-plan-goals/{gid}?expected_version={version}", headers=hr),
                      "The goal is marked `discontinued` and kept. `expected_version` is a query parameter.")
    version = goal_gone.json()["plan_version"]

    task = _save("507-a-task-add.json", client.post(f"{ADMIN}/care-plans/{pid}/tasks", headers=hr, json={
        "title": "Placeholder task two", "time_slot": "evening", "time_of_day": "18:00", "days_of_week": [1, 2, 3, 4, 5, 6, 7]}))
    tid, version = task.json()["task"]["id"], task.json()["plan_version"]
    task_edit = _save("507-b-task-edit.json", client.patch(f"{ADMIN}/care-plan-tasks/{tid}", headers=hr, json={
        "expected_version": version, "time_slot": "night"}))
    version = task_edit.json()["plan_version"]
    task_gone = _save("507-c-task-remove.json", client.delete(f"{ADMIN}/care-plan-tasks/{tid}?expected_version={version}", headers=hr),
                      "The task stops being scheduled and leaves `tasks`; its ticks are kept.")
    version = task_gone.json()["plan_version"]
    _save("507-d-task-edit-removed.json", client.patch(f"{ADMIN}/care-plan-tasks/{tid}", headers=hr, json={
        "expected_version": version, "title": "x"}), "A removed task is the plain 404.")

    med = _save("508-a-medication-add.json", client.post(f"{ADMIN}/care-plans/{pid}/medications", headers=hr, json={
        "name": "Placeholder medicine A", "strength": "500 mg", "dose_amount": "1", "dose_unit": "tablet", "route": "oral",
        "reason": "Placeholder reason", "prescribed_by": "Placeholder prescriber", "frequency": "twice_daily",
        "times_of_day": ["20:00", "08:00"], "start_date": today.isoformat()}),
        "Record only. The medications notice from the caregiver guide (16.21) applies to this section too.")
    mid, version = med.json()["medication"]["id"], med.json()["plan_version"]
    med_edit = _save("508-b-medication-edit.json", client.patch(f"{ADMIN}/care-plan-medications/{mid}", headers=hr, json={
        "expected_version": version, "dose_amount": "2"}))
    version = med_edit.json()["plan_version"]
    med_stop = _save("508-c-medication-stop.json", client.post(f"{ADMIN}/care-plan-medications/{mid}/stop", headers=hr, json={
        "expected_version": version}))
    version = med_stop.json()["plan_version"]
    _save("508-d-medication-edit-stopped.json", client.patch(f"{ADMIN}/care-plan-medications/{mid}", headers=hr, json={
        "expected_version": version, "dose_amount": "3"}), "A stopped medication cannot be edited: 409 `medication_stopped`.")
    _save("508-e-goal-validation.json", client.post(f"{ADMIN}/care-plans/{pid}/goals", headers=hr, json={
        "title": "Placeholder rejected title", "frequency": "hourly"}), "Validation: 422, the rejected value is not echoed.")

    note = _save("509-a-note-add.json", client.post(f"{ADMIN}/care-plans/{pid}/observations", headers=hr, json={
        "body": "Placeholder review note", "category": "special_note"}),
        "No visit needed. `author.kind` is `admin`. Notes never change the plan's `version`.")
    nurse = make_nurse(db, full_name="Placeholder Nurse")
    make_booking(db, client_one, nurse, status=BookingStatus.IN_PROGRESS, booking_for="myself", actual_start_time=datetime.utcnow())
    from app.tests.factories import auth_headers as app_headers
    hn = app_headers(nurse.user)
    nurse_note = client.post(f"{BASE}/care-plans/{pid}/observations", headers=hn, json={"body": "Placeholder visit note"})
    assert nurse_note.status_code == 201, nurse_note.text
    _save("509-b-notes-list.json", client.get(f"{ADMIN}/care-plans/{pid}/observations?limit=10", headers=hr),
          "Newest first, from nurses and admins alike. Reading the notes is audited (`care_plan.observations_view`).")
    _save("509-c-notes-list-as-support.json", client.get(f"{ADMIN}/care-plans/{pid}/observations", headers=hs))

    _save("510-a-caregiver-sees-the-plan.json", client.get(f"{BASE}/care-plans/{pid}", headers=hn),
          "The same plan through the caregiver route (a live visit for this client): `author.kind` is `admin`, and the admin's "
          "edits are in it.", auth="Bearer <CAREGIVER_ACCESS_TOKEN_JWT>")

    _save("511-a-templates.json", client.get(f"{ADMIN}/care-plan-templates", headers=hr),
          "Every template, active or not, with its full content. Trimmed in the guide.")
    review = _save("511-b-review-approve.json", client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", headers=hr,
                                                            json={"status": "approved"}),
                   "Records the reviewer and the time. Audited.")
    _save("511-c-review-take-back.json", client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", headers=hr,
                                                     json={"status": "pending"}))
    _save("511-d-review-bad-status.json", client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", headers=hr,
                                                      json={"status": "maybe"}))
    _save("511-e-review-unknown.json", client.post(f"{ADMIN}/care-plan-templates/00000000-0000-4000-8000-000000000004/review",
                                                   headers=hr, json={"status": "approved"}), "404 `template_not_found`.")
    _save("511-f-review-as-support.json", client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", headers=hs,
                                                      json={"status": "approved"}), "`careplans.templates` is missing: 403.")
    assert review.json()["clinical_review_status"] == "approved"

    final = client.get(f"{ADMIN}/care-plans/{pid}", headers=hr).json()
    _save("512-a-complete.json", client.patch(f"{ADMIN}/care-plans/{pid}", headers=hr, json={
        "expected_version": final["version"], "status": "completed"}), "Completing makes the plan read-only and frees the client for a new one.")
    _save("512-b-write-on-completed.json", client.post(f"{ADMIN}/care-plans/{pid}/goals", headers=hr, json={
        "title": "Placeholder goal three", "frequency": "daily"}), "409 `plan_not_editable`. Reads still work.")
    _save("512-c-detail-completed.json", client.get(f"{ADMIN}/care-plans/{pid}", headers=hr),
          "A completed plan stays readable.")
    _save("512-d-list-completed.json", client.get(f"{ADMIN}/care-plans?status=completed", headers=hr))
    _save("512-e-create-again-after-completing.json", client.post(f"{ADMIN}/care-plans", headers=hr, json=plan_body),
          "The client can have a new active plan once the old one is completed.")

    _save("513-a-audit.json", client.get(f"{ADMIN}/audit?entity_type=care_plan&limit=100", headers=hf),
          "Finance has `audit.read`. Each admin write is a row; the `after` holds field names and ids only, never plan text.")
    _save("513-b-audit-template-review.json", client.get(f"{ADMIN}/audit?entity_type=care_plan_template", headers=hf))
    assert from_template.status_code == 201
