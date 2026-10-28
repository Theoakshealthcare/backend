"""Care plans: encryption at rest, key rotation, account erasure and the family-view groundwork."""

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.core.security import decrypt
from app.db.models.booking import BookingStatus
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanGoal, CarePlanTask
from app.db.models.consent import Consent
from app.db.models.patient import Patient
from app.services import care_plans
from app.services.account_erasure import AccountErasureService
from app.services.care_plan_access import PlanNotAvailable, resolve_family_access
from app.tests.care_plan_helpers import (
    BASE, SECRET_ALLERGY, SECRET_DIAGNOSIS, SECRET_GOAL, SECRET_INSTRUCTION, SECRET_TARGET, SECRET_TASK, SECRET_TEXTS,
    create_plan, h, make_nurse, relative_visit, self_visit,
)
from app.tests.factories import make_user

CARE_PLAN_TABLES = ("care_plans", "care_plan_goals", "care_plan_tasks")


# --- encryption at rest ---------------------------------------------------------------------------------------------

def test_health_text_is_ciphertext_in_the_database_and_round_trips(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    plan_id = create_plan(client, nurse, booking).json()["id"]
    plan = db.get(CarePlan, plan_id)
    goal = db.query(CarePlanGoal).filter_by(plan_id=plan_id).one()
    task = db.query(CarePlanTask).filter_by(plan_id=plan_id).one()
    stored = {
        "diagnosis": plan.diagnosis_encrypted, "allergies": plan.allergies_encrypted,
        "instructions": plan.special_instructions_encrypted, "goal": goal.title_encrypted,
        "target": goal.target_encrypted, "task": task.title_encrypted,
    }
    assert all(v and v.startswith("gAAAAA") for v in stored.values())
    for secret in SECRET_TEXTS:
        assert all(secret not in v for v in stored.values())
    assert decrypt(stored["diagnosis"]) == SECRET_DIAGNOSIS and decrypt(stored["goal"]) == SECRET_GOAL
    assert decrypt(stored["target"]) == SECRET_TARGET and decrypt(stored["task"]) == SECRET_TASK
    assert json.loads(decrypt(stored["allergies"])) == [SECRET_ALLERGY]
    assert json.loads(decrypt(stored["instructions"])) == [SECRET_INSTRUCTION]
    # nothing readable anywhere in the plan's rows, including the event trail
    dump = db.execute(text("SELECT row_to_json(p)::text FROM care_plans p")).scalar()
    dump += "".join(r[0] for r in db.execute(text("SELECT row_to_json(e)::text FROM care_plan_events e")))
    for secret in SECRET_TEXTS:
        assert secret not in dump


def test_a_wrong_key_is_a_server_error_not_a_blank_and_the_log_never_holds_the_value(db, client, caplog):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    row = db.get(CarePlan, plan["id"])
    row.diagnosis_encrypted = Fernet(Fernet.generate_key()).encrypt(SECRET_DIAGNOSIS.encode()).decode()  # another key
    db.commit()
    with caplog.at_level(logging.ERROR):
        r = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse))
    assert r.status_code == 500 and SECRET_DIAGNOSIS not in r.text and "diagnosis" not in r.text
    line = next(r.getMessage() for r in caplog.records if "decrypt failed" in r.getMessage())
    assert plan["id"] in line and "care_plans.diagnosis_encrypted" in line
    assert SECRET_DIAGNOSIS not in caplog.text


def test_a_plaintext_value_is_not_read_as_a_fallback(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    row = db.get(CarePlan, plan["id"])
    row.diagnosis_encrypted = "plain text someone wrote straight into the column"
    db.commit()
    r = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse))
    assert r.status_code == 500 and "plain text" not in r.text


def test_every_encrypted_column_on_the_care_plan_models_is_in_the_rotation_list():
    from scripts.reencrypt_health_data import COLUMNS

    import app.db.models  # noqa: F401
    from app.db.base import Base

    listed = {(c.table, c.column) for c in COLUMNS if c.table in CARE_PLAN_TABLES}
    actual = {(t, c.name) for t in CARE_PLAN_TABLES for c in Base.metadata.tables[t].columns if c.name.endswith("_encrypted")}
    assert actual and actual == listed
    assert all(not c.plaintext_fallback for c in COLUMNS if c.table in CARE_PLAN_TABLES)


