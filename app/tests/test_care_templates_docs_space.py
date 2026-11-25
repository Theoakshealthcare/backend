"""Slice 1a: the clinician-approved template text (0031), the Elderly template for any caregiver, the nurse's 2-hour grace for
care-plan notes, and the caregiver's own documentation space (private, windowed, audited admin read)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.security import decrypt
from app.db.models.audit import AdminAuditLog
from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan, CarePlanObservation, CarePlanTemplate, VisitDocumentation
from app.tests.care_plan_helpers import (
    BASE, SECRET_NOTE, a_template, colleague, create_plan, h, make_nurse, plan_body, self_visit, started_plan,
)
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient

ADMIN = "/api/v1/admin"
FAMILY_LINE = "Contact the family and inform the family's doctor"
GRACE = timedelta(minutes=settings.VOICE_NOTE_POST_VISIT_GRACE_MINUTES)
SECRET_DOC = "Placeholder own documentation mu"


# --- template text (0031) --------------------------------------------------------------------------------------------------

def _titles(template):
    return [t["title"] for t in template.body["tasks"]]


@pytest.mark.parametrize("key", ["stroke_recovery", "diabetes_management", "post_surgery_recovery", "cardiac_rehabilitation"])
def test_the_four_approved_templates_say_contact_the_family_and_the_family_doctor(db, key):
    template = a_template(db, key)
    assert template.version == 2
    assert FAMILY_LINE in template.body["special_instructions"]


def test_diabetes_text_follows_the_annotation(db):
    template = a_template(db, "diabetes_management")
    assert "Meal support, note what was eaten and document" in _titles(template)
    assert "Report dizziness, low or high blood glucose, sweating or unusual drowsiness at once" \
        in template.body["special_instructions"]
    assert "Record readings, do not interpret them" in template.body["special_instructions"]


def test_post_surgery_has_the_pain_task_its_goal_was_missing(db):
    template = a_template(db, "post_surgery_recovery")
    pain = [t for t in template.body["tasks"] if t["title"] == "Ask about pain and record it"]
    assert [t["time_slot"] for t in pain] == ["morning", "evening"]
    assert all(t["days_of_week"] == [1, 2, 3, 4, 5, 6, 7] for t in pain)
    assert any(g["title"] == "Pain reported and recorded" for g in template.body["goals"])


def test_cardiac_activity_wording_includes_discharge_instructions(db):
    assert "Light activity as instructed by care team or following post-hospital discharge instructions" \
        in _titles(a_template(db, "cardiac_rehabilitation"))


def test_elderly_template_text_is_unchanged_and_the_only_caregiver_template(db):
    rows = {t.key: t for t in db.query(CarePlanTemplate).all()}
    assert {k for k, t in rows.items() if t.caregiver_can_use} == {"elderly_daily_living_dementia"}
    assert rows["elderly_daily_living_dementia"].version == 1
    assert all(t.clinical_review_status == "pending" for t in rows.values())  # sign-off is the admin's, not the migration's


def test_a_plan_made_before_the_edit_keeps_its_own_copy_and_template_version(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    template = a_template(db, "post_surgery_recovery")
    r = create_plan(client, nurse, booking, template_id=str(template.id), goals=None, tasks=None, special_instructions=None)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["template"]["version"] == 2
    assert "Ask about pain and record it" in [t["title"] for t in plan["tasks"]]
    assert FAMILY_LINE in plan["special_instructions"]


# --- the Elderly template for a caregiver who is not a nurse --------------------------------------------------------------

def _elderly(db):
    return a_template(db, "elderly_daily_living_dementia")


def test_a_caregiver_who_is_not_a_nurse_may_start_a_plan_from_the_elderly_template(db, client):
    cg = make_caregiver(db)
    _, booking = self_visit(db, cg)
    summary = client.get(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(cg)).json()
    assert summary["can_author"] is False and summary["author_scope"] == "caregiver_templates"
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(cg),
                    json={"template_id": str(_elderly(db).id), "diagnosis": "Placeholder diagnosis"})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["capabilities"]["can_edit_plan"] is True and plan["capabilities"]["can_edit_medications"] is False
    assert plan["template"]["name"] == "Elderly Daily Living and Dementia Support"
    assert plan["tasks"], "the template's tasks were copied"


def test_a_nurse_summary_says_full_scope(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    summary = client.get(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse)).json()
    assert summary["can_author"] is True and summary["author_scope"] == "full"


@pytest.mark.parametrize("body", [
    {"diagnosis": "Placeholder diagnosis", "risk_level": "low"},  # blank plan
    "stroke",  # a nurse-only template
])
def test_a_non_nurse_cannot_start_a_blank_plan_or_use_a_nurse_only_template(db, client, body):
    cg = make_caregiver(db)
    _, booking = self_visit(db, cg)
    if body == "stroke":
        body = {"template_id": str(a_template(db, "stroke_recovery").id), "diagnosis": "Placeholder diagnosis"}
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(cg), json=body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_nurse"
    assert db.query(CarePlan).count() == 0


def test_an_unknown_template_id_is_404_for_a_non_nurse(db, client):
    cg = make_caregiver(db)
    _, booking = self_visit(db, cg)
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(cg),
                    json={"template_id": "00000000-0000-4000-8000-000000000009", "diagnosis": "Placeholder diagnosis"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "template_not_found"


def test_any_caregiver_may_edit_a_plan_made_from_the_elderly_template_but_not_its_medications(db, client):
    cg = make_caregiver(db)
    patient, booking = self_visit(db, cg)
    plan = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(cg),
                       json={"template_id": str(_elderly(db).id), "diagnosis": "Placeholder diagnosis"}).json()
    other = colleague(db, patient)  # a second plain caregiver on the same client
    patched = client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(other),
                           json={"expected_version": plan["version"], "diagnosis": "Placeholder changed"})
    assert patched.status_code == 200, patched.text
    added = client.post(f"{BASE}/care-plans/{plan['id']}/goals", headers=h(other),
                        json={"title": "Placeholder goal", "frequency": "daily"})
    assert added.status_code == 201, added.text
    meds = client.post(f"{BASE}/care-plans/{plan['id']}/medications", headers=h(other),
                       json={"name": "Placeholder med", "frequency": "once_daily", "start_date": "2026-11-25"})
    assert meds.status_code == 403 and meds.json()["detail"]["code"] == "not_a_nurse"


def test_a_plan_from_a_nurse_only_template_stays_nurse_only_to_edit(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    plain = colleague(db, patient)
    r = client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(plain),
                     json={"expected_version": plan["version"], "diagnosis": "Placeholder changed"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_nurse"


def test_candidates_are_open_to_any_caregiver(db, client):
    cg = make_caregiver(db)
    self_visit(db, cg)
    r = client.get(f"{BASE}/care-plans/candidates", headers=h(cg))
    assert r.status_code == 200 and len(r.json()["items"]) == 1


# --- care-plan notes: the assigned nurse's 2-hour grace -------------------------------------------------------------------

def _complete(db, booking, minutes_ago):
    booking.status = BookingStatus.COMPLETED
    booking.actual_end_time = datetime.utcnow() - timedelta(minutes=minutes_ago)
    db.commit()


def _note(client, caregiver, plan_id, body=SECRET_NOTE):
    return client.post(f"{BASE}/care-plans/{plan_id}/observations", json={"body": body}, headers=h(caregiver))


def test_the_assigned_nurse_may_add_a_note_for_two_hours_after_the_visit(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    _complete(db, booking, minutes_ago=119)
    r = _note(client, nurse, plan["id"])
    assert r.status_code == 201, r.text
    row = db.get(CarePlanObservation, r.json()["id"])
    assert row.booking_id == booking.id and row.author_caregiver_id == nurse.id and row.family_visible is False


def test_the_grace_ends_after_two_hours_with_the_usual_404(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    _complete(db, booking, minutes_ago=121)
    r = _note(client, nurse, plan["id"])
    assert r.status_code == 404 and r.json() == {"detail": "Care plan not found"}
    assert db.query(CarePlanObservation).count() == 0


def test_the_grace_is_the_nurses_only_a_plain_caregiver_loses_access_at_completion(db, client):
    _, patient, _, plan = started_plan(client, db)
    plain = make_caregiver(db)
    visit = make_booking(db, patient, plain, status=BookingStatus.COMPLETED, booking_for="myself",
                         actual_end_time=datetime.utcnow() - timedelta(minutes=10))
    assert visit.status == BookingStatus.COMPLETED
    assert _note(client, plain, plan["id"]).status_code == 404


def test_a_nurse_cannot_use_another_nurses_completed_visit(db, client):
    _, patient, booking, plan = started_plan(client, db)
    _complete(db, booking, minutes_ago=10)
    stranger = make_nurse(db)
    assert _note(client, stranger, plan["id"]).status_code == 404


def test_a_suspended_nurse_gets_no_grace(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    _complete(db, booking, minutes_ago=10)
    nurse.profile_status = "suspended"
    db.commit()
    assert _note(client, nurse, plan["id"]).status_code == 404


def test_in_the_grace_the_nurse_can_read_the_plan_and_notes_but_not_edit_or_tick(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    first = _note(client, nurse, plan["id"]).json()
    _complete(db, booking, minutes_ago=30)
    got = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["capabilities"] == {"can_edit_plan": False, "can_edit_medications": False, "can_tick_tasks": False,
                                      "can_add_observation": True}
    closes = datetime.fromisoformat(body["observations_close_at"])
    assert abs(closes - (booking.actual_end_time + GRACE)) < timedelta(seconds=1)
    listed = client.get(f"{BASE}/care-plans/{plan['id']}/observations", headers=h(nurse)).json()
    assert [i["id"] for i in listed["items"]] == [first["id"]]
    edit = client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse),
                        json={"expected_version": body["version"], "diagnosis": "Placeholder changed"})
    assert edit.status_code == 404  # edits still need a live visit
    task_id = body["tasks"][0]["id"]
    tick = client.put(f"{BASE}/care-plan-tasks/{task_id}/completion", headers=h(nurse),
                      json={"date": datetime.utcnow().date().isoformat(), "done": True})
    assert tick.status_code == 404


def test_the_visit_summary_leads_the_nurse_to_the_plan_during_the_grace_and_not_after(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    summary_url = f"{BASE}/bookings/{booking.id}/care-plan"
    _complete(db, booking, minutes_ago=30)
    got = client.get(summary_url, headers=h(nurse))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["plan"]["id"] == plan["id"] and body["can_author"] is False and body["author_scope"] == "full"
    _complete(db, booking, minutes_ago=121)
    assert client.get(summary_url, headers=h(nurse)).status_code == 404
    plain = make_caregiver(db)
    other = make_booking(db, db.get(type(booking), booking.id).patient, plain, status=BookingStatus.COMPLETED,
                         booking_for="myself", actual_end_time=datetime.utcnow() - timedelta(minutes=5))
    assert client.get(f"{BASE}/bookings/{other.id}/care-plan", headers=h(plain)).status_code == 404  # not a nurse


def test_a_live_visit_reports_no_close_time(db, client):
    nurse, _, _, plan = started_plan(client, db)
    body = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()
    assert body["observations_close_at"] is None and body["capabilities"]["can_add_observation"] is True


def test_the_grace_follows_the_shared_setting(db, client, monkeypatch):
    nurse, _, booking, plan = started_plan(client, db)
    _complete(db, booking, minutes_ago=30)
    monkeypatch.setattr(settings, "VOICE_NOTE_POST_VISIT_GRACE_MINUTES", 20)
    assert _note(client, nurse, plan["id"]).status_code == 404
    monkeypatch.setattr(settings, "VOICE_NOTE_POST_VISIT_GRACE_MINUTES", 60)
    assert _note(client, nurse, plan["id"]).status_code == 201


# --- the caregiver's own documentation ------------------------------------------------------------------------------------

def _doc_url(booking):
    return f"{BASE}/bookings/{booking.id}/documentation"


def test_a_caregiver_who_is_not_a_nurse_documents_a_visit_privately(db, client):
    cg = make_caregiver(db)
    patient, booking = self_visit(db, cg)
    r = client.post(_doc_url(booking), json={"body": SECRET_DOC}, headers=h(cg))
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "booking_id", "plan_id", "body", "created_at"}
    assert body["body"] == SECRET_DOC and body["booking_id"] == str(booking.id) and body["plan_id"] is None
    raw = db.execute(text("SELECT body_encrypted FROM visit_documentation WHERE id = :i"), {"i": body["id"]}).scalar_one()
    assert SECRET_DOC not in raw and decrypt(raw) == SECRET_DOC
    listed = client.get(_doc_url(booking), headers=h(cg)).json()
    assert [i["body"] for i in listed["items"]] == [SECRET_DOC]
    assert listed["can_add"] is True and listed["closes_at"] is None and listed["readable_until"] is None


def test_an_entry_links_the_clients_active_plan_when_there_is_one(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    r = client.post(_doc_url(booking), json={"body": SECRET_DOC}, headers=h(nurse))
    assert r.status_code == 201 and r.json()["plan_id"] == plan["id"]


def test_documentation_is_private_to_its_author_even_from_a_colleague_on_the_same_client(db, client):
    nurse, patient, booking, _ = started_plan(client, db)
    client.post(_doc_url(booking), json={"body": SECRET_DOC}, headers=h(nurse))
    plain = colleague(db, patient)
    other_booking = db.query(type(booking)).filter_by(caregiver_id=plain.id).one()
    assert client.get(_doc_url(booking), headers=h(plain)).status_code == 404  # not their visit
    mine = client.get(_doc_url(other_booking), headers=h(plain)).json()
    assert mine["items"] == []  # their own visit shows only their own entries


def test_no_patient_or_family_route_returns_documentation(db, client):
    nurse, patient, booking, plan = started_plan(client, db)
    client.post(_doc_url(booking), json={"body": SECRET_DOC}, headers=h(nurse))
    headers = auth_headers(patient.user)
    assert client.get(_doc_url(booking), headers=headers).status_code == 403
    for path in (f"/api/v1/booking/notes/{booking.id}", f"/api/v1/booking/{booking.id}"):
        resp = client.get(path, headers=headers)
        assert SECRET_DOC not in resp.text


def test_adding_is_refused_before_and_after_the_window(db, client):
    cg = make_caregiver(db)
    patient = make_patient(db)
    accepted = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, booking_for="myself")
    r = client.post(_doc_url(accepted), json={"body": SECRET_DOC}, headers=h(cg))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "visit_not_active"
    live = make_booking(db, patient, cg, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    assert client.post(_doc_url(live), json={"body": SECRET_DOC}, headers=h(cg)).status_code == 201
    _complete(db, live, minutes_ago=119)
    assert client.post(_doc_url(live), json={"body": "Placeholder second"}, headers=h(cg)).status_code == 201
    listed = client.get(_doc_url(live), headers=h(cg)).json()
    assert listed["can_add"] is True and listed["closes_at"] is not None and listed["readable_until"] is not None
    _complete(db, live, minutes_ago=121)
    late = client.post(_doc_url(live), json={"body": "Placeholder third"}, headers=h(cg))
    assert late.status_code == 409 and late.json()["detail"]["code"] == "visit_not_active"
    # still readable (24 h), but no longer writable
    after = client.get(_doc_url(live), headers=h(cg)).json()
    assert after["can_add"] is False and len(after["items"]) == 2
    _complete(db, live, minutes_ago=60 * 25)
    assert client.get(_doc_url(live), headers=h(cg)).status_code == 404


def test_someone_elses_visit_and_a_missing_one_are_the_same_404(db, client):
    cg, other = make_caregiver(db), make_caregiver(db)
    _, booking = self_visit(db, cg)
    for target, who in ((booking, other), ):
        assert client.get(_doc_url(target), headers=h(who)).status_code == 404
        assert client.post(_doc_url(target), json={"body": SECRET_DOC}, headers=h(who)).status_code == 404
    ghost = client.get(f"{BASE}/bookings/00000000-0000-4000-8000-000000000009/documentation", headers=h(cg))
    assert ghost.status_code == 404 and ghost.json() == client.get(_doc_url(booking), headers=h(other)).json()


def test_a_suspended_caregiver_cannot_document_a_completed_visit(db, client):
    cg = make_caregiver(db)
    _, booking = self_visit(db, cg)
    _complete(db, booking, minutes_ago=10)
    cg.profile_status = "suspended"
    db.commit()
    assert client.post(_doc_url(booking), json={"body": SECRET_DOC}, headers=h(cg)).status_code == 404


@pytest.mark.parametrize("body", [{}, {"body": ""}, {"body": "   "}, {"body": "x" * 4001}])
def test_validation_never_echoes_the_text(db, client, body):
    cg = make_caregiver(db)
    _, booking = self_visit(db, cg)
    r = client.post(_doc_url(booking), json=body, headers=h(cg))
    assert r.status_code == 422 and "input" not in r.text


def test_the_routes_need_a_caregiver_token(db, client):
    cg = make_caregiver(db)
    patient, booking = self_visit(db, cg)
    assert client.get(_doc_url(booking)).status_code == 401
    assert client.post(_doc_url(booking), json={"body": "x"}).status_code == 401
    assert client.post(_doc_url(booking), json={"body": "x"}, headers=auth_headers(patient.user)).status_code == 403
    assert client.get(_doc_url(booking), headers=h(cg)).headers["cache-control"] == "no-store"


# --- the admin read view ---------------------------------------------------------------------------------------------------

@pytest.fixture
def reviewer(db):
    return make_admin(db, ["clinical_reviewer"], password=None, full_name="Placeholder Reviewer")


def test_an_admin_reads_documentation_filtered_and_the_read_is_audited(db, client, reviewer):
    nurse, _, booking, plan = started_plan(client, db)
    client.post(_doc_url(booking), json={"body": SECRET_DOC}, headers=h(nurse))
    plain = make_caregiver(db)
    _, other_booking = self_visit(db, plain)
    client.post(_doc_url(other_booking), json={"body": "Placeholder other entry"}, headers=h(plain))
    rh = auth_headers(reviewer)
    everything = client.get(f"{ADMIN}/visit-documentation", headers=rh)
    assert everything.status_code == 200 and everything.json()["total"] == 2
    assert everything.headers["cache-control"] == "no-store"
    by_booking = client.get(f"{ADMIN}/visit-documentation", params={"booking_id": str(booking.id)}, headers=rh).json()
    assert [(i["body"], i["caregiver"]["name"]) for i in by_booking["items"]] == [(SECRET_DOC, nurse.full_name)]
    assert by_booking["items"][0]["plan_id"] == plan["id"]
    by_plan = client.get(f"{ADMIN}/visit-documentation", params={"plan_id": plan["id"]}, headers=rh).json()
    assert by_plan["total"] == 1
    by_cg = client.get(f"{ADMIN}/visit-documentation", params={"caregiver_id": str(plain.id)}, headers=rh).json()
    assert [i["body"] for i in by_cg["items"]] == ["Placeholder other entry"]
    audit = db.query(AdminAuditLog).filter_by(action="visit_documentation.view").all()
    assert len(audit) == 4 and all(a.admin_user_id == reviewer.id for a in audit)
    assert SECRET_DOC not in "".join(str(a.after) for a in audit)


def test_support_cannot_read_documentation(db, client):
    support = make_admin(db, ["support"], password=None)
    assert client.get(f"{ADMIN}/visit-documentation", headers=auth_headers(support)).status_code == 403
    assert client.get(f"{ADMIN}/visit-documentation").status_code == 401


def test_erasing_the_patient_blanks_the_documentation_text_but_keeps_the_rows(db):
    from app.services.visit_documentation import blank_for_patient

    cg = make_caregiver(db)
    patient, booking = self_visit(db, cg)
    from app.services.care_plans import seal

    db.add(VisitDocumentation(caregiver_id=cg.id, booking_id=booking.id, body_encrypted=seal(SECRET_DOC)))
    db.commit()
    assert blank_for_patient(db, patient.id) == 1
    db.commit()
    row = db.query(VisitDocumentation).one()
    assert row.body_encrypted is None and row.booking_id == booking.id
