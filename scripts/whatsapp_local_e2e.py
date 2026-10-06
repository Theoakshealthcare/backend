"""Drive the running Docker stack through the WhatsApp channel, end to end, against a local fake Meta.

    poetry run python -m scripts.whatsapp_local_e2e --up      # start the stack, run, leave it up
    poetry run python -m scripts.whatsapp_local_e2e           # run against a stack that is already up
    poetry run python -m scripts.whatsapp_local_e2e --up --down   # start, run, then shut the stack down

What it does
- Starts a fake Meta Graph API on this machine (``app/tests/fake_meta.py``, the same fake the unit
  tests use) and points the containers at it through ``WHATSAPP_GRAPH_URL``.
- Posts Meta-shaped, correctly signed webhooks to the API, exactly as Meta would: bad signatures are
  refused; a first message gets the consent buttons; a replay is ignored; accepting consent gets the
  menu; an emergency message gets the emergency reply, an urgent ticket and a silenced bot.
- Reads the database back (loopback port 5432) to check consents, the ticket, and that no message
  text was stored anywhere.

W2 adds booking in chat, end to end, against a fake Paystack as well (``app/tests/fake_meta.py``, served on
its own local port, with the API pointed at it by ``PAYSTACK_BASE_URL``):
- consent -> menu -> Book a visit -> the booking Flow, played the way Meta plays it: signed, RSA/AES-GCM
  encrypted calls to ``/api/v1/whatsapp/flows/data`` using a throwaway keypair (the private half goes to the
  stack through ``WHATSAPP_FLOW_PRIVATE_KEY``) -> the completion message -> a location pin -> the itemised quote.
- A price change while the person decides does not change what is charged. Confirm creates the booking and the
  Pay now link; a signed Paystack ``charge.success`` webhook settles it, dispatch starts, and the receipt arrives.
- The unhappy and slower paths: a skipped pin (booking held for an address check), the 20 minute reminder and the
  30 minute cancellation (worker jobs, the cancellation as a template outside the 24 hour window), returning to the
  chat before the webhook (the bot verifies with Paystack), and a city outside Lagos and Abuja (waitlist).
- Nothing the person typed in the Flow (the condition) is in any message the bot sent, any table, or Redis.

``--up`` writes a throwaway env file with random secrets to the system temp dir and starts compose
with it (``OAKLY_ENV_FILE_PATH``). Nothing is read from or written to a real ``.env``.
Needs Docker and nothing else: no Meta account, no tunnel, no internet.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

# The app settings are read when app.tests.fake_meta is imported, so they need harmless values. The ones
# set here are remembered and kept out of the environment of `docker compose`: a placeholder DATABASE_URL
# would otherwise override the stack's own.
_PLACEHOLDERS = {
    "OAKLY_ENV_FILE": "",
    "DATABASE_URL": "postgresql://x:x@127.0.0.1:1/x",
    "SECRET_KEY": "e2e-local-only",
    "VITALS_ENCRYPTION_KEY": "0" * 43 + "=",
}
_SET_HERE = [key for key in _PLACEHOLDERS if key not in os.environ]
for _key in _SET_HERE:
    os.environ[_key] = _PLACEHOLDERS[_key]

import hashlib  # noqa: E402
import hmac  # noqa: E402

import httpx  # noqa: E402
import psycopg2  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from app.tests.fake_meta import (  # noqa: E402
    FakeMetaGraph, FakePaystack, FlowClient, body_bytes, button_payload, flow_reply_payload, flow_request,
    list_payload, location_payload, make_flow_keypair, media_payload, serve_fake_meta, serve_fake_paystack,
    signed_headers, text_payload,
)

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = Path(tempfile.gettempdir()) / "oakly-wa-e2e"
ENV_FILE = ENV_DIR / "stack.env"
PHONE_NUMBER_ID = "100000000000001"
WA_ID = "23480" + "".join(secrets.choice("0123456789") for _ in range(8))  # fresh placeholder number per run, so reruns never collide
WA_ID_CAPTION = "23481" + "".join(secrets.choice("0123456789") for _ in range(8))  # second and third contacts
WA_ID_PIDGIN = "23482" + "".join(secrets.choice("0123456789") for _ in range(8))
WA_ID_BOOK = "23483" + "".join(secrets.choice("0123456789") for _ in range(8))  # the booking contacts
WA_ID_EXPIRY = "23484" + "".join(secrets.choice("0123456789") for _ in range(8))
WA_ID_RETURN = "23485" + "".join(secrets.choice("0123456789") for _ in range(8))
WA_ID_FAR = "23486" + "".join(secrets.choice("0123456789") for _ in range(8))
CONDITION = "post-surgery wound dressing, SENTINEL-CONDITION-7731"  # must appear nowhere but encrypted on the booking
RUN = secrets.token_hex(4)  # makes the Meta message ids unique per run too (a reused id is a replay)
DB_DSN = "postgresql://oakly:oakly@127.0.0.1:5432/oakly"  # the compose db, loopback only


def load_env(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))


def write_env(fake_port: int, paystack_port: int) -> dict:
    from cryptography.fernet import Fernet

    ENV_DIR.mkdir(parents=True, exist_ok=True)
    private_pem, _ = make_flow_keypair()
    values = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "VITALS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ENVIRONMENT": "development",
        "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "WHATSAPP_ACCESS_TOKEN": "e2e-access-token-placeholder",
        "WHATSAPP_APP_SECRET": secrets.token_urlsafe(32),
        "WHATSAPP_VERIFY_TOKEN": secrets.token_urlsafe(16),
        "WHATSAPP_GRAPH_URL": f"http://host.docker.internal:{fake_port}",
        "WHATSAPP_EMERGENCY_NUMBERS": "112",
        "PAYSTACK_SECRET_KEY": "sk_test_" + secrets.token_hex(16),
        "PAYSTACK_BASE_URL": f"http://host.docker.internal:{paystack_port}",
        "WHATSAPP_BOOKING_FLOW_ID": "e2e-booking-flow",
        # one line, newlines written as backslash-n (config.py and flows/crypto.py accept that)
        "WHATSAPP_FLOW_PRIVATE_KEY": private_pem.strip().replace("\n", "\\n"),
    }
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    ENV_FILE.chmod(0o600)
    return values


def compose(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in _SET_HERE}
    env["OAKLY_ENV_FILE_PATH"] = str(ENV_FILE)
    subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, check=True)


def wait_for(description: str, check: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for: {description}")


class Run:
    def __init__(self, base_url: str, env: dict, graph: FakeMetaGraph, paystack: FakePaystack):
        self.base, self.env, self.graph, self.paystack = base_url.rstrip("/"), env, graph, paystack
        self.http = httpx.Client(timeout=15)
        self.results: list[tuple[str, bool, str]] = []
        private = serialization.load_pem_private_key(env["WHATSAPP_FLOW_PRIVATE_KEY"].replace("\\n", "\n").encode(), None)
        self.flow_public = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.flow_latencies: list[float] = []

    def post(self, payload: dict, *, sign_with: str | None = None, headers: dict | None = None) -> httpx.Response:
        body = body_bytes(payload)
        hdrs = headers if headers is not None else signed_headers(body, sign_with or self.env["WHATSAPP_APP_SECRET"])
        return self.http.post(f"{self.base}/api/v1/whatsapp/webhook", content=body, headers=hdrs)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print(f"  [{'ok' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail and not ok else ''}")
        if not ok:
            raise AssertionError(name)

    def next_reply(self, after: int, timeout: float = 30.0) -> dict:
        wait_for("an outbound message from the bot", lambda: len(self.graph.bodies) > after, timeout)
        return self.graph.bodies[after]

    def settle(self, seconds: float = 2.5) -> None:
        time.sleep(seconds)


def db_rows(sql: str) -> list:
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def redis_dump() -> str:
    """Every key in the stack's Redis with its full value, whatever the type (redis-cli picks up the password
    from REDISCLI_AUTH inside the container). Used to prove no message text lingers there."""
    script = ('for k in $(redis-cli --scan); do t=$(redis-cli type "$k"); echo "KEY $k $t"; case $t in '
              'string) redis-cli get "$k";; hash) redis-cli hgetall "$k";; list) redis-cli lrange "$k" 0 -1;; '
              'set) redis-cli smembers "$k";; zset) redis-cli zrange "$k" 0 -1 withscores;; esac; done')
    env = {k: v for k, v in os.environ.items() if k not in _SET_HERE}
    env["OAKLY_ENV_FILE_PATH"] = str(ENV_FILE)
    out = subprocess.run(["docker", "compose", "exec", "-T", "redis", "sh", "-c", script], cwd=ROOT, env=env,
                         check=True, capture_output=True, text=True)
    return out.stdout


def scenario(run: Run) -> None:
    env, graph = run.env, run.graph

    print("webhook handshake and signature")
    r = run.http.get(f"{run.base}/api/v1/whatsapp/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": env["WHATSAPP_VERIFY_TOKEN"], "hub.challenge": "424242"})
    run.check("verification handshake echoes the challenge", r.status_code == 200 and r.text == "424242")
    r = run.http.get(f"{run.base}/api/v1/whatsapp/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"})
    run.check("wrong verify token is refused", r.status_code == 403)
    hi = text_payload(WA_ID, "Hi", name="E2E Person", message_id=f"wamid.E2E.{RUN}.HI")
    run.check("missing signature is refused", run.post(hi, headers={}).status_code == 403)
    run.check("wrong signature is refused", run.post(hi, sign_with="not-the-secret").status_code == 403)
    run.settle(1)
    run.check("a refused webhook produces no reply", len(graph.bodies) == 0)

    print("first contact and consent")
    sent = len(graph.bodies)
    started = time.time()
    run.check("signed first message is accepted", run.post(hi).status_code == 200)
    reply = run.next_reply(sent)
    latency = time.time() - started
    run.check("bot replies to the right number", reply["to"] == WA_ID)
    buttons = [b["reply"]["id"] for b in reply["interactive"]["action"]["buttons"]]
    run.check("reply is the consent buttons", buttons == ["consent:accept", "consent:decline"], str(buttons))
    run.check("first reply arrives inside 5 seconds (WA-FR-1)", latency < 5, f"{latency:.1f}s")

    print("replay is ignored")
    sent = len(graph.bodies)
    run.check("replayed message id is acknowledged", run.post(hi).status_code == 200)
    run.settle()
    run.check("replay produced no second reply", len(graph.bodies) == sent)

    print("consent and menu")
    sent = len(graph.bodies)
    run.post(button_payload(WA_ID, "consent:accept", "I agree", name="E2E Person", message_id=f"wamid.E2E.{RUN}.ACCEPT"))
    reply = run.next_reply(sent)
    rows = [r["id"] for s in reply["interactive"]["action"]["sections"] for r in s["rows"]]
    run.check("accepting consent shows the menu", rows == ["menu:book", "menu:bookings", "menu:doctor",
                                                           "menu:human", "menu:help"], str(rows))
    consents = db_rows("SELECT c.type, c.decision, c.policy_version, c.evidence_ref FROM consents c "
                       f"JOIN whatsapp_contacts w ON w.id = c.contact_id WHERE w.wa_id = '{WA_ID}' ORDER BY c.type")
    run.check("two consent rows with policy version and message id as evidence",
              [(c[0], c[1]) for c in consents] == [("health_processing", "granted"), ("terms", "granted")]
              and all(c[2] and c[3] == f"wamid.E2E.{RUN}.ACCEPT" for c in consents), str(consents))
    users = db_rows(f"SELECT email, phone_number, user_type FROM users WHERE phone_number = '+{WA_ID}'")
    run.check("a phone-first patient user exists (no email)", users == [(None, "+" + WA_ID, "patient")], str(users))

    sent = len(graph.bodies)
    run.post(list_payload(WA_ID, "menu:help", "Help", name="E2E Person", message_id=f"wamid.E2E.{RUN}.HELP"))
    reply = run.next_reply(sent)
    run.check("Help answers with the menu again", reply["interactive"]["type"] == "list")

    print("emergency")
    sent = len(graph.bodies)
    started = time.time()
    run.post(text_payload(WA_ID, "my mother has chest pain and cannot wake up", name="E2E Person",
                          message_id=f"wamid.E2E.{RUN}.EMERGENCY"))
    reply = run.next_reply(sent)
    latency = time.time() - started
    body = reply["text"]["body"]
    run.check("emergency reply carries the emergency number", "112" in body)
    run.check("emergency reply does not echo the symptom", "chest" not in body.lower() and "mother" not in body.lower())
    run.check("emergency reply arrives inside 3 seconds (WA-NFR-1)", latency < 3, f"{latency:.1f}s")
    tickets = db_rows("SELECT s.reason, s.priority, s.status FROM support_conversations s "
                      f"JOIN whatsapp_contacts w ON w.id = s.contact_id WHERE w.wa_id = '{WA_ID}'")
    run.check("an urgent emergency ticket is open", tickets == [("emergency", "urgent", "open")], str(tickets))
    paused = db_rows("SELECT v.state, v.automation_paused FROM whatsapp_conversations v "
                     f"JOIN whatsapp_contacts w ON w.id = v.contact_id WHERE w.wa_id = '{WA_ID}'")
    run.check("automation is paused", paused == [("emergency", True)], str(paused))

    sent = len(graph.bodies)
    run.post(text_payload(WA_ID, "hello?", name="E2E Person", message_id=f"wamid.E2E.{RUN}.AFTER"))
    run.settle()
    run.check("the bot stays silent while a human owns the chat", len(graph.bodies) == sent)

    print("privacy")
    dump = ""
    for table in ("whatsapp_contacts", "whatsapp_conversations", "whatsapp_messages", "support_conversations",
                  "consents", "care_recipients"):
        dump += json.dumps(db_rows(f"SELECT t::text FROM {table} t"))  # the whole table, every contact
    run.check("no message text is in the database",
              not any(s in dump.lower() for s in ("chest pain", "mother", "wake up", "hello?")))
    outbound = db_rows("SELECT m.direction FROM whatsapp_messages m JOIN whatsapp_contacts w ON w.id = m.contact_id "
                       f"WHERE w.wa_id = '{WA_ID}' AND m.direction = 'outbound'")
    run.check("every bot reply is recorded as metadata", len(outbound) == len(graph.bodies), str(len(outbound)))


def more_emergencies(run: Run) -> None:
    env, graph = run.env, run.graph

    print("emergency in a photo caption and in Pidgin")
    sent = len(graph.bodies)
    photo = media_payload(WA_ID_CAPTION, "image", name="E2E Caption", message_id=f"wamid.E2E.{RUN}.CAPTION")
    photo["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = "my grandfather is unconscious"
    run.check("a captioned photo is accepted", run.post(photo).status_code == 200)
    reply = run.next_reply(sent)
    run.check("the caption alone triggers the emergency reply", reply["to"] == WA_ID_CAPTION
              and "112" in reply["text"]["body"] and "grandfather" not in reply["text"]["body"].lower())

    sent = len(graph.bodies)
    run.post(text_payload(WA_ID_PIDGIN, "i no wan live again", name="E2E Pidgin",
                          message_id=f"wamid.E2E.{RUN}.PIDGIN"))
    reply = run.next_reply(sent)
    run.check("a Pidgin self-harm phrase triggers the emergency reply",
              reply["to"] == WA_ID_PIDGIN and "112" in reply["text"]["body"])
    tickets = db_rows("SELECT s.reason, s.priority FROM support_conversations s JOIN whatsapp_contacts w "
                      f"ON w.id = s.contact_id WHERE w.wa_id IN ('{WA_ID_CAPTION}', '{WA_ID_PIDGIN}') ORDER BY 1")
    run.check("both opened an urgent emergency ticket", tickets == [("emergency", "urgent")] * 2, str(tickets))

    print("nothing the user typed is left in Redis")
    run.settle(3)
    dump = redis_dump()
    run.check("the Redis dump is readable (so an empty match means something)", "KEY " in dump)
    needles = ("chest pain", "mother", "wake up", "hello?", "unconscious", "grandfather", "no wan live",
               WA_ID, WA_ID_CAPTION, WA_ID_PIDGIN, "E2E Person", "E2E Pidgin", "E2E Caption")
    run.check("no message text, phone number or profile name in any Redis key or value",
              not any(n.lower() in dump.lower() for n in needles),
              str([n for n in needles if n.lower() in dump.lower()]))
    run.check("no leftover body keys or ARQ result records",
              "wa:inbound-body:" not in dump and "arq:result:" not in dump)


# --- W2: booking in chat -------------------------------------------------------------------------------
def sql_exec(sql: str) -> None:
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql)


def to_naira_text(body: dict) -> str:
    interactive = body.get("interactive") or {}
    return body.get("text", {}).get("body") or interactive.get("body", {}).get("text") or ""


def post_flow(run: Run, public_pem: str, payload: dict, *, sign_with: str | None = None) -> tuple[int, dict | None]:
    """One Flow data-endpoint call, signed and encrypted the way Meta does it."""
    crypto = FlowClient(public_pem)
    raw = body_bytes(crypto.encrypt(payload))
    started = time.time()
    r = run.http.post(f"{run.base}/api/v1/whatsapp/flows/data", content=raw,
                      headers=signed_headers(raw, sign_with or run.env["WHATSAPP_APP_SECRET"]))
    run.flow_latencies.append(time.time() - started)
    return r.status_code, (crypto.decrypt_response(r.text) if r.status_code == 200 else None)


def walk_flow(run: Run, token: str, *, city: str = "Lagos", service: str = "nurse", hours: int = 4) -> dict:
    """Every screen of the booking Flow, in order. Returns the last response (SUCCESS)."""
    day = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()

    def call(action, screen=None, data=None):
        status, body = post_flow(run, run.flow_public, flow_request(action, screen, data, token))
        run.check(f"flow {action} {screen or ''} answered 200".strip(), status == 200, f"{status}")
        return body

    run.check("INIT opens the first screen", call("INIT")["screen"] == "WHO")
    run.check("choosing 'myself' moves on to the service screen", call("data_exchange", "WHO", {"who": "self"})["screen"] == "SERVICE")
    services = call("data_exchange", "SERVICE", {"service": service})
    run.check("choosing a priced service moves on to date and time", services["screen"] == "WHEN")
    run.check("the calendar and time slots come from the endpoint", services["data"]["slots"] and services["data"]["max_date"])
    run.check("the time is accepted", call("data_exchange", "WHEN", {"trigger": "submit", "date": day, "time": "10:00"})["screen"] == "DETAILS")
    run.check("duration and the condition are accepted", call("data_exchange", "DETAILS", {
        "duration": str(hours), "condition": CONDITION, "instructions": ""})["screen"] == "ADDRESS")
    return call("data_exchange", "ADDRESS", {"street": "12 E2E Close", "area": "Lekki Phase 1", "city": city})


def flow_token_in(graph: FakeMetaGraph, after: int, wa_id: str) -> str:
    for body in graph.bodies[after:]:
        if body.get("to") == wa_id and (body.get("interactive") or {}).get("type") == "flow":
            return body["interactive"]["action"]["parameters"]["flow_token"]
    raise AssertionError("the bot did not send the booking Flow")


def onboard_and_open_flow(run: Run, wa_id: str, tag: str) -> str:
    """Hi -> I agree -> Book a visit. Returns the flow token from the Flow message the bot sent."""
    graph = run.graph
    name = f"E2E {tag}"
    sent = len(graph.bodies)
    run.post(text_payload(wa_id, "Hi", name=name, message_id=f"wamid.E2E.{RUN}.{tag}.HI"))
    run.next_reply(sent)
    sent = len(graph.bodies)
    run.post(button_payload(wa_id, "consent:accept", "I agree", name=name, message_id=f"wamid.E2E.{RUN}.{tag}.OK"))
    run.next_reply(sent)
    sent = len(graph.bodies)
    run.post(list_payload(wa_id, "menu:book", "Book a visit", name=name, message_id=f"wamid.E2E.{RUN}.{tag}.BOOK"))
    reply = run.next_reply(sent)
    parameters = reply["interactive"]["action"]["parameters"]
    run.check(f"[{tag}] Book a visit sends the booking Flow", reply["interactive"]["type"] == "flow"
              and parameters["flow_id"] == "e2e-booking-flow" and parameters["flow_action"] == "data_exchange")
    return parameters["flow_token"]


def finish_flow_and_pin(run: Run, wa_id: str, tag: str, token: str, *, pin: bool = True) -> dict:
    graph = run.graph
    sent = len(graph.bodies)
    run.post(flow_reply_payload(wa_id, token, message_id=f"wamid.E2E.{RUN}.{tag}.FLOW"))
    reply = run.next_reply(sent)
    run.check(f"[{tag}] the finished Flow is answered with a location request",
              reply["interactive"]["type"] == "location_request_message")
    sent = len(graph.bodies)
    if pin:
        run.post(location_payload(wa_id, 6.4474, 3.4723, message_id=f"wamid.E2E.{RUN}.{tag}.PIN"))
    else:
        run.post(text_payload(wa_id, "skip", message_id=f"wamid.E2E.{RUN}.{tag}.SKIP"))
    return run.next_reply(sent)


def book_until_quote(run: Run, wa_id: str, tag: str, *, pin: bool = True) -> dict:
    token = onboard_and_open_flow(run, wa_id, tag)
    done = walk_flow(run, token)
    run.check(f"[{tag}] the Flow ends with SUCCESS and only the token", done["screen"] == "SUCCESS" and done["data"][
        "extension_message_response"]["params"] == {"flow_token": token})
    return finish_flow_and_pin(run, wa_id, tag, token, pin=pin)


def confirm(run: Run, wa_id: str, tag: str) -> dict:
    sent = len(run.graph.bodies)
    run.post(button_payload(wa_id, "quote:confirm", "Confirm & pay", message_id=f"wamid.E2E.{RUN}.{tag}.CONFIRM"))
    reply = run.next_reply(sent)
    run.check(f"[{tag}] Confirm & pay answers with a Pay now link", reply["interactive"]["type"] == "cta_url"
              and reply["interactive"]["action"]["parameters"]["display_text"] == "Pay now"
              and reply["interactive"]["action"]["parameters"]["url"].startswith("https://checkout.paystack.com/"))
    return reply


def booking_row(wa_id: str, tag: str = "") -> tuple:
    rows = db_rows("SELECT b.id, b.booking_code, b.channel, b.status, b.payment_status, b.total_amount_kobo, b.condition, "
                   "b.condition_encrypted, b.needs_address_check, b.dispatch_state, b.dispatch_started_at, "
                   "b.paystack_reference, b.booked_by_user_id, b.care_recipient_id FROM bookings b "
                   "JOIN users u ON u.id = b.booked_by_user_id "
                   f"WHERE u.phone_number = '+{wa_id}' ORDER BY b.created_at DESC")
    assert rows, f"no booking for {wa_id}"
    return rows[0]


def paystack_webhook(run: Run, reference: str, amount_kobo: int) -> int:
    body = json.dumps({"event": "charge.success", "data": {
        "reference": reference, "amount": amount_kobo, "status": "success", "customer": {"email": "x@y.z"}}}).encode()
    signature = hmac.new(run.env["PAYSTACK_SECRET_KEY"].encode(), body, hashlib.sha512).hexdigest()
    return run.http.post(f"{run.base}/api/v1/payment/paystack/webhook", content=body,
                         headers={"x-paystack-signature": signature, "Content-Type": "application/json"}).status_code


def booking_scenarios(run: Run) -> None:
    graph = run.graph

    print("booking in chat: prices")
    sql_exec("DELETE FROM booking_settings")
    sql_exec("INSERT INTO booking_settings (id, base_fee_kobo, platform_fee_percent, multipliers, surge_pricing, "
             "surge_multiplier, discount, created_at, updated_at) VALUES (gen_random_uuid(), 1000000, 10, "
             "'{\"nurse\": 1.0, \"caregiver\": 0.8, \"physiotherapist\": 1.2, \"doctor\": 2.0}', false, 1.0, 0, now(), now())")

    print("booking in chat: the Flow endpoint refuses what it should")
    ping = FlowClient(run.flow_public)
    raw = body_bytes(ping.encrypt({"version": "3.0", "action": "ping"}))
    r = run.http.post(f"{run.base}/api/v1/whatsapp/flows/data", content=raw, headers=signed_headers(raw, run.env["WHATSAPP_APP_SECRET"]))
    run.check("health check answers encrypted and decrypts to active", r.status_code == 200
              and ping.decrypt_response(r.text) == {"version": "3.0", "data": {"status": "active"}})
    r = run.http.post(f"{run.base}/api/v1/whatsapp/flows/data", content=raw, headers=signed_headers(raw, "not-the-secret"))
    run.check("a wrong signature is refused with 432", r.status_code == 432)
    tampered = json.loads(raw)
    tampered["encrypted_flow_data"] = tampered["encrypted_flow_data"][:-4] + "AAAA"
    raw2 = body_bytes(tampered)
    r = run.http.post(f"{run.base}/api/v1/whatsapp/flows/data", content=raw2, headers=signed_headers(raw2, run.env["WHATSAPP_APP_SECRET"]))
    run.check("a tampered ciphertext is refused with 421", r.status_code == 421)

    print("booking A: the happy path with a price change while deciding")
    quote = book_until_quote(run, WA_ID_BOOK, "A")
    body = to_naira_text(quote)
    run.check("the quote is itemised in naira with the cancellation line", all(t in body for t in (
        "Service fee", "₦40,000", "Platform fee", "₦4,000", "Total", "₦44,000", "Free cancellation until 12 hours")), body)
    run.check("the quote offers Confirm & pay, Change and Cancel", [b["reply"]["title"] for b in quote["interactive"]["action"][
        "buttons"]] == ["Confirm & pay", "Change", "Cancel"])
    sql_exec("UPDATE booking_settings SET base_fee_kobo = 2000000")  # the admin doubles the rate while the person decides
    pay = confirm(run, WA_ID_BOOK, "A")
    row = booking_row(WA_ID_BOOK)
    run.check("the booking is a WhatsApp booking, pending and unpaid", (row[2], row[3].lower(), row[4].lower()) == ("whatsapp", "pending", "pending"), str(row[:5]))
    run.check("it was created at the locked price, not the new one", row[5] == 4_400_000, str(row[5]))
    run.check("Paystack was asked for the locked kobo with the internal email", len(run.paystack.initialised) == 1
              and run.paystack.initialised[0]["amount"] == 4_400_000
              and run.paystack.initialised[0]["email"].endswith("@pay.oaklycare.com")
              and WA_ID_BOOK not in run.paystack.initialised[0]["email"])
    run.check("the condition is encrypted on the booking and the plain column is empty",
              row[6] is None and row[7] and "SENTINEL" not in row[7])
    sql_exec("UPDATE booking_settings SET base_fee_kobo = 1000000")  # back to the original rate for the later bookings
    sent = len(graph.bodies)
    run.post(button_payload(WA_ID_BOOK, "quote:confirm", "Confirm & pay", message_id=f"wamid.E2E.{RUN}.A.CONFIRM2"))
    run.next_reply(sent)
    run.check("a second Confirm tap makes no second booking and no second Paystack transaction",
              len(db_rows(f"SELECT 1 FROM bookings WHERE id = '{row[0]}' OR booked_by_user_id = '{row[12]}'")) == 1
              and len(run.paystack.initialised) == 1)
    run.check("a wrong Paystack signature is refused", run.http.post(
        f"{run.base}/api/v1/payment/paystack/webhook", content=b"{}", headers={"x-paystack-signature": "bad"}).status_code == 400)
    sent = len(graph.bodies)
    run.check("the signed charge.success webhook is accepted", paystack_webhook(run, row[11], 4_400_000) == 200)
    wait_for("dispatch to start", lambda: booking_row(WA_ID_BOOK)[10] is not None, 30)
    paid = booking_row(WA_ID_BOOK)
    run.check("the booking is paid and dispatch has started", (paid[4].lower(), paid[9]) == ("paid", "dispatching"), str(paid[4:11]))
    receipt = run.next_reply(sent, timeout=45)  # the worker's events job runs every 15 seconds
    run.check("the receipt arrives: payment of ₦44,000 for the booking code",
              to_naira_text(receipt) == f"Payment of ₦44,000 received for booking {paid[1]}. We're assigning a professional now.",
              to_naira_text(receipt))
    run.settle(1)
    run.check("the person is told once", sum(1 for b in graph.bodies[sent:] if "Payment of" in to_naira_text(b)) == 1)
    state = db_rows("SELECT v.state FROM whatsapp_conversations v JOIN whatsapp_contacts w ON w.id = v.contact_id "
                    f"WHERE w.wa_id = '{WA_ID_BOOK}'")
    run.check("the conversation is idle again", state == [("idle",)], str(state))

    print("booking B: no pin, the 20 minute reminder, the 30 minute cancellation as a template")
    quote = book_until_quote(run, WA_ID_EXPIRY, "B", pin=False)
    run.check("skipping the pin still gets a quote", quote["interactive"]["type"] == "button")
    confirm(run, WA_ID_EXPIRY, "B")
    held = booking_row(WA_ID_EXPIRY)
    run.check("the booking is flagged for an address check", held[8] is True)
    sql_exec(f"UPDATE bookings SET payment_expires_at = now() + interval '9 minutes' WHERE id = '{held[0]}'")
    sent = len(graph.bodies)
    reminder = run.next_reply(sent, timeout=45)
    run.check("the reminder arrives after 20 minutes with a Pay now button",
              to_naira_text(reminder) == f"Your payment link for booking {held[1]} expires in 10 minutes."
              and reminder["interactive"]["action"]["parameters"]["display_text"] == "Pay now", to_naira_text(reminder))
    sql_exec("UPDATE whatsapp_contacts SET last_inbound_at = now() - interval '26 hours' "
             f"WHERE wa_id = '{WA_ID_EXPIRY}'")  # now outside the 24 hour window
    sql_exec(f"UPDATE bookings SET payment_expires_at = now() - interval '1 minute' WHERE id = '{held[0]}'")
    sent = len(graph.bodies)
    notice = run.next_reply(sent, timeout=120)  # the expiry job runs at the top of the minute, then the events job
    run.check("at 30 minutes the booking is cancelled for non-payment",
              booking_row(WA_ID_EXPIRY)[3].lower() == "cancelled", str(booking_row(WA_ID_EXPIRY)[3]))
    run.check("outside the window the notice is the booking_cancelled_unpaid template with a Book again button",
              notice["type"] == "template" and notice["template"]["name"] == "booking_cancelled_unpaid"
              and notice["template"]["components"][1]["parameters"] == [{"type": "payload", "payload": "menu:book"}], str(notice))
    reason = db_rows(f"SELECT cancel_reason FROM bookings WHERE id = '{held[0]}'")
    run.check("the cancel reason is unpaid_timeout", reason == [("unpaid_timeout",)], str(reason))

    print("booking C: back in the chat before the webhook")
    book_until_quote(run, WA_ID_RETURN, "C")
    confirm(run, WA_ID_RETURN, "C")
    returned = booking_row(WA_ID_RETURN)
    sent = len(graph.bodies)
    run.post(text_payload(WA_ID_RETURN, "did it work?", message_id=f"wamid.E2E.{RUN}.C.BACK"))
    waiting = run.next_reply(sent)
    run.check("unpaid: the bot verifies with Paystack and says it has not arrived, with the link again",
              returned[11] in run.paystack.verified and waiting["interactive"]["type"] == "cta_url"
              and "haven't received" in to_naira_text(waiting))
    run.paystack.paid.add(returned[11])  # the customer paid; our webhook has not arrived
    sent = len(graph.bodies)
    run.post(text_payload(WA_ID_RETURN, "I have paid", message_id=f"wamid.E2E.{RUN}.C.PAID"))
    done = run.next_reply(sent)
    run.check("paid: the receipt comes straight away", "Payment of ₦44,000 received" in to_naira_text(done), to_naira_text(done))
    after = booking_row(WA_ID_RETURN)
    run.check("settled through verification, dispatch started", (after[4].lower(), after[9]) == ("paid", "dispatching"), str(after[4:11]))

    print("booking D: outside Lagos and Abuja")
    token = onboard_and_open_flow(run, WA_ID_FAR, "D")
    run.check("the Flow still completes", walk_flow(run, token, city="other")["screen"] == "SUCCESS")
    sent = len(graph.bodies)
    run.post(flow_reply_payload(WA_ID_FAR, token, message_id=f"wamid.E2E.{RUN}.D.FLOW"))
    offer = run.next_reply(sent)
    run.check("the bot offers the waitlist", [b["reply"]["id"] for b in offer["interactive"]["action"]["buttons"]] == [
        "waitlist:yes", "waitlist:no"])
    sent = len(graph.bodies)
    run.post(button_payload(WA_ID_FAR, "waitlist:yes", "Yes, add me", message_id=f"wamid.E2E.{RUN}.D.YES"))
    run.next_reply(sent)
    wait = db_rows("SELECT q.status, q.sensitive_encrypted IS NULL, q.payload::text FROM quotes q JOIN whatsapp_contacts w "
                   f"ON w.id = q.contact_id WHERE w.wa_id = '{WA_ID_FAR}'")
    run.check("the waitlist row keeps no address and no condition", wait[0][0] == "waitlist" and wait[0][1] is True
              and "E2E Close" not in wait[0][2], str(wait))
    run.check("no booking was made", db_rows("SELECT count(*) FROM bookings b JOIN users u ON u.id = b.booked_by_user_id "
                                              f"WHERE u.phone_number = '+{WA_ID_FAR}'") == [(0,)])

    print("booking in chat: the Flow endpoint is fast and nothing private leaks")
    ordered = sorted(run.flow_latencies)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    run.check(f"the Flow data endpoint p95 is under 3 seconds ({len(ordered)} calls)", p95 < 3, f"p95 {p95:.2f}s")
    spoken = json.dumps(graph.bodies)
    run.check("the condition is in no message the bot sent", "SENTINEL" not in spoken and "wound" not in spoken.lower())
    dump = ""
    for table in ("whatsapp_contacts", "whatsapp_conversations", "whatsapp_messages", "support_conversations",
                  "consents", "care_recipients", "quotes", "booking_events"):
        dump += json.dumps(db_rows(f"SELECT t::text FROM {table} t"))
    run.check("the condition is in no WhatsApp table", "SENTINEL" not in dump)
    all_bookings = json.dumps(db_rows("SELECT b::text FROM bookings b"))
    run.check("nor in plain text on any booking", "SENTINEL" not in all_bookings)
    redis = redis_dump()
    run.check("nor in Redis (no condition, and no pin left behind)", "SENTINEL" not in redis and "3.4723" not in redis)


def wait_for_api(base: str, timeout: float = 240.0) -> None:
    def up() -> bool:
        try:
            return httpx.get(f"{base}/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    wait_for("the API to answer /health", up, timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--up", action="store_true", help="build and start the Docker stack first")
    parser.add_argument("--down", action="store_true", help="shut the stack down afterwards (docker compose down -v)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--fake-port", type=int, default=9911, help="port the fake Meta listens on")
    parser.add_argument("--paystack-port", type=int, default=9912, help="port the fake Paystack listens on")
    args = parser.parse_args()

    if args.up:
        env = write_env(args.fake_port, args.paystack_port)
    elif ENV_FILE.exists():
        env = load_env(ENV_FILE)
    else:
        print(f"No stack env at {ENV_FILE}. Run with --up first.", file=sys.stderr)
        return 2

    graph = FakeMetaGraph()
    server = serve_fake_meta(graph, "0.0.0.0", args.fake_port)
    paystack = FakePaystack()
    paystack_server = serve_fake_paystack(paystack, "0.0.0.0", args.paystack_port)
    print(f"fake Meta listening on :{args.fake_port}, fake Paystack on :{args.paystack_port}")
    code = 1
    try:
        if args.up:
            try:
                compose("up", "-d", "--build")
            except subprocess.CalledProcessError:
                print("stack start failed once; the API log follows, then one retry")
                compose("logs", "--no-color", "--tail", "40", "api")
                compose("up", "-d")
        wait_for_api(args.base_url)
        run = Run(args.base_url, env, graph, paystack)
        scenario(run)
        booking_scenarios(run)
        more_emergencies(run)
        print(f"\nALL {len(run.results)} CHECKS PASSED")
        code = 0
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
    finally:
        server.shutdown()
        paystack_server.shutdown()
        if args.down:
            compose("down", "-v")
    return code


if __name__ == "__main__":
    sys.exit(main())
