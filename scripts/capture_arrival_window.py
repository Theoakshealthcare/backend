"""Capture the 2026-11-08 arrival-window shapes against a RUNNING Oakly API into docs/fe-captures/caregiver/810+.

The caregiver's "I've arrived" is allowed from ARRIVAL_EARLIEST_MINUTES_BEFORE (60) before the visit starts. This records the
422 refusal for a visit 3 hours away, then the same caregiver succeeding on a visit 30 minutes away. Same local stack and fake
Paystack as ``capture_arrival_earning.py`` (see its docstring); run it on a FRESH database. Re-running replaces only 810 to 819.

    OAKLY_ENV_FILE=/path/scratch.env poetry run python -W ignore -m scripts.capture_arrival_window \\
        --base-url http://127.0.0.1:8001 --db-url postgresql://oakly:oakly@127.0.0.1:5433/oakly --env-file /path/scratch.env

Real HTTP calls; people registered through the endpoints (the admin is seeded); bookings made through /booking/create and paid
by a signed webhook against the fake Paystack. No worker. Placeholder data only.
"""

import argparse
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, FakePaystack, Recorder, approve_caregiver, onboard_caregiver, pay_booking, post_location, read_env,
    register_verify_login, seed_admin_session, serve_fake_paystack,
)

FIRST = 810
API = "/api/v1/booking"


def run(args) -> None:
    out = Path(args.caregiver_out)
    for f in out.glob("*.json"):
        if re.match(r"\d{3}-", f.name) and FIRST <= int(f.name[:3]) < FIRST + 10:
            f.unlink()
    env = read_env(args.env_file)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    quiet = Recorder(args.base_url, scratch)
    cg = Recorder(args.base_url, out)
    cg.n = FIRST - 1
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
    ta = a["token"]
    post_location(ctx, "a-location", ta, available=True)
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]
    quiet.call("admin-first-pricing", "POST", "/api/v1/booking-settings/", token=admin,
               json_body={"base_fee": 5000.0, "platform_fee_percent": 10.0, "multipliers": {"nurse": 1.0, "caregiver": 0.8},
                          "reason": "Placeholder: first pricing version"})

    def booking_in(minutes: int) -> str:
        body = {"service_type": "nurse", "assistance_type": "Placeholder assistance", "duration_hours": 4,
                "scheduled_date": (datetime.utcnow() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S"),
                "patient_address": "12 Placeholder Street, Lekki", "patient_latitude": 6.4474, "patient_longitude": 3.4723,
                "patient_city": "Lagos", "patient_state": "Lagos", "booking_for": "myself"}
        made = quiet.call("create", "POST", f"{API}/create", token=tp, json_body=body).json()
        (ref, amount) = db.run("SELECT paystack_reference, total_amount_kobo FROM bookings WHERE id = %s", (made["id"],))[0]
        pay_booking(ctx, {"reference": ref, "amount_kobo": amount}, "quiet")
        quiet.call("accept", "POST", f"{API}/caregiver/action/{made['id']}", token=ta, json_body={"action": "accept"})
        return made["id"]

    far, near = booking_in(180), booking_in(30)
    quiet.quiet = False
    cg.call("a-arrived-too-early", "POST", f"{API}/caregiver/action/{far}", token=ta, expect=422,
            json_body={"action": "arrived"},
            note="A visit 3 hours away: earlier than 60 minutes before the start. earliest_at is naive UTC (no Z).")
    cg.call("a-booking-after-refusal", "GET", f"{API}/booking/{far}", token=ta,
            note="The refusal changed nothing: arrived_at is still null and the status is still accepted.")
    cg.call("a-arrived-inside-window", "POST", f"{API}/caregiver/action/{near}", token=ta, json_body={"action": "arrived"},
            note="A visit 30 minutes away is inside the window: 200, arrived_at stamped.")
    server.shutdown()
    print(f"wrote {len(cg.index)} captures to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8001")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:5433/oakly")
    p.add_argument("--env-file", required=True)
    p.add_argument("--caregiver-out", default="docs/fe-captures/caregiver")
    p.add_argument("--scratch", default="/tmp/oakly-arrival-window-setup")
    p.add_argument("--paystack-port", type=int, default=9912)
    run(p.parse_args())


if __name__ == "__main__":
    main()
