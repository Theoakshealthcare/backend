"""Writes the live captures behind docs/fe-integration-guide-care-alerts.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CARE_ALERTS=1 poetry run pytest app/tests/test_capture_care_alerts.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-care-alerts.md docs/fe-captures/care-alerts`.
"""

import html
import json
import os
import re
import uuid
from pathlib import Path

import pytest

from app.tests.care_alert_helpers import ADMIN_ALERTS, outbox, patient_with_family, post_reading  # noqa: F401
from app.tests.factories import auth_headers, make_admin, make_patient
from app.tests.fakes import expo  # noqa: F401
from app.tests.test_care_alerts_missed_tasks import one_visit, run

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CARE_ALERTS"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "care-alerts"


def _save(name, r, note=None, body=None):
    doc = {
        "request": {"method": r.request.method, "path": re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<ALERT_ID>", r.request.url.path)
                    + (f"?{r.request.url.query.decode()}" if r.request.url.query else ""),
                    "auth": "Bearer <ADMIN_ACCESS_TOKEN_JWT>", "body": body},
        "response": {"status_code": r.status_code, "body": r.json()},
    }
    if note:
        doc["note"] = note
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _save_raw(name, observed, note):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps({"note": note, **observed}, indent=2, ensure_ascii=False) + "\n")


def test_capture(db, client, outbox, expo):
    admins = {role: make_admin(db, [role], password=None, email=f"{role}@example.test")
              for role in ("super_admin", "clinical_reviewer", "operations", "support", "finance")}
    h = auth_headers(admins["super_admin"])

    # a family contact who allowed it, with an e-mail and the app: one critical reading reaches them both
    ada, _recipient, _family = patient_with_family(db, first_name="Ada", push=True)
    ada.phone_number = "+2340000719201"
    bola = make_patient(db, full_name="Bola Placeholder", phone_number="+2340000719202")
    db.commit()
    post_reading(client, bola, "temperature", "38.5", "°C")
    post_reading(client, ada, "heart_rate", "45", "bpm")
    mail = outbox.mails[0]
    _save_raw("001-family-email-critical.json", {"observed": {
        "from": mail["from"], "to": "<FAMILY_EMAIL>", "subject": mail["subject"],
        "message_in_body": re.search(r"<p>(A heart rate.*?)</p>", html.unescape(mail["html"])).group(1)}},
        "What the family e-mail says (the HTML wraps this one sentence). No reading, no surname, no condition.")
    _save_raw("002-family-push-critical.json", {"observed": {k: v for k, v in expo.messages[0].items() if k != "to"}},
              "The push message (token omitted).")
    admin_mail = next(m for m in outbox.mails if m["to"][0] == "super_admin@example.test")
    _save_raw("003-admin-email-critical.json", {"observed": {"to": "<ADMIN_EMAIL>", "subject": admin_mail["subject"], "message_in_body": re.search(
        r"<p>(A critical.*?)</p>", html.unescape(admin_mail["html"])).group(1)}},
        "The e-mail every active admin holding support.inbox gets for a CRITICAL alert (and ADMIN_NOTIFY_EMAIL). It names nobody.")

    # a missed task, judged by the sweep
    one_visit(db)
    run(db)

    r = client.get(ADMIN_ALERTS, headers=h)
    _save("010-list-active-super-admin.json", r, "Default: status=active (open + acknowledged). Critical first.")
    assert r.status_code == 200
    items = r.json()["items"]
    critical = next(i for i in items if i["severity"] == "critical")
    missed = next(i for i in items if i["type"] == "task_missed")

    _save("011-list-active-support-masked.json", client.get(ADMIN_ALERTS, headers=auth_headers(admins["support"])),
          "Support holds support.inbox only: the name and phone are masked and detail is null (detail_masked true).")
    _save("012-list-filtered.json", client.get(f"{ADMIN_ALERTS}?severity=warning&type=task_missed&limit=10", headers=h),
          "Filters: status, severity, type, limit (1-100), offset.")
    _save("013-detail-clinical-reviewer.json", client.get(f"{ADMIN_ALERTS}/{critical['id']}", headers=auth_headers(admins["clinical_reviewer"])),
          "clinical.read shows detail and note; people.pii is missing, so the name is masked. Opening it leaves a care_alert.read audit row.")
    _save("014-detail-missed-task.json", client.get(f"{ADMIN_ALERTS}/{missed['id']}", headers=h), "A missed-task alert: detail.tasks and detail.days. (Its timestamps are the capture's fixed sweep clock, the visit day; family_notified_* are null because this capture ran the sweep without the delivery step.)")

    body = {"note": "Phoned the family; ambulance called (placeholder note)."}
    _save("020-acknowledge.json", client.post(f"{ADMIN_ALERTS}/{critical['id']}/acknowledge", json=body, headers=h), body=body)
    _save("021-acknowledge-again-409.json", client.post(f"{ADMIN_ALERTS}/{critical['id']}/acknowledge", json={}, headers=h), body={})
    body = {"note": "Patient is at hospital (placeholder note)."}
    _save("022-resolve.json", client.post(f"{ADMIN_ALERTS}/{critical['id']}/resolve", json=body, headers=h), body=body)
    _save("023-resolve-again-409.json", client.post(f"{ADMIN_ALERTS}/{critical['id']}/resolve", json={}, headers=h), body={})
    _save("024-resolve-without-ack.json", client.post(f"{ADMIN_ALERTS}/{missed['id']}/resolve", json={}, headers=h), body={},
          note="An open alert may be resolved directly.")
    _save("025-list-resolved.json", client.get(f"{ADMIN_ALERTS}?status=resolved", headers=h))
    _save("030-not-found-404.json", client.get(f"{ADMIN_ALERTS}/{uuid.uuid4()}", headers=h))
    _save("031-finance-403.json", client.get(ADMIN_ALERTS, headers=auth_headers(admins["finance"])))
    _save("032-bad-status-422.json", client.get(f"{ADMIN_ALERTS}?status=nonsense", headers=h))
    r = client.post(f"{ADMIN_ALERTS}/{critical['id']}/resolve", json={"note": "x" * 1001}, headers=h)
    _save("033-note-too-long-422.json", r, body={"note": "<1001 characters>"})
