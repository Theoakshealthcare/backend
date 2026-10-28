"""Care plan medications, slice B3: add, edit, stop; the record-only notice; the plan detail. Record only (decision D4).

Access across the three routes is in test_care_plans_b3_access.py; encryption, rotation and erasure in
test_care_plans_b3_security.py; the table's constraints in test_care_plan_medications_migration.py.
"""

import json
from datetime import date

import pytest

from app.db.models.care_plan import CarePlanEvent, CarePlanMedication
from app.schemas.care_plan import MEDICATION_NOTICE
from app.tests.care_plan_helpers import (
    BASE, SECRET_MED, SECRET_MED_NOTE, add_med, colleague, h, started_plan,
)

OUT_KEYS = {"id", "name", "strength", "dose_amount", "dose_unit", "route", "reason", "prescribed_by", "note", "frequency",
            "times_of_day", "start_date", "end_date", "status", "stopped_at"}


def _med_url(medication_id, suffix=""):
    return f"{BASE}/care-plan-medications/{medication_id}{suffix}"


def _plan(client, caregiver, plan_id):
    return client.get(f"{BASE}/care-plans/{plan_id}", headers=h(caregiver)).json()


def _events(db, plan_id, action):
    return [e for e in db.query(CarePlanEvent).filter_by(plan_id=plan_id, action=action).order_by(CarePlanEvent.created_at)]


# --- add ------------------------------------------------------------------------------------------------------------------

def test_a_nurse_records_a_medication_and_it_appears_on_the_plan(db, client):
    nurse, _, _, plan = started_plan(client, db)
    assert plan["medications"] == []
    r = add_med(client, nurse, plan["id"])
    assert r.status_code == 201, r.text
    medication = r.json()["medication"]
    assert set(medication) == OUT_KEYS and r.json()["plan_version"] == plan["version"] + 1
    assert medication["name"] == SECRET_MED and medication["status"] == "active" and medication["stopped_at"] is None
    assert medication["times_of_day"] == ["08:00", "20:00"]  # earliest first
    got = _plan(client, nurse, plan["id"])
    assert got["medications"] == [medication] and got["version"] == plan["version"] + 1
    assert [e.detail for e in _events(db, plan["id"], "med.add")] == [{"medication_id": medication["id"]}]


def test_only_the_name_and_frequency_are_required(db, client):
    nurse, _, _, plan = started_plan(client, db)
    r = client.post(f"{BASE}/care-plans/{plan['id']}/medications", headers=h(nurse),
                    json={"name": SECRET_MED, "frequency": "as_needed"})
    assert r.status_code == 201, r.text
    medication = r.json()["medication"]
    assert medication["times_of_day"] == [] and medication["end_date"] is None
    assert all(medication[f] is None for f in ("strength", "dose_amount", "dose_unit", "route", "reason", "prescribed_by", "note"))
    from app.services.care_plans import local_today

    assert medication["start_date"] == local_today().isoformat()  # defaults to today in Africa/Lagos


@pytest.mark.parametrize("over", [
    {"name": ""}, {"name": "x" * 121}, {"frequency": "hourly"}, {"times_of_day": ["8:00"]}, {"times_of_day": ["25:00"]},
    {"times_of_day": ["08:00", "08:00"]}, {"times_of_day": [f"{h:02d}:00" for h in range(13)]}, {"dose_amount": ""},
    {"note": "x" * 1001}, {"strength": "x" * 61}, {"start_date": "tomorrow"},
])
def test_invalid_medications_are_a_422_that_does_not_echo_the_value(db, client, over):
    nurse, _, _, plan = started_plan(client, db)
    r = add_med(client, nurse, plan["id"], **over)
    assert r.status_code == 422, r.text
    assert all("input" not in error for error in r.json()["detail"]) and "x" * 40 not in r.text and "tomorrow" not in r.text


def test_unicode_digits_are_not_a_time_and_a_patch_422_does_not_echo(db, client):
    nurse, _, _, plan = started_plan(client, db)
    assert add_med(client, nurse, plan["id"], times_of_day=["\u0660\u0668:\u0660\u0660"]).status_code == 422
    added = add_med(client, nurse, plan["id"]).json()
    r = client.patch(_med_url(added["medication"]["id"]), headers=h(nurse),
                     json={"expected_version": added["plan_version"], "note": "y" * 1001})
    assert r.status_code == 422 and all("input" not in e for e in r.json()["detail"]) and "y" * 40 not in r.text


def test_an_end_date_before_the_start_date_is_refused(db, client):
    nurse, _, _, plan = started_plan(client, db)
    r = add_med(client, nurse, plan["id"], start_date="2026-10-28", end_date="2026-10-27")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_care_plan"
    assert db.query(CarePlanMedication).count() == 0