def test_key_rotation_reaches_the_care_plan_columns(db, client):
    from scripts.reencrypt_health_data import reencrypt

    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    report = reencrypt(dry_run=False)
    for column in ("care_plans.diagnosis_encrypted", "care_plans.allergies_encrypted",
                   "care_plans.special_instructions_encrypted", "care_plan_goals.title_encrypted",
                   "care_plan_goals.target_encrypted", "care_plan_tasks.title_encrypted"):
        assert report[column].rotated >= 1 and report[column].unreadable == 0, column
    db.expire_all()
    assert client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()["diagnosis"] == SECRET_DIAGNOSIS


# --- account erasure ------------------------------------------------------------------------------------------------

def _no_ciphertext(db, plan_id):
    plan = db.get(CarePlan, plan_id)
    db.refresh(plan)
    assert plan.status == "archived" and plan.archived_at is not None
    assert plan.diagnosis_encrypted is None and plan.allergies_encrypted is None
    assert plan.special_instructions_encrypted is None
    goals = db.query(CarePlanGoal).filter_by(plan_id=plan_id).all()
    tasks = db.query(CarePlanTask).filter_by(plan_id=plan_id).all()
    assert goals and tasks
    assert all(g.title_encrypted is None and g.target_encrypted is None for g in goals)
    assert all(t.title_encrypted is None for t in tasks)


