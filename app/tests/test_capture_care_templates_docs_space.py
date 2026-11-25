"""Writes the live captures behind docs/fe-integration-guide-care-templates-docs-space.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CARE_TEMPLATES=1 poetry run pytest app/tests/test_capture_care_templates_docs_space.py -q

Every body in the guide is one of these files, byte for byte (long lists are trimmed and the file says so). Names and ids are
placeholders made up here; the tokens are replaced by a placeholder.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlanTemplate
from app.tests.care_plan_helpers import BASE, h, make_nurse, plan_body, self_visit
from app.tests.factories import auth_headers, make_admin, make_caregiver

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CARE_TEMPLATES"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "care-templates-docs-space"
ADMIN = "/api/v1/admin"


def _write(name, doc):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _http(r, method="GET", body=None, note=None, trim=None):
    payload = r.json() if r.content else None
    doc = {"request": {"method": method, "path": r.request.url.path + (f"?{r.request.url.query}" if r.request.url.query else ""),
                       "auth": "Bearer <ACCESS_TOKEN_JWT>", "body": body},
           "response": {"status_code": r.status_code, "body": payload}}
    if trim and isinstance(payload, dict):
        for key, keep in trim.items():
            if isinstance(payload.get(key), list) and len(payload[key]) > keep:
                payload[key] = payload[key][:keep]
                doc["note"] = ((note + " ") if note else "") + f"`{key}` trimmed to its first {keep} items; the real list is longer."
    if note and "note" not in doc:
        doc["note"] = note
    return doc


def _save(name, r, method="GET", body=None, note=None, trim=None):
    _write(name, _http(r, method, body, note, trim))
    return r


def _template(db, key):
    return db.query(CarePlanTemplate).filter_by(key=key).one()


def test_capture(db, client):
    nurse = make_nurse(db, full_name="Placeholder Nurse Ngozi")
    plain = make_caregiver(db, full_name="Placeholder Caregiver Chidi")
    hn, hp = h(nurse), h(plain)

    # --- templates ---------------------------------------------------------------------------------------------------
    _save("001-nurse-templates-list.json", client.get(f"{BASE}/care-plan-templates", headers=hn),
          note="A nurse sees all five. Four are version 2 (clinician-approved text); Elderly is version 1 and `caregiver_can_use`.")
    _save("002-caregiver-templates-list.json", client.get(f"{BASE}/care-plan-templates", headers=hp),
          note="A caregiver without the nurse skill sees only the template flagged `caregiver_can_use` (it was a 403 before).")
    post_surgery = _template(db, "post_surgery_recovery")
    _save("003-nurse-template-post-surgery.json", client.get(f"{BASE}/care-plan-templates/{post_surgery.id}", headers=hn),
          note="Version 2: the pain task and the family-and-doctor line.", trim={"tasks": 12})
    stroke = _template(db, "stroke_recovery")
    _save("004-caregiver-template-nurse-only.json", client.get(f"{BASE}/care-plan-templates/{stroke.id}", headers=hp),
          note="A nurse-only template, asked for by a caregiver without the nurse skill.")

    # --- who can start a plan ----------------------------------------------------------------------------------------
    _, nurse_visit = self_visit(db, nurse)
    _, plain_visit = self_visit(db, plain)
    _save("010-nurse-visit-summary.json", client.get(f"{BASE}/bookings/{nurse_visit.id}/care-plan", headers=hn),
          note="`author_scope: full` for a nurse.")
    _save("011-caregiver-visit-summary.json", client.get(f"{BASE}/bookings/{plain_visit.id}/care-plan", headers=hp),
          note="`author_scope: caregiver_templates` and `can_author: false` for a caregiver without the nurse skill.")
    blank = {"diagnosis": "Placeholder diagnosis", "risk_level": "low"}
    _save("012-caregiver-create-blank-refused.json",
          client.post(f"{BASE}/bookings/{plain_visit.id}/care-plan", headers=hp, json=blank), "POST", blank)
    elderly = _template(db, "elderly_daily_living_dementia")
    body = {"template_id": str(elderly.id), "diagnosis": "Placeholder diagnosis"}
    created = _save("013-caregiver-create-from-elderly.json",
                    client.post(f"{BASE}/bookings/{plain_visit.id}/care-plan", headers=hp, json=body), "POST", body,
                    note="`capabilities.can_edit_plan` is true; `can_edit_medications` is false.", trim={"tasks": 3})
    plan_id = created.json()["id"]
    med = {"name": "Placeholder medicine", "frequency": "once_daily", "start_date": "2026-11-25"}
    _save("014-caregiver-add-medication-refused.json",
          client.post(f"{BASE}/care-plans/{plan_id}/medications", headers=hp, json=med), "POST", med,
          note="Medicines stay nurse-only, even on a plan the caregiver may edit.")
    patch = {"expected_version": created.json()["version"], "diagnosis": "Placeholder diagnosis, updated"}
    _save("015-caregiver-edit-elderly-plan.json", client.patch(f"{BASE}/care-plans/{plan_id}", headers=hp, json=patch), "PATCH", patch,
          trim={"tasks": 2, "goals": 2})

    # --- the nurse's 2-hour grace for care-plan notes ----------------------------------------------------------------
    nurse_plan = client.post(f"{BASE}/bookings/{nurse_visit.id}/care-plan", headers=hn,
                             json=plan_body(start_date=datetime.utcnow().date().isoformat())).json()
    live = _save("020-nurse-plan-live-visit.json", client.get(f"{BASE}/care-plans/{nurse_plan['id']}", headers=hn),
                 note="During the visit: `observations_close_at` is null.", trim={"tasks": 2})
    nurse_visit.status = BookingStatus.COMPLETED
    nurse_visit.actual_end_time = datetime.utcnow() - timedelta(minutes=30)
    db.commit()
    _save("021-nurse-visit-summary-grace.json", client.get(f"{BASE}/bookings/{nurse_visit.id}/care-plan", headers=hn),
          note="30 minutes after the visit was completed. `can_author` is false (a plan is created during a live visit).")
    _save("022-nurse-plan-grace.json", client.get(f"{BASE}/care-plans/{nurse_plan['id']}", headers=hn),
          note="30 minutes after completion: only `can_add_observation` is true; `observations_close_at` is the end of the window.",
          trim={"tasks": 2})
    note = {"body": "Placeholder: client rested well after the visit."}
    _save("023-nurse-note-in-grace.json",
          client.post(f"{BASE}/care-plans/{nurse_plan['id']}/observations", headers=hn, json=note), "POST", note)
    edit = {"expected_version": live.json()["version"], "diagnosis": "Placeholder"}
    _save("024-nurse-edit-in-grace-refused.json",
          client.patch(f"{BASE}/care-plans/{nurse_plan['id']}", headers=hn, json=edit), "PATCH", edit,
          note="Editing still needs a live visit.")
    nurse_visit.actual_end_time = datetime.utcnow() - timedelta(minutes=125)
    db.commit()
    _save("025-nurse-note-after-grace.json",
          client.post(f"{BASE}/care-plans/{nurse_plan['id']}/observations", headers=hn, json=note), "POST", note,
          note="125 minutes after completion: the same 404 as any lost access.")

    # --- the caregiver's own documentation ---------------------------------------------------------------------------
    doc_visit = self_visit(db, plain)[1]
    doc_url = f"{BASE}/bookings/{doc_visit.id}/documentation"
    entry = {"body": "Placeholder: gave breakfast at 08:30, client walked in the garden."}
    first = _save("030-caregiver-documentation-add.json", client.post(doc_url, headers=hp, json=entry), "POST", entry,
                  note="Any caregiver on the visit may write one. Private: never shown to the family.")
    assert first.status_code == 201
    _save("031-caregiver-documentation-list-live.json", client.get(doc_url, headers=hp),
          note="`can_add` true; `closes_at` and `readable_until` are null while the visit is in progress.")
    doc_visit.status = BookingStatus.COMPLETED
    doc_visit.actual_end_time = datetime.utcnow() - timedelta(minutes=30)
    db.commit()
    _save("032-caregiver-documentation-list-after-visit.json", client.get(doc_url, headers=hp),
          note="30 minutes after completion: `closes_at` is 2 h after the end, `readable_until` 24 h after it.")
    doc_visit.actual_end_time = datetime.utcnow() - timedelta(minutes=130)
    db.commit()
    _save("033-caregiver-documentation-add-after-window.json", client.post(doc_url, headers=hp, json=entry), "POST", entry,
          note="More than 2 h after completion.")
    _save("034-caregiver-documentation-list-after-add-window.json", client.get(doc_url, headers=hp),
          note="Still readable (24 h), `can_add` false.")
    other = make_caregiver(db, full_name="Placeholder Other Caregiver")
    _save("035-other-caregiver-refused.json", client.get(doc_url, headers=h(other)), note="Someone else's visit: 404.")
    _save("036-documentation-validation.json", client.post(doc_url, headers=hp, json={"body": ""}), "POST", {"body": ""},
          note="An empty or whitespace entry, or one over 4000 characters, is the standard 422 (the rejected value is not echoed).")
    doc_visit.actual_end_time = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    _save("037-caregiver-documentation-list-after-24h.json", client.get(doc_url, headers=hp),
          note="More than 24 h after the visit ended: 404 `not_found`.")

    # --- the admin read view -----------------------------------------------------------------------------------------
    reviewer = make_admin(db, ["clinical_reviewer"], password=None, full_name="Placeholder Reviewer")
    ra = auth_headers(reviewer)
    _save("040-admin-documentation-list.json", client.get(f"{ADMIN}/visit-documentation", headers=ra),
          note="Needs `careplans.read`. Every call is written to the audit log (`visit_documentation.view`).")
    _save("041-admin-documentation-filtered.json",
          client.get(f"{ADMIN}/visit-documentation", params={"booking_id": str(doc_visit.id), "limit": 5}, headers=ra))
    support = make_admin(db, ["support"], password=None)
    _save("042-admin-documentation-no-permission.json", client.get(f"{ADMIN}/visit-documentation", headers=auth_headers(support)),
          note="A Support admin lacks `careplans.read`.")
