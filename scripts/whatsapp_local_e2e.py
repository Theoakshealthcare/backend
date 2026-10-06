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

import httpx  # noqa: E402
import psycopg2  # noqa: E402

from app.tests.fake_meta import (  # noqa: E402
    FakeMetaGraph, body_bytes, button_payload, list_payload, media_payload, serve_fake_meta, signed_headers,
    text_payload,
)

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = Path(tempfile.gettempdir()) / "oakly-wa-e2e"
ENV_FILE = ENV_DIR / "stack.env"
PHONE_NUMBER_ID = "100000000000001"
WA_ID = "23480" + "".join(secrets.choice("0123456789") for _ in range(8))  # fresh placeholder number per run, so reruns never collide
WA_ID_CAPTION = "23481" + "".join(secrets.choice("0123456789") for _ in range(8))  # second and third contacts
WA_ID_PIDGIN = "23482" + "".join(secrets.choice("0123456789") for _ in range(8))
RUN = secrets.token_hex(4)  # makes the Meta message ids unique per run too (a reused id is a replay)
DB_DSN = "postgresql://oakly:oakly@127.0.0.1:5432/oakly"  # the compose db, loopback only


def load_env(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))


def write_env(fake_port: int) -> dict:
    from cryptography.fernet import Fernet

    ENV_DIR.mkdir(parents=True, exist_ok=True)
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
    def __init__(self, base_url: str, env: dict, graph: FakeMetaGraph):
        self.base, self.env, self.graph = base_url.rstrip("/"), env, graph
        self.http = httpx.Client(timeout=15)
        self.results: list[tuple[str, bool, str]] = []

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
    args = parser.parse_args()

    if args.up:
        env = write_env(args.fake_port)
    elif ENV_FILE.exists():
        env = load_env(ENV_FILE)
    else:
        print(f"No stack env at {ENV_FILE}. Run with --up first.", file=sys.stderr)
        return 2

    graph = FakeMetaGraph()
    server = serve_fake_meta(graph, "0.0.0.0", args.fake_port)
    print(f"fake Meta listening on :{args.fake_port}")
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
        run = Run(args.base_url, env, graph)
        scenario(run)
        more_emergencies(run)
        print(f"\nALL {len(run.results)} CHECKS PASSED")
        code = 0
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
    finally:
        server.shutdown()
        if args.down:
            compose("down", "-v")
    return code


if __name__ == "__main__":
    sys.exit(main())
