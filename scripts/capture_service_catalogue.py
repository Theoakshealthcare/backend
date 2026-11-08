"""Capture the 2026-11-08 service-catalogue shapes against a RUNNING Oakly API and write them to the FE guides' capture folders:

* patient (docs/fe-captures/patient/200+): ``GET /booking/services`` (before pricing, nurse only, after another service is
  priced and enabled, without a token) and the 422 ``service_unavailable`` on preview and create;
* admin (docs/fe-captures/admin/900-919): ``GET /admin/service-catalog`` and ``PUT /admin/service-catalog/{service_type}``:
  success, a retried PUT, ``service_has_no_price``, validation errors, an unknown service, a role without ``pricing.write``.

Re-running replaces only this script's own blocks (patient 200-299, admin 900-919). Run on a FRESH database (it creates the first
pricing version). Light local stack, never ``backend/.env``:

    OAKLY_ENV_FILE=/path/scratch.env poetry run alembic upgrade head
    OAKLY_ENV_FILE=/path/scratch.env poetry run uvicorn app.main:app --port 8001
    OAKLY_ENV_FILE=/path/scratch.env poetry run python -m scripts.capture_service_catalogue \\
        --base-url http://127.0.0.1:8001 --db-url postgresql://oakly:oakly@127.0.0.1:5433/oakly --env-file /path/scratch.env

Every API call is a real HTTP call; people are registered through the endpoints (the OTP is read from the database because no
SMTP is configured) and the admins are seeded. No Paystack call is made. Everything uses placeholder data.
"""

import argparse
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, Recorder, read_env, register_verify_login, seed_admin_session,
)

PATIENT_FIRST, ADMIN_FIRST = 200, 900
API = "/api/v1/booking"
CATALOG = "/api/v1/admin/service-catalog"


def clear_block(folder: Path, first: int, count: int = 100) -> None:
    for f in folder.glob("*.json"):
        if re.match(r"\d{3}-", f.name) and first <= int(f.name[:3]) < first + count:
            f.unlink()


