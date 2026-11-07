"""Capture the patient notification feed against a RUNNING Oakly API and write every request and response to
docs/fe-captures/patient-notifications/*.json. docs/fe-integration-guide-patient-notifications.md is copied from these
files, never from the schemas.

    DATABASE_URL=<the scratch database the API runs on> SECRET_KEY=... VITALS_ENCRYPTION_KEY=... \\
        poetry run python -m scripts.capture_patient_notifications --base-url http://127.0.0.1:8013

What is real and what is not
- Every endpoint call is a real HTTP call to the running API: sign-in, the push-token registration, the feed, read, read-all,
  and the caregiver's accept / on-my-way / start / complete / cancel (which is what makes the booking events).
- Seeded directly in the database (the booking endpoint needs a live Paystack account, caregiver approval needs an admin):
  the patients, the caregiver and the paid bookings. Each is labelled in the captures.
- DELIVERY is the real function (``deliver_pending``, the body of the worker's ``whatsapp_events_job``) run once after each
  step inside THIS process against the same database; in production the ARQ worker runs it every 15 seconds. It writes the
  feed rows and calls Expo.
- Expo is a LOCAL FAKE receiver started by this script. The push REQUEST bodies in ``*-expo-*.json`` are exactly what the
  delivery code sent; the responses are the fake's, shaped after Expo's documented ticket format. NO REAL PUSH IS SENT and
  nothing here proves a device receives one: that needs an EAS project, APNs/FCM credentials and a physical device.
- Events whose trigger needs an admin session, Paystack or a caregiver "I've arrived" step that does not exist yet (Oakly
  cancels, refund started / completed / needs bank details, no caregiver found, arrived) are recorded in this process through
  the real ``NotificationService`` methods, then delivered by the same function. Labelled ``in-process`` in the captures.

Everything created here uses placeholder data (example.com emails, ExponentPushToken[placeholder-...] tokens). JWTs and
passwords are replaced by placeholders in the files.
"""

import argparse
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

OUT = Path("docs/fe-captures/patient-notifications")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PHONE = re.compile(r"\+234\d{10}")  # the factories' random numbers: shown as an obvious placeholder
LIVE_TOKEN = "ExponentPushToken[placeholder-patient-phone]"
DEAD_TOKEN = "ExponentPushToken[placeholder-old-phone]"


