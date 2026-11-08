"""Smoke test for a freshly deployed staging API. Standard library only: run it from your laptop, no install.

    python3 -m scripts.staging_smoke https://api.<domain> --email you@your-real-mailbox.com

What it checks, in order (each line is PASS / FAIL / SKIP; the exit code is 1 if anything FAILED):
  1. GET /health through nginx and TLS, and the OpenAPI document
  2. both webhooks are reachable from the internet and refuse an unsigned call (Paystack 400, Dojah 401)
  3. pricing exists: POST /booking/preview is 200 (503 booking_settings_not_configured = the one-time pricing row is missing)
  4. sign-up: registers a throwaway patient with --email, then waits for YOU to read the 5-digit code from that mailbox
     (type it at the prompt, or pass --code). This proves SMTP: without it the code is only written to the API log
  5. login, then a Paystack link: POST /patient/wallet/deposit returns a checkout.paystack.com URL; pay it with a
     Paystack TEST card, add --wait-payment 180 and the script waits for the webhook to credit the wallet
  6. push token round trip: register a (fake) Expo token, delete it. With --expo-token ExponentPushToken[...] from a
     real phone it also sends a push straight through Expo and reads the receipt (this tests the Expo + Firebase
     credential chain, not the backend)

Without --email nothing that needs an account runs (steps 4-6 are SKIP): the script never invents addresses, because
a bounce from a fake domain would hurt the mail provider's view of the sender. To reuse an account instead of
registering: --login-email you@x.com with the password in the SMOKE_PASSWORD environment variable.

It prints no token, password or code. The throwaway account stays in the staging database (it is staging).
"""

import argparse
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

USER_AGENT = "oakly-staging-smoke/1"  # the default python-urllib agent is refused by some CDNs
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"