def run(args) -> None:
    patient_dir, admin_dir = Path(args.patient_out), Path(args.admin_out)
    clear_block(patient_dir, PATIENT_FIRST)
    clear_block(admin_dir, ADMIN_FIRST, 20)
    env = read_env(args.env_file)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    quiet = Recorder(args.base_url, scratch)
    pat, adm = Recorder(args.base_url, patient_dir), Recorder(args.base_url, admin_dir)
    pat.n, adm.n = PATIENT_FIRST - 1, ADMIN_FIRST - 1
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    ctx = Ctx(quiet, db, env, None, tag)  # type: ignore[arg-type]
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731

    quiet.quiet = True
    admin = seed_admin_session(quiet, db, env, mail("admin"))
    ops = seed_admin_session(quiet, db, env, mail("ops"), role="operations")
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]

    preview = {"service_type": "nurse", "duration_hours": 2}
    create = {"service_type": "nurse", "assistance_type": "Placeholder assistance", "duration_hours": 2,
              "scheduled_date": "2030-01-01T10:00:00", "patient_address": "12 Placeholder Street, Lekki",
              "patient_latitude": 6.4474, "patient_longitude": 3.4723, "patient_city": "Lagos",
              "patient_state": "Lagos", "booking_for": "myself"}

    # --- before any pricing exists ---------------------------------------------------------------------------------
    pat.call("p-services-no-pricing-yet", "GET", f"{API}/services", token=tp,
             note="A new environment: no pricing version, so nothing can be priced and nothing is offered.")
    adm.call("admin-catalog-no-pricing-yet", "GET", CATALOG, token=admin,
             note="The seeded catalogue before any pricing exists: nothing is priced, so only enabled-but-unpriced shows.")
    adm.call("admin-enable-before-pricing", "PUT", CATALOG + "/doctor", token=admin, expect=422, json_body={
        "enabled": True, "display_name": "Doctor visit", "mode": "home", "min_minutes": 60, "step_minutes": 30,
        "max_minutes": 1440, "sort_order": 20}, note="No pricing version exists: nothing has a price.")

    # --- first pricing: no multipliers (the nurse is still sold, at 1.0) ---------------------------------------------
    first = quiet.call("admin-first-pricing", "POST", "/api/v1/booking-settings/", token=admin, json_body={
        "base_fee": 5000.0, "platform_fee_percent": 10.0, "reason": "Placeholder: first pricing version"}).json()
    pat.call("p-services-nurse-only", "GET", f"{API}/services", token=tp,
             note="Pricing exists but sets no multipliers: only the nurse (enabled by the seed) is offered, at the default 1.0. "
                  "No prices are in this response.")
    pat.call("p-services-no-token", "GET", f"{API}/services", expect=401)
    pat.call("p-preview-nurse", "POST", f"{API}/preview", json_body=preview,
             note="The nurse still previews with no multiplier configured (unchanged).")
    pat.call("p-preview-hidden-service", "POST", f"{API}/preview", expect=422,
             json_body={**preview, "service_type": "doctor"}, note="Doctor is disabled in the catalogue.")
    pat.call("p-create-hidden-service", "POST", f"{API}/create", token=tp, expect=422,
             json_body={**create, "service_type": "doctor"}, note="Create refuses it too, before any booking or payment.")
    adm.call("admin-catalog-nurse-only", "GET", CATALOG, token=admin,
             note="priced: true for the nurse only (no multiplier needed for it); bookable = enabled AND priced.")

    # --- enabling needs a price ------------------------------------------------------------------------------------
    body = {"enabled": True, "display_name": "Doctor visit", "mode": "home", "min_minutes": 60, "step_minutes": 30,
            "max_minutes": 1440, "sort_order": 20}
    adm.call("admin-enable-without-price", "PUT", CATALOG + "/doctor", token=admin, expect=422, json_body=body,
             note="The doctor has no multiplier in the pricing yet: the console should say 'set its multiplier in Pricing first'.")
    for label, override, note in (
        ("video-mode", {"mode": "video"}, "Video is deferred: only 'home' is accepted."),
        ("step-45", {"step_minutes": 45}, "The step must be 15, 30 or 60 minutes."),
        ("min-100", {"min_minutes": 100}, "The minimum must be a multiple of the step (30 here)."),
        ("max-1500", {"max_minutes": 1500}, "At most 1440 minutes."),
        ("blank-name", {"display_name": "  "}, "A blank name."),
    ):
        adm.call(f"admin-put-invalid-{label}", "PUT", CATALOG + "/doctor", token=admin, expect=422,
                 json_body={**body, **{"enabled": False}, **override}, note=note)
    adm.call("admin-put-unknown-service", "PUT", CATALOG + "/wizard", token=admin, expect=404, json_body=body)
    adm.call("admin-put-forbidden-operations", "PUT", CATALOG + "/doctor", token=ops, expect=403, json_body=body,
             note="An Operations admin may read the catalogue but not change it (needs pricing.write).")
    adm.call("admin-get-operations", "GET", CATALOG, token=ops, note="Reading needs only bookings.read.")
    adm.call("admin-put-no-token", "PUT", CATALOG + "/doctor", expect=401, json_body=body)

    # --- price it, then enable --------------------------------------------------------------------------------------
    quiet.call("admin-price-doctor", "PUT", f"/api/v1/booking-settings/{first['id']}", token=admin, json_body={
        "base_fee": 5000.0, "platform_fee_percent": 10.0, "multipliers": {"nurse": 1.0, "doctor": 2.0},
        "reason": "Placeholder: add the doctor multiplier"})
    adm.call("admin-catalog-doctor-priced", "GET", CATALOG, token=admin,
             note="Doctor is now priced (a multiplier exists) but still disabled, so not bookable.")
    pat.call("p-services-doctor-priced-not-enabled", "GET", f"{API}/services", token=tp,
             note="Priced but disabled: still hidden.")
    adm.call("admin-enable-doctor", "PUT", CATALOG + "/doctor", token=admin, json_body={
        **body, "display_name": "Doctor home visit", "reason": "Placeholder: launch doctor visits"},
        note="Success: returns the whole row with priced and bookable.")
    adm.call("admin-enable-doctor-retry", "PUT", CATALOG + "/doctor", token=admin, json_body={
        **body, "display_name": "Doctor home visit", "reason": "Placeholder: launch doctor visits"},
        note="The same PUT repeated: identical answer (same updated_at); no second audit row is written.")
    audit = db.run("SELECT count(*) FROM admin_audit_log WHERE entity_type = 'service_catalog'")[0][0]
    adm.save("db-audit-rows", {"observed": True, "sql": "SELECT count(*) FROM admin_audit_log WHERE entity_type = 'service_catalog'",
                               "count": audit, "note": "SQL observation after the PUT and its retry: one audit row."})
    pat.call("p-services-doctor-offered", "GET", f"{API}/services", token=tp,
             note="Enabled and priced: the doctor is offered, after the nurse (sort_order).")
    pat.call("p-preview-doctor", "POST", f"{API}/preview", json_body={**preview, "service_type": "doctor"},
             note="Now priced at the doctor's multiplier (2.0).")

    stamp = {"generated_by": "scripts/capture_service_catalogue.py", "run_tag": tag,
             "captured_at": datetime.utcnow().isoformat() + "Z"}
    for rec, folder, label, number in ((pat, patient_dir, "patient", 299), (adm, admin_dir, "admin", 919)):
        (folder / f"{number}-service-catalogue-index.json").write_text(
            json.dumps({**stamp, "folder": label, "captures": rec.index}, indent=2) + "\n")
        print(f"wrote {len(rec.index)} captures to {folder}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8001")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:5433/oakly")
    p.add_argument("--env-file", required=True)
    p.add_argument("--patient-out", default="docs/fe-captures/patient")
    p.add_argument("--admin-out", default="docs/fe-captures/admin")
    p.add_argument("--scratch", default="/tmp/oakly-catalogue-setup")
    run(p.parse_args())


if __name__ == "__main__":
    main()
