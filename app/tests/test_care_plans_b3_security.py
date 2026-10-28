"""Slice B3 privacy: a medication is ciphertext at rest, key rotation reaches it, erasure wipes it, events hold no health text.
The model-introspection test that every ``*_encrypted`` column is in the rotation list is in test_care_plans_security.py and
covers every table named ``care_plan*``, this one included."""

import json

from sqlalchemy import text

from app.db.models.booking import BookingStatus
from app.db.models.care import CareRelationship
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanMedication
from app.services.account_erasure import AccountErasureService
from app.tests.care_plan_helpers import (
    BASE, SECRET_MED, SECRET_MED_TEXTS, SECRET_TEXTS, add_med, create_plan, h, make_nurse, relative_visit, started_plan,
)


def test_the_medication_text_is_ciphertext_in_the_table_and_the_enums_are_not(db, client):
    nurse, _, _, plan = started_plan(client, db)
    add_med(client, nurse, plan["id"])
    dump = db.execute(text("SELECT row_to_json(t)::text FROM care_plan_medications t")).scalar_one()
    for secret in SECRET_MED_TEXTS + ("500 mg", "tablet", "oral"):
        assert secret not in dump
    row = db.query(CarePlanMedication).one()
    assert row.detail_encrypted.startswith("gAAAAA") and row.frequency == "twice_daily" and row.times_of_day == ["08:00", "20:00"]


def test_an_edit_reseals_the_whole_document(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    before = db.query(CarePlanMedication).one().detail_encrypted
    client.patch(f"{BASE}/care-plan-medications/{added['medication']['id']}", headers=h(nurse),
                 json={"expected_version": added["plan_version"], "route": "topical"})
    db.expire_all()
    after = db.query(CarePlanMedication).one().detail_encrypted
    assert after != before and "topical" not in after and after.startswith("gAAAAA")


def test_the_events_hold_ids_and_field_names_never_health_text(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    mid, v = added["medication"]["id"], added["plan_version"]
    v = client.patch(f"{BASE}/care-plan-medications/{mid}", headers=h(nurse), json={"expected_version": v, "name": "Placeholder renamed"}).json()["plan_version"]
    client.post(f"{BASE}/care-plan-medications/{mid}/stop", headers=h(nurse), json={"expected_version": v})
    events = db.query(CarePlanEvent).filter(CarePlanEvent.action.like("med.%")).all()
    assert sorted(e.action for e in events) == ["med.add", "med.stop", "med.update"]
    blob = json.dumps([e.detail for e in events])
    for secret in SECRET_MED_TEXTS + ("Placeholder renamed", "500 mg"):
        assert secret not in blob


def test_a_medication_that_cannot_be_decrypted_is_a_server_error_never_a_blank(db, client):
    nurse, _, _, plan = started_plan(client, db)
    added = add_med(client, nurse, plan["id"]).json()
    db.execute(text("UPDATE care_plan_medications SET detail_encrypted = 'not-a-token' WHERE id = :i"), {"i": added["medication"]["id"]})
    db.commit()
    r = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse))
    assert r.status_code == 500 and SECRET_MED not in r.text


def test_key_rotation_reaches_the_medication_detail(db, client):
    from scripts.reencrypt_health_data import reencrypt

    nurse, _, _, plan = started_plan(client, db)
    add_med(client, nurse, plan["id"])
    report = reencrypt(dry_run=False)
    assert report["care_plan_medications.detail_encrypted"].rotated >= 1
    assert report["care_plan_medications.detail_encrypted"].unreadable == 0
    db.expire_all()
    assert client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()["medications"][0]["name"] == SECRET_MED


def test_erasing_the_person_wipes_every_medication_and_keeps_the_rows_and_the_trail(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    first = add_med(client, nurse, plan["id"]).json()
    second = add_med(client, nurse, plan["id"], name="Placeholder second medicine").json()
    client.post(f"{BASE}/care-plan-medications/{first['medication']['id']}/stop", headers=h(nurse),
                json={"expected_version": second["plan_version"]})
    db.add(CareRelationship(contact_user_id=booker.user_id, recipient_id=recipient.id, role="self", clinical_access=True))
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(booker.user_id)
    db.commit()
    rows = db.query(CarePlanMedication).filter_by(plan_id=plan["id"]).all()
    assert len(rows) == 2 and all(m.detail_encrypted is None for m in rows)
    assert {m.status for m in rows} == {"active", "stopped"} and {m.frequency for m in rows} == {"twice_daily"}
    assert db.get(CarePlan, plan["id"]).status == "archived"
    dump = "".join(r[0] for table in ("care_plans", "care_plan_goals", "care_plan_tasks", "care_plan_medications", "care_plan_events")
                   for r in db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")))
    for secret in SECRET_TEXTS + SECRET_MED_TEXTS + ("Placeholder second medicine",):
        assert secret not in dump


def test_erasing_a_booker_leaves_the_medications_of_the_person_they_booked_for(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    add_med(client, nurse, plan["id"])
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(booker.user_id)
    db.commit()
    assert db.query(CarePlanMedication).one().detail_encrypted is not None
