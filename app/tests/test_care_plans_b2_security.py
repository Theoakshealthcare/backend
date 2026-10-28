"""Slice B2 privacy: notes are ciphertext, key rotation reaches them, erasure wipes them (and the ticks stay), and the family
flag stays private. The model-introspection test that every ``*_encrypted`` column is in the rotation list is in
test_care_plans_security.py and covers every table named ``care_plan*``."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.models.booking import BookingStatus
from app.db.models.care import CareRelationship
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanObservation, CarePlanTask, CarePlanTaskCompletion
from app.db.models.consent import Consent
from app.services.account_erasure import AccountErasureService
from app.services.care_plan_access import PlanNotAvailable, resolve_family_access
from app.tests.care_plan_helpers import (
    BASE, SECRET_NOTE, SECRET_TEXTS, create_plan, daily_task, h, make_nurse, relative_visit, started_plan, tick, today_iso,
)
from app.tests.factories import make_user


def _note(client, caregiver, plan_id, body=SECRET_NOTE, **extra):
    r = client.post(f"{BASE}/care-plans/{plan_id}/observations", json={"body": body, **extra}, headers=h(caregiver))
    assert r.status_code == 201, r.text
    return r.json()


def test_key_rotation_reaches_the_note_bodies(db, client):
    from scripts.reencrypt_health_data import reencrypt

    nurse, _, _, plan = started_plan(client, db)
    _note(client, nurse, plan["id"])
    report = reencrypt(dry_run=False)
    assert report["care_plan_observations.body_encrypted"].rotated >= 1
    assert report["care_plan_observations.body_encrypted"].unreadable == 0
    db.expire_all()
    assert client.get(f"{BASE}/care-plans/{plan['id']}/observations", headers=h(nurse)).json()["items"][0]["body"] == SECRET_NOTE


def test_a_note_that_cannot_be_decrypted_is_a_server_error_never_a_blank(db, client):
    nurse, _, _, plan = started_plan(client, db)
    note = _note(client, nurse, plan["id"])
    db.execute(text("UPDATE care_plan_observations SET body_encrypted = 'not-a-token' WHERE id = :i"), {"i": note["id"]})
    db.commit()
    r = client.get(f"{BASE}/care-plans/{plan['id']}/observations", headers=h(nurse))
    assert r.status_code == 500 and SECRET_NOTE not in r.text


def _seed_everything(db, client):
    """A relative's plan filed under the person themselves (the 'self' recipient), with a tick and two notes."""
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking, start_date=today_iso(), tasks=[daily_task()]).json()
    task_id = plan["tasks"][0]["id"]
    assert tick(client, nurse, task_id).status_code == 200
    _note(client, nurse, plan["id"])
    _note(client, nurse, plan["id"], body="Placeholder second note", category="special_note")
    db.add(CareRelationship(contact_user_id=booker.user_id, recipient_id=recipient.id, role="self", clinical_access=True))
    booking.status = BookingStatus.COMPLETED
    db.commit()
    return booker, plan, task_id


def test_erasing_the_person_wipes_every_note_and_keeps_the_ticks_and_the_trail(db, client):
    booker, plan, task_id = _seed_everything(db, client)
    AccountErasureService(db).erase(booker.user_id)
    db.commit()
    notes = db.query(CarePlanObservation).filter_by(plan_id=plan["id"]).all()
    assert len(notes) == 2 and all(n.body_encrypted is None for n in notes)
    assert {n.category for n in notes} == {"observation", "special_note"}  # the rows and their enums stay
    ticks = db.query(CarePlanTaskCompletion).filter_by(task_id=task_id).all()
    assert len(ticks) == 1 and ticks[0].for_date.isoformat() == today_iso()  # a tick is an id and a date, not health text
    assert db.get(CarePlan, plan["id"]).status == "archived"
    assert [e.action for e in db.query(CarePlanEvent).filter_by(plan_id=plan["id"]).order_by(CarePlanEvent.created_at)][-1] == "plan.erased"
    dump = "".join(r[0] for table in ("care_plans", "care_plan_goals", "care_plan_tasks", "care_plan_events",
                                      "care_plan_observations", "care_plan_task_completions")
                   for r in db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")))
    for secret in SECRET_TEXTS + (SECRET_NOTE, "Placeholder second note"):
        assert secret not in dump


def test_erasing_a_booker_leaves_the_notes_of_the_person_they_booked_for(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    _note(client, nurse, plan["id"])
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(booker.user_id)
    db.commit()
    assert db.query(CarePlanObservation).filter_by(plan_id=plan["id"]).one().body_encrypted is not None


# --- the family flag stays private ---------------------------------------------------------------------------------------

def test_the_flag_defaults_to_private_at_the_database_level_on_notes_too(db, client):
    nurse, _, _, plan = started_plan(client, db)
    note_id = _note(client, nurse, plan["id"])["id"]
    db.execute(text("INSERT INTO care_plan_observations (id, plan_id, author_caregiver_id, category, body_encrypted, created_at) "
                    "SELECT gen_random_uuid(), :p, author_caregiver_id, 'observation', 'x', now() FROM care_plan_observations "
                    "WHERE id = :n"), {"p": plan["id"], "n": note_id})
    db.commit()
    assert db.execute(text("SELECT count(*) FROM care_plan_observations WHERE family_visible")).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM care_plan_task_completions")).scalar_one() == 0


@pytest.fixture
def shared(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    old = _note(client, nurse, plan["id"], body="Placeholder shared note")
    _note(client, nurse, plan["id"], body="Placeholder private note")
    row = db.get(CarePlanObservation, old["id"])
    row.family_visible = True  # nothing in v1 does this; the test does it directly
    relative = make_user(db)
    db.add(CareRelationship(contact_user_id=relative.id, recipient_id=recipient.id, role="family", clinical_access=True))
    db.add(Consent(subject_recipient_id=recipient.id, type="share_with_family", decision="granted", policy_version="v1",
                   channel="whatsapp_button", created_at=datetime.utcnow() - timedelta(minutes=5)))
    db.commit()
    return {"plan_id": plan["id"], "relative": relative, "shared_note": old["id"]}


def test_a_linked_relative_sees_only_the_notes_flagged_for_the_family(db, shared):
    access = resolve_family_access(db, shared["relative"], shared["plan_id"])
    assert [str(n.id) for n in access.observations] == [shared["shared_note"]]


def test_a_relative_who_is_refused_sees_no_notes(db, shared):
    db.query(CareRelationship).update({"clinical_access": False})
    db.commit()
    with pytest.raises(PlanNotAvailable):
        resolve_family_access(db, shared["relative"], shared["plan_id"])


def test_no_caregiver_route_can_set_the_flag(db, client):
    """The flag is in no request model, so a request that sends it sets nothing (notes, tasks, goals)."""
    from app.schemas import care_plan as schemas

    for name in dir(schemas):
        model = getattr(schemas, name)
        if isinstance(model, type) and hasattr(model, "model_fields") and name.endswith(("In", "Create", "Patch")):
            assert "family_visible" not in model.model_fields, name
    nurse, _, _, plan = started_plan(client, db)
    _note(client, nurse, plan["id"], family_visible=True)
    assert db.query(CarePlanObservation).filter(CarePlanObservation.family_visible.is_(True)).count() == 0
    assert db.query(CarePlanTask).filter(CarePlanTask.family_visible.is_(True)).count() == 0
