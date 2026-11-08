"""Capture the 2026-11-08 shapes against a RUNNING Oakly API and write them to the FE guides' capture folders:

* caregiver (docs/fe-captures/caregiver/800+): the ``arrived`` action's errors as ``detail = {code, message}``, and one
  other action's unchanged plain-string error next to them;
* admin (docs/fe-captures/admin/800+): ``cancel_terms`` on the booking detail (the snapshot stored on the booking once its
  booker cancelled, the tiers in force, the tier that applies now, what a booker cancel applied), and the live policy it is
  read against.

Re-running replaces only this script's own block (numbers 800 to 899). Same local stack and fake Paystack as
``capture_arrival_earning.py`` (see its docstring for the stack recipe):

    OAKLY_ENV_FILE=/path/scratch.env poetry run python -m scripts.capture_arrived_errors_cancel_terms \\
        --base-url http://127.0.0.1:8001 --db-url postgresql://oakly:oakly@127.0.0.1:5433/oakly --env-file /path/scratch.env

Run it on a FRESH database (it creates the first pricing version and its "before" policy capture expects the default policy).
Every API call is a real HTTP call. People are registered / onboarded / approved through the endpoints (the admin user is
seeded); bookings go through the real ``/booking/create`` and are paid by a signed webhook against the local fake Paystack.
The worker is NOT running. Everything uses placeholder data.
"""

import argparse
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, FakePaystack, Recorder, approve_caregiver, onboard_caregiver, pay_booking, post_location, read_env,
    register_verify_login, seed_admin_session, serve_fake_paystack,
)

FIRST = 800
API = "/api/v1/booking"


def clear_block(folder: Path) -> None:
    for f in folder.glob("*.json"):
        if re.match(r"\d{3}-", f.name) and FIRST <= int(f.name[:3]) < FIRST + 100:
            f.unlink()