def test_a_plan_holds_at_most_forty_active_medications_and_a_stopped_one_frees_a_place(db, client):
    nurse, _, _, plan = started_plan(client, db)
    for i in range(40):
        db.add(CarePlanMedication(plan_id=plan["id"], detail_encrypted="x", frequency="other", start_date=date(2026, 10, 28)))
    db.commit()
    full = add_med(client, nurse, plan["id"])
    assert full.status_code == 422 and full.json()["detail"]["code"] == "invalid_care_plan"
    first = db.query(CarePlanMedication).first()
    first.status, first.stopped_at = "stopped", first.created_at
    db.commit()
    assert add_med(client, nurse, plan["id"]).status_code == 201


def test_adding_accepts_a_stale_version_only_when_none_is_sent(db, client):
    nurse, _, _, plan = started_plan(client, db)
    stale = add_med(client, nurse, plan["id"], expected_version=99)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    assert stale.json()["detail"]["plan"]["id"] == plan["id"] and stale.json()["detail"]["plan"]["medications"] == []
    assert add_med(client, nurse, plan["id"], expected_version=plan["version"]).status_code == 201


def test_the_family_flag_and_other_unknown_fields_cannot_be_set(db, client):
    nurse, _, _, plan = started_plan(client, db)
    r = add_med(client, nurse, plan["id"], family_visible=True, status="stopped", stopped_at="2026-10-28T00:00:00")
    assert r.status_code == 201 and r.json()["medication"]["status"] == "active" and r.json()["medication"]["stopped_at"] is None
    assert "family_visible" not in r.text


# --- edit -----------------------------------------------------------------------------------------------------------------

