"""Capture the patient-app cancel flow and the payment-link ("Pay now" after a restart) flow against a RUNNING Oakly API and write every request/response to
docs/fe-captures/patient/*.json. docs/fe-integration-guide-patient-bookings.md is copied from these files, never from schemas.

    # a scratch env file (generated values, never committed), a throwaway Postgres, the API from this checkout
    docker run -d --name oakly-patient-capture-db -e POSTGRES_USER=oakly -e POSTGRES_PASSWORD=oakly -e POSTGRES_DB=oakly \\
        -p 55432:5432 postgres:16-alpine
    DATABASE_URL=postgresql://oakly:oakly@127.0.0.1:55432/oakly OAKLY_ENV_FILE=/path/to/scratch.env poetry run alembic upgrade head
    OAKLY_ENV_FILE=/path/to/scratch.env poetry run uvicorn app.main:app --port 8000
    poetry run python -m scripts.capture_patient_flow --env-file /path/to/scratch.env --db-url postgresql://oakly:oakly@127.0.0.1:55432/oakly

The scratch env needs DATABASE_URL, SECRET_KEY, VITALS_ENCRYPTION_KEY, PAYSTACK_SECRET_KEY, PAYSTACK_WEBHOOK_SECRET (placeholders)
and PAYSTACK_BASE_URL=http://127.0.0.1:<--paystack-port> so the API's Paystack calls reach the local fake.

What is real and what is not
- Every API call is a real HTTP call to the running API (default http://127.0.0.1:8000): accounts are registered and verified
  through the endpoints, the caregiver is onboarded and approved through the endpoints, the caregiver accepts, starts and
  completes through the endpoints, the refund is approved through the admin endpoints.
- Seeded directly in the database for the cancel sections (section 5, the payment link, uses the real create endpoint
  against the local fake Paystack): the
  booking rows (the same insert capture_caregiver_flow.py uses), and the admin user. Each is labelled in the captures.
- Payment is settled with a correctly signed Paystack *webhook* sent to the API's own webhook route. Paystack itself is a LOCAL
  FAKE (app/tests/fake_meta.py): verify-before-cancel for an unpaid booking asks it. Nothing here proves a live Paystack account.
- The worker is NOT running: the caregiver's "booking cancelled" push is recorded as an outbox row (booking_events) and shown
  as such; it was not delivered. The refund dispatch job (money out through Paystack) is not run: refunds stop at "approved".

Everything created here uses placeholder data (example.com emails, +2340000000xxx phones). JWTs and passwords are replaced by
placeholders in the files.
"""

import argparse
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

# capture_caregiver_flow sets harmless placeholder settings before it imports the app's fake Paystack; import it first.
from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, FakePaystack, Recorder, approve_caregiver, onboard_caregiver, pay_booking, post_location,
    read_env, register_verify_login, seed_admin_session, seed_booking, serve_fake_paystack,
)

CODE_KEYS = ("fee_tier", "fee_amount_kobo", "refundable_amount_kobo")


def observe(rec: Recorder, label: str, sql: str, rows: Any, note: str) -> None:
    rec.save(label, {"request": {"method": "SQL", "path": sql, "auth": None, "body": None},
                     "response": {"status_code": None, "headers": {}, "body": rows}, "note": note})


def refund_rows(db: DB, booking_id: str) -> list:
    return [{"status": s, "reason_code": r, "origin": o, "suggested_amount_kobo": a, "amount_kobo": m, "destination": d}
            for (s, r, o, a, m, d) in db.run(
                "SELECT status, reason_code, origin, suggested_amount_kobo, amount_kobo, destination FROM refunds"
                " WHERE booking_id = %s ORDER BY created_at", (booking_id,))]


def paid_booking(ctx: Ctx, email: str, hours_ahead: float, label: str, capture: bool = False) -> Dict[str, Any]:
    booking = seed_booking(ctx, email, hours_ahead=hours_ahead)
    saved = ctx.rec.quiet
    ctx.rec.quiet = not capture
    pay_booking(ctx, booking, label)
    ctx.rec.quiet = saved
    return booking


FIRST_LINK_CAPTURE = 65  # section 5 starts here; --only-payment-link keeps 001-064 and rewrites everything from this number


