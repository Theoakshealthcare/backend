"""Capture `POST /api/v1/auth/change-password` against a RUNNING Oakly API and write every request and response to
docs/fe-captures/caregiver/change-password/*.json. The "Change password" section of docs/fe-integration-guide-caregiver.md
is copied from these files, never from the schemas.

    poetry run python -m scripts.capture_change_password --base-url http://127.0.0.1:8001

Every call is a real HTTP call. Nothing is seeded in the database: the caregiver is registered through the API (example.com
email, +2340000000xxx phone, placeholder password). The email is never verified (login does not need it). JWTs are replaced
by <ACCESS_TOKEN_n> / <REFRESH_TOKEN_n> in the files (the same n is the same token), passwords by <CURRENT_PASSWORD> and
<NEW_PASSWORD>. Not exercised here (tests only): the doubling of the wait after the second throttled failure, the wait
ending, the one-hour reset of the failure count.
"""

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

OUT = Path("docs/fe-captures/caregiver/change-password")
CURRENT = "Placeholder-Pass-1!"
NEW = "Placeholder-New-Pass-2!"
THIRD = "Placeholder-Third-Pass-3!"
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PASSWORDS = {CURRENT: "<CURRENT_PASSWORD>", NEW: "<NEW_PASSWORD>", THIRD: "<THIRD_PASSWORD>"}


class Recorder:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=30)
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
            return JWT.sub(lambda m: self.tokens.setdefault(m.group(0), f"<TOKEN_{len(self.tokens) + 1}>"), value)
        return value

    def call(self, label: str, method: str, path: str, *, token: str = None, json_body=None, note: str = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self.http.request(method, path, headers=headers, json=json_body)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        keep = {h: r.headers[h] for h in ("content-type", "retry-after", "cache-control") if h in r.headers}
        record = {
            "request": {"method": method, "path": path, "auth": "Bearer <ACCESS_TOKEN of the session named in the label>" if token else None,
                        "body": json_body},
            "response": {"status_code": r.status_code, "headers": keep, "body": body},
        }
        if note:
            record["note"] = note
        n = len(self.index) + 1
        name = f"{n:02d}-{label}.json"
        # Tokens in a response must be registered (in order of appearance) before the request's own are replaced.
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(json.dumps(self._scrub(record), indent=2, ensure_ascii=False) + "\n")
        self.index.append({"file": name, "label": label, "status": r.status_code})
        print(f"{name}  {r.status_code}", file=sys.stderr)
        return r


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = p.parse_args(argv)
    rec = Recorder(args.base_url)
    api = "/api/v1/auth"
    tag = uuid.uuid4().hex[:6]

    def register(name: str) -> str:
        email = f"cpw-{name}-{tag}@example.com"
        r = rec.http.post(f"{api}/register", json={
            "email": email, "password": CURRENT, "full_name": f"Placeholder Caregiver {name.upper()}",
            "phone_number": f"+23400000{int(tag, 16) % 100000:05d}{ord(name[0]) % 10}", "user_type": "caregiver"})
        r.raise_for_status()
        return email

    def login(email: str, password: str) -> dict:
        r = rec.http.post(f"{api}/login/mobile", json={"email_or_phone": email, "password": password})
        r.raise_for_status()
        return r.json()

    email = register("a")
    phone = login(email, CURRENT)  # the device that changes the password ("this device")
    time.sleep(1.1)  # tokens have no unique id: two logins in the same second would be the same token
    other = login(email, CURRENT)  # a second device that must be signed out
    assert phone["access_token"] != other["access_token"] and phone["refresh_token"] != other["refresh_token"]
    # Register the tokens so every later mention is the same placeholder, in a fixed order.
    for t in (phone["access_token"], phone["refresh_token"], other["access_token"], other["refresh_token"]):
        rec.tokens.setdefault(t, f"<TOKEN_{len(rec.tokens) + 1}>")

    rec.call("no-token", "POST", f"{api}/change-password", json_body={"current_password": CURRENT, "new_password": NEW})
    rec.call("missing-field", "POST", f"{api}/change-password", token=phone["access_token"],
             json_body={"current_password": CURRENT})
    rec.call("wrong-current", "POST", f"{api}/change-password", token=phone["access_token"],
             json_body={"current_password": THIRD, "new_password": NEW})
    rec.call("weak-too-short", "POST", f"{api}/change-password", token=phone["access_token"],
             json_body={"current_password": CURRENT, "new_password": "short1"})
    rec.call("weak-repetitive", "POST", f"{api}/change-password", token=phone["access_token"],
             json_body={"current_password": CURRENT, "new_password": "aaaaaaaabbbb"})
    rec.call("weak-too-long", "POST", f"{api}/change-password", token=phone["access_token"],
             json_body={"current_password": CURRENT, "new_password": "Ab1!" * 19})
    rec.call("same-password", "POST", f"{api}/change-password", token=phone["access_token"],
             json_body={"current_password": CURRENT, "new_password": CURRENT})
    ok = rec.call("success", "POST", f"{api}/change-password", token=phone["access_token"],
                  json_body={"current_password": CURRENT, "new_password": NEW})
    ok.raise_for_status()
    fresh = ok.json()
    for t in (fresh["access_token"], fresh["refresh_token"]):
        rec.tokens.setdefault(t, f"<TOKEN_{len(rec.tokens) + 1}>")

    rec.call("me-with-new-token", "GET", f"{api}/me", token=fresh["access_token"], note="THIS device, on the tokens the change returned")
    rec.call("refresh-with-new-token", "POST", f"{api}/refresh", json_body={"refresh_token": fresh["refresh_token"]},
             note="THIS device, refreshing on the returned refresh token")
    rec.call("me-with-old-token-this-device", "GET", f"{api}/me", token=phone["access_token"],
             note="THIS device's access token from before the change is dead too: the app must swap in the returned pair")
    rec.call("me-other-device", "GET", f"{api}/me", token=other["access_token"], note="the OTHER device's access token")
    rec.call("refresh-other-device", "POST", f"{api}/refresh", json_body={"refresh_token": other["refresh_token"]},
             note="the OTHER device's refresh token")
    rec.call("login-old-password", "POST", f"{api}/login/mobile", json_body={"email_or_phone": email, "password": CURRENT})
    rec.call("login-new-password", "POST", f"{api}/login/mobile", json_body={"email_or_phone": email, "password": NEW})

    # Throttle: a second user, so the first one's counts do not matter.
    email_b = register("b")
    b = login(email_b, CURRENT)
    rec.tokens.setdefault(b["access_token"], f"<TOKEN_{len(rec.tokens) + 1}>")
    rec.tokens.setdefault(b["refresh_token"], f"<TOKEN_{len(rec.tokens) + 1}>")
    for i in range(5):
        rec.call(f"throttle-wrong-{i + 1}", "POST", f"{api}/change-password", token=b["access_token"],
                 json_body={"current_password": THIRD, "new_password": NEW})
    rec.call("throttle-blocked-even-with-the-right-password", "POST", f"{api}/change-password", token=b["access_token"],
             json_body={"current_password": CURRENT, "new_password": NEW})

    (OUT / "00-index.json").write_text(json.dumps({"generated_by": "scripts/capture_change_password.py", "captures": rec.index}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
