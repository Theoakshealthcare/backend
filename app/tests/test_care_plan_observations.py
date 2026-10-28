"""Care plan observations, slice B2: append-only visit notes, newest first, keyset paged."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.security import decrypt
from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanObservation
from app.main import app as fastapi_app
from app.tests.care_plan_helpers import BASE, SECRET_NOTE, colleague, h, started_plan


def _url(plan_id):
    return f"{BASE}/care-plans/{plan_id}/observations"


def _add(client, caregiver, plan_id, body=SECRET_NOTE, **extra):
    return client.post(_url(plan_id), json={"body": body, **extra}, headers=h(caregiver))


def test_a_nurse_adds_a_note_during_the_visit(db, client):
    nurse, _, booking, plan = started_plan(client, db)
    r = _add(client, nurse, plan["id"])
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"id", "category", "body", "author", "created_at"}
    assert r.json()["category"] == "observation" and r.json()["body"] == SECRET_NOTE
    assert r.json()["author"] == {"name": nurse.full_name, "kind": "caregiver"}
    row = db.get(CarePlanObservation, r.json()["id"])
    assert row.author_caregiver_id == nurse.id and row.author_admin_id is None and row.booking_id == booking.id
    assert row.family_visible is False
    assert db.get(CarePlan, plan["id"]).version == plan["version"]  # a note overwrites nothing, so no version bump


def test_a_caregiver_who_is_not_a_nurse_may_add_a_note(db, client):
    _, patient, _, plan = started_plan(client, db)
    plain = colleague(db, patient)
    r = _add(client, plain, plan["id"], category="special_note")
    assert r.status_code == 201 and r.json()["category"] == "special_note"
    assert r.json()["author"]["name"] == plain.full_name


def test_the_body_is_ciphertext_at_rest_and_the_event_holds_no_text(db, client):
    nurse, _, _, plan = started_plan(client, db)
    note = _add(client, nurse, plan["id"]).json()
    raw = db.execute(text("SELECT body_encrypted FROM care_plan_observations WHERE id = :i"), {"i": note["id"]}).scalar_one()
    assert SECRET_NOTE not in raw and decrypt(raw) == SECRET_NOTE
    events = db.query(CarePlanEvent).filter_by(plan_id=plan["id"], action="observation.add").all()
    assert [e.detail for e in events] == [{"observation_id": note["id"], "category": "observation"}]
    assert events[0].actor_id == nurse.id
    assert SECRET_NOTE not in "".join(str(e.detail) for e in db.query(CarePlanEvent).all())


def test_the_family_flag_is_not_a_request_field(db, client):
    nurse, _, _, plan = started_plan(client, db)
    r = _add(client, nurse, plan["id"], family_visible=True)
    assert r.status_code == 201
    assert db.get(CarePlanObservation, r.json()["id"]).family_visible is False
    assert "family_visible" not in r.text


@pytest.mark.parametrize("over", [{"body": ""}, {"body": "   "}, {"body": "x" * 2001}, {"category": "diary"}, {"body": None}])
def test_note_limits_are_422_and_never_echo_the_text(db, client, over):
    nurse, _, _, plan = started_plan(client, db)
    r = client.post(_url(plan["id"]), json={"body": SECRET_NOTE, **over}, headers=h(nurse))
    assert r.status_code == 422 and all("input" not in e for e in r.json()["detail"])
    assert db.query(CarePlanObservation).count() == 0


def test_a_note_of_exactly_two_thousand_characters_is_accepted(db, client):
    nurse, _, _, plan = started_plan(client, db)
    assert _add(client, nurse, plan["id"], body="x" * 2000).status_code == 201


@pytest.mark.parametrize("status", (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE))
def test_a_note_needs_a_visit_in_progress(db, client, status):
    _, patient, _, plan = started_plan(client, db)
    waiting = colleague(db, patient, status=status)
    r = _add(client, waiting, plan["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "visit_not_in_progress"
    assert db.query(CarePlanObservation).count() == 0


def test_a_closed_plan_takes_no_new_notes_but_keeps_the_old_ones(db, client):
    nurse, _, _, plan = started_plan(client, db)
    _add(client, nurse, plan["id"])
    client.patch(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse), json={"expected_version": plan["version"], "status": "completed"})
    r = _add(client, nurse, plan["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "plan_not_editable"
    assert len(client.get(_url(plan["id"]), headers=h(nurse)).json()["items"]) == 1


def test_notes_are_listed_newest_first_with_their_authors_to_every_caregiver_on_the_client(db, client):
    nurse, patient, _, plan = started_plan(client, db)
    helper = colleague(db, patient)
    first = _add(client, nurse, plan["id"], body="Placeholder first").json()
    second = _add(client, helper, plan["id"], body="Placeholder second", category="special_note").json()
    r = client.get(_url(plan["id"]), headers=h(helper))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert [i["id"] for i in r.json()["items"]] == [second["id"], first["id"]]
    assert [i["author"]["name"] for i in r.json()["items"]] == [helper.full_name, nurse.full_name]
    assert r.json()["next_before"] is None
    only = client.get(_url(plan["id"]) + "?category=special_note", headers=h(nurse)).json()["items"]
    assert [i["id"] for i in only] == [second["id"]]
    assert client.get(_url(plan["id"]) + "?category=diary", headers=h(nurse)).status_code == 422


def test_keyset_paging_walks_every_note_once(db, client):
    nurse, _, _, plan = started_plan(client, db)
    ids = []
    base = datetime.utcnow() - timedelta(hours=1)
    for n in range(5):
        ids.append(_add(client, nurse, plan["id"], body=f"Placeholder note {n}").json()["id"])
    for n, note_id in enumerate(ids):  # distinct, well-separated timestamps
        db.query(CarePlanObservation).filter_by(id=note_id).update({"created_at": base + timedelta(minutes=n)})
    db.commit()
    seen, before = [], None
    for _ in range(5):
        url = _url(plan["id"]) + "?limit=2" + (f"&before={before}" if before else "")
        page = client.get(url, headers=h(nurse)).json()
        seen += [i["id"] for i in page["items"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == list(reversed(ids))
    assert before is None


def test_before_accepts_a_timezone_aware_timestamp(db, client):
    nurse, _, _, plan = started_plan(client, db)
    note = _add(client, nurse, plan["id"]).json()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S") + "%2B01:00"  # 02:00 ahead at +01:00 = 01:00 ahead of UTC
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [i["id"] for i in client.get(_url(plan["id"]) + f"?before={future}", headers=h(nurse)).json()["items"]] == [note["id"]]
    assert client.get(_url(plan["id"]) + f"?before={past}", headers=h(nurse)).json()["items"] == []


@pytest.mark.parametrize("limit", ("0", "51", "-1", "many"))
def test_limit_is_bounded(db, client, limit):
    nurse, _, _, plan = started_plan(client, db)
    assert client.get(_url(plan["id"]) + f"?limit={limit}", headers=h(nurse)).status_code == 422


def test_notes_cannot_be_edited_or_deleted(db, client):
    """Append-only: a correction is a new note. No route accepts another method on a note."""
    nurse, _, _, plan = started_plan(client, db)
    note = _add(client, nurse, plan["id"]).json()
    for method in ("PATCH", "PUT", "DELETE"):
        for path in (_url(plan["id"]), f"{BASE}/care-plan-observations/{note['id']}", f"{_url(plan['id'])}/{note['id']}"):
            assert client.request(method, path, json={"body": "changed"}, headers=h(nurse)).status_code in (404, 405), (method, path)
    paths = {(m, r.path) for r in fastapi_app.routes if hasattr(r, "methods") and "observation" in r.path for m in r.methods}
    assert {m for m, _ in paths} <= {"GET", "POST"}
    assert decrypt(db.get(CarePlanObservation, note["id"]).body_encrypted) == SECRET_NOTE


def test_a_note_after_erasure_reads_as_empty_text(db, client):
    nurse, _, _, plan = started_plan(client, db)
    note = _add(client, nurse, plan["id"]).json()
    db.query(CarePlanObservation).filter_by(id=note["id"]).update({"body_encrypted": None})
    db.commit()
    assert client.get(_url(plan["id"]), headers=h(nurse)).json()["items"][0]["body"] == ""


def test_notes_with_the_same_timestamp_are_not_skipped_when_the_id_cursor_is_used(db, client):
    nurse, _, _, plan = started_plan(client, db)
    ids = [_add(client, nurse, plan["id"], body=f"Placeholder note {n}").json()["id"] for n in range(4)]
    same = datetime.utcnow() - timedelta(minutes=5)
    db.query(CarePlanObservation).filter_by(plan_id=plan["id"]).update({"created_at": same})
    db.commit()
    seen, params = [], {"limit": 1}
    for _ in range(6):
        page = client.get(_url(plan["id"]), params=params, headers=h(nurse)).json()
        seen += [i["id"] for i in page["items"]]
        if page["next_before"] is None:
            assert page["next_before_id"] is None
            break
        params = {"limit": 1, "before": page["next_before"], "before_id": page["next_before_id"]}
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen)) == 4