def run(args) -> None:
    caregiver_dir, admin_dir = Path(args.caregiver_out), Path(args.admin_out)
    for folder in (caregiver_dir, admin_dir):
        clear_block(folder)
    env = read_env(args.env_file)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    quiet = Recorder(args.base_url, scratch)
    cg, adm = Recorder(args.base_url, caregiver_dir), Recorder(args.base_url, admin_dir)
    cg.n = adm.n = FIRST - 1
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    paystack = FakePaystack()
    server = serve_fake_paystack(paystack, port=args.paystack_port)
    ctx = Ctx(quiet, db, env, None, tag, paystack)  # type: ignore[arg-type]
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731

    quiet.quiet = True
    admin = seed_admin_session(quiet, db, env, mail("admin"))
    a = onboard_caregiver(ctx, "a", mail("caregiver-a"), "Placeholder Caregiver A", capture=False)
    approve_caregiver(ctx, admin, a, capture=False)
    other = onboard_caregiver(ctx, "o", mail("caregiver-o"), "Placeholder Caregiver O", capture=False)
    approve_caregiver(ctx, admin, other, capture=False)
    ta, to = a["token"], other["token"]
    post_location(ctx, "a-location", ta, available=True)
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]
    quiet.call("admin-first-pricing", "POST", "/api/v1/booking-settings/", token=admin,
               json_body={"base_fee": 5000.0, "platform_fee_percent": 10.0, "multipliers": {"nurse": 1.0, "caregiver": 0.8},
                          "reason": "Placeholder: first pricing version"})

    def body(hours: float) -> Dict[str, Any]:
        return {"service_type": "nurse", "assistance_type": "Placeholder assistance", "duration_hours": 4,
                "scheduled_date": (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S"),
                "patient_address": "12 Placeholder Street, Lekki", "patient_latitude": 6.4474, "patient_longitude": 3.4723,
                "patient_city": "Lagos", "patient_state": "Lagos", "booking_for": "myself"}

    def create(hours: float, pay: bool = True, accept: bool = True) -> str:
        made = quiet.call("create", "POST", f"{API}/create", token=tp, json_body=body(hours)).json()
        if pay:
            (ref, amount) = db.run("SELECT paystack_reference, total_amount_kobo FROM bookings WHERE id = %s", (made["id"],))[0]
            pay_booking(ctx, {"reference": ref, "amount_kobo": amount}, "quiet")
        if pay and accept:
            quiet.call("accept", "POST", f"{API}/caregiver/action/{made['id']}", token=ta, json_body={"action": "accept"})
        return made["id"]

    policy = "/api/v1/admin/booking-policy"
    adm.call("admin-policy-get-before", "GET", policy, token=admin, note="The live policy at the start: 12 h free, 2 h late, 20 % / 50 %.")
    quiet.call("policy-change", "PATCH", policy, token=admin, json_body={
        "reason": "Placeholder: stricter cancel terms", "cancel_free_hours": 24, "cancel_late_hours": 6,
        "cancel_fee_percent": 25, "cancel_late_fee_percent": 60})
    adm.call("admin-policy-get-after", "GET", policy, token=admin, note="The live policy after an admin edit (24 h / 6 h, 25 % / 60 %).")
    b_far = create(48)
    b_late = create(1)
    b_std = create(10)
    b_unpaid = create(30, pay=False)

    # --- admin: the booking's own cancel terms --------------------------------------------------------------------
    adm.call("admin-booking-far", "GET", f"/api/v1/admin/bookings/{b_far}", token=admin,
             note="Accepted, 48 h from the start. The booking has NO stored snapshot yet (it is taken when the booker cancels), "
                  "so policy_source is live_policy and snapshot is null. Nothing to pay.")
    adm.call("admin-booking-late", "GET", f"/api/v1/admin/bookings/{b_late}", token=admin,
             note="Accepted, 1 hour from the start: the late tier would apply now (60 % of the service fee).")
    adm.call("admin-booking-standard", "GET", f"/api/v1/admin/bookings/{b_std}", token=admin,
             note="10 hours from the start: inside the 24 h free window but outside the 6 h late window: the standard tier.")
    adm.call("admin-booking-unpaid", "GET", f"/api/v1/admin/bookings/{b_unpaid}", token=admin,
             note="Unpaid and pending: would_apply_now.allowed is false with refusal 'unpaid'.")
    terms = quiet.call("p-cancel-terms", "GET", f"{API}/{b_std}/cancel-terms", token=tp).json()
    quiet.call("p-cancel", "POST", f"{API}/{b_std}/cancel", token=tp,
               json_body={"reason": "Placeholder: plans changed", "accepted_fee_kobo": terms["fee_amount_kobo"]})
    adm.call("admin-booking-cancelled-by-booker", "GET", f"/api/v1/admin/bookings/{b_std}", token=admin,
             note="The same booking after its patient cancelled it with the fee shown: the cancel froze the snapshot on the "
                  "booking (policy_source booking_snapshot) and `applied` holds the tier and the fee; would_apply_now is "
                  "now not allowed (closed).")
    quiet.call("policy-change-2", "PATCH", policy, token=admin, json_body={
        "reason": "Placeholder: another edit", "cancel_free_hours": 48, "cancel_late_hours": 12,
        "cancel_fee_percent": 40, "cancel_late_fee_percent": 80})
    adm.call("admin-booking-cancelled-after-policy-edit", "GET", f"/api/v1/admin/bookings/{b_std}", token=admin,
             note="After a further policy edit (48 h / 12 h, 40 % / 80 %) the cancelled booking still shows the terms it was "
                  "cancelled under: the stored snapshot does not follow the live policy.")
    adm.call("admin-booking-far-after-policy-edit", "GET", f"/api/v1/admin/bookings/{b_far}", token=admin,
             note="A booking with no stored snapshot follows the live policy: the same far booking now sits inside the 48 h "
                  "window, so the standard tier (40 %) would apply.")

    # --- caregiver: the arrived errors -----------------------------------------------------------------------------
    cg.call("o-arrived-not-assigned", "POST", f"{API}/caregiver/action/{b_far}", token=to, expect=403,
            json_body={"action": "arrived"}, note="A caregiver who does not hold the booking.")
    pending = create(5, accept=False)  # paid, pending: caregiver A holds an offer only
    cg.call("a-arrived-offer-only", "POST", f"{API}/caregiver/action/{pending}", token=ta, expect=403,
            json_body={"action": "arrived"}, note="Holding only an offer is not being assigned.")
    cg.call("a-arrived-unknown-booking", "POST", f"{API}/caregiver/action/{uuid.uuid4()}", token=ta, expect=404,
            json_body={"action": "arrived"}, note="A booking id that does not exist.")
    started = create(1)
    code = quiet.call("p-code", "GET", f"{API}/booking/{started}", token=tp).json()["visit_code"]
    quiet.call("a-start", "POST", f"{API}/caregiver/action/{started}", token=ta, json_body={"action": "start", "visit_code": code})
    cg.call("a-arrived-after-start", "POST", f"{API}/caregiver/action/{started}", token=ta, expect=409,
            json_body={"action": "arrived"}, note="Arriving after the visit started.")
    cg.call("o-start-not-assigned-plain-string", "POST", f"{API}/caregiver/action/{b_far}", token=to, expect=403,
            json_body={"action": "start", "visit_code": "0000"},
            note="Unchanged: every other action still answers a plain string `detail`.")

    stamp = {"generated_by": "scripts/capture_arrived_errors_cancel_terms.py", "run_tag": tag,
             "captured_at": datetime.utcnow().isoformat() + "Z"}
    for rec, folder, label in ((cg, caregiver_dir, "caregiver"), (adm, admin_dir, "admin")):
        (folder / "899-small-fixes-index.json").write_text(
            json.dumps({**stamp, "folder": label, "captures": rec.index}, indent=2) + "\n")
        print(f"wrote {len(rec.index)} captures to {folder}")
    server.shutdown()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8001")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:5433/oakly")
    p.add_argument("--env-file", required=True)
    p.add_argument("--caregiver-out", default="docs/fe-captures/caregiver")
    p.add_argument("--admin-out", default="docs/fe-captures/admin")
    p.add_argument("--scratch", default="/tmp/oakly-small-setup")
    p.add_argument("--paystack-port", type=int, default=9912)
    run(p.parse_args())


if __name__ == "__main__":
    main()