class FakeExpo:
    """Records every request body and answers with Expo's ticket format. Tokens in ``dead`` are reported unregistered."""

    def __init__(self, port: int):
        self.batches: list = []
        self.dead: set = set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                batch = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.batches.append(batch)
                tickets = [{"status": "error", "message": "not registered", "details": {"error": "DeviceNotRegistered"}}
                           if m["to"] in outer.dead else {"status": "ok", "id": f"placeholder-ticket-{i}"}
                           for i, m in enumerate(batch)]
                body = json.dumps({"data": tickets}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def take(self) -> list:
        messages = [m for b in self.batches for m in b]
        self.batches.clear()
        return messages


class Recorder:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=30)
        self.tokens: dict = {}
        self.index: list = []

    def _scrub(self, value):
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, str):
            value = PHONE.sub("+2340000000000", value)
            return JWT.sub(lambda m: self.tokens.setdefault(m.group(0), f"<TOKEN_{len(self.tokens) + 1}>"), value)
        return value

    def _write(self, label: str, record: dict, status) -> None:
        n = len(self.index) + 1
        name = f"{n:02d}-{label}.json"
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(json.dumps(self._scrub(record), indent=2, ensure_ascii=False) + "\n")
        self.index.append({"file": name, "label": label, "status": status})
        print(f"{name}  {status}", file=sys.stderr)

    def call(self, label, method, path, *, token=None, json_body=None, params=None, note=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self.http.request(method, path, headers=headers, json=json_body, params=params)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        shown_path = path + (("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else "")
        record = {
            "request": {"method": method, "path": shown_path,
                        "auth": "Bearer <ACCESS_TOKEN of the user named in the label>" if token else None,
                        "body": json_body},
            "response": {"status_code": r.status_code, "body": body},
        }
        if note:
            record["note"] = note
        self._write(label, record, r.status_code)
        return r

    def observe(self, label: str, observed, note: str) -> None:
        self._write(label, {"observed": observed, "note": note}, "observed")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8013")
    p.add_argument("--expo-port", type=int, default=9921)
    args = p.parse_args(argv)
    if not os.environ.get("DATABASE_URL"):
        p.error("DATABASE_URL must point at the database the API under capture uses")
    os.environ["EXPO_PUSH_URL"] = f"http://127.0.0.1:{args.expo_port}/--/api/v2/push/send"
    os.environ.setdefault("OAKLY_ENV_FILE", "")

    from app.channels.whatsapp.notifications import deliver_pending
    from app.core.security import decrypt
    from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
    from app.db.models.push_token import PushToken
    from app.db.models.user import User
    from app.db.session import SessionLocal
    from app.services import booking_events
    from app.services.notification_service import NotificationService
    from app.tests.factories import DEFAULT_PASSWORD, make_booking, make_caregiver, make_patient

    expo = FakeExpo(args.expo_port)
    rec = Recorder(args.base_url)
    api = "/api/v1"
    db = SessionLocal()

    def login(user: User) -> str:
        r = rec.http.post(f"{api}/auth/login/mobile", json={"email_or_phone": user.email, "password": DEFAULT_PASSWORD})
        r.raise_for_status()
        return r.json()["access_token"]

    def deliver(label: str = None, note: str = "") -> list:
        deliver_pending(db)
        messages = expo.take()
        if label:
            rec.observe(f"expo-{label}", messages, "what the delivery code sent to the (fake) Expo API. " + note)
        return messages

    patient_a, patient_b = make_patient(db), make_patient(db)
    caregiver = make_caregiver(db, full_name="Ngozi Placeholder")
    user_a, user_b, user_c = (db.get(User, patient_a.user_id), db.get(User, patient_b.user_id),
                              db.get(User, caregiver.user_id))
    ta, tb, tc = login(user_a), login(user_b), login(user_c)
    for t in (ta, tb, tc):
        rec.tokens.setdefault(t, f"<TOKEN_{len(rec.tokens) + 1}>")
    feed = f"{api}/patient/notifications"

    rec.call("feed-no-token", "GET", feed)
    rec.call("feed-as-caregiver", "GET", feed, token=tc, note="a caregiver account is refused: the feed is for patients")
    rec.call("feed-empty", "GET", feed, token=ta, note="patient A, before anything happened")
    rec.call("register-push-token", "POST", f"{api}/devices/push-token", token=ta,
             json_body={"token": LIVE_TOKEN, "platform": "ios"},
             note="the existing endpoint, called as a PATIENT: nothing new to build in the app for the token itself")

    # --- a visit, through the caregiver's real HTTP actions --------------------------------------------------
    start = datetime.utcnow() + timedelta(minutes=10)
    booking = make_booking(db, patient_a, status=BookingStatus.PENDING, payment_status=PaymentStatus.PAID,
                           scheduled_date=start, scheduled_end_date=start + timedelta(hours=4))
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="New Booking Request",
                               message="x", notification_type="new_booking"))
    db.commit()
    action = f"{api}/booking/caregiver/action/{booking.id}"
    deliver()
    rec.call("caregiver-accepts", "POST", action, token=tc, json_body={"action": "accept"},
             note="SEEDED: the paid booking and the dispatch offer. The accept is a real HTTP call.")
    deliver("caregiver-found", "Event caregiver_assigned: the patient is told who is coming.")
    rec.call("caregiver-on-my-way", "POST", action, token=tc, json_body={"action": "on_my_way"})
    deliver("caregiver-on-the-way")
    detail = rec.call("patient-reads-the-visit-code", "GET", f"{api}/booking/booking/{booking.id}", token=ta,
                      note="the visit code is on the booking detail, not in any notification")
    code = detail.json()["visit_code"]
    rec.call("caregiver-starts", "POST", action, token=tc, json_body={"action": "start", "visit_code": code})
    deliver("visit-started")
    rec.call("caregiver-completes", "POST", action, token=tc, json_body={"action": "complete"})
    deliver("visit-completed")
    db.expire_all()
    assert db.get(Booking, booking.id).status == BookingStatus.COMPLETED

    # --- the caregiver cancels a second booking -------------------------------------------------------------
    second = make_booking(db, patient_a, caregiver, status=BookingStatus.ACCEPTED, payment_status=PaymentStatus.PAID)
    rec.call("caregiver-cancels", "POST", f"{api}/booking/caregiver/action/{second.id}", token=tc,
             json_body={"action": "cancel", "reason": "placeholder reason"}, note="SEEDED: the accepted booking")
    deliver("booking-cancelled-by-caregiver")

    # --- IN-PROCESS: events whose trigger cannot be driven over HTTP here -----------------------------------
    notify = NotificationService(db)
    arriving = make_booking(db, patient_a, caregiver, status=BookingStatus.EN_ROUTE, payment_status=PaymentStatus.PAID,
                            visit_code="4821")
    notify.caregiver_arrived(arriving)
    db.commit()
    deliver("caregiver-arrived", "IN-PROCESS: NotificationService.caregiver_arrived (no booking action calls it yet). "
            "The visit code on this booking is 4821: it is not in the push.")
    cancelled = make_booking(db, patient_a, caregiver, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PAID,
                             refund_required=True)
    notify.cancelled_by_admin(cancelled)
    notify.refund_flagged(cancelled)
    notify.refund_processed(cancelled)
    db.commit()
    deliver("oakly-cancelled-and-refund", "IN-PROCESS: cancelled_by_admin, refund_flagged, refund_processed.")
    paying = make_booking(db, patient_a, status=BookingStatus.PENDING, payment_status=PaymentStatus.PAID)
    booking_events.emit(db, paying, "payment_received")
    reassigned = make_booking(db, patient_a, caregiver, status=BookingStatus.ACCEPTED, payment_status=PaymentStatus.PAID,
                              visit_code="5173")
    notify.caregiver_reassigned(reassigned)
    db.commit()
    deliver("payment-received-and-reassigned", "IN-PROCESS: payment_received (what settling a payment records) and "
            "caregiver_reassigned (what an admin reassign records).")
    unmatched = make_booking(db, patient_a, status=BookingStatus.PENDING, payment_status=PaymentStatus.PAID)
    notify.no_caregiver_found(unmatched)
    db.commit()
    deliver("no-caregiver-found", "IN-PROCESS: NotificationService.no_caregiver_found (what dispatch calls when it gives up).")

    # --- a push token Expo says is dead is deleted ----------------------------------------------------------------
    rec.call("register-second-token", "POST", f"{api}/devices/push-token", token=ta,
             json_body={"token": DEAD_TOKEN, "platform": "android"})
    expo.dead.add(DEAD_TOKEN)
    before = sorted(t.token for t in db.query(PushToken).filter_by(user_id=user_a.id))
    visit = make_booking(db, patient_a, caregiver, status=BookingStatus.COMPLETED, payment_status=PaymentStatus.PAID)
    notify.visit_completed(visit)
    db.commit()
    deliver("two-devices", "one device is live, one answers DeviceNotRegistered")
    db.expire_all()
    after = sorted(t.token for t in db.query(PushToken).filter_by(user_id=user_a.id))
    rec.observe("dead-token-cleanup", {"tokens_before": before, "tokens_after": after},
                "read from the database after the delivery: the token Expo reported unregistered is gone")

    # --- the feed, over HTTP ------------------------------------------------------------------------------------------
    full = rec.call("feed-page-1", "GET", feed, token=ta, params={"limit": 5}, note="newest first; unread_count is for the whole feed")
    rec.call("feed-page-2", "GET", feed, token=ta, params={"limit": 5, "offset": 5})
    rec.call("feed-default-paging", "GET", feed, token=ta)
    rec.call("feed-limit-zero", "GET", feed, token=ta, params={"limit": 0})
    rec.call("feed-limit-too-big", "GET", feed, token=ta, params={"limit": 101})
    rec.call("feed-negative-offset", "GET", feed, token=ta, params={"offset": -1})
    first_id = full.json()["items"][0]["id"]
    one = rec.call("read-one", "POST", f"{feed}/{first_id}/read", token=ta)
    again = rec.call("read-one-again", "POST", f"{feed}/{first_id}/read", token=ta, note="idempotent: read_at is unchanged")
    assert one.json()["read_at"] == again.json()["read_at"]
    rec.call("read-someone-elses", "POST", f"{feed}/{first_id}/read", token=tb, note="patient B tries patient A's notification")
    rec.call("read-unknown-id", "POST", f"{feed}/00000000-0000-0000-0000-000000000000/read", token=ta)
    rec.call("read-bad-id", "POST", f"{feed}/not-a-uuid/read", token=ta)
    rec.call("read-all", "POST", f"{feed}/read-all", token=ta)
    rec.call("read-all-again", "POST", f"{feed}/read-all", token=ta, note="idempotent: nothing left to mark")
    rec.call("read-all-no-token", "POST", f"{feed}/read-all")
    rec.call("feed-after-read-all", "GET", feed, token=ta, params={"limit": 2})
    rec.call("feed-patient-b", "GET", feed, token=tb, note="patient B never sees patient A's rows")

    (OUT / "00-index.json").write_text(json.dumps(
        {"generated_by": "scripts/capture_patient_notifications.py", "captures": rec.index}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
