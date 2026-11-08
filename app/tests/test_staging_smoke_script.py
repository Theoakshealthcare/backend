"""scripts/staging_smoke.py against a fake staging server.

The response shapes in ``FakeStaging`` are copied from the live captures in docs/fe-captures (register, verify-email,
login/mobile, the 503 preview) and from the route code (webhook refusals, wallet deposit, push token). They prove the
script reads those shapes and exits 0/1 correctly; they do NOT prove a real server is configured.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import staging_smoke


class FakeStaging(BaseHTTPRequestHandler):
    pricing_ok = True
    mail_ok = True

    def log_message(self, *args):  # keep pytest output quiet
        pass

    def _send(self, status, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "healthy"})
        if self.path == "/api/v1/openapi.json":
            return self._send(200, {"paths": {"/a": {}, "/b": {}}})
        if self.path == "/api/v1/patient/wallet":
            return self._send(200, {"balance": "100.00", "total_deposited": "100.00", "total_spent": "0", "total_refunded": "0", "currency": "NGN"})
        self._send(404, {"detail": "Not Found"})

    def do_DELETE(self):
        self._body()
        self._send(204) if self.path == "/api/v1/devices/push-token" else self._send(404)

    def do_POST(self):
        body = self._body()
        path = self.path
        if path == "/api/v1/payment/paystack/webhook":
            return self._send(400, {"detail": "Invalid webhook signature"})
        if path == "/api/v1/kyc/dojah/webhook":
            return self._send(401, {"detail": "Invalid signature"})
        if path == "/api/v1/booking/preview":
            if self.pricing_ok:
                return self._send(200, {"total_amount": 1000})
            return self._send(503, {"detail": {"code": "booking_settings_not_configured",
                                               "message": "Pricing is not available right now. Please try again later."}})
        if path == "/api/v1/auth/register":
            return self._send(200, {"email": body["email"], "is_verified": False})
        if path == "/api/v1/auth/verify-email":
            if body["code"] == "12345" and self.mail_ok:
                return self._send(200, {"message": "Email verified successfully"})
            return self._send(400, {"detail": "Invalid verification code"})
        if path == "/api/v1/auth/login/mobile":
            return self._send(200, {"access_token": "<ACCESS_TOKEN_JWT>", "refresh_token": "<REFRESH_TOKEN_JWT>", "token_type": "bearer"})
        if path == "/api/v1/patient/wallet/deposit":
            return self._send(200, {"transaction_id": "t", "paystack_reference": "DEP_x", "amount": "100",
                                    "authorization_url": "https://checkout.paystack.com/abc123", "access_code": "abc123", "currency": "NGN"})
        if path == "/api/v1/devices/push-token":
            return self._send(200, {"token": body["token"], "platform": body["platform"]})
        if path == "/expo/send":
            return self._send(200, {"data": [{"status": "ok", "id": "ticket-1"}]})
        if path == "/expo/send/receipts":
            return self._send(200, {"data": {"ticket-1": {"status": "ok"}}})
        self._send(404, {"detail": "Not Found"})


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(staging_smoke.time, "sleep", lambda _s: None)
    FakeStaging.pricing_ok = True
    FakeStaging.mail_ok = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeStaging)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_everything_passes_against_a_healthy_server(server, capsys):
    code = staging_smoke.main([server, "--email", "someone@real-mailbox.test", "--code", "12345", "--wait-payment", "5",
                               "--expo-token", "ExponentPushToken[abc]", "--expo-url", server + "/expo/send"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "0 failed, 0 skipped" in out
    for line in ("GET /health", "Paystack webhook reachable", "Dojah webhook reachable", "Pricing row exists", "Sign-up code arrived",
                 "Paystack payment link", "Webhook credits the wallet", "Push token registered", "Expo delivered the push", "Push token deleted"):
        assert any(l.startswith("PASS") and line in l for l in out.splitlines()), f"no PASS line for {line}"
    assert "<ACCESS_TOKEN_JWT>" not in out  # tokens are never printed


def test_missing_pricing_row_fails_and_says_why(server, capsys):
    FakeStaging.pricing_ok = False
    code = staging_smoke.main([server, "--email", "someone@real-mailbox.test", "--code", "12345"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  Pricing row exists" in out and "booking_settings_not_configured" in out


def test_wrong_or_missing_email_code_fails(server, capsys):
    code = staging_smoke.main([server, "--email", "someone@real-mailbox.test", "--code", "00000"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  Sign-up code arrived by email and verifies" in out


def test_without_an_email_the_account_checks_are_skipped_not_invented(server, capsys):
    code = staging_smoke.main([server])
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("SKIP") == 4 and "Sign-up (POST /auth/register)" not in out  # no account was created


def test_nothing_listening_is_a_clear_failure(capsys):
    code = staging_smoke.main(["http://127.0.0.1:9"])  # the discard port: connection refused
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  GET /health" in out and "no answer" in out


# ---- scripts/staging_set_pricing.py (the one-time pricing row) ----

from scripts import staging_set_pricing  # noqa: E402


class FakeAdminApi(BaseHTTPRequestHandler):
    """Admin login/MFA shapes from docs/fe-captures/admin (034 first-time login, 035 verify); pricing from section 13."""

    pricing = None  # None = no row yet
    posted = []
    first_time = True

    def log_message(self, *args):
        pass

    def _send(self, status, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/v1/booking-settings/latest":
            if self.headers.get("Authorization") != "Bearer <ACCESS_TOKEN_JWT>":
                return self._send(401, {"detail": "Not authenticated"})
            return self._send(200, self.pricing) if self.pricing else self._send(404, {"detail": "Booking settings not found"})
        self._send(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/api/v1/admin/auth/login":
            if self.first_time:
                return self._send(200, {"mfa_enrollment_required": True, "mfa_token": "<MFA_TOKEN_JWT>",
                                        "otpauth_uri": "otpauth://totp/x?secret=<TOTP_SECRET>", "secret": "<TOTP_SECRET>"})
            return self._send(200, {"mfa_required": True, "mfa_token": "<MFA_TOKEN_JWT>"})
        if self.path == "/api/v1/admin/auth/mfa/verify":
            if body.get("code") == "123456":
                return self._send(200, {"access_token": "<ACCESS_TOKEN_JWT>", "refresh_token": "<REFRESH_TOKEN>", "token_type": "bearer"})
            return self._send(401, {"detail": "Invalid verification code"})
        if self.path == "/api/v1/booking-settings/":
            self.posted.append(body)
            return self._send(200, {**body, "id": "37fab89f-6286-4aa0-b23f-fcd28bd7096b"})
        self._send(404)


@pytest.fixture
def admin_server(monkeypatch):
    monkeypatch.setenv("OAKLY_ADMIN_PASSWORD", "placeholder-password-for-test")
    monkeypatch.setenv("OAKLY_ADMIN_TOTP", "123456")
    FakeAdminApi.pricing, FakeAdminApi.posted, FakeAdminApi.first_time = None, [], True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeAdminApi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


ARGS = ["--admin-email", "owner@real-domain.test", "--base-fee", "1234", "--platform-fee-percent", "7.5",
        "--multiplier", "nurse=1.0", "--reason", "Opening staging price list", "--yes"]


def test_pricing_is_published_exactly_as_given(admin_server, capsys):
    assert staging_set_pricing.main([admin_server, *ARGS]) == 0
    assert FakeAdminApi.posted == [{"base_fee": 1234.0, "platform_fee_percent": 7.5, "multipliers": {"nurse": 1.0},
                                    "surge_pricing": False, "surge_multiplier": 1.0, "discount": 0.0,
                                    "reason": "Opening staging price list"}]
    out = capsys.readouterr().out
    assert "Setup key" in out  # first sign-in: the authenticator key is shown once for enrolment
    assert "<ACCESS_TOKEN_JWT>" not in out


def test_existing_pricing_is_left_alone(admin_server, capsys):
    FakeAdminApi.first_time = False
    FakeAdminApi.pricing = {"id": "abc", "base_fee": 5000.0, "platform_fee_percent": 10.0}
    assert staging_set_pricing.main([admin_server, *ARGS]) == 0
    assert FakeAdminApi.posted == [] and "Nothing changed" in capsys.readouterr().out


def test_prices_have_no_defaults(admin_server):
    with pytest.raises(SystemExit):  # --base-fee and --platform-fee-percent are required: nothing is auto-seeded
        staging_set_pricing.main([admin_server, "--admin-email", "owner@real-domain.test", "--reason", "x", "--yes"])
    assert FakeAdminApi.posted == []


def test_wrong_authenticator_code_sends_nothing(admin_server, monkeypatch):
    monkeypatch.setenv("OAKLY_ADMIN_TOTP", "000000")
    assert staging_set_pricing.main([admin_server, *ARGS]) == 1
    assert FakeAdminApi.posted == []
