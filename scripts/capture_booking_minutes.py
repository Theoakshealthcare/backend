"""Capture the 2026-11-08 booking-minutes shapes against a RUNNING Oakly API:

* patient (docs/fe-captures/patient/300-399): ``GET /booking/services`` with 30-minute steps; preview with ``duration_minutes``,
  the older ``duration_hours`` (whole and 1.5), both, neither, and every ``invalid_duration`` case; create in minutes and in
  hours; the booking's own views (detail, list) with both fields; a 30-minute nutrition consult;
* admin (docs/fe-captures/admin/920-949): the booking detail with ``duration_minutes`` and ``duration_hours``.

Re-running replaces only this script's own blocks. Run on a FRESH database (it creates the first pricing version). Same local stack
as ``capture_service_catalogue.py`` plus the fake Paystack this script serves (the scratch env needs
``PAYSTACK_BASE_URL=http://127.0.0.1:<--paystack-port>``). Everything uses placeholder data.
"""

import argparse
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, FakePaystack, Recorder, read_env, register_verify_login, seed_admin_session, serve_fake_paystack,
)

PATIENT_FIRST, ADMIN_FIRST = 300, 920
API = "/api/v1/booking"
CATALOG = "/api/v1/admin/service-catalog"


def clear_block(folder: Path, first: int, count: int = 100) -> None:
    for f in folder.glob("*.json"):
        if re.match(r"\d{3}-", f.name) and first <= int(f.name[:3]) < first + count:
            f.unlink()