class Smoke:
    def __init__(self, base: str, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.results: List[Tuple[str, str, str]] = []
        self.token: Optional[str] = None

    # -- plumbing -----------------------------------------------------------------------------------------------
    def record(self, status: str, name: str, detail: str = "") -> bool:
        self.results.append((status, name, detail))
        print(f"{status:<5} {name}" + (f"  ({detail})" if detail else ""), flush=True)
        return status == PASS

    def call(self, method: str, path: str, body: Any = None, auth: bool = False,
             url: Optional[str] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        """One HTTP call. Returns (status, parsed JSON or raw text); status 0 means the call never completed."""
        data = None
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if auth and self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        hdrs.update(headers or {})
        request = urllib.request.Request(url or self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=ssl.create_default_context()) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as err:
            raw, status = err.read(), err.code
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return 0, str(getattr(err, "reason", err))
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")[:200]

    # -- checks -------------------------------------------------------------------------------------------------
    def check_health(self) -> bool:
        status, body = self.call("GET", "/health")
        if status == 0:
            return self.record(FAIL, "GET /health", f"no answer: {body}. DNS, firewall, nginx or TLS?")
        if status == 200 and isinstance(body, dict) and body.get("status") == "healthy":
            self.record(PASS, "GET /health", "200 healthy" + ("" if self.base.startswith("https://") else ", but NOT https"))
        else:
            return self.record(FAIL, "GET /health", f"HTTP {status}: {str(body)[:120]}")
        status, body = self.call("GET", "/api/v1/openapi.json")
        if status == 200 and isinstance(body, dict) and body.get("paths"):
            self.record(PASS, "OpenAPI document", f"{len(body['paths'])} paths")
        else:
            self.record(FAIL, "OpenAPI document", f"HTTP {status}")
        return True

    def check_webhooks(self) -> None:
        status, body = self.call("POST", "/api/v1/payment/paystack/webhook", body={"event": "smoke"},
                                 headers={"x-paystack-signature": "0" * 128})
        ok = status == 400 and isinstance(body, dict) and body.get("detail") == "Invalid webhook signature"
        self.record(PASS if ok else FAIL, "Paystack webhook reachable and signed-only", f"HTTP {status}, want 400 Invalid webhook signature")
        status, body = self.call("POST", "/api/v1/kyc/dojah/webhook", body={"event": "smoke"},
                                 headers={"x-dojah-signature": "0" * 64})
        ok = status == 401 and isinstance(body, dict) and body.get("detail") == "Invalid signature"
        self.record(PASS if ok else FAIL, "Dojah webhook reachable and signed-only", f"HTTP {status}, want 401 Invalid signature")

    def check_pricing(self) -> None:
        status, body = self.call("POST", "/api/v1/booking/preview", body={"service_type": "nurse", "duration_hours": 3})
        code = body.get("detail", {}).get("code") if isinstance(body, dict) and isinstance(body.get("detail"), dict) else None
        if status == 200:
            self.record(PASS, "Pricing row exists (booking preview)", "200")
        elif status == 503 and code == "booking_settings_not_configured":
            self.record(FAIL, "Pricing row exists (booking preview)",
                        "503 booking_settings_not_configured: do first-run step 'create the pricing row'")
        else:
            self.record(FAIL, "Pricing row exists (booking preview)", f"HTTP {status}: {str(body)[:120]}")

    def sign_up(self, email: str, code: Optional[str]) -> Optional[str]:
        """Register a throwaway patient and verify the emailed code. Returns the password on success."""
        password = os.environ.get("SMOKE_PASSWORD") or f"Smoke-{secrets.token_hex(6)}-Aa1!"
        phone = "+23400" + "".join(secrets.choice("0123456789") for _ in range(8))
        status, body = self.call("POST", "/api/v1/auth/register", body={
            "email": email, "password": password, "full_name": "Smoke Test Patient", "phone_number": phone, "user_type": "patient"})
        if status != 200:
            detail = body.get("detail") if isinstance(body, dict) else body
            self.record(FAIL, "Sign-up (POST /auth/register)", f"HTTP {status}: {str(detail)[:120]}")
            return None
        self.record(PASS, "Sign-up (POST /auth/register)", "200, user created")
        if code is None and sys.stdin.isatty():
            code = input(f"   Check the inbox of {email} (and spam). Enter the 5-digit code, or press Enter to give up: ").strip() or None
        if code is None:
            self.record(FAIL, "Sign-up code arrived by email", "no code entered: SMTP is not delivering (see `logs api | grep -i smtp`)")
            return password
        status, body = self.call("POST", "/api/v1/auth/verify-email", body={"email": email, "code": code})
        self.record(PASS if status == 200 else FAIL, "Sign-up code arrived by email and verifies",
                    "200 Email verified successfully" if status == 200 else f"HTTP {status}: {str(body)[:100]}")
        return password

    def login(self, email: str, password: str) -> bool:
        status, body = self.call("POST", "/api/v1/auth/login/mobile", body={"email_or_phone": email, "password": password})
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            self.token = body["access_token"]
            return self.record(PASS, "Login (POST /auth/login/mobile)", "200, token pair")
        return self.record(FAIL, "Login (POST /auth/login/mobile)", f"HTTP {status}: {str(body)[:100]}")

    def check_paystack_link(self, wait_seconds: int) -> None:
        amount = 100  # naira: MIN_DEPOSIT_AMOUNT
        status, body = self.call("POST", "/api/v1/patient/wallet/deposit", body={"amount": amount}, auth=True)
        url = body.get("authorization_url") if isinstance(body, dict) else None
        if status != 200 or not isinstance(url, str) or not url.startswith("https://checkout.paystack.com/"):
            self.record(FAIL, "Paystack payment link (POST /patient/wallet/deposit)", f"HTTP {status}: {str(body)[:140]}")
            return
        self.record(PASS, "Paystack payment link (POST /patient/wallet/deposit)", "200, checkout.paystack.com URL")
        print(f"      Pay it with a Paystack TEST card (Paystack docs list them): {url}", flush=True)
        if wait_seconds <= 0:
            self.record(SKIP, "Webhook credits the wallet", "pass --wait-payment 180 and pay the link to check it")
            return
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            wstatus, wallet = self.call("GET", "/api/v1/patient/wallet", auth=True)
            if wstatus == 200 and isinstance(wallet, dict) and float(wallet.get("balance", 0)) >= amount:
                self.record(PASS, "Webhook credits the wallet", f"balance {wallet['balance']} NGN: the Paystack webhook URL works")
                return
            time.sleep(5)
        self.record(FAIL, "Webhook credits the wallet", f"balance still 0 after {wait_seconds}s: webhook URL/secret/test-mode keys?")

    def check_push_token(self, expo_token: Optional[str], expo_url: str) -> None:
        token = expo_token or f"ExponentPushToken[smoke-{secrets.token_hex(8)}]"
        status, body = self.call("POST", "/api/v1/devices/push-token", body={"token": token, "platform": "android"}, auth=True)
        ok = status == 200 and isinstance(body, dict) and body.get("token") == token
        self.record(PASS if ok else FAIL, "Push token registered (POST /devices/push-token)", f"HTTP {status}")
        if expo_token:
            self.send_expo_push(expo_token, expo_url)
        status, _ = self.call("DELETE", "/api/v1/devices/push-token", body={"token": token}, auth=True)
        self.record(PASS if status == 204 else FAIL, "Push token deleted (DELETE /devices/push-token)", f"HTTP {status}, want 204")

    def send_expo_push(self, expo_token: str, expo_url: str) -> None:
        status, body = self.call("POST", "", url=expo_url, body={
            "to": expo_token, "title": "Oakly staging", "body": "Smoke test push", "sound": "default"})
        data = body.get("data") if isinstance(body, dict) else None
        ticket = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
        if status != 200 or ticket.get("status") != "ok":
            self.record(FAIL, "Expo accepted the push", f"HTTP {status}: {str(ticket or body)[:140]}")
            return
        self.record(PASS, "Expo accepted the push", "ticket ok")
        time.sleep(15)  # Expo needs a few seconds before the receipt exists
        rstatus, receipts = self.call("POST", "", url=EXPO_RECEIPTS_URL if expo_url.startswith("https://exp.host") else expo_url + "/receipts",
                                      body={"ids": [ticket.get("id")]})
        receipt = (receipts.get("data") or {}).get(ticket.get("id"), {}) if isinstance(receipts, dict) else {}
        if receipt.get("status") == "ok":
            self.record(PASS, "Expo delivered the push to Firebase", "receipt ok: the phone should have buzzed")
        else:
            self.record(FAIL, "Expo delivered the push to Firebase",
                        f"{receipt.get('message') or receipt.get('details') or receipts}"[:160] + " (FCM credentials uploaded with `eas credentials`?)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deployed Oakly staging API.")
    parser.add_argument("base_url", help="https://api.<domain>")
    parser.add_argument("--email", help="a REAL mailbox you can read: registers a throwaway patient and waits for the emailed code")
    parser.add_argument("--code", help="the 5-digit sign-up code, when stdin is not a terminal")
    parser.add_argument("--login-email", help="log in with an existing account instead of registering (password in SMOKE_PASSWORD)")
    parser.add_argument("--wait-payment", type=int, default=0, metavar="SECONDS", help="after printing the Paystack link, wait this long for the wallet to be credited")
    parser.add_argument("--expo-token", help="ExponentPushToken[...] copied from a real phone: also sends a push through Expo")
    parser.add_argument("--expo-url", default="https://exp.host/--/api/v2/push/send", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    smoke = Smoke(args.base_url)
    print(f"Oakly staging smoke test against {smoke.base}\n")
    if smoke.check_health():
        smoke.check_webhooks()
        smoke.check_pricing()
        email, password = args.login_email, os.environ.get("SMOKE_PASSWORD")
        if args.email:
            email, password = args.email, smoke.sign_up(args.email, args.code)
        if email and password:
            if smoke.login(email, password):
                smoke.check_paystack_link(args.wait_payment)
                smoke.check_push_token(args.expo_token, args.expo_url)
        else:
            for name in ("Sign-up code arrived by email", "Login", "Paystack payment link", "Push token round trip"):
                smoke.record(SKIP, name, "needs --email (a real mailbox) or --login-email with SMOKE_PASSWORD")

    failed = [r for r in smoke.results if r[0] == FAIL]
    skipped = [r for r in smoke.results if r[0] == SKIP]
    print(f"\nSTAGING SMOKE: {sum(r[0] == PASS for r in smoke.results)} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