def run(args) -> None:
    out = Path(args.out)
    kept_index: list = []
    if args.only_payment_link:
        # Re-capture section 5 only: leave the cancel captures (and the guide text quoting them) exactly as they are.
        index = json.loads((out / "000-index.json").read_text())
        kept_index = [c for c in index["captures"] if int(c["file"][:3]) < FIRST_LINK_CAPTURE]
        for f in out.glob("*.json"):
            if f.name != "000-index.json" and int(f.name[:3]) >= FIRST_LINK_CAPTURE:
                f.unlink()
    else:
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
    env = read_env(args.env_file)
    rec = Recorder(args.base_url, out)
    if args.only_payment_link:  # the sign-up helpers save their own captures: send those to a throwaway folder
        rec.out = Path(tempfile.mkdtemp(prefix="oakly-capture-setup-"))
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    paystack = FakePaystack()
    server = serve_fake_paystack(paystack, port=args.paystack_port)
    ctx = Ctx(rec, db, env, None, tag, paystack)  # type: ignore[arg-type]  # no Expo receiver: pushes are not captured here
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731
    api = "/api/v1/booking"

    # --- people (quiet): an admin, one approved caregiver, the patient, a second patient -----------------------------
    rec.quiet = True
    admin = seed_admin_session(rec, db, env, mail("admin"))
    cg = onboard_caregiver(ctx, "c", mail("caregiver"), "Placeholder Caregiver", capture=False)
    approve_caregiver(ctx, admin, cg, capture=False)
    tc = cg["token"]
    post_location(ctx, "c-location", tc, available=True)
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]
    tq = register_verify_login(ctx, "q", mail("other"), "Placeholder Other Patient", "patient")["access_token"]
    rec.quiet = False
    patient_email = mail("patient")
    if args.only_payment_link:
        shutil.rmtree(rec.out, ignore_errors=True)
        rec.out, rec.n, rec.index = out, FIRST_LINK_CAPTURE - 1, []

    def unpaid(label: str) -> Dict[str, Any]:
        booking = seed_booking(ctx, patient_email, hours_ahead=5)
        paystack.initialised.append({"reference": booking["reference"], "amount": booking["amount_kobo"], "email": patient_email})
        return booking

    def cancel_flow() -> None:
        nonlocal server
        # --- 1. a paid booking still looking for a caregiver: free ---------------------------------------------------------
        b1 = paid_booking(ctx, patient_email, 30, "free-booking-payment-webhook", capture=True)
        rec.call("p-bookings-list-dispatching", "GET", f"{api}/patient/bookings", token=tp,
                 note="Booking 1: paid, 30 hours out, no caregiver yet (the booking row was seeded, the payment settled by webhook).")
        rec.call("p-b1-detail-before-cancel", "GET", f"{api}/booking/{b1['id']}", token=tp)
        rec.call("p-b1-cancel-terms-free", "GET", f"{api}/{b1['id']}/cancel-terms", token=tp)
        rec.call("p-b1-cancel", "POST", f"{api}/{b1['id']}/cancel", token=tp,
                 json_body={"reason": "Placeholder: plans changed", "accepted_fee_kobo": 0})
        rec.call("p-b1-cancel-again", "POST", f"{api}/{b1['id']}/cancel", token=tp,
                 note="A double tap or a retry: 200, already_cancelled true, the same numbers. No second refund.")
        rec.call("p-b1-cancel-terms-after", "GET", f"{api}/{b1['id']}/cancel-terms", token=tp)
        observe(rec, "db-b1-refund-rows", "SELECT status, reason_code, origin, suggested_amount_kobo, amount_kobo, destination FROM refunds"
                " WHERE booking_id = <booking 1>", refund_rows(db, b1["id"]),
                "One refund row after two cancels. pending_approval: nothing was sent, nobody approved it, destination not chosen.")
        rec.call("p-b1-detail-after-cancel", "GET", f"{api}/booking/{b1['id']}", token=tp)
        rec.call("p-bookings-list-after-cancel", "GET", f"{api}/patient/bookings", token=tp)
        queue = rec.call("admin-refunds-queue", "GET", "/api/v1/admin/refunds", token=admin,
                         note="What Finance sees: the cancel queued a pending_approval refund. The family's reason is in the note.")
        refund_id = next(r["id"] for r in queue.json()["items"] if r["booking_id"] == b1["id"])
        rec.call("admin-refund-approve", "POST", f"/api/v1/admin/refunds/{refund_id}/approve", token=admin,
                 json_body={"destination": "original_method"},
                 note="A person approves it. The refund dispatch job (money out) was NOT run in this capture.")
        rec.call("p-b1-detail-refund-approved", "GET", f"{api}/booking/{b1['id']}", token=tp,
                 note="The family's view of an approved refund: status processing.")

        # --- 2. an accepted booking, 5 hours out: the 20% tier --------------------------------------------------------------
        b2 = paid_booking(ctx, patient_email, 5, "standard-booking-payment-webhook")
        rec.call("c-accept-b2", "POST", f"{api}/caregiver/action/{b2['id']}", token=tc, json_body={"action": "accept"})
        rec.call("p-b2-detail-accepted", "GET", f"{api}/booking/{b2['id']}", token=tp,
                 note="Booking 2: accepted by the caregiver, 5 hours out. visit_code is shown to its patient while accepted.")
        rec.call("p-bookings-list-accepted", "GET", f"{api}/patient/bookings", token=tp, params={"status": "accepted"},
                 note="The list never carries the visit code.")
        rec.call("p-b2-cancel-terms-standard", "GET", f"{api}/{b2['id']}/cancel-terms", token=tp)
        rec.call("q-b2-cancel-terms-other-patient", "GET", f"{api}/{b2['id']}/cancel-terms", token=tq, expect=403)
        rec.call("q-b2-cancel-other-patient", "POST", f"{api}/{b2['id']}/cancel", token=tq, expect=403)
        rec.call("c-b2-cancel-terms-as-caregiver", "GET", f"{api}/{b2['id']}/cancel-terms", token=tc, expect=403)
        rec.call("none-b2-cancel-terms-no-token", "GET", f"{api}/{b2['id']}/cancel-terms", expect=401)
        rec.call("p-b2-cancel-terms-missing", "GET", f"{api}/{uuid.uuid4()}/cancel-terms", token=tp, expect=404)
        rec.call("p-b2-cancel-missing", "POST", f"{api}/{uuid.uuid4()}/cancel", token=tp, expect=404)
        rec.call("p-b2-cancel-bad-body", "POST", f"{api}/{b2['id']}/cancel", token=tp, expect=422,
                 json_body={"accepted_fee_kobo": -5})
        rec.call("p-b2-cancel-terms-changed", "POST", f"{api}/{b2['id']}/cancel", token=tp, expect=409,
                 json_body={"accepted_fee_kobo": 0},
                 note="The family had seen a fee of 0 (as if before the caregiver accepted). The fee is 20% now: nothing is cancelled.")
        rec.call("p-b2-cancel-standard", "POST", f"{api}/{b2['id']}/cancel", token=tp,
                 json_body={"accepted_fee_kobo": 1_000_000})
        rec.call("p-b2-cancel-standard-again", "POST", f"{api}/{b2['id']}/cancel", token=tp, json_body={"accepted_fee_kobo": 1_000_000})
        observe(rec, "db-b2-refund-rows", "SELECT ... FROM refunds WHERE booking_id = <booking 2>", refund_rows(db, b2["id"]),
                "Exactly one refund: total paid minus 20% of the service fee.")
        observe(rec, "db-b2-caregiver-outbox", "SELECT event, dedupe_key IS NOT NULL AS keyed, delivered_at IS NOT NULL AS delivered"
                " FROM booking_events WHERE booking_id = <booking 2>",
                [{"event": e, "keyed": k, "delivered": d} for (e, k, d) in db.run(
                    "SELECT event, dedupe_key <> '', delivered_at IS NOT NULL FROM booking_events WHERE booking_id = %s"
                    " AND event = 'caregiver_booking_cancelled'", (b2["id"],))],
                "NOT DELIVERED: the worker was not running, so this is only the outbox row the cancel wrote (once, despite the "
                "retry). The push itself is not exercised live.")
        rec.call("p-b2-detail-after-cancel", "GET", f"{api}/booking/{b2['id']}", token=tp,
                 note="visit_code is null once cancelled; cancel_fee_tier, cancel_fee and refund are filled.")
        rec.call("c-b2-detail-as-assigned-caregiver", "GET", f"{api}/booking/{b2['id']}", token=tc,
                 note="The caregiver does not see the family's fee or refund: the three fields are null for them.")

        # --- 3. a caregiver on the way (50% tier), then started and completed -----------------------------------------------
        b3 = paid_booking(ctx, patient_email, 1, "late-booking-payment-webhook")
        rec.call("c-accept-b3", "POST", f"{api}/caregiver/action/{b3['id']}", token=tc, json_body={"action": "accept"})
        rec.call("p-b3-cancel-terms-late-accepted", "GET", f"{api}/{b3['id']}/cancel-terms", token=tp,
                 note="Booking 3: 1 hour out: inside the late window.")
        rec.call("c-on-my-way-b3", "POST", f"{api}/caregiver/action/{b3['id']}", token=tc, json_body={"action": "on_my_way"})
        rec.call("p-b3-cancel-terms-en-route", "GET", f"{api}/{b3['id']}/cancel-terms", token=tp)
        code = rec.call("p-b3-detail-en-route", "GET", f"{api}/booking/{b3['id']}", token=tp,
                        note="visit_code is still shown while en_route.").json()["visit_code"]
        rec.call("c-start-b3", "POST", f"{api}/caregiver/action/{b3['id']}", token=tc,
                 json_body={"action": "start", "visit_code": code})
        rec.call("p-b3-cancel-terms-in-progress", "GET", f"{api}/{b3['id']}/cancel-terms", token=tp)
        rec.call("p-b3-cancel-in-progress", "POST", f"{api}/{b3['id']}/cancel", token=tp, expect=409)
        rec.call("c-complete-b3", "POST", f"{api}/caregiver/action/{b3['id']}", token=tc, json_body={"action": "complete"})
        rec.call("p-b3-cancel-terms-completed", "GET", f"{api}/{b3['id']}/cancel-terms", token=tp)
        rec.call("p-b3-cancel-completed", "POST", f"{api}/{b3['id']}/cancel", token=tp, expect=409)
        rec.call("p-b3-detail-completed", "GET", f"{api}/booking/{b3['id']}", token=tp)
        observe(rec, "db-b3-refund-rows", "SELECT ... FROM refunds WHERE booking_id = <booking 3>", refund_rows(db, b3["id"]),
                "No refund rows: the refused cancels queued nothing.")

        # --- 4. before paying ------------------------------------------------------------------------------------------------
        u1 = unpaid("u1")
        rec.call("p-u1-cancel-terms-unpaid", "GET", f"{api}/{u1['id']}/cancel-terms", token=tp,
                 note="Booking 4: created, payment link not used. (Seeded row; the fake Paystack knows its reference, not paid.)")
        rec.call("p-u1-cancel-unpaid", "POST", f"{api}/{u1['id']}/cancel", token=tp, json_body={"accepted_fee_kobo": 0})
        rec.call("p-u1-cancel-unpaid-again", "POST", f"{api}/{u1['id']}/cancel", token=tp)
        observe(rec, "db-u1-refund-rows", "SELECT ... FROM refunds WHERE booking_id = <booking 4>", refund_rows(db, u1["id"]),
                "A free cancel of an unpaid booking creates no refund.")
        rec.call("p-u1-detail-after-cancel", "GET", f"{api}/booking/{u1['id']}", token=tp)

        u2 = unpaid("u2")
        paystack.paid.add(u2["reference"])
        rec.call("p-u2-cancel-paid-but-webhook-late", "POST", f"{api}/{u2['id']}/cancel", token=tp, json_body={"accepted_fee_kobo": 0},
                 note="Booking 5: the family paid on the link a moment ago and the webhook has not arrived. SIMULATED: the fake "
                      "Paystack says it was paid. The cancel settles the payment first, then cancels it as a paid booking.")
        observe(rec, "db-u2-refund-rows", "SELECT ... FROM refunds WHERE booking_id = <booking 5>", refund_rows(db, u2["id"]),
                "The payment was not cancelled away: it is refunded through the queue.")

        u3 = unpaid("u3")
        server.shutdown()
        server.server_close()
        rec.call("p-u3-cancel-paystack-unreachable", "POST", f"{api}/{u3['id']}/cancel", token=tp, expect=503,
                 json_body={"accepted_fee_kobo": 0},
                 note="SIMULATED: the fake Paystack was shut down for this call, so Oakly could not ask it.")
        server = serve_fake_paystack(paystack, port=args.paystack_port)
        rec.call("p-u3-detail-still-pending", "GET", f"{api}/booking/{u3['id']}", token=tp,
                 note="Nothing changed by the 503: the booking is still pending.")


    if not args.only_payment_link:
        cancel_flow()

    # --- 5. "Pay now" after an app restart: POST /booking/{id}/payment-link ---------------------------------------------------
    # A fresh database has no pricing version yet (booking_settings is never seeded), so the first calls show the 503.
    preview_body = {"service_type": "nurse", "duration_hours": 3}
    create_body = {"service_type": "nurse", "assistance_type": "Placeholder assistance", "duration_hours": 3,
                   "scheduled_date": (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT10:00:00"),
                   "patient_address": "12 Placeholder Street, Lekki", "patient_latitude": 6.4474,
                   "patient_longitude": 3.4723, "patient_city": "Lagos", "patient_state": "Lagos", "booking_for": "myself"}
    rec.call("p-preview-no-pricing", "POST", f"{api}/preview", expect=503, json_body=preview_body,
             note="A new environment with no booking_settings row yet: a 503 with a stable code (it used to be a 500).")
    rec.call("p-create-no-pricing", "POST", f"{api}/create", token=tp, expect=503, json_body=create_body,
             note="Same for create: nothing was created.")
    rec.call("admin-first-pricing", "POST", "/api/v1/booking-settings/", token=admin,
             json_body={"base_fee": 5000.0, "platform_fee_percent": 10.0,
                        "multipliers": {"nurse": 1.0, "caregiver": 0.8}, "reason": "Placeholder: first pricing version"},
             note="What every new environment needs once, done by an admin with pricing.write. Never seeded automatically.")
    rec.call("p-preview", "POST", f"{api}/preview", json_body=preview_body)
    paystack.reject_duplicate_reference = True  # like the real Paystack: a reference it has seen cannot be initialised again
    created = rec.call("p-create", "POST", f"{api}/create", token=tp, json_body=create_body,
                       note="The real create endpoint (the API's Paystack calls reach the local fake Paystack, which here refuses a "
                            "reference it has already seen, like the real one). payment_url is the link; the app is closed "
                            "before the person pays. The link is also stored on the booking.").json()
    bid = created["id"]
    link_path = f"{api}/{bid}/payment-link"
    rec.call("p-link", "POST", link_path, token=tp,
             note="The app restarted: it lists the booking (payment_status pending) and asks for a link. Same Paystack "
                  "reference and the SAME payment_url / payment_access_code that create returned (the stored link; Paystack "
                  "is not asked to initialise the reference again), plus paystack_reference and payment_expires_at.")
    rec.call("p-link-double-tap", "POST", link_path, token=tp,
             note="A second tap: 200 again, the SAME reference and the same link. Nothing is written and no second transaction exists.")
    stored = db.run("SELECT paystack_authorization_url, paystack_access_code, paystack_reference FROM bookings WHERE id = %s", (bid,))[0]
    observe(rec, "db-link-stored-and-paystack-asked-once",
            "SELECT paystack_authorization_url, paystack_access_code FROM bookings WHERE id = <the created booking> "
            "(compared with create's payment_url / payment_access_code; the count is the fake Paystack's own tally)",
            [{"stored_url": stored[0], "stored_access_code": stored[1],
              "equals_create_response": stored[0] == created["payment_url"] and stored[1] == created["payment_access_code"],
              "paystack_initialise_calls_for_this_reference": sum(1 for x in paystack.initialised if x["reference"] == stored[2])}],
            "The link create returned is stored on the booking, and Paystack was asked to initialise the reference once (at create) "
            "although the link was asked for twice since.")
    rec.call("p-link-detail-has-no-link", "GET", f"{api}/booking/{bid}", token=tp,
             note="The booking detail (and list) never carry the stored link: payment_url and payment_access_code are null here. "
                  "The link comes only from create and from payment-link.")
    observe(rec, "db-link-one-booking-one-reference", "SELECT count(*), count(DISTINCT paystack_reference) FROM bookings"
            " WHERE id = <the created booking>", db.run("SELECT count(*), count(DISTINCT paystack_reference) FROM bookings"
                                                         " WHERE id = %s", (bid,)),
            "One booking row and one reference after create plus two links.")
    rec.call("q-link-other-patient", "POST", link_path, token=tq, expect=403)
    rec.call("c-link-as-caregiver", "POST", link_path, token=tc, expect=403)
    rec.call("none-link-no-token", "POST", link_path, expect=401)
    rec.call("p-link-missing", "POST", f"{api}/{uuid.uuid4()}/payment-link", token=tp, expect=404)

    paystack.fail_init = "Placeholder: Paystack is having a bad moment"
    rec.call("p-link-stored-while-paystack-init-down", "POST", link_path, token=tp,
             note="SIMULATED: the fake Paystack refuses to initialise. The stored link is still handed out (200): Paystack is "
                  "not asked to initialise a booking that already has its link. (It is still asked whether the booking was paid.)")
    old = seed_booking(ctx, patient_email, hours_ahead=5)  # a booking row with NO stored link, unknown to Paystack
    rec.call("p-link-no-stored-link-paystack-refuses", "POST", f"{api}/{old['id']}/payment-link", token=tp, expect=503,
             note="SIMULATED, and a SEEDED row with no stored link (what a booking created before migration 0023 looks like): "
                  "Oakly tries to initialise its reference, the fake Paystack refuses. 503, the booking is NOT cancelled.")
    paystack.fail_init = None
    rec.call("p-link-no-stored-link-detail-still-pending", "GET", f"{api}/booking/{old['id']}", token=tp)
    rec.call("p-link-no-stored-link-initialised", "POST", f"{api}/{old['id']}/payment-link", token=tp,
             note="Same seeded booking, Paystack healthy again and not having seen the reference: it is initialised and "
                  "the link is stored from now on.")
    legacy = unpaid("legacy")  # a seeded row with no stored link whose reference Paystack already holds
    rec.call("p-link-no-stored-link-reference-already-seen", "POST", f"{api}/{legacy['id']}/payment-link", token=tp, expect=503,
             note="A SEEDED booking with no stored link whose reference the (duplicate-refusing) fake Paystack has already seen, "
                  "as a booking created before migration 0023 would be on the real Paystack: 503 payment_link_unavailable, "
                  "nothing changes, the booking is not cancelled. Its payment window closes on its own.")
    rec.call("p-link-detail-still-pending", "GET", f"{api}/booking/{bid}", token=tp)

    server.shutdown()
    server.server_close()
    rec.call("p-link-paystack-unreachable", "POST", link_path, token=tp, expect=503,
             note="SIMULATED: the fake Paystack was shut down, so Oakly could not ask whether it was already paid.")
    server = serve_fake_paystack(paystack, port=args.paystack_port)

    (ref, total) = db.run("SELECT paystack_reference, total_amount_kobo FROM bookings WHERE id = %s", (bid,))[0]
    paystack.paid.add(ref)
    rec.call("p-link-paid-but-webhook-late", "POST", link_path, token=tp, expect=409,
             note="SIMULATED: the family paid a moment ago and the webhook has not arrived (the fake Paystack says paid). "
                  "The booking is settled first, then the link is refused.")
    observe(rec, "db-link-settled", "SELECT payment_status, status FROM bookings WHERE id = <the created booking>",
            [{"payment_status": a, "status": b} for (a, b) in db.run(
                "SELECT payment_status::text, status::text FROM bookings WHERE id = %s", (bid,))],
            "Settled by the link request (the same settlement the webhook uses).")
    rec.call("p-link-after-paid", "POST", link_path, token=tp, expect=409, note="A repeat: refused again, nothing re-notified.")

    v1 = unpaid("v1")
    db.run("UPDATE bookings SET payment_expires_at = now() - interval '1 minute' WHERE id = %s", (v1["id"],))
    rec.call("p-link-window-expired", "POST", f"{api}/{v1['id']}/payment-link", token=tp, expect=409,
             note="Booking whose payment window closed (the row was edited to be 1 minute past; the expiry sweep has not run).")
    v2 = unpaid("v2")
    rec.call("p-v2-cancel-unpaid", "POST", f"{api}/{v2['id']}/cancel", token=tp, json_body={"accepted_fee_kobo": 0})
    rec.call("p-link-cancelled", "POST", f"{api}/{v2['id']}/payment-link", token=tp, expect=409)

    (out / "000-index.json").write_text(json.dumps({
        "generated_by": "scripts/capture_patient_flow.py", "run_tag": tag, "captured_at": datetime.utcnow().isoformat() + "Z",
        "captures": kept_index + rec.index}, indent=2) + "\n")
    server.shutdown()
    print(f"wrote {len(rec.index)} captures to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:55432/oakly")
    p.add_argument("--env-file", required=True, help="the env file the API was started with (for the Paystack key)")
    p.add_argument("--out", default="docs/fe-captures/patient")
    p.add_argument("--only-payment-link", action="store_true",
                   help="re-capture section 5 (the payment link) only: keeps captures 001-064, rewrites 065 onward")
    p.add_argument("--paystack-port", type=int, default=9912,
                   help="where the local fake Paystack listens; the API's PAYSTACK_BASE_URL must point at it")
    run(p.parse_args())


if __name__ == "__main__":
    main()