def run(args) -> None:
    patient_dir, admin_dir = Path(args.patient_out), Path(args.admin_out)
    clear_block(patient_dir, PATIENT_FIRST)
    clear_block(admin_dir, ADMIN_FIRST, 30)
    env = read_env(args.env_file)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    quiet = Recorder(args.base_url, scratch)
    pat, adm = Recorder(args.base_url, patient_dir), Recorder(args.base_url, admin_dir)
    pat.n, adm.n = PATIENT_FIRST - 1, ADMIN_FIRST - 1
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    paystack = FakePaystack()
    server = serve_fake_paystack(paystack, port=args.paystack_port)
    ctx = Ctx(quiet, db, env, None, tag, paystack)  # type: ignore[arg-type]
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731

    quiet.quiet = True
    admin = seed_admin_session(quiet, db, env, mail("admin"))
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]
    first = quiet.call("admin-first-pricing", "POST", "/api/v1/booking-settings/", token=admin, json_body={
        "base_fee": 10000.0, "platform_fee_percent": 10.0, "multipliers": {"nurse": 1.0, "nutritionist": 0.9},
        "reason": "Placeholder: first pricing version"}).json()
    quiet.call("enable-nutritionist", "PUT", CATALOG + "/nutritionist", token=admin, json_body={
        "enabled": True, "display_name": "Nutrition consult", "mode": "home", "min_minutes": 30, "step_minutes": 30,
        "max_minutes": 1440, "sort_order": 50})

    def create_body(**extra):
        return {"service_type": "nurse", "assistance_type": "Placeholder assistance",
                "scheduled_date": "2030-01-01T10:00:00", "patient_address": "12 Placeholder Street, Lekki",
                "patient_latitude": 6.4474, "patient_longitude": 3.4723, "patient_city": "Lagos",
                "patient_state": "Lagos", "booking_for": "myself", **extra}

    pat.call("p-services", "GET", f"{API}/services", token=tp,
             note="Step 30 for every service; the nutritionist's shortest booking is 30 minutes, the nurse's 60.")
    pat.call("p-preview-minutes-90", "POST", f"{API}/preview", json_body={"service_type": "nurse", "duration_minutes": 90},
             note="90 minutes at 10,000.00 an hour: 15,000.00 service fee (exactly 1.5 hours), 10 % platform fee.")
    pat.call("p-preview-hours-4", "POST", f"{API}/preview", json_body={"service_type": "nurse", "duration_hours": 4},
             note="The older field still works: both fields come back, `duration_hours` is the integer 4.")
    pat.call("p-preview-hours-1-5", "POST", f"{API}/preview", json_body={"service_type": "nurse", "duration_hours": 1.5},
             note="Hours may now be fractional: 1.5 hours is 90 minutes.")
    pat.call("p-preview-both-agree", "POST", f"{API}/preview", json_body={
        "service_type": "nurse", "duration_minutes": 120, "duration_hours": 2}, note="Both fields, agreeing.")
    pat.call("p-preview-nutrition-30", "POST", f"{API}/preview", json_body={
        "service_type": "nutritionist", "duration_minutes": 30},
        note="A 30-minute consult at 0.9 x 10,000.00 an hour: 4,500.00 service fee.")
    pat.call("p-preview-both-disagree", "POST", f"{API}/preview", expect=422, json_body={
        "service_type": "nurse", "duration_minutes": 60, "duration_hours": 2},
        note="A malformed request keeps FastAPI's list shape (`detail` is an array).")
    pat.call("p-preview-neither", "POST", f"{API}/preview", expect=422, json_body={"service_type": "nurse"})
    pat.call("p-preview-hours-not-whole-minutes", "POST", f"{API}/preview", expect=422,
             json_body={"service_type": "nurse", "duration_hours": 1.001})
    for label, minutes, note in (
        ("45", 45, "Not on the 30-minute step from the minimum 60."),
        ("30-nurse", 30, "Below the nurse's minimum of 60 (the nutritionist's is 30)."),
        ("0", 0, "Zero."),
        ("1470", 1470, "Above the maximum 1440."),
    ):
        pat.call(f"p-preview-invalid-{label}", "POST", f"{API}/preview", expect=422,
                 json_body={"service_type": "nurse", "duration_minutes": minutes},
                 note=note + " `detail` is an object: the allowed values are listed.")
    pat.call("p-preview-invalid-hours-25", "POST", f"{API}/preview", expect=422,
             json_body={"service_type": "nurse", "duration_hours": 25},
             note="The old field gets the same rule (25 hours is 1500 minutes).")

    made = pat.call("p-create-minutes-90", "POST", f"{API}/create", token=tp,
                    json_body=create_body(duration_minutes=90), note="Create in minutes.").json()
    pat.call("p-create-invalid-45", "POST", f"{API}/create", token=tp, expect=422,
             json_body=create_body(duration_minutes=45), note="Nothing is created and Paystack is not called.")
    old = pat.call("p-create-hours-3", "POST", f"{API}/create", token=tp, json_body=create_body(duration_hours=3),
                   note="Create with the older field: stored as 180 minutes.").json()
    pat.call("p-booking-detail", "GET", f"{API}/booking/{made['id']}", token=tp,
             note="The booking detail carries both fields.")
    pat.call("p-booking-list", "GET", f"{API}/patient/bookings", token=tp, note="The list carries both fields too.")
    stored = db.run("SELECT duration_minutes, scheduled_end_date - scheduled_date, total_amount_kobo FROM bookings "
                    "WHERE id = %s", (made["id"],))[0]
    pat.save("db-minutes-90", {"observed": {"duration_minutes": stored[0], "scheduled_end_minus_start": str(stored[1]),
                                            "total_amount_kobo": stored[2]},
                               "sql": "SELECT duration_minutes, scheduled_end_date - scheduled_date, total_amount_kobo FROM bookings",
                               "note": "SQL observation: minutes stored, end = start + 90 minutes, and the total in kobo."})
    adm.call("admin-booking-detail", "GET", f"/api/v1/admin/bookings/{old['id']}", token=admin,
             note="The admin booking detail: `duration_minutes` and `duration_hours`.")

    stamp = {"generated_by": "scripts/capture_booking_minutes.py", "run_tag": tag,
             "captured_at": datetime.utcnow().isoformat() + "Z"}
    for rec, folder, label, number in ((pat, patient_dir, "patient", 399), (adm, admin_dir, "admin", 949)):
        (folder / f"{number}-booking-minutes-index.json").write_text(
            json.dumps({**stamp, "folder": label, "captures": rec.index}, indent=2) + "\n")
        print(f"wrote {len(rec.index)} captures to {folder}")
    server.shutdown()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8001")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:5433/oakly")
    p.add_argument("--env-file", required=True)
    p.add_argument("--patient-out", default="docs/fe-captures/patient")
    p.add_argument("--admin-out", default="docs/fe-captures/admin")
    p.add_argument("--scratch", default="/tmp/oakly-minutes-setup")
    p.add_argument("--paystack-port", type=int, default=9912)
    run(p.parse_args())


if __name__ == "__main__":
    main()
