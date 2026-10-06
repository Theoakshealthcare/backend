"""Capture the caregiver-app flow against a RUNNING Oakly stack and write every request/response
to docs/fe-captures/caregiver/*.json. docs/fe-integration-guide-caregiver.md is copied from these
files, never from the schemas.

    OAKLY_ENV_FILE_PATH=/path/to/temp.env docker compose -p oakly-c3 up --build -d
    poetry run python -m scripts.capture_caregiver_flow --env-file /path/to/temp.env

What is real and what is not
- Every API call is a real HTTP call to the running API (default http://127.0.0.1:8000).
- Seeded directly in the database (there is no API for it): the admin user, the booking rows (the
  patient booking endpoint needs a live Paystack account), the caregiver wallet balance, and two
  "wait 15 minutes" shortcuts (the visit-code lock expiry). Each is labelled in the captures.
- Payment is settled with a correctly signed Paystack *webhook* sent to the API's own webhook route.
- Expo is a local fake receiver started by this script (the API is pointed at it through
  EXPO_PUSH_URL in the env file). The push REQUEST bodies are exactly what the API sent; the
  responses are the fake's, shaped after Expo's documented ticket format. No real push is sent.

Everything created here uses placeholder data (example.com emails, +2340000000xxx phones,
ExponentPushToken[placeholder-...] tokens). JWTs and passwords are replaced by placeholders in the files.
"""

import argparse
import hashlib
import hmac
import json
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import psycopg2

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)
PLACEHOLDER_PASSWORD = "Placeholder-Pass-1!"
JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
LAT, LON = 6.4474, 3.4723  # the seeded bookings' location (Lekki, Lagos)


# --- redaction -----------------------------------------------------------------------------------


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("password", "new_password"):
                out[k] = "<PLACEHOLDER_PASSWORD>"
            elif k == "access_token" and isinstance(v, str) and JWT_RE.fullmatch(v):
                out[k] = "<ACCESS_TOKEN_JWT>"
            elif k == "refresh_token" and isinstance(v, str) and JWT_RE.fullmatch(v):
                out[k] = "<REFRESH_TOKEN_JWT>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return JWT_RE.sub("<JWT>", obj)
    return obj


# --- fake Expo receiver ------------------------------------------------------------------------


