"""Capture the 2026-11-07 slice-2 shapes against a RUNNING Oakly API and append them to the FE guides' capture folders:

* caregiver (docs/fe-captures/caregiver/700+): the caregiver's own earning on the booking views, the "arrived" action,
  its guards, and the short-visit hold that uses the booking's own threshold;
* admin (docs/fe-captures/admin/700+): the booking policy with the new ``short_visit_min_percent``;
* patient (docs/fe-captures/patient/100+): ``arrived_at`` and the (absent) earning on the patient's view, and the reason
  typed on a free cancel of an unpaid booking.

Existing captures are NOT touched or renumbered (the guides' older examples predate the new fields; each guide says so).
Re-running replaces only the files this script wrote (numbers 700+ / 100+).

    # light local stack (never backend/.env): Postgres + Redis in docker, a scratch env with generated keys, no worker
    OAKLY_ENV_FILE=/path/scratch.env poetry run alembic upgrade head
    OAKLY_ENV_FILE=/path/scratch.env poetry run uvicorn app.main:app --port 8001
    OAKLY_ENV_FILE=/path/scratch.env poetry run python -m scripts.capture_arrival_earning --base-url http://127.0.0.1:8001 \\
        --db-url postgresql://oakly:oakly@127.0.0.1:5433/oakly --env-file /path/scratch.env

The scratch env needs PAYSTACK_BASE_URL=http://127.0.0.1:<--paystack-port> (a local fake Paystack served by this script).

What is real and what is not: every API call is a real HTTP call. People are registered / onboarded / approved through the
endpoints; the admin user is seeded (nothing creates the first one). Bookings are created through the real ``/booking/create``
(so they carry the snapshotted earnings terms) against the local FAKE Paystack and paid by a correctly signed webhook. The
worker is NOT running: the family's "arrived" notice is shown as the outbox row the booking wrote, not a delivered push.
SQL observations are labelled. Everything uses placeholder data.
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

CG_FIRST, ADMIN_FIRST, PATIENT_FIRST = 700, 700, 100


def clear_from(folder: Path, first: int) -> None:
    """Remove only this script's own block (``first`` to ``first + 99``): later blocks, e.g. 800+ written by
    ``capture_arrived_errors_cancel_terms.py``, belong to other scripts."""
    for f in folder.glob("*.json"):
        if re.match(r"\d{3}-", f.name) and first <= int(f.name[:3]) < first + 100:
            f.unlink()


def observe(rec: Recorder, label: str, sql: str, rows: Any, note: str) -> None:
    rec.save(label, {"request": {"method": "SQL", "path": sql, "auth": None, "body": None},
                     "response": {"status_code": None, "headers": {}, "body": rows}, "note": note})


def run(args) -> None:
    caregiver_dir, admin_dir, patient_dir = Path(args.caregiver_out), Path(args.admin_out), Path(args.patient_out)
    for folder, first in ((caregiver_dir, CG_FIRST), (admin_dir, ADMIN_FIRST), (patient_dir, PATIENT_FIRST)):
        clear_from(folder, first)
    env = read_env(args.env_file)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    quiet_rec = Recorder(args.base_url, scratch)  # the people-setup helpers save here, then it is thrown away
    cg, adm, pat = (Recorder(args.base_url, caregiver_dir), Recorder(args.base_url, admin_dir),
                    Recorder(args.base_url, patient_dir))
    cg.n, adm.n, pat.n = CG_FIRST - 1, ADMIN_FIRST - 1, PATIENT_FIRST - 1
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    paystack = FakePaystack()
    server = serve_fake_paystack(paystack, port=args.paystack_port)
    ctx = Ctx(quiet_rec, db, env, None, tag, paystack)  # type: ignore[arg-type]
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731
    api = "/api/v1/booking"

    # --- people (not captured) -------------------------------------------------------------------------------------
    quiet_rec.quiet = True
    admin = seed_admin_session(quiet_rec, db, env, mail("admin"))
    a = onboard_caregiver(ctx, "a", mail("caregiver-a"), "Placeholder Caregiver A", capture=False)
    approve_caregiver(ctx, admin, a, capture=False)
    other = onboard_caregiver(ctx, "o", mail("caregiver-o"), "Placeholder Caregiver O", capture=False)
    approve_caregiver(ctx, admin, other, capture=False)
    ta, to = a["token"], other["token"]
    post_location(ctx, "a-location", ta, available=True)
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]
    quiet_rec.quiet = False

    # --- admin: the policy gains short_visit_min_percent -----------------------------------------------------------
    policy = "/api/v1/admin/booking-policy"
    adm.call("admin-policy-get", "GET", policy, token=admin,
             note="short_visit_min_percent is new: 50 by default (a visit with under half of the booked time on site is "
                  "credited already held for Finance).")
    adm.call("admin-policy-update-short-visit", "PATCH", policy, token=admin,
             json_body={"reason": "Placeholder: pilot threshold", "short_visit_min_percent": 40},
             note="Editable like any policy field. Copied onto each booking created from now on.")
    adm.call("admin-policy-update-short-visit-over-100", "PATCH", policy, token=admin, expect=422,
             json_body={"reason": "Placeholder", "short_visit_min_percent": 101})
    adm.call("admin-policy-update-short-visit-not-a-number", "PATCH", policy, token=admin, expect=422,
             json_body={"reason": "Placeholder", "short_visit_min_percent": "40"})
    adm.call("admin-policy-get-after", "GET", policy, token=admin)

    # --- a real booking made after the policy change (so it carries the snapshot) ---------------------------------
    quiet_rec.quiet = True
    quiet_rec.call("admin-first-pricing", "POST", "/api/v1/booking-settings/", token=admin,
                   json_body={"base_fee": 5000.0, "platform_fee_percent": 10.0, "multipliers": {"nurse": 1.0, "caregiver": 0.8},
                              "reason": "Placeholder: first pricing version"})
    quiet_rec.quiet = False
    create_body = {"service_type": "nurse", "assistance_type": "Placeholder assistance", "duration_hours": 4,
                   "scheduled_date": (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                   "patient_address": "12 Placeholder Street, Lekki", "patient_latitude": 6.4474,
                   "patient_longitude": 3.4723, "patient_city": "Lagos", "patient_state": "Lagos", "booking_for": "myself"}

    def create_paid() -> Dict[str, Any]:
        quiet_rec.quiet = True
        made = quiet_rec.call("p-create", "POST", f"{api}/create", token=tp, json_body=create_body).json()
        (ref, amount) = db.run("SELECT paystack_reference, total_amount_kobo FROM bookings WHERE id = %s", (made["id"],))[0]
        pay_booking(ctx, {"reference": ref, "amount_kobo": amount}, "quiet")
        quiet_rec.quiet = False
        return made

    b1 = create_paid()
    bid = b1["id"]
    observe(cg, "db-booking-earnings-terms", "SELECT earnings_terms, service_fee_kobo FROM bookings WHERE id = <booking 1>",
            [{"earnings_terms": t, "service_fee_kobo": f} for (t, f) in db.run(
                "SELECT earnings_terms, service_fee_kobo FROM bookings WHERE id = %s", (bid,))],
            "The terms copied onto the booking when it was created (new key: short_visit_min_percent, here 40 from the "
            "policy change above). The caregiver's earning is visit_share_percent of service_fee_kobo.")
    cg.call("a-notifications-offer", "GET", f"{api}/caregiver/notifications", token=ta,
            note="While the caregiver only holds an OFFER, booking.caregiver_earning is null and arrived_at is null.")
    cg.call("a-accept", "POST", f"{api}/caregiver/action/{bid}", token=ta, json_body={"action": "accept"},
            note="After accepting, the booking in the action response carries caregiver_earning (naira) and arrived_at null.")
    cg.call("a-booking-detail-with-earning", "GET", f"{api}/booking/{bid}", token=ta,
            note="Job Details: caregiver_earning is the caregiver's share of the service fee from the booking's own terms.")
    cg.call("a-bookings-list-with-earning", "GET", f"{api}/caregiver/bookings", token=ta, params={"status": "accepted"})
    pat.call("p-detail-before-arrival", "GET", f"{api}/booking/{bid}", token=tp,
             note="The patient's view: arrived_at null, caregiver_earning null (never shown to the family).")

    # --- arrived: guards -------------------------------------------------------------------------------------------
    cg.call("o-arrived-not-assigned", "POST", f"{api}/caregiver/action/{bid}", token=to, expect=403,
            json_body={"action": "arrived"}, note="A caregiver who does not hold the booking.")
    cg.call("none-arrived-no-token", "POST", f"{api}/caregiver/action/{bid}", expect=401, json_body={"action": "arrived"})
    cg.call("p-arrived-patient-token", "POST", f"{api}/caregiver/action/{bid}", token=tp, expect=403,
            json_body={"action": "arrived"}, note="A patient token on the caregiver action route.")
    b2 = create_paid()  # a second paid booking that nobody accepted: still pending
    cg.call("a-arrived-pending-booking", "POST", f"{api}/caregiver/action/{b2['id']}", token=ta, expect=403,
            json_body={"action": "arrived"},
            note="A booking the caregiver was merely offered (not assigned) is refused like any action on one.")

    # --- arrived: the happy path ---------------------------------------------------------------------------------
    cg.call("a-arrived", "POST", f"{api}/caregiver/action/{bid}", token=ta, json_body={"action": "arrived"},
            note="Status stays accepted; arrived_at is stamped; the family is told once.")
    cg.call("a-arrived-again", "POST", f"{api}/caregiver/action/{bid}", token=ta, json_body={"action": "arrived"},
            note="A repeat (double tap or retry): 200, the SAME arrived_at, nobody is told again.")
    observe(cg, "db-arrived-outbox", "SELECT event, count(*) FROM booking_events WHERE booking_id = <booking 1> GROUP BY event",
            [{"event": e, "count": c} for (e, c) in db.run(
                "SELECT event, count(*) FROM booking_events WHERE booking_id = %s GROUP BY event ORDER BY event", (bid,))],
            "The outbox rows the booking wrote: exactly one caregiver_arrived after two taps. NOT a delivered push: the "
            "worker was not running (its feed row and Expo push are covered by the notifications guide and unit tests).")
    pat.call("p-detail-after-arrival", "GET", f"{api}/booking/{bid}", token=tp,
             note="The patient's booking now has arrived_at; status is still accepted and the visit code is still shown.")
    code = pat.call("p-detail-visit-code", "GET", f"{api}/booking/{bid}", token=tp).json()["visit_code"]
    cg.call("a-start", "POST", f"{api}/caregiver/action/{bid}", token=ta, json_body={"action": "start", "visit_code": code})
    cg.call("a-action-invalid-enum", "POST", f"{api}/caregiver/action/{bid}", token=ta, expect=422,
            json_body={"action": "teleport"}, note="The list of actions in the validation message now includes arrived.")
    cg.call("a-arrived-after-start", "POST", f"{api}/caregiver/action/{bid}", token=ta, expect=409,
            json_body={"action": "arrived"}, note="Arriving after the visit started is a 409.")

    # --- the booking's own short-visit threshold (40 %), visit completed within seconds -----------------------------
    cg.call("a-complete-short", "POST", f"{api}/caregiver/action/{bid}", token=ta, json_body={"action": "complete"},
            note="Completed seconds after starting (far under 40 % of 4 hours): still completed, earning held.")
    observe(cg, "db-short-visit-hold", "SELECT status, status_reason, amount, base_amount_kobo, share_percent FROM caregiver_transactions"
            " WHERE booking_id = <booking 1> AND type = 'earning'",
            [{"status": s, "status_reason": r, "amount": am, "base_amount_kobo": b, "share_percent": p} for (s, r, am, b, p) in db.run(
                "SELECT status, status_reason, amount, base_amount_kobo, share_percent FROM caregiver_transactions"
                " WHERE booking_id = %s AND type = 'earning'", (bid,))],
            "The hold reason names the booking's own threshold (40), and amount equals the earning shown before completion.")
    cg.call("a-booking-detail-completed", "GET", f"{api}/booking/{bid}", token=ta,
            note="The completed booking still shows caregiver_earning.")

    # --- the family's reason on a free cancel of an unpaid booking -----------------------------------------------
    quiet_rec.quiet = True
    unpaid = quiet_rec.call("p-create-unpaid", "POST", f"{api}/create", token=tp, json_body=create_body).json()
    quiet_rec.quiet = False
    pat.call("p-cancel-unpaid-with-reason", "POST", f"{api}/{unpaid['id']}/cancel", token=tp,
             json_body={"reason": "Placeholder: plans changed", "accepted_fee_kobo": 0},
             note="Free cancel of an unpaid booking. The reason is now stored on the booking (it was dropped before).")
    observe(pat, "db-cancel-note", "SELECT cancel_reason, cancel_note FROM bookings WHERE id = <the unpaid booking>",
            [{"cancel_reason": r, "cancel_note": n} for (r, n) in db.run(
                "SELECT cancel_reason, cancel_note FROM bookings WHERE id = %s", (unpaid["id"],))],
            "cancel_reason is the code; cancel_note is the family's text (max 500 characters, null when none was given).")
    pat.call("p-cancel-detail-no-note", "GET", f"{api}/booking/{unpaid['id']}", token=tp,
             note="The note is not echoed in any booking view (no field for it): it is stored for the team.")

    stamp = {"generated_by": "scripts/capture_arrival_earning.py", "run_tag": tag,
             "captured_at": datetime.utcnow().isoformat() + "Z"}
    for rec, folder, label in ((cg, caregiver_dir, "caregiver"), (adm, admin_dir, "admin"), (pat, patient_dir, "patient")):
        (folder / f"{'799' if folder != patient_dir else '199'}-slice2-index.json").write_text(
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
    p.add_argument("--patient-out", default="docs/fe-captures/patient")
    p.add_argument("--scratch", default="/tmp/oakly-slice2-setup")
    p.add_argument("--paystack-port", type=int, default=9912)
    run(p.parse_args())


if __name__ == "__main__":
    main()
