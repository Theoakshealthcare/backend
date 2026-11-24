"""GET /api/v1/payment/callback: the static page Paystack sends the in-app browser to after paying."""

import re

import pytest

from app.core.config import Settings, settings
from app.db.models.booking import BookingStatus, PaymentStatus
from app.services import payment_return_page
from app.tests.factories import make_booking, make_patient

URL = "/api/v1/payment/callback"


def test_returns_the_page_with_no_login(client):
    r = client.get(URL)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "confirming your payment" in r.text
    assert 'href="oaklypatient://payment-complete"' in r.text and "Return to the app" in r.text
    assert "#168283" in r.text and 'name="viewport"' in r.text


@pytest.mark.parametrize("query", [
    "", "?trxref=abc&reference=abc", "?reference=" + "A" * 5000, "?x=1&x=2&reference", "?reference=%00%ff", "?%",
])
def test_any_query_string_gets_the_same_200_page(client, query):
    r = client.get(URL + query)
    assert r.status_code == 200 and r.text == client.get(URL).text


def test_never_echoes_the_reference(client):
    payload = '"><script>alert(1)</script>BOOKREF-123'
    r = client.get(URL, params={"reference": payload, "trxref": payload})
    assert r.status_code == 200
    assert "BOOKREF-123" not in r.text and "<script" not in r.text and "alert(1)" not in r.text


def test_the_page_is_self_contained_and_script_free(client):
    html = client.get(URL).text
    assert "<script" not in html.lower() and "javascript:" not in html.lower()
    assert not re.search(r'(src|href)="https?://', html) and "@import" not in html and "url(" not in html
    assert " style=" not in html and "onclick" not in html.lower()  # an inline style attribute would be blocked by the CSP


def test_security_headers(client):
    h = client.get(URL).headers
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp
    assert "script-src" not in csp  # default-src 'none' already forbids every script
    assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"


def test_csp_hash_matches_the_inline_style(client):
    html = client.get(URL).text
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    import base64
    import hashlib
    digest = "sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"style-src '{digest}'" in client.get(URL).headers["content-security-policy"]


def test_arriving_marks_nothing_paid(db, client):
    patient = make_patient(db)
    booking = make_booking(db, patient, None, status=BookingStatus.PENDING, payment_status=PaymentStatus.PENDING)
    ref = booking.paystack_reference or booking.booking_code
    assert client.get(URL, params={"reference": ref, "trxref": ref}).status_code == 200
    db.refresh(booking)
    assert booking.payment_status == PaymentStatus.PENDING and booking.status == BookingStatus.PENDING


def test_the_deep_link_is_the_patient_apps():
    assert payment_return_page.APP_RETURN_URL == "oaklypatient://payment-complete"


# --- the setting -----------------------------------------------------------------------


def test_explicit_callback_url_wins():
    s = Settings(PAYMENT_CALLBACK_URL="https://example.test/back", KYC_WIDGET_PUBLIC_BASE_URL="https://api.example.test")
    assert s.PAYMENT_CALLBACK_URL == "https://example.test/back"


def test_empty_callback_url_derives_from_the_public_api_origin():
    s = Settings(PAYMENT_CALLBACK_URL="", KYC_WIDGET_PUBLIC_BASE_URL="https://staging-api.example.test/")
    assert s.PAYMENT_CALLBACK_URL == "https://staging-api.example.test/api/v1/payment/callback"


def test_with_nothing_set_it_falls_back_to_the_website_default():
    s = Settings(PAYMENT_CALLBACK_URL="", KYC_WIDGET_PUBLIC_BASE_URL="")
    assert s.PAYMENT_CALLBACK_URL == "https://www.oaklycare.com/payment/callback"


def test_the_running_settings_always_have_a_callback_url():
    assert settings.PAYMENT_CALLBACK_URL.startswith("http")