def test_a_nurse_edits_a_medication_and_only_the_fields_sent_change(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    r = client.patch(_med_url(added["medication"]["id"]), headers=h(nurse), json={
        "expected_version": added["plan_version"], "strength": "250 mg", "times_of_day": ["12:00"], "note": None,
        "end_date": None, "frequency": "once_daily"})
    assert r.status_code == 200, r.text
    edited = r.json()["medication"]
    assert edited == {**added["medication"], "strength": "250 mg", "times_of_day": ["12:00"], "note": None, "end_date": None,
                      "frequency": "once_daily"}
    assert r.json()["plan_version"] == added["plan_version"] + 1
    (event,) = _events(db, plan["id"], "med.update")
    assert event.detail == {"medication_id": edited["id"], "fields": ["strength", "note", "frequency", "end_date", "times_of_day"]}
    assert SECRET_MED_NOTE not in json.dumps(event.detail)


def test_an_edit_that_changes_nothing_keeps_the_version(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    r = client.patch(_med_url(added["medication"]["id"]), headers=h(nurse), json={"expected_version": added["plan_version"]})
    assert r.status_code == 200 and r.json()["plan_version"] == added["plan_version"]
    assert _events(db, plan["id"], "med.update") == []


def test_an_edit_needs_the_current_version_and_a_conflict_returns_the_plan(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    r = client.patch(_med_url(added["medication"]["id"]), headers=h(nurse), json={"expected_version": plan["version"], "name": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "version_conflict"
    assert r.json()["detail"]["plan"]["medications"][0]["name"] == SECRET_MED  # unchanged
    assert client.patch(_med_url(added["medication"]["id"]), headers=h(nurse), json={"name": "x"}).status_code == 422  # required


def test_required_fields_cannot_be_cleared_and_dates_are_checked_together(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    url, v = _med_url(added["medication"]["id"]), added["plan_version"]
    for field in ("name", "frequency", "times_of_day", "start_date"):
        assert client.patch(url, headers=h(nurse), json={"expected_version": v, field: None}).status_code == 422, field
    r = client.patch(url, headers=h(nurse), json={"expected_version": v, "start_date": "2026-12-01"})  # after the stored end date
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_care_plan"
    assert client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()["medications"][0]["start_date"] == "2026-10-28"


# --- stop -----------------------------------------------------------------------------------------------------------------

def test_stopping_keeps_the_medication_as_history_and_lists_it_after_the_active_ones(db, client):
    nurse, _, _, plan = started_plan(client, db)
    first = add_med(client, nurse, plan["id"], name="Placeholder first medicine").json()
    second = add_med(client, nurse, plan["id"], name="Placeholder second medicine").json()
    r = client.post(_med_url(first["medication"]["id"], "/stop"), headers=h(nurse), json={"expected_version": second["plan_version"]})
    assert r.status_code == 200, r.text
    stopped = r.json()["medication"]
    assert stopped["status"] == "stopped" and stopped["stopped_at"] is not None and stopped["name"] == "Placeholder first medicine"
    assert r.json()["plan_version"] == second["plan_version"] + 1
    listed = _plan(client, nurse, plan["id"])["medications"]
    assert [(m["name"], m["status"]) for m in listed] == [("Placeholder second medicine", "active"),
                                                         ("Placeholder first medicine", "stopped")]
    assert db.query(CarePlanMedication).count() == 2  # never deleted
    assert [e.detail for e in _events(db, plan["id"], "med.stop")] == [{"medication_id": stopped["id"]}]


def test_stopping_twice_changes_nothing(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    url = _med_url(added["medication"]["id"], "/stop")
    first = client.post(url, headers=h(nurse), json={"expected_version": added["plan_version"]}).json()
    again = client.post(url, headers=h(nurse), json={"expected_version": first["plan_version"]})
    assert again.status_code == 200 and again.json() == first
    assert len(_events(db, plan["id"], "med.stop")) == 1


def test_a_stopped_medication_cannot_be_edited(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    stopped = client.post(_med_url(added["medication"]["id"], "/stop"), headers=h(nurse),
                          json={"expected_version": added["plan_version"]}).json()
    r = client.patch(_med_url(added["medication"]["id"]), headers=h(nurse),
                     json={"expected_version": stopped["plan_version"], "name": "Placeholder other"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "medication_stopped"
    assert db.query(CarePlanMedication).one().status == "stopped"


def test_stopping_needs_the_current_version(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    url = _med_url(added["medication"]["id"], "/stop")
    assert client.post(url, headers=h(nurse), json={"expected_version": plan["version"]}).status_code == 409
    assert client.post(url, headers=h(nurse), json={}).status_code == 422
    assert db.query(CarePlanMedication).one().status == "active"


# --- closed plans, roles --------------------------------------------------------------------------------------------------

def test_a_completed_plan_takes_no_medication_writes_but_still_shows_them(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    done = client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse),
                        json={"expected_version": added["plan_version"], "status": "completed"}).json()
    mid = added["medication"]["id"]
    assert add_med(client, nurse, plan["id"]).json()["detail"]["code"] == "plan_not_editable"
    assert client.patch(_med_url(mid), headers=h(nurse), json={"expected_version": done["version"], "name": "x"}).status_code == 409
    assert client.post(_med_url(mid, "/stop"), headers=h(nurse), json={"expected_version": done["version"]}).status_code == 409
    assert [m["name"] for m in done["medications"]] == [SECRET_MED]


def test_only_a_nurse_writes_but_every_assigned_caregiver_reads(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    helper = colleague(db, patient)
    mid, v = added["medication"]["id"], added["plan_version"]
    for r in (add_med(client, helper, plan["id"]),
              client.patch(_med_url(mid), headers=h(helper), json={"expected_version": v, "name": "x"}),
              client.post(_med_url(mid, "/stop"), headers=h(helper), json={"expected_version": v})):
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_nurse"
    assert _plan(client, helper, plan["id"])["medications"][0]["name"] == SECRET_MED
    assert db.query(CarePlanMedication).one().status == "active"


def test_a_second_nurse_on_the_same_client_may_stop_it(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    other = colleague(db, patient, nurse=True)
    r = client.post(_med_url(added["medication"]["id"], "/stop"), headers=h(other), json={"expected_version": added["plan_version"]})
    assert r.status_code == 200 and r.json()["medication"]["status"] == "stopped"


# --- record only, and the notice -------------------------------------------------------------------------------------------

def test_the_notice_is_the_decision_d4_wording():
    assert MEDICATION_NOTICE == ("This is a record of what the prescriber ordered. Oakly does not check doses or interactions "
                                 "and does not remind anyone to give medicines. Follow the prescriber's instructions.")


def test_the_notice_is_in_the_openapi_description_of_all_three_routes_and_of_the_plan_field(client):
    spec = client.get("/api/v1/openapi.json").json()
    paths = spec["paths"]
    operations = [paths["/api/v1/caregiver/care-plans/{plan_id}/medications"]["post"],
                  paths["/api/v1/caregiver/care-plan-medications/{medication_id}"]["patch"],
                  paths["/api/v1/caregiver/care-plan-medications/{medication_id}/stop"]["post"]]
    for operation in operations:
        assert MEDICATION_NOTICE in operation["description"]
    assert MEDICATION_NOTICE in spec["components"]["schemas"]["CarePlanOut"]["properties"]["medications"]["description"]
