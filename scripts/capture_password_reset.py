"""Capture the password-reset flow against a RUNNING Oakly API and write every request and response to
docs/fe-captures/password-reset/*.json. docs/fe-integration-guide-password-reset.md is copied from these files, never from
the schemas.

    # terminal 1: scratch Postgres at head, then the API with a recording mailbox (see capture_password_reset_server)
    poetry run python -m scripts.capture_password_reset_server --port 8022
    # terminal 2
    poetry run python -m scripts.capture_password_reset --base-url http://127.0.0.1:8022 --mailbox /tmp/mbox

Every call is a real HTTP call. Nothing is seeded: accounts are registered through the API (example.com emails,
placeholder passwords). The ONLY fake is the mail transport: the server writes each outgoing email to --mailbox and this
script reads the code from it, as a person would from an inbox. In the files the real code is replaced by <CODE>, JWTs by
<ACCESS_TOKEN_n> / <REFRESH_TOKEN_n> / <LEGACY_TOKEN>, passwords by <...PASSWORD> placeholders.

Not exercised here (tests only): the 15-minute expiry, the hourly window passing, the concurrent double-submit, the
5-attempt burst under load, the email arriving in a real inbox.
"""
import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

OUT = Path("docs/fe-captures/password-reset")
CURRENT, NEW, OTHER = "Placeholder-Pass-1!", "Placeholder-New-Pass-2!", "Placeholder-Other-Pass-3!"
PASSWORDS = {CURRENT: "<CURRENT_PASSWORD>", NEW: "<NEW_PASSWORD>", OTHER: "<OTHER_PASSWORD>"}
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
CODE_IN_MAIL = re.compile(r">\s*([0-9]{5})\s*</p>")