def test_erasing_a_patient_wipes_their_plan_and_keeps_the_trail(db, client):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(patient.user_id)
    db.commit()
    _no_ciphertext(db, plan["id"])
    actions = [e.action for e in db.query(CarePlanEvent).filter_by(plan_id=plan["id"]).order_by(CarePlanEvent.created_at)]
    assert actions == ["plan.create", "plan.erased"]
    erased = db.query(CarePlanEvent).filter_by(action="plan.erased").one()
    assert erased.actor_type == "system" and erased.actor_id is None
    row = db.get(CarePlan, plan["id"])
    assert row.patient_id == patient.id and row.risk_level == "high" and row.start_date  # ids, enums and dates stay
    dump = "".join(r[0] for table in ("care_plans", "care_plan_goals", "care_plan_tasks", "care_plan_events")
                   for r in db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")))
    for secret in SECRET_TEXTS:
        assert secret not in dump


def test_erasing_the_booker_leaves_a_relatives_plan_alone(db, client):
    """A recipient someone booked for is another person's data: erasure leaves it, as it leaves their bookings."""
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(booker.user_id)
    db.commit()
    row = db.get(CarePlan, plan["id"])
    db.refresh(row)
    assert row.status == "active" and row.diagnosis_encrypted is not None and decrypt(row.diagnosis_encrypted) == SECRET_DIAGNOSIS


def test_erasing_a_person_wipes_the_plan_filed_under_their_self_recipient_row(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    # The recipient is the person themselves (a WhatsApp user): their own account erases their own row and plan.
    db.add(CareRelationship(contact_user_id=booker.user_id, recipient_id=recipient.id, role="self", clinical_access=True))
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(booker.user_id)
    db.commit()
    _no_ciphertext(db, plan["id"])


# --- family-view groundwork (not exposed by any route) -----------------------------------------------------------------

@pytest.fixture
def shared(db, client):
    """A relative's plan with one shared goal and one private goal, one shared task and one private task, and a family
    member linked with clinical access and a granted consent."""
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking, goals=[
        {"title": "Shared goal", "frequency": "daily"}, {"title": "Private goal", "frequency": "daily"}],
        tasks=[{"title": "Shared task", "time_slot": "morning", "days_of_week": [1]},
               {"title": "Private task", "time_slot": "evening", "days_of_week": [2]}]).json()
    goals = {g.sort_order: g for g in db.query(CarePlanGoal).filter_by(plan_id=plan["id"])}
    tasks = {t.sort_order: t for t in db.query(CarePlanTask).filter_by(plan_id=plan["id"])}
    goals[0].family_visible = tasks[0].family_visible = True  # nothing in v1 does this; the test does it directly
    relative = make_user(db)
    link = CareRelationship(contact_user_id=relative.id, recipient_id=recipient.id, role="family", clinical_access=True)
    db.add(link)
    _consent(db, recipient, "granted", minutes_ago=10)
    db.commit()
    return {"plan_id": plan["id"], "recipient": recipient, "relative": relative, "link": link, "booker": booker}


def _consent(db, recipient, decision, minutes_ago=0):
    db.add(Consent(subject_recipient_id=recipient.id, type="share_with_family", decision=decision, policy_version="v1",
                   channel="whatsapp_button", created_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
    db.commit()


def test_family_visible_defaults_to_private_at_the_database_level(db, shared):
    db.execute(text("INSERT INTO care_plan_goals (id, plan_id, frequency, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :p, 'daily', now(), now())"), {"p": shared["plan_id"]})
    db.execute(text("INSERT INTO care_plan_tasks (id, plan_id, time_slot, days_of_week, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :p, 'morning', '{1}', now(), now())"), {"p": shared["plan_id"]})
    db.commit()
    assert db.execute(text("SELECT count(*) FROM care_plan_goals WHERE family_visible AND plan_id = :p"),
                      {"p": shared["plan_id"]}).scalar() == 1  # only the one the fixture shared


def test_a_linked_relative_with_clinical_access_and_consent_sees_only_the_shared_items(db, shared):
    access = resolve_family_access(db, shared["relative"], shared["plan_id"])
    assert [decrypt(g.title_encrypted) for g in access.goals] == ["Shared goal"]
    assert [decrypt(t.title_encrypted) for t in access.tasks] == ["Shared task"]
    assert not hasattr(access, "plan") and not hasattr(access, "diagnosis")  # the header never travels with it


def test_two_relatives_of_one_client_both_get_in(db, shared):
    second = make_user(db)
    db.add(CareRelationship(contact_user_id=second.id, recipient_id=shared["recipient"].id, role="guardian",
                            clinical_access=True))
    db.commit()
    assert len(resolve_family_access(db, second, shared["plan_id"]).goals) == 1
    assert len(resolve_family_access(db, shared["relative"], shared["plan_id"]).goals) == 1


def test_the_latest_consent_decides(db, shared):
    _consent(db, shared["recipient"], "withdrawn", minutes_ago=5)
    with pytest.raises(PlanNotAvailable):
        resolve_family_access(db, shared["relative"], shared["plan_id"])
    _consent(db, shared["recipient"], "granted", minutes_ago=1)
    assert resolve_family_access(db, shared["relative"], shared["plan_id"]).goals


def _refused(db, user, plan_id):
    with pytest.raises(PlanNotAvailable):
        resolve_family_access(db, user, plan_id)


def test_no_consent_no_clinical_access_wrong_role_or_no_link_is_refused(db, shared):
    plan_id, relative, recipient = shared["plan_id"], shared["relative"], shared["recipient"]
    _refused(db, make_user(db), plan_id)  # not linked at all
    shared["link"].clinical_access = False
    db.commit()
    _refused(db, relative, plan_id)
    shared["link"].clinical_access, shared["link"].role = True, "self"
    db.commit()
    _refused(db, relative, plan_id)  # role self is the person, not a family member
    shared["link"].role = "family"
    db.commit()
    assert resolve_family_access(db, relative, plan_id)
    other_recipient = CareRecipient(full_name="Placeholder Other", created_by_user_id=shared["booker"].user_id)
    db.add(other_recipient)
    db.commit()
    stranger = make_user(db)
    db.add(CareRelationship(contact_user_id=stranger.id, recipient_id=other_recipient.id, role="family", clinical_access=True))
    _consent(db, other_recipient, "granted")
    _refused(db, stranger, plan_id)  # another client's relative, with their own consent, still sees nothing here


def test_a_relative_without_any_consent_row_is_refused(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    relative = make_user(db)
    db.add(CareRelationship(contact_user_id=relative.id, recipient_id=recipient.id, role="family", clinical_access=True))
    db.commit()
    _refused(db, relative, plan["id"])


def test_an_archived_erased_or_missing_plan_and_an_inactive_relative_are_refused(db, shared):
    plan_id, relative = shared["plan_id"], shared["relative"]
    relative.is_active = False
    db.commit()
    _refused(db, relative, plan_id)
    relative.is_active = True
    db.commit()
    plan = db.get(CarePlan, plan_id)
    plan.status = "archived"
    db.commit()
    _refused(db, relative, plan_id)
    plan.status, plan.diagnosis_encrypted = "completed", None  # what erasure leaves
    db.commit()
    _refused(db, relative, plan_id)
    _refused(db, relative, uuid.uuid4())


def test_a_plan_filed_under_a_patient_is_refused_until_the_family_view_decides_how_to_link_it(db, client):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    _refused(db, make_user(db), plan["id"])


def test_no_route_calls_the_family_check():
    root = Path(__file__).resolve().parents[1]
    callers = [str(p.relative_to(root)) for p in (root / "api").rglob("*.py") if "resolve_family_access" in p.read_text()]
    assert callers == []
