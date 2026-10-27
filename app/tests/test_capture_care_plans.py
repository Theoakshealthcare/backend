"""Writes the live captures behind the care plans section of the caregiver FE guide (slice B1).

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CARE_PLANS=1 poetry run pytest app/tests/test_capture_care_plans.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver`.
It only writes the 2xx files; scripts/capture_caregiver_flow.py (compose stack) rewrites the whole directory,
so fold these calls into it the next time it is run. All data is placeholder.
"""

import json
import os
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care import CareRecipient
from app.db.models.care_plan import CarePlanTemplate
from app.tests.care_plan_helpers import BASE, make_nurse
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CARE_PLANS"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "caregiver"


def _save(name, r, note=None):
    sent = r.request.content
    doc = {
        "request": {"method": r.request.method, "path": r.request.url.path + (f"?{r.request.url.query.decode()}" if r.request.url.query else ""),
                    "auth": "Bearer <ACCESS_TOKEN_JWT>", "body": json.loads(sent) if sent else None},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")},
                     "body": r.json()},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return r


def test_capture(db, client):
    from datetime import date, timedelta

    nurse = make_nurse(db, full_name="Placeholder Nurse")
    plain = make_caregiver(db, full_name="Placeholder Caregiver")
    hn, hp = auth_headers(nurse.user), auth_headers(plain.user)

    client_one = make_patient(db, full_name="Placeholder Client", gender="female",
                              date_of_birth=date.today() - timedelta(days=71 * 366))
    visit_one = make_booking(db, client_one, nurse, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    client_two = make_patient(db, full_name="Placeholder Client Two", gender="male")
    visit_two = make_booking(db, client_two, nurse, status=BookingStatus.ACCEPTED, booking_for="myself")
    booker = make_patient(db, full_name="Placeholder Booker")
    relative = CareRecipient(full_name="Placeholder Relative", gender="female", date_of_birth=date(1944, 1, 1),
                             created_by_user_id=booker.user_id)
    db.add(relative)
    db.commit()
    visit_rel = make_booking(db, booker, nurse, status=BookingStatus.EN_ROUTE, booking_for="mother", channel="whatsapp",
                             booked_by_user_id=booker.user_id, care_recipient_id=relative.id)
    unidentified = make_booking(db, make_patient(db, full_name="Placeholder Booker Two"), nurse,
                                status=BookingStatus.IN_PROGRESS, booking_for="father")
    visit_plain = make_booking(db, client_one, plain, status=BookingStatus.ACCEPTED, booking_for="myself")
    summary = lambda b: f"{BASE}/bookings/{b.id}/care-plan"  # noqa: E731

    _save("200-a-home-can-author.json", client.get(f"{BASE}/home", headers=hn),
          "Home for a caregiver with the nurse skill: `can_author_care_plans` is true. Other fields trimmed in the guide.")
    _save("200-b-home-not-author.json", client.get(f"{BASE}/home", headers=hp),
          "Home for a caregiver without the nurse skill (the free-text specialization says 'nurse' and does not count).")

    templates = _save("201-a-templates-list.json", client.get(f"{BASE}/care-plan-templates", headers=hn),
                      "Nurse only. Seeded drafts, all `clinical_review_status: pending`.")
    stroke = db.query(CarePlanTemplate).filter_by(key="stroke_recovery").one()
    _save("202-a-template-preview.json", client.get(f"{BASE}/care-plan-templates/{stroke.id}", headers=hn))
    _save("203-a-templates-not-a-nurse.json", client.get(f"{BASE}/care-plan-templates", headers=hp),
          "A caregiver without the nurse skill.")
    _save("204-a-template-not-found.json", client.get(f"{BASE}/care-plan-templates/00000000-0000-4000-8000-000000000009", headers=hn))

    _save("205-a-visit-summary-no-plan.json", client.get(summary(visit_one), headers=hn),
          "Access fine, the client has no active plan yet: `plan` is null.")
    _save("205-b-visit-summary-not-identified.json", client.get(summary(unidentified), headers=hn),
          "An app booking for someone else: no identifiable client, so no care plan is possible.")
    _save("205-c-visit-summary-plain-caregiver.json", client.get(summary(visit_plain), headers=hp),
          "A caregiver without the nurse skill: `can_author` is false.")

    _save("206-a-create-blank.json", client.post(summary(visit_one), headers=hn, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "high", "start_date": "2026-10-28", "duration_days": 30,
        "review_every_days": 14,
        "goals": [{"title": "Placeholder goal", "target": "Placeholder target", "frequency": "daily"}],
        "tasks": [{"title": "Placeholder task", "time_slot": "morning", "time_of_day": "08:00", "days_of_week": [1, 3, 5]}],
        "allergies": ["Placeholder allergen"], "special_instructions": ["Placeholder instruction"]}),
          "Create a plan for a client booked for themselves (the plan belongs to the patient).")
    plan = client.get(f"{BASE}/bookings/{visit_one.id}/care-plan", headers=hn).json()["plan"]
    _save("207-a-visit-summary-with-plan.json", client.get(summary(visit_one), headers=hn))
    _save("208-a-get-plan.json", client.get(f"{BASE}/care-plans/{plan['id']}", headers=hn))
    _save("209-a-visit-summary-plain-caregiver-with-plan.json", client.get(summary(visit_plain), headers=hp),
          "Another caregiver on the same client sees the same plan, and cannot author.")
    plain_view = client.get(f"{BASE}/care-plans/{plan['id']}", headers=hp)
    _save("209-b-get-plan-plain-caregiver.json", plain_view, "`capabilities.can_edit_plan` is false for a non-nurse.")

    _save("210-a-create-from-template.json", client.post(summary(visit_rel), headers=hn, json={
        "template_id": str(stroke.id), "diagnosis": "Placeholder diagnosis two"}),
          "Create from a template for a client booked by someone else (WhatsApp): the plan belongs to the care recipient. "
          "Nothing sent but `diagnosis`, so goals, tasks, instructions, risk, duration and review come from the template.")
    _save("211-a-create-already-exists.json", client.post(summary(visit_one), headers=hn, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "low"}))
    _save("212-a-create-not-identified.json", client.post(summary(unidentified), headers=hn, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "low"}))
    _save("213-a-create-needs-risk-level.json", client.post(summary(visit_two), headers=hn, json={"diagnosis": "Placeholder diagnosis"}))
    _save("214-a-create-validation.json", client.post(summary(visit_two), headers=hn, json={
        "diagnosis": "", "risk_level": "high"}), "A field limit: the standard 422 shape (no value is echoed).")
    _save("215-a-create-not-a-nurse.json", client.post(summary(visit_plain), headers=hp, json={
        "diagnosis": "Placeholder diagnosis", "risk_level": "low"}))

    _save("216-a-candidates.json", client.get(f"{BASE}/care-plans/candidates", headers=hn),
          "Live-booking clients without an active plan. Only identifiable clients.")
    _save("217-a-assigned-list.json", client.get(f"{BASE}/care-plans", headers=hn))
    _save("217-b-assigned-list-search.json", client.get(f"{BASE}/care-plans?q=relative", headers=hn))

    patched = _save("218-a-patch-plan.json", client.patch(f"{BASE}/care-plans/{plan['id']}", headers=hn, json={
        "expected_version": plan["version"], "risk_level": "medium", "allergies": ["Placeholder allergen", "Placeholder allergen two"]}))
    _save("219-a-patch-version-conflict.json", client.patch(f"{BASE}/care-plans/{plan['id']}", headers=hn, json={
        "expected_version": plan["version"], "risk_level": "low"}),
          "The version was already bumped by the call above. The body carries the current plan.")
    _save("219-b-patch-not-a-nurse.json", client.patch(f"{BASE}/care-plans/{plan['id']}", headers=hp, json={
        "expected_version": plan["version"], "risk_level": "low"}))

    version = patched.json()["version"]
    added = _save("220-a-goal-add.json", client.post(f"{BASE}/care-plans/{plan['id']}/goals", headers=hn, json={
        "title": "Placeholder goal two", "frequency": "weekly"}))
    goal_id, version = added.json()["goal"]["id"], added.json()["plan_version"]
    edited = _save("220-b-goal-edit.json", client.patch(f"{BASE}/care-plan-goals/{goal_id}", headers=hn, json={
        "expected_version": version, "target": "Placeholder target two", "status": "achieved"}))
    version = edited.json()["plan_version"]
    removed = _save("220-c-goal-remove.json", client.delete(f"{BASE}/care-plan-goals/{goal_id}?expected_version={version}", headers=hn),
                    "Removing marks the goal `discontinued`; it stays in the plan's `goals`.")
    version = removed.json()["plan_version"]

    done = _save("221-a-plan-complete.json", client.patch(f"{BASE}/care-plans/{plan['id']}", headers=hn, json={
        "expected_version": version, "status": "completed"}), "Completing makes the plan read-only and frees the client.")
    _save("221-b-plan-not-editable.json", client.patch(f"{BASE}/care-plans/{plan['id']}", headers=hn, json={
        "expected_version": done.json()["version"], "risk_level": "low"}))
    _save("221-c-assigned-completed.json", client.get(f"{BASE}/care-plans?status=completed", headers=hn))

    other = make_nurse(db, full_name="Placeholder Nurse Two")
    _save("222-a-no-access.json", client.get(f"{BASE}/care-plans/{plan['id']}", headers=auth_headers(other.user)),
          "A caregiver with no live visit for this client: the same 404 as for a plan that does not exist.")
    _save("222-b-no-access-patient-token.json", client.get(f"{BASE}/care-plans/{plan['id']}", headers=auth_headers(client_one.user)))
    assert templates.status_code == 200