class Recorder:
    def __init__(self, base_url: str, mailbox: Path):
        self.http = httpx.Client(base_url=base_url, timeout=30)
        self.mailbox = mailbox
        self.secrets: dict[str, str] = {}  # real value -> placeholder (a code, a legacy token)
        self.tokens: dict[str, str] = {}
        self.index: list[dict] = []

    def _scrub(self, value):
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, str):
            if value in PASSWORDS:
                return PASSWORDS[value]
            if value in self.secrets:
                return self.secrets[value]
            return JWT.sub(lambda m: self.tokens.setdefault(m.group(0), f"<TOKEN_{len(self.tokens) + 1}>"), value)
        return value

    def _write(self, label: str, record: dict) -> None:
        n = len(self.index) + 1
        name = f"{n:02d}-{label}.json"
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(json.dumps(self._scrub(record), indent=2, ensure_ascii=False) + "\n")
        self.index.append({"file": name, "label": label, "status": record.get("response", {}).get("status_code")})
        print(name, record.get("response", {}).get("status_code", "observed"), file=sys.stderr)

    def call(self, label: str, method: str, path: str, *, token: str = None, json_body=None, note: str = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self.http.request(method, path, headers=headers, json=json_body)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        keep = {h: r.headers[h] for h in ("content-type", "retry-after", "cache-control") if h in r.headers}
        record = {
            "request": {"method": method, "path": path, "auth": "Bearer <ACCESS_TOKEN>" if token else None, "body": json_body},
            "response": {"status_code": r.status_code, "headers": keep, "body": body},
        }
        if note:
            record["note"] = note
        self._write(label, record)
        return r

    def observe(self, label: str, observed: dict, note: str) -> None:
        self._write(label, {"observed": observed, "note": note})

    def mails_for(self, email: str) -> list[str]:
        time.sleep(0.6)  # the app mails after it has replied (a background task): give it a moment
        """The password-reset emails sent to ``email`` (not the verification email registering sends)."""
        texts = [f.read_text() for f in sorted(self.mailbox.glob("*.eml"))]
        return [t for t in texts
                if t.startswith(f"X-Capture-Recipient: {email}\n") and "password reset code" in t.split("\n\n", 1)[0]]

    def latest_code(self, email: str) -> str:
        import email as email_lib

        raw = self.mails_for(email)[-1]
        msg = email_lib.message_from_string(raw.split("\n", 1)[1])
        html = msg.get_payload(0).get_payload(decode=True).decode()
        code = CODE_IN_MAIL.search(html).group(1)
        self.secrets[code] = "<CODE>"
        return code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8022")
    p.add_argument("--mailbox", type=Path, required=True)
    args = p.parse_args(argv)
    rec = Recorder(args.base_url, args.mailbox)
    import shutil

    shutil.rmtree(OUT, ignore_errors=True)
    api = "/api/v1/auth"
    tag = uuid.uuid4().hex[:6]
    RECOVER, RESET = f"{api}/password-recovery", f"{api}/reset-password"

    def register(name: str) -> str:
        email = f"pr-{name}-{tag}@example.com"
        r = rec.http.post(f"{api}/register", json={
            "email": email, "password": CURRENT, "full_name": f"Placeholder Person {name.upper()}",
            "phone_number": f"+23400000{int(tag, 16) % 100000:05d}{ord(name[0]) % 10}", "user_type": "patient"})
        r.raise_for_status()
        return email

    def login(email: str, password: str) -> dict:
        r = rec.http.post(f"{api}/login/mobile", json={"email_or_phone": email, "password": password})
        r.raise_for_status()
        return r.json()

    def not_the(code: str) -> str:
        return "00000" if code != "00000" else "11111"

    # ---- 1. the happy path, with a second device that must be signed out -------------------------------------
    email = register("a")
    device = login(email, CURRENT)
    for t in (device["access_token"], device["refresh_token"]):
        rec.tokens.setdefault(t, f"<TOKEN_{len(rec.tokens) + 1}>")
    rec.call("recovery-known-email", "POST", RECOVER, json_body={"email": email})
    rec.call("recovery-unknown-email", "POST", RECOVER, json_body={"email": f"nobody-{tag}@example.com"},
             note="Same status and body as the known address; no email is sent")
    code = rec.latest_code(email)
    rec.observe("mailbox-after-two-requests", {"emails_for_known_address": len(rec.mails_for(email)),
                                               "emails_for_unknown_address": len(rec.mails_for(f"nobody-{tag}@example.com"))},
                "Read from the recording mailbox (the one fake: the mail transport). The code is the 5 digits shown in the email.")
    rec.call("reset-wrong-code", "POST", RESET, json_body={"email": email, "code": not_the(code), "new_password": NEW})
    rec.call("reset-unknown-email", "POST", RESET, json_body={"email": f"nobody-{tag}@example.com", "code": not_the(code),
                                                              "new_password": NEW}, note="Identical to the wrong-code answer")
    rec.call("reset-malformed-code", "POST", RESET, json_body={"email": email, "code": "12ab", "new_password": NEW},
             note="A code that is not 5 digits gets the same 400 (and counts as a wrong try)")
    rec.call("reset-weak-password", "POST", RESET, json_body={"email": email, "code": code, "new_password": "short1"},
             note="The code is NOT used up and the try is not counted")
    rec.call("reset-missing-code", "POST", RESET, json_body={"email": email, "new_password": NEW})
    rec.call("reset-token-and-code", "POST", RESET, json_body={"token": "x", "email": email, "code": code, "new_password": NEW})
    rec.call("reset-missing-password", "POST", RESET, json_body={"email": email, "code": code})
    ok = rec.call("reset-success", "POST", RESET, json_body={"email": email, "code": code, "new_password": NEW})
    ok.raise_for_status()
    rec.call("reset-replay", "POST", RESET, json_body={"email": email, "code": code, "new_password": OTHER},
             note="The same code a second time")
    rec.call("me-with-old-access-token", "GET", f"{api}/me", token=device["access_token"],
             note="A device that was signed in before the reset is signed out")
    rec.call("refresh-with-old-refresh-token", "POST", f"{api}/refresh", json_body={"refresh_token": device["refresh_token"]})
    rec.call("login-old-password", "POST", f"{api}/login/mobile", json_body={"email_or_phone": email, "password": CURRENT})
    rec.call("login-new-password", "POST", f"{api}/login/mobile", json_body={"email_or_phone": email, "password": NEW})

    # ---- 2. lockout after five wrong codes -------------------------------------------------------------------
    email_b = register("b")
    rec.call("lockout-recovery", "POST", RECOVER, json_body={"email": email_b})
    code_b = rec.latest_code(email_b)
    for i in range(1, 6):
        rec.call(f"lockout-wrong-{i}", "POST", RESET, json_body={"email": email_b, "code": not_the(code_b), "new_password": NEW})
    rec.call("lockout-right-code-is-refused", "POST", RESET, json_body={"email": email_b, "code": code_b, "new_password": NEW},
             note="The fifth wrong try killed the code: even the right one is refused until a new code is requested")
    rec.call("lockout-new-code", "POST", RECOVER, json_body={"email": email_b})
    fresh_b = rec.latest_code(email_b)
    rec.call("lockout-reset-with-new-code", "POST", RESET, json_body={"email": email_b, "code": fresh_b, "new_password": NEW})

    # ---- 3. a new request replaces the previous code ------------------------------------------------------------
    email_c = register("c")
    rec.call("replace-first-request", "POST", RECOVER, json_body={"email": email_c})
    first = rec.latest_code(email_c)
    rec.call("replace-second-request", "POST", RECOVER, json_body={"email": email_c})
    second = rec.latest_code(email_c)
    if first == second:  # one in 100000: ask again so the demonstration stays honest
        rec.call("replace-third-request", "POST", RECOVER, json_body={"email": email_c})
        second = rec.latest_code(email_c)
    if first != second:
        rec.secrets[first] = "<FIRST_CODE>"
        rec.call("replace-old-code-refused", "POST", RESET, json_body={"email": email_c, "code": first, "new_password": NEW})
    rec.call("replace-new-code-works", "POST", RESET, json_body={"email": email_c, "code": second, "new_password": NEW})

    # ---- 4. request throttle: three emails an hour per address ---------------------------------------------------
    email_d = register("d")
    for i in range(1, 4):
        rec.call(f"throttle-request-{i}", "POST", RECOVER, json_body={"email": email_d})
    sent = len(rec.mails_for(email_d))
    rec.call("throttle-request-4", "POST", RECOVER, json_body={"email": email_d}, note="Same reply; no fourth email")
    rec.observe("throttle-mailbox", {"emails_for_address_after_3_requests": sent,
                                     "emails_for_address_after_4th_request": len(rec.mails_for(email_d))},
                "From the recording mailbox")

    # ---- 5. deprecated token form (shipped caregiver app) -----------------------------------------------------
    email_e = register("e")
    rec.call("legacy-recovery", "POST", RECOVER, json_body={"email": email_e})
    mail = rec.mails_for(email_e)[-1]
    token = JWT.search(mail).group(0) if JWT.search(mail) else None
    if token is None:  # the body is quoted-printable/base64 inside the MIME message
        import email as email_lib

        msg = email_lib.message_from_string(mail.split("\n", 1)[1])
        token = JWT.search(msg.get_payload(0).get_payload(decode=True).decode()).group(0)
    rec.secrets[token] = "<LEGACY_TOKEN>"
    rec.call("legacy-garbage-token", "POST", RESET, json_body={"token": "not-a-token", "new_password": NEW})
    rec.call("legacy-weak-password", "POST", RESET, json_body={"token": token, "new_password": "short1"})
    rec.call("legacy-success", "POST", RESET, json_body={"token": token, "new_password": NEW})
    rec.call("legacy-replay", "POST", RESET, json_body={"token": token, "new_password": OTHER})
    rec.call("legacy-login-new-password", "POST", f"{api}/login/mobile", json_body={"email_or_phone": email_e, "password": NEW})

    (OUT / "000-index.json").write_text(json.dumps(rec.index, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