class FakeExpo:
    """Records what the API POSTs. Replies with Expo-shaped tickets; any token containing 'dead'
    gets DeviceNotRegistered so token pruning can be observed."""

    def __init__(self, port: int):
        self.received: List[Dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.received.append({
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()
                                if k.lower() in ("content-type", "accept", "authorization")},
                    "body": body,
                })
                tickets = []
                for m in body:
                    if "dead" in m["to"]:
                        tickets.append({"status": "error", "message": "device not registered",
                                        "details": {"error": "DeviceNotRegistered"}})
                    else:
                        tickets.append({"status": "ok", "id": str(uuid.uuid4())})
                payload = json.dumps({"data": tickets}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


# --- recorder ----------------------------------------------------------------------------------


class Recorder:
    def __init__(self, base_url: str, out_dir: Path):
        self.http = httpx.Client(base_url=base_url, timeout=30)
        self.out = out_dir
        self.n = 0
        self.quiet = False
        self.index: List[Dict[str, Any]] = []

    def save(self, label: str, doc: Dict[str, Any]) -> None:
        if self.quiet:
            return
        self.n += 1
        name = f"{self.n:03d}-{label}.json"
        (self.out / name).write_text(json.dumps(redact(doc), indent=2, ensure_ascii=False) + "\n")
        self.index.append({"file": name, "label": label,
                           "status": doc.get("response", {}).get("status_code")})

    def call(self, label: str, method: str, path: str, *, token: Optional[str] = None,
             json_body: Any = None, files: Any = None, data: Any = None, params: Any = None,
             headers: Optional[Dict[str, str]] = None, expect: int = 200, note: Optional[str] = None) -> httpx.Response:
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        r = self.http.request(method, path, json=json_body, files=files, data=data, params=params, headers=h)
        try:
            body: Any = r.json()
        except ValueError:
            body = r.text
        req_body: Any = json_body
        if files:
            req_body = {"multipart": {**(data or {}), "file": {
                "filename": files["file"][0], "content_type": files["file"][2], "bytes": len(files["file"][1])}}}
        doc: Dict[str, Any] = {
            "request": {
                "method": method, "path": path + (f"?{r.request.url.query.decode()}" if params else ""),
                "auth": "Bearer <ACCESS_TOKEN_JWT>" if token else None,
                "body": req_body,
            },
            "response": {
                "status_code": r.status_code,
                "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "retry-after")},
                "body": body,
            },
        }
        if note:
            doc["note"] = note
        self.save(label, doc)
        if r.status_code != expect:
            sys.exit(f"capture '{label}': expected {expect}, got {r.status_code}: {r.text[:300]}")
        return r


# --- helpers -----------------------------------------------------------------------------------


class DB:
    def __init__(self, url: str):
        self.conn = psycopg2.connect(url)
        self.conn.autocommit = True

    def run(self, sql: str, params: tuple = ()) -> List[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []


def read_env(path: str) -> Dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def hash_password(pw: str) -> str:
    from passlib.context import CryptContext

    return CryptContext(schemes=["bcrypt"]).hash(pw)


class Ctx:
    def __init__(self, rec: Recorder, db: DB, env: Dict[str, str], expo: FakeExpo, tag: str):
        self.rec, self.db, self.env, self.expo, self.tag = rec, db, env, expo, tag
        self.phone_counter = 0

    def phone(self) -> str:
        self.phone_counter += 1
        return f"+2340000{int(self.tag, 16) % 1000:03d}{self.phone_counter:03d}"


def register_verify_login(ctx: Ctx, label: str, email: str, name: str, user_type: str,
                          capture_extras: bool = False) -> Dict[str, Any]:
    rec = ctx.rec
    rec.call(f"{label}-auth-register", "POST", "/api/v1/auth/register", json_body={
        "email": email, "password": PLACEHOLDER_PASSWORD, "full_name": name,
        "phone_number": ctx.phone(), "user_type": user_type})
    if capture_extras:
        rec.call(f"{label}-auth-register-duplicate", "POST", "/api/v1/auth/register", expect=400, json_body={
            "email": email, "password": PLACEHOLDER_PASSWORD, "full_name": name, "user_type": user_type})
        rec.call(f"{label}-auth-register-invalid-email", "POST", "/api/v1/auth/register", expect=422, json_body={
            "email": "not-an-email", "password": PLACEHOLDER_PASSWORD, "full_name": name, "user_type": user_type})
        rec.call(f"{label}-auth-verify-email-wrong-code", "POST", "/api/v1/auth/verify-email", expect=400,
                 json_body={"email": email, "code": "00000"})
    (code,), = ctx.db.run("SELECT verification_code FROM users WHERE email = %s", (email,))
    rec.call(f"{label}-auth-verify-email", "POST", "/api/v1/auth/verify-email", json_body={"email": email, "code": code},
             note="The OTP is emailed in production. In this stack SMTP is not configured, so the script read it "
                  "from users.verification_code.")
    if capture_extras:
        rec.call(f"{label}-auth-login-wrong-password", "POST", "/api/v1/auth/login/mobile", expect=401,
                 json_body={"email_or_phone": email, "password": "wrong-password"})
    tokens = rec.call(f"{label}-auth-login-mobile", "POST", "/api/v1/auth/login/mobile", json_body={
        "email_or_phone": email, "password": PLACEHOLDER_PASSWORD}).json()
    return tokens


def onboard_caregiver(ctx: Ctx, label: str, email: str, name: str, capture: bool) -> Dict[str, Any]:
    rec = ctx.rec
    rec.quiet = not capture
    tokens = register_verify_login(ctx, label, email, name, "caregiver", capture_extras=capture)
    t = tokens["access_token"]
    if capture:
        rec.call(f"{label}-auth-me", "GET", "/api/v1/auth/me", token=t)
        rec.call(f"{label}-auth-me-no-token", "GET", "/api/v1/auth/me", expect=401)
        rec.call(f"{label}-auth-me-refresh-token-as-bearer", "GET", "/api/v1/auth/me", expect=401,
                 token=tokens["refresh_token"])
        rec.call(f"{label}-auth-refresh", "POST", "/api/v1/auth/refresh", json_body={"refresh_token": tokens["refresh_token"]})
        rec.call(f"{label}-auth-refresh-invalid", "POST", "/api/v1/auth/refresh", expect=401,
                 json_body={"refresh_token": "not-a-token"})
        rec.call(f"{label}-form-options", "GET", "/api/v1/caregiver/form-options")
        rec.call(f"{label}-verification-status-initial", "GET", "/api/v1/caregiver/verification-status", token=t)
    rec.call(f"{label}-profile-step1", "PUT", "/api/v1/caregiver/profile/step1", token=t, json_body={
        "date_of_birth": "1990-01-01", "gender": "female", "state": "Lagos",
        "address": "1 Placeholder Street, Lekki", "emergency_contact_phone": "+2340000000999"})
    if capture:
        rec.call(f"{label}-profile-step1-missing-fields", "PUT", "/api/v1/caregiver/profile/step1", token=t,
                 expect=422, json_body={"gender": "female"})
    rec.call(f"{label}-profile-step2", "PUT", "/api/v1/caregiver/profile/step2", token=t, json_body={
        "specialization": "Elderly Care", "years_of_experience": 5, "preferred_work_hours": "weekdays",
        "service_areas": ["Personal Care", "Medication Reminders"]})
    if capture:
        rec.call(f"{label}-documents-upload-bad-type", "POST", "/api/v1/caregiver/documents/upload", token=t,
                 expect=400, data={"document_type": "license"}, files={"file": ("notes.txt", b"hello", "text/plain")})
    doc = rec.call(f"{label}-documents-upload", "POST", "/api/v1/caregiver/documents/upload", token=t,
                   data={"document_type": "license"}, files={"file": ("license.png", PNG_1X1, "image/png")}).json()
    rec.call(f"{label}-consent", "PUT", "/api/v1/caregiver/consent", token=t, json_body={
        "terms_accepted": True, "privacy_policy_accepted": True, "background_check_consent": True})
    if capture:
        rec.call(f"{label}-profile", "GET", "/api/v1/caregiver/profile", token=t)
        rec.call(f"{label}-verification-status-under-review", "GET", "/api/v1/caregiver/verification-status", token=t)
    profile = rec.call(f"{label}-profile-after", "GET", "/api/v1/caregiver/profile", token=t)
    rec.quiet = False
    return {"token": t, "refresh": tokens["refresh_token"], "doc_id": doc["id"], "id": profile.json()["id"],
            "email": email}


def approve_caregiver(ctx: Ctx, admin: str, cg: Dict[str, Any], capture: bool) -> None:
    rec = ctx.rec
    rec.quiet = not capture
    cid = cg["id"]
    base = f"/api/v1/admin/caregivers/{cid}"
    rec.call("admin-queue", "GET", "/api/v1/admin/caregivers", token=admin)
    rec.call("admin-document-decision", "POST", f"/api/v1/admin/caregivers/documents/{cg['doc_id']}/decision",
             token=admin, json_body={"decision": "verified"})
    rec.call("admin-identity-decision", "POST", f"{base}/identity-decision", token=admin,
             json_body={"decision": "verified", "notes": "Placeholder: ID checked."})
    rec.call("admin-background-check-decision", "POST", f"{base}/background-check-decision", token=admin,
             json_body={"decision": "cleared", "notes": "Placeholder: check cleared."})
    rec.call("admin-approve-no-skills", "POST", f"{base}/approve", token=admin, expect=409)
    rec.call("admin-skills", "PUT", f"{base}/skills", token=admin,
             json_body={"skills": [{"service_type": "nurse"}, {"service_type": "caregiver"}]})
    rec.call("admin-approve", "POST", f"{base}/approve", token=admin)
    rec.quiet = False


def seed_booking(ctx: Ctx, patient_email: str, hours_ahead: float = 0.25) -> Dict[str, Any]:
    (patient_id,) = ctx.db.run("SELECT p.id FROM patients p JOIN users u ON u.id = p.user_id WHERE u.email = %s",
                               (patient_email,))[0]
    bid, code = uuid.uuid4(), uuid.uuid4().hex[:8].upper()
    ref = f"BKG_{code}_{uuid.uuid4().hex[:8]}"
    start = datetime.utcnow() + timedelta(hours=hours_ahead)
    ctx.db.run(
        "INSERT INTO bookings (id, booking_code, patient_id, service_type, assistance_type, condition, duration_hours,"
        " scheduled_date, scheduled_end_date, patient_address, patient_latitude, patient_longitude, patient_city,"
        " patient_state, status, payment_status, total_amount_kobo, service_fee_kobo, platform_fee_kobo,"
        " paystack_reference, booking_for, special_instructions, payment_expires_at, created_at, updated_at)"
        " VALUES (%s,%s,%s,'nurse','Placeholder assistance','Placeholder condition',2,%s,%s,"
        " '12 Placeholder Street, Lekki',%s,%s,'Lagos','Lagos','PENDING','PENDING',5500000,5000000,500000,%s,"
        " 'myself','Placeholder instructions',%s,now(),now())",
        (str(bid), code, str(patient_id), start, start + timedelta(hours=2), LAT, LON, ref,
         datetime.utcnow() + timedelta(minutes=30)))
    return {"id": str(bid), "code": code, "reference": ref, "amount_kobo": 5500000}


def pay_booking(ctx: Ctx, booking: Dict[str, Any], label: str) -> None:
    body = json.dumps({"event": "charge.success", "data": {
        "reference": booking["reference"], "amount": booking["amount_kobo"], "status": "success",
        "channel": "card", "customer": {"email": "placeholder@example.com"}}}).encode()
    sig = hmac.new(ctx.env["PAYSTACK_SECRET_KEY"].encode(), body, hashlib.sha512).hexdigest()
    r = ctx.rec.http.post("/api/v1/payment/paystack/webhook", content=body,
                          headers={"x-paystack-signature": sig, "Content-Type": "application/json"})
    ctx.rec.save(label, {
        "request": {"method": "POST", "path": "/api/v1/payment/paystack/webhook",
                    "auth": "x-paystack-signature: <HMAC-SHA512 of the body with the Paystack secret key>",
                    "body": json.loads(body)},
        "response": {"status_code": r.status_code, "headers": {}, "body": r.json()},
        "note": "Signed with the stack's (placeholder) Paystack key. Settling a booking is what starts dispatch wave 1.",
    })
    if r.status_code != 200:
        sys.exit(f"webhook failed: {r.status_code} {r.text}")


def post_location(ctx: Ctx, label: str, token: str, available: bool, offset: float = 0.002, expect: int = 200):
    return ctx.rec.call(label, "POST", "/api/v1/booking/caregiver/location", token=token, expect=expect, json_body={
        "latitude": LAT + offset, "longitude": LON, "accuracy": 10.0, "is_available": available})


def ws_probe(ctx: Ctx, label: str, base_url: str, token: Optional[str]) -> None:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    url = base_url.replace("http", "ws", 1) + "/api/v1/booking/ws/caregiver/location"
    if token:
        url += f"?token={token}"
    frames: List[Any] = []
    close: Optional[Dict[str, Any]] = None
    try:
        with connect(url, open_timeout=10) as ws:
            if token and label.endswith("ok"):
                ws.send(json.dumps({"type": "location_update", "data": {
                    "latitude": LAT, "longitude": LON, "is_available": True}}))
                ws.send(json.dumps({"type": "heartbeat"}))
            while True:
                try:
                    frames.append(json.loads(ws.recv(timeout=2)))
                except TimeoutError:
                    break
    except ConnectionClosed as e:
        close = {"code": e.rcvd.code if e.rcvd else None, "reason": e.rcvd.reason if e.rcvd else None}
    ctx.rec.save(label, {
        "request": {"method": "WEBSOCKET", "path": "/api/v1/booking/ws/caregiver/location" + ("?token=<ACCESS_TOKEN_JWT>" if token else ""),
                    "auth": None, "body": None},
        "response": {"status_code": None, "headers": {}, "body": {"frames_received": frames, "closed_by_server": close}},
    })


# --- the flow ----------------------------------------------------------------------------------


def run(args) -> None:
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    env = read_env(args.env_file)
    expo = FakeExpo(args.expo_port)
    rec = Recorder(args.base_url, out)
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    ctx = Ctx(rec, db, env, expo, tag)
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731

    # --- admin (seeded directly: there is no API to create one) ----------------------------------
    admin_id, admin_email = uuid.uuid4(), mail("admin")
    db.run("INSERT INTO users (id, email, full_name, hashed_password, user_type, is_active, is_verified, created_at,"
           " updated_at) VALUES (%s,%s,'Placeholder Admin',%s,'patient',true,true,now(),now())",
           (str(admin_id), admin_email, hash_password(PLACEHOLDER_PASSWORD)))
    db.run("INSERT INTO admins (id, user_id, email, full_name, permissions, created_at, updated_at)"
           " VALUES (%s,%s,%s,'Placeholder Admin','{}',now(),now())",
           (str(uuid.uuid4()), str(admin_id), admin_email))
    rec.quiet = True
    admin = rec.call("admin-login", "POST", "/api/v1/auth/login/mobile",
                     json_body={"email_or_phone": admin_email, "password": PLACEHOLDER_PASSWORD}).json()["access_token"]
    rec.quiet = False

    # --- caregiver A: the main character, every step captured ------------------------------------
    a = onboard_caregiver(ctx, "a", mail("caregiver-a"), "Placeholder Caregiver A", capture=True)
    ta = a["token"]
    rec.call("a-availability-before-approval", "POST", "/api/v1/caregiver/availability", token=ta, expect=403,
             json_body={"is_available": True})
    rec.call("a-home-under-review", "GET", "/api/v1/caregiver/home", token=ta)
    rec.call("a-accept-unknown-booking-before-approval", "POST",
             f"/api/v1/booking/caregiver/action/{uuid.uuid4()}", token=ta, expect=404, json_body={"action": "accept"},
             note="An unknown booking id: 404. (An approved-only 403 on accept is captured later with caregiver B.)")
    approve_caregiver(ctx, admin, a, capture=True)
    rec.call("a-verification-status-approved", "GET", "/api/v1/caregiver/verification-status", token=ta)
    rec.call("a-home-approved-empty", "GET", "/api/v1/caregiver/home", token=ta)

    # --- caregiver B: changes requested, then rejected --------------------------------------------
    b = onboard_caregiver(ctx, "b", mail("caregiver-b"), "Placeholder Caregiver B", capture=False)
    tb = b["token"]
    base_b = f"/api/v1/admin/caregivers/{b['id']}"
    rec.call("b-admin-request-changes", "POST", f"{base_b}/request-changes", token=admin, json_body={
        "items": ["Upload a clearer licence photo"], "note": "Placeholder: the licence image is blurry."})
    rec.call("b-verification-status-changes-requested", "GET", "/api/v1/caregiver/verification-status", token=tb)
    rec.call("b-consent-resubmit", "PUT", "/api/v1/caregiver/consent", token=tb, json_body={
        "terms_accepted": True, "privacy_policy_accepted": True, "background_check_consent": True},
        note="Resubmitting consent moves changes_requested back to under_review.")
    rec.call("b-admin-reject", "POST", f"{base_b}/reject", token=admin,
             json_body={"reason": "Placeholder: licence could not be verified."})
    rec.call("b-verification-status-rejected", "GET", "/api/v1/caregiver/verification-status", token=tb)
    rec.call("b-availability-rejected", "POST", "/api/v1/caregiver/availability", token=tb, expect=403,
             json_body={"is_available": True})
    rec.call("b-home-rejected", "GET", "/api/v1/caregiver/home", token=tb)

    # --- caregiver D: an approved rival (quiet), used for the "taken by someone else" races -------
    d = onboard_caregiver(ctx, "d", mail("caregiver-d"), "Placeholder Caregiver D", capture=False)
    approve_caregiver(ctx, admin, d, capture=False)
    td = d["token"]

    # --- patient (quiet): owns the seeded bookings --------------------------------------------------
    rec.quiet = True
    patient_email = mail("patient")
    ptokens = register_verify_login(ctx, "p", patient_email, "Placeholder Patient", "patient")
    tp = ptokens["access_token"]
    rec.quiet = False

    # --- availability + location ------------------------------------------------------------------
    rec.call("a-availability-on-without-location", "POST", "/api/v1/caregiver/availability", token=ta, expect=409,
             json_body={"is_available": True})
    rec.call("a-availability-patient-forbidden", "POST", "/api/v1/caregiver/availability", token=tp, expect=403,
             json_body={"is_available": True})
    rec.call("a-availability-no-auth", "POST", "/api/v1/caregiver/availability", expect=401, json_body={"is_available": True})
    rec.call("a-availability-bad-body", "POST", "/api/v1/caregiver/availability", token=ta, expect=422, json_body={})
    post_location(ctx, "a-location-post", ta, available=True)
    rec.quiet = True
    post_location(ctx, "d-location-post", td, available=True, offset=0.004)
    rec.quiet = False
    rec.call("a-location-get", "GET", "/api/v1/booking/caregiver/location", token=ta)
    rec.call("a-availability-off", "POST", "/api/v1/caregiver/availability", token=ta, json_body={"is_available": False})
    rec.call("a-home-unavailable", "GET", "/api/v1/caregiver/home", token=ta)
    post_location(ctx, "a-location-ping-with-is-available-true", ta, available=True)
    rec.call("a-home-after-ping-true", "GET", "/api/v1/caregiver/home", token=ta,
             note="TRAP: availability was switched off above, but this location ping said is_available=true "
                  "(also the default when the field is omitted), so the caregiver is available again.")
    rec.call("a-availability-off-2", "POST", "/api/v1/caregiver/availability", token=ta, json_body={"is_available": False})
    post_location(ctx, "a-location-ping-with-is-available-false", ta, available=False)
    rec.call("a-home-after-ping-false", "GET", "/api/v1/caregiver/home", token=ta)
    rec.call("a-availability-on", "POST", "/api/v1/caregiver/availability", token=ta, json_body={"is_available": True})
    post_location(ctx, "a-location-ping-with-is-available-true-again", ta, available=True)
    ws_probe(ctx, "ws-no-token", args.base_url, None)
    ws_probe(ctx, "ws-patient-token", args.base_url, tp)
    ws_probe(ctx, "ws-caregiver-ok", args.base_url, ta)

    # --- push tokens -----------------------------------------------------------------------------
    live_tok = f"ExponentPushToken[placeholder-live-{tag}]"
    dead_tok = f"ExponentPushToken[placeholder-dead-{tag}]"
    rec.call("devices-push-token-no-auth", "POST", "/api/v1/devices/push-token", expect=401,
             json_body={"token": live_tok, "platform": "ios"})
    rec.call("devices-push-token-register", "POST", "/api/v1/devices/push-token", token=ta,
             json_body={"token": live_tok, "platform": "ios"})
    rec.call("devices-push-token-refresh", "POST", "/api/v1/devices/push-token", token=ta,
             json_body={"token": live_tok, "platform": "ios"})
    rec.call("devices-push-token-invalid-format", "POST", "/api/v1/devices/push-token", token=ta, expect=422,
             json_body={"token": "not-an-expo-token", "platform": "ios"})
    rec.call("devices-push-token-invalid-platform", "POST", "/api/v1/devices/push-token", token=ta, expect=422,
             json_body={"token": live_tok, "platform": "web"})
    rec.call("devices-push-token-register-second-device", "POST", "/api/v1/devices/push-token", token=ta,
             json_body={"token": dead_tok, "platform": "android"},
             note="Registered so the fake Expo can answer DeviceNotRegistered for it (token pruning).")
    tmp_tok = f"ExponentPushToken[placeholder-logout-{tag}]"
    rec.call("devices-push-token-register-for-logout", "POST", "/api/v1/devices/push-token", token=ta,
             json_body={"token": tmp_tok, "platform": "ios"})
    rec.call("devices-push-token-delete", "DELETE", "/api/v1/devices/push-token", token=ta, expect=204,
             json_body={"token": tmp_tok})
    rec.call("devices-push-token-delete-again", "DELETE", "/api/v1/devices/push-token", token=ta, expect=204,
             json_body={"token": tmp_tok})

    # --- offers: booking 1 (D wins the race), booking 2 (A's whole visit), 3 (decline), 4 (cancel) --
    b1, b2, b3, b4 = (seed_booking(ctx, patient_email) for _ in range(4))
    pay_booking(ctx, b1, "dispatch-1-payment-webhook")
    time.sleep(1.5)
    push_requests = list(expo.received)
    rec.save("dispatch-1-push-received-by-fake-expo", {
        "request": {"method": "POST", "path": "(the API's outbound call to EXPO_PUSH_URL)", "auth": None,
                    "body": [r["body"] for r in push_requests]},
        "response": {"status_code": 200, "headers": {}, "body": "(generated by the capture script's fake Expo)"},
        "note": "NOT a real Expo delivery. The request bodies are exactly what the API sent after the wave was "
                "committed; the ticket replies are the fake's, shaped after Expo's documented format. The second "
                "device token was answered DeviceNotRegistered.",
        "request_headers_seen_by_fake": [r["headers"] for r in push_requests],
    })
    (tokens_left,) = db.run("SELECT count(*) FROM push_tokens WHERE user_id = (SELECT user_id FROM caregivers WHERE id = %s)",
                            (a["id"],))[0]
    rec.save("dispatch-1-push-token-pruned", {
        "request": {"method": "SQL", "path": "SELECT count(*) FROM push_tokens WHERE user_id = <caregiver A>",
                    "auth": None, "body": None},
        "response": {"status_code": None, "headers": {}, "body": {"count": tokens_left}},
        "note": "A had 2 tokens before the wave (live + dead). The dead one was pruned after Expo said "
                "DeviceNotRegistered.",
    })
    rec.call("a-home-with-offer", "GET", "/api/v1/caregiver/home", token=ta)
    rec.call("a-notifications", "GET", "/api/v1/booking/caregiver/notifications", token=ta)
    rec.call("a-bookings-new-requests", "GET", "/api/v1/booking/caregiver/bookings", token=ta,
             params={"status": "new_requests"})
    rec.call("a-booking-detail-not-assigned", "GET", f"/api/v1/booking/booking/{b1['id']}", token=ta, expect=403)
    rec.call("d-accept-b1", "POST", f"/api/v1/booking/caregiver/action/{b1['id']}", token=td, json_body={"action": "accept"})
    rec.call("a-accept-taken-by-someone-else", "POST", f"/api/v1/booking/caregiver/action/{b1['id']}", token=ta,
             expect=409, json_body={"action": "accept"})
    rec.call("b-accept-not-approved", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=tb, expect=403,
             json_body={"action": "accept"})
    rec.call("a-accept-unknown-booking", "POST", f"/api/v1/booking/caregiver/action/{uuid.uuid4()}", token=ta,
             expect=404, json_body={"action": "accept"})
    rec.call("a-action-invalid-enum", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=422,
             json_body={"action": "teleport"})
    rec.call("a-action-as-patient", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=tp, expect=403,
             json_body={"action": "accept"})
    rec.call("a-action-no-auth", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", expect=401,
             json_body={"action": "accept"})

    # booking 3: decline
    pay_booking_quiet(ctx, b2)
    pay_booking_quiet(ctx, b3)
    pay_booking_quiet(ctx, b4)
    rec.call("a-decline-b3", "POST", f"/api/v1/booking/caregiver/action/{b3['id']}", token=ta,
             json_body={"action": "decline", "reason": "Placeholder: too far"})
    rec.call("a-home-after-decline", "GET", "/api/v1/caregiver/home", token=ta)

    # booking 2: the whole visit
    rec.call("a-accept-b2", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta,
                   json_body={"action": "accept"})
    rec.call("a-accept-b2-again", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=409,
             json_body={"action": "accept"})
    pd = rec.call("p-booking-detail-accepted", "GET", f"/api/v1/booking/booking/{b2['id']}", token=tp)
    code = pd.json()["visit_code"]
    rec.call("a-booking-detail-accepted", "GET", f"/api/v1/booking/booking/{b2['id']}", token=ta)
    rec.call("a-bookings-list-accepted", "GET", "/api/v1/booking/caregiver/bookings", token=ta, params={"status": "accepted"})
    rec.call("a-home-with-visit", "GET", "/api/v1/caregiver/home", token=ta)
    rec.call("d-start-not-assigned", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=td, expect=403,
             json_body={"action": "start", "visit_code": code})
    rec.call("a-on-my-way", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, json_body={"action": "on_my_way"})
    rec.call("a-on-my-way-again", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=409,
             json_body={"action": "on_my_way"})
    rec.call("a-home-en-route", "GET", "/api/v1/caregiver/home", token=ta)
    rec.call("a-bookings-list-en-route", "GET", "/api/v1/booking/caregiver/bookings", token=ta, params={"status": "en_route"})
    rec.call("p-booking-detail-en-route", "GET", f"/api/v1/booking/booking/{b2['id']}", token=tp)
    rec.call("a-complete-before-start", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=409,
             json_body={"action": "complete"})
    rec.call("a-start-missing-code", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=400,
             json_body={"action": "start"})
    rec.call("a-start-malformed-code", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=422,
             json_body={"action": "start", "visit_code": "12ab"})
    wrong = "0000" if code != "0000" else "1111"
    for i, exp in enumerate((400, 400, 423), 1):
        rec.call(f"a-start-wrong-code-{i}", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=exp,
                 json_body={"action": "start", "visit_code": wrong})
    rec.call("a-start-right-code-while-locked", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta,
             expect=423, json_body={"action": "start", "visit_code": code})
    db.run("UPDATE bookings SET visit_code_locked_until = now() at time zone 'utc' - interval '1 second' WHERE id = %s",
           (b2["id"],))
    rec.save("db-simulated-15-minute-wait", {
        "request": {"method": "SQL", "path": "UPDATE bookings SET visit_code_locked_until = <1 second ago> WHERE id = <booking 2>",
                    "auth": None, "body": None},
        "response": {"status_code": None, "headers": {}, "body": None},
        "note": "SIMULATION: stands in for waiting 15 minutes so the capture can continue.",
    })
    rec.call("a-start", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta,
             json_body={"action": "start", "visit_code": code})
    rec.call("a-start-again", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=409,
             json_body={"action": "start", "visit_code": code})
    rec.call("p-booking-detail-in-progress", "GET", f"/api/v1/booking/booking/{b2['id']}", token=tp)
    rec.call("a-complete", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, json_body={"action": "complete"})
    rec.call("a-complete-again", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=409,
             json_body={"action": "complete"})
    rec.call("a-cancel-after-complete", "POST", f"/api/v1/booking/caregiver/action/{b2['id']}", token=ta, expect=409,
             json_body={"action": "cancel"})
    rec.call("a-booking-detail-completed", "GET", f"/api/v1/booking/booking/{b2['id']}", token=ta)

    # booking 4: accept then cancel
    rec.call("a-accept-b4", "POST", f"/api/v1/booking/caregiver/action/{b4['id']}", token=ta, json_body={"action": "accept"})
    rec.call("a-cancel-b4", "POST", f"/api/v1/booking/caregiver/action/{b4['id']}", token=ta,
             json_body={"action": "cancel", "reason": "Placeholder: unwell"})

    # booking 6: far enough ahead to be "upcoming" rather than "today"
    b6 = seed_booking(ctx, patient_email, hours_ahead=72)
    pay_booking_quiet(ctx, b6)
    rec.call("a-accept-b6", "POST", f"/api/v1/booking/caregiver/action/{b6['id']}", token=ta, json_body={"action": "accept"})
    rec.call("a-home-with-upcoming", "GET", "/api/v1/caregiver/home", token=ta)

    # --- unavailable stops new offers ----------------------------------------------------------------
    rec.call("a-availability-off-again", "POST", "/api/v1/caregiver/availability", token=ta, json_body={"is_available": False})
    b5 = seed_booking(ctx, patient_email)
    before = len(expo.received)
    pay_booking(ctx, b5, "dispatch-5-payment-webhook-while-a-unavailable")
    time.sleep(1.0)
    (notified,) = db.run("SELECT notified_caregiver_ids::text[] FROM booking_dispatch_waves WHERE booking_id = %s AND wave_no = 1",
                         (b5["id"],))[0]
    rec.save("dispatch-5-who-was-offered", {
        "request": {"method": "SQL", "path": "SELECT notified_caregiver_ids FROM booking_dispatch_waves WHERE booking_id = <booking 5>",
                    "auth": None, "body": None},
        "response": {"status_code": None, "headers": {}, "body": {
            "caregiver_A_offered": a["id"] in notified,
            "caregiver_D_offered": d["id"] in notified,
            "pushes_sent_to_expo_for_this_wave": len(expo.received) - before}},
        "note": "A was unavailable when the booking was paid, so was not offered it (and got no push).",
    })
    rec.call("a-home-unavailable-no-offers", "GET", "/api/v1/caregiver/home", token=ta)

    # --- wallet and withdrawal ---------------------------------------------------------------------
    db.run("INSERT INTO caregiver_wallets (id, caregiver_id, balance, created_at, updated_at)"
           " VALUES (%s,%s,2500000,now(),now()) ON CONFLICT (caregiver_id) DO UPDATE SET balance = 2500000",
           (str(uuid.uuid4()), a["id"]))
    rec.save("db-seeded-wallet-balance", {
        "request": {"method": "SQL", "path": "INSERT INTO caregiver_wallets ... balance = 2500000 (kobo)", "auth": None, "body": None},
        "response": {"status_code": None, "headers": {}, "body": None},
        "note": "SEEDED: nothing in the API credits a caregiver wallet yet, so the balance was inserted directly.",
    })
    rec.call("a-wallet", "GET", "/api/v1/caregiver/wallet", token=ta)
    rec.call("a-bank-account-not-set", "GET", "/api/v1/caregiver/bank-account", token=ta, expect=404)
    rec.call("a-withdraw-no-bank-account", "POST", "/api/v1/caregiver/wallet/withdraw", token=ta, expect=400,
             json_body={"amount": 200000, "narration": "Placeholder"})
    rec.call("a-bank-account-set", "PUT", "/api/v1/caregiver/bank-account", token=ta, json_body={
        "bank_name": "Placeholder Bank", "account_number": "0000000000", "account_name": "Placeholder Caregiver A"})
    rec.call("a-withdraw-below-minimum", "POST", "/api/v1/caregiver/wallet/withdraw", token=ta, expect=400,
             json_body={"amount": 5000, "narration": "Placeholder"})
    rec.call("a-withdraw-insufficient", "POST", "/api/v1/caregiver/wallet/withdraw", token=ta, expect=400,
             json_body={"amount": 99999999, "narration": "Placeholder"})
    rec.call("a-withdraw", "POST", "/api/v1/caregiver/wallet/withdraw", token=ta,
             json_body={"amount": 200000, "narration": "Placeholder"})
    rec.call("a-wallet-after-withdrawal", "GET", "/api/v1/caregiver/wallet", token=ta)
    rec.call("a-wallet-transactions", "GET", "/api/v1/caregiver/wallet/transactions", token=ta)
    rec.call("a-home-final", "GET", "/api/v1/caregiver/home", token=ta)
    rec.call("a-home-as-patient", "GET", "/api/v1/caregiver/home", token=tp, expect=403)
    rec.call("a-home-no-auth", "GET", "/api/v1/caregiver/home", expect=401)

    (out / "000-index.json").write_text(json.dumps({
        "generated_by": "scripts/capture_caregiver_flow.py", "run_tag": tag, "captures": rec.index}, indent=2) + "\n")
    expo.stop()
    print(f"wrote {len(rec.index)} captures to {out}")


def pay_booking_quiet(ctx: Ctx, booking: Dict[str, Any]) -> None:
    saved = ctx.rec.quiet
    ctx.rec.quiet = True
    pay_booking(ctx, booking, "quiet")
    ctx.rec.quiet = saved


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:5432/oakly")
    p.add_argument("--env-file", required=True, help="the env file the stack was started with (for the Paystack key)")
    p.add_argument("--out", default="docs/fe-captures/caregiver")
    p.add_argument("--expo-port", type=int, default=9999)
    run(p.parse_args())


if __name__ == "__main__":
    main()
