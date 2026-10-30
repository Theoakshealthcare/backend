"""KD2: the hosted Dojah widget page and its signed link.

The app opens ``widget_url`` in the in-app browser; the page loads Dojah's web widget with the PUBLIC values only. The token in the URL is
the only credential (no auth header), so these tests pin: forgery and expiry of the token, that the page carries nothing secret, that
every value is encoded safely, the security headers, the closed-attempt page, and that the return link comes from config only.
"""
import json
import re
import time
import uuid

import pytest

from app.core.config import Settings, settings
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import IdentityVerification
from app.services import kyc_widget
from app.tests.factories import auth_headers, make_caregiver
from app.tests.fake_dojah import (
    PRODUCTION_KYC_KEYS, TEST_APP_ID, TEST_FINGERPRINT_KEY, TEST_PUBLIC_KEY, TEST_SECRET_KEY, TEST_WEBHOOK_SECRET, TEST_WIDGET_ID,
    new_reference,
)

SESSIONS = "/api/v1/caregiver/identity-verification/sessions"
PAGE = "/api/v1/kyc/widget"
POLICY = "kyc-2026-10"
NOW = 1_800_000_000
HANDLE = "0f8fad5b-d9cb-469f-a165-70867728950e"
BASE_URL = "https://api.oakly.test"
FINISHED = "This verification link has finished. Return to the app."


@pytest.fixture(autouse=True)
def env(fake_dojah, kyc_availability):
    return kyc_availability


def attempt(db, status="created", reference=None):
    caregiver = make_caregiver(db, approved=False, profile_completion_step=2)
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=1, status=status,
                               provider_session_id=reference or new_reference(), provider_reference=reference)
    db.add(row)
    db.commit()
    return row


def handle():
    return str(uuid.uuid4())


def csp(response):
    """The CSP header as {directive: [sources]}."""
    out = {}
    for part in response.headers["content-security-policy"].split(";"):
        name, *sources = part.split()
        out[name] = sources
    return out


# --- the token --------------------------------------------------------------------------------------------------------------
def test_a_token_round_trips_to_its_reference():
    reference = handle()
    assert kyc_widget.verify_token(kyc_widget.issue_token(reference, now=NOW), now=NOW + 60) == reference


def test_a_token_is_one_url_path_segment_of_known_shape():
    token = kyc_widget.issue_token(handle(), now=NOW)
    assert re.fullmatch(r"[1-9]\d{9}\.[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\.[0-9a-f]{64}", token)


def test_a_token_lasts_thirty_minutes():
    token = kyc_widget.issue_token(handle(), now=NOW)
    assert kyc_widget.verify_token(token, now=NOW + 30 * 60) is not None
    assert kyc_widget.verify_token(token, now=NOW + 30 * 60 + 1) is None


def test_a_token_with_an_expiry_beyond_its_lifetime_is_refused():
    # An expiry further out than the lifetime can only come from a forger (or a wrong clock), never from issue_token.
    reference = handle()
    far = kyc_widget.issue_token(reference, now=NOW + 24 * 3600)
    assert kyc_widget.verify_token(far, now=NOW) is None


def test_a_tampered_reference_is_refused():
    token = kyc_widget.issue_token(handle(), now=NOW)
    exp, _, mac = token.split(".")
    assert kyc_widget.verify_token(f"{exp}.{handle()}.{mac}", now=NOW) is None


def test_a_tampered_expiry_is_refused():
    reference = handle()
    exp, _, mac = kyc_widget.issue_token(reference, now=NOW).split(".")
    assert kyc_widget.verify_token(f"{int(exp) + 3600}.{reference}.{mac}", now=NOW) is None


def test_a_tampered_mac_is_refused():
    reference = handle()
    exp, _, mac = kyc_widget.issue_token(reference, now=NOW).split(".")
    flipped = ("0" if mac[0] != "0" else "1") + mac[1:]
    assert kyc_widget.verify_token(f"{exp}.{reference}.{flipped}", now=NOW) is None


def test_a_token_signed_with_another_key_is_refused(monkeypatch):
    token = kyc_widget.issue_token(handle(), now=NOW)
    monkeypatch.setattr(settings, "SECRET_KEY", settings.SECRET_KEY + "-rotated")
    assert kyc_widget.verify_token(token, now=NOW) is None


def test_a_token_for_one_attempt_does_not_open_another():
    first, second = handle(), handle()
    token = kyc_widget.issue_token(first, now=NOW)
    assert kyc_widget.verify_token(token, now=NOW) == first != second


def test_the_signing_key_is_derived_not_the_secret_key_itself():
    # Domain separation: a JWT-style HMAC over the same bytes with SECRET_KEY itself must not verify as a widget token.
    import hashlib
    import hmac

    reference = handle()
    exp = NOW + 600
    forged = hmac.new(settings.SECRET_KEY.encode(), f"{exp}.{reference}".encode(), hashlib.sha256).hexdigest()
    assert kyc_widget.verify_token(f"{exp}.{reference}.{forged}", now=NOW) is None
    assert kyc_widget.signing_key() != settings.SECRET_KEY.encode()


@pytest.mark.parametrize("garbage", [
    "", ".", "..", "a.b.c", "x" * 400, "1800000000.short.deadbeef", "1800000000.not-a-handle." + "0" * 64,
    "-1." + HANDLE + "." + "0" * 64, "1800000000." + HANDLE.upper() + "." + "0" * 64,
    "1800000000." + HANDLE + "." + "0" * 63, "1800000000." + HANDLE + "." + "Z" * 64,
    "9" * 40 + "." + HANDLE + "." + "0" * 64, "1800000000\n." + HANDLE + "." + "0" * 64,
    "１８００００００００." + HANDLE + "." + "0" * 64,  # full-width digits
    "1800000000." + HANDLE + "." + "0" * 64 + ".extra",
    "1800000000.OKL-" + "a" * 32 + "." + "0" * 64,  # a Dojah reference is not a handle
    "01800000000." + HANDLE + "." + "0" * 64,  # leading zero
])
def test_garbage_is_refused_without_raising(garbage):
    assert kyc_widget.verify_token(garbage, now=NOW) is None


def test_a_non_string_is_refused():
    assert kyc_widget.verify_token(None, now=NOW) is None  # type: ignore[arg-type]


def test_an_empty_secret_key_never_verifies(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "")
    with pytest.raises(RuntimeError):
        kyc_widget.issue_token(handle(), now=NOW)
    assert kyc_widget.verify_token(f"{NOW + 60}.{handle()}.{'0' * 64}", now=NOW) is None


def test_a_token_has_exactly_one_spelling():
    handle_ = handle()
    exp, _, mac = kyc_widget.issue_token(handle_, now=NOW).split(".")
    assert kyc_widget.verify_token(f"0{exp}.{handle_}.{mac}", now=NOW) is None


def test_the_token_never_carries_the_dojah_reference(db):
    row = attempt(db)
    token = kyc_widget.issue_token(str(row.id), now=NOW)
    assert row.provider_session_id not in token


# --- the start endpoint ------------------------------------------------------------------------------------------------------
def start(client, caregiver, **body):
    return client.post(SESSIONS, json={"consent": True, "policy_version": POLICY, **body}, headers=auth_headers(caregiver.user))


def test_start_returns_an_absolute_https_widget_url_for_the_attempts_reference(db, client):
    caregiver = make_caregiver(db, approved=False, profile_completion_step=2)
    r = start(client, caregiver)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"verification_id", "attempt_no", "widget", "state", "widget_url"}  # additive: nothing else changed
    assert body["widget_url"].startswith(f"{BASE_URL}{PAGE}/")
    token = body["widget_url"].rsplit("/", 1)[1]
    assert kyc_widget.verify_token(token) == body["verification_id"] != body["widget"]["reference_id"]
    assert body["widget"]["reference_id"] not in body["widget_url"]  # the link never carries the Dojah reference


def test_resuming_gives_a_fresh_token_for_the_same_reference(db, client, monkeypatch):
    caregiver = make_caregiver(db, approved=False, profile_completion_step=2)
    first = start(client, caregiver).json()
    monkeypatch.setattr(kyc_widget, "_now", lambda: time.time() + 120)
    second = start(client, caregiver)
    assert second.status_code == 200
    assert second.json()["widget"]["reference_id"] == first["widget"]["reference_id"]
    assert second.json()["widget_url"] != first["widget_url"]


def test_the_trailing_slash_of_the_base_url_is_ignored(db, client, monkeypatch):
    monkeypatch.setattr(settings, "KYC_WIDGET_PUBLIC_BASE_URL", BASE_URL + "/")
    url = start(client, make_caregiver(db, approved=False, profile_completion_step=2)).json()["widget_url"]
    assert url.startswith(f"{BASE_URL}{PAGE}/")


def test_an_unset_base_url_is_an_outage_not_a_half_session(db, client, monkeypatch):
    monkeypatch.setattr(settings, "KYC_WIDGET_PUBLIC_BASE_URL", "")
    caregiver = make_caregiver(db, approved=False, profile_completion_step=2)
    r = start(client, caregiver)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "PROVIDER_UNAVAILABLE"
    assert db.query(IdentityVerification).filter_by(caregiver_id=caregiver.id).count() == 0


# --- the page ---------------------------------------------------------------------------------------------------------------
def open_page(client, row, token=None):
    return client.get(f"{PAGE}/{token or kyc_widget.issue_token(str(row.id))}")


def config_of(html):
    block = re.search(r'<script type="application/json" id="oakly-config">(.*?)</script>', html, re.S)
    assert block, "the config block is missing"
    return json.loads(block.group(1))


def test_the_page_carries_only_the_public_values(db, client):
    row = attempt(db)
    r = open_page(client, row)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert config_of(r.text) == {"app_id": TEST_APP_ID, "public_key": TEST_PUBLIC_KEY, "widget_id": TEST_WIDGET_ID,
                                 "reference_id": row.provider_session_id, "return_url": settings.KYC_RETURN_URL}


def test_no_secret_ever_appears_in_the_page_or_its_headers(db, client):
    row = attempt(db)
    r = open_page(client, row)
    haystack = r.text + json.dumps(dict(r.headers))
    for secret in (TEST_SECRET_KEY, TEST_WEBHOOK_SECRET, TEST_FINGERPRINT_KEY, settings.SECRET_KEY, str(row.reference),
                   str(row.id), str(row.caregiver_id), kyc_widget.signing_key().hex()):
        assert secret not in haystack


def test_the_page_names_no_person(db, client):
    row = attempt(db)
    caregiver = db.query(Caregiver).filter_by(id=row.caregiver_id).one()
    html = open_page(client, row).text
    for value in (caregiver.full_name, caregiver.email, caregiver.user.email):
        if value:
            assert value not in html


def test_the_page_does_not_contain_the_token(db, client):
    row = attempt(db)
    token = kyc_widget.issue_token(str(row.id))
    assert token not in open_page(client, row, token).text


def test_the_page_loads_the_widget_and_reports_nowhere(db, client):
    html = open_page(client, attempt(db)).text
    assert "https://widget.dojah.io/widget.js" in html
    assert "new window.Connect" in html and "type:'custom'" in html.replace(" ", "")
    for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "postMessage"):
        assert call not in html  # the callbacks are not trusted and the page sends the outcome nowhere
    assert "Start verification" in html
    assert "Done. You can return to the app" in html


def test_every_script_has_the_per_response_nonce_and_only_that(db, client):
    row = attempt(db)
    first, second = open_page(client, row), open_page(client, row)
    nonce = re.search(r"'nonce-([^']+)'", first.headers["content-security-policy"]).group(1)
    assert len(nonce) >= 16
    scripts = re.findall(r"<script\b([^>]*)>", first.text)
    executable = [a for a in scripts if "application/json" not in a]
    assert executable and all(f'nonce="{nonce}"' in a for a in executable)
    assert not re.search(r"<[^>]*\son\w+\s*=", first.text) and "javascript:" not in first.text.lower()  # no inline handler attributes
    other = re.search(r"'nonce-([^']+)'", second.headers["content-security-policy"]).group(1)
    assert other != nonce  # a new nonce per response


def test_the_response_headers(db, client):
    r = open_page(client, attempt(db))
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    policy = r.headers["permissions-policy"]
    assert 'camera=(self "https://identity.dojah.io")' in policy
    assert 'microphone=(self "https://identity.dojah.io")' in policy
    assert 'geolocation=(self "https://identity.dojah.io")' in policy


def test_the_csp_allows_the_page_dojah_and_nothing_else(db, client):
    policy = csp(open_page(client, attempt(db)))
    assert policy["default-src"] == ["'none'"]
    assert policy["frame-ancestors"] == ["'none'"] and policy["base-uri"] == ["'none'"]
    assert policy["form-action"] == ["'none'"] and policy["object-src"] == ["'none'"]
    assert "'unsafe-inline'" not in policy["script-src"] and "'unsafe-eval'" not in policy["script-src"]
    assert "*" not in {s for sources in policy.values() for s in sources}
    for directive in ("script-src", "connect-src", "frame-src"):
        assert "https://*.dojah.io" in policy[directive]
    for sources in policy.values():  # no host but Dojah's anywhere
        assert all(not s.startswith(("http:", "https:", "wss:")) or s.endswith(".dojah.io") for s in sources)


def test_the_headers_are_on_the_not_found_and_finished_responses_too(db, client):
    for r in (client.get(f"{PAGE}/garbage"), open_page(client, attempt(db, status="passed"))):
        assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer" and "content-security-policy" in r.headers


def test_the_page_is_not_in_the_openapi_schema(client):
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    assert paths and not any("/kyc/widget" in path for path in paths)


def test_an_unconfigured_widget_is_a_bare_503_not_a_broken_page(db, client, monkeypatch):
    row = attempt(db)
    monkeypatch.setattr(settings, "DOJAH_WIDGET_ID", "")
    r = open_page(client, row)
    assert (r.status_code, r.text) == (503, "Unavailable") and "content-security-policy" in r.headers


def test_the_page_needs_no_auth_header(db, client):
    assert open_page(client, attempt(db)).status_code == 200


# --- who gets the page ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["garbage", "a.b.c", "1800000000." + HANDLE + "." + "0" * 64, "%00", "x" * 5000])
def test_garbage_is_a_plain_404_with_no_detail(client, token):
    r = client.get(f"{PAGE}/{token}")
    assert r.status_code == 404 and r.text == "Not found" and r.headers["content-type"].startswith("text/plain")


def test_a_forged_token_looks_like_any_other_404(db, client):
    row = attempt(db)
    exp, ref, mac = kyc_widget.issue_token(str(row.id)).split(".")
    forged = client.get(f"{PAGE}/{exp}.{ref}.{'0' * 64}")
    garbage = client.get(f"{PAGE}/garbage")
    assert (forged.status_code, forged.text) == (garbage.status_code, garbage.text) == (404, "Not found")


def test_a_valid_token_for_an_unknown_attempt_is_the_same_404(db, client):
    token = kyc_widget.issue_token(handle())  # signed by us, but no attempt has this id
    r = client.get(f"{PAGE}/{token}")
    assert (r.status_code, r.text) == (404, "Not found")


def test_an_attempt_without_one_of_our_references_is_the_same_404(db, client):
    row = attempt(db, reference="not-one-of-ours")
    r = open_page(client, row)
    assert (r.status_code, r.text) == (404, "Not found")


def test_an_expired_token_is_a_410_that_does_not_open_the_widget(db, client):
    row = attempt(db)
    token = kyc_widget.issue_token(str(row.id), now=time.time() - 3600)
    r = open_page(client, row, token)
    assert r.status_code == 410 and FINISHED in r.text and "widget.dojah.io" not in r.text


@pytest.mark.parametrize("status", ["passed", "failed", "needs_review", "expired", "error", "awaiting_result"])
def test_an_attempt_that_is_no_longer_open_shows_the_finished_page(db, client, status):
    row = attempt(db, status=status)
    r = open_page(client, row)
    assert r.status_code == 410
    assert FINISHED in r.text and "widget.dojah.io" not in r.text and "app_id" not in r.text
    assert "script-src" in r.headers["content-security-policy"]


@pytest.mark.parametrize("status", ["created", "started"])
def test_an_open_attempt_gets_the_widget(db, client, status):
    r = open_page(client, attempt(db, status=status))
    assert r.status_code == 200 and "widget.dojah.io" in r.text


def test_a_page_opened_for_one_attempt_names_only_that_reference(db, client):
    first, second = attempt(db), attempt(db)
    assert config_of(open_page(client, first).text)["reference_id"] == first.provider_session_id
    assert config_of(open_page(client, second).text)["reference_id"] == second.provider_session_id


def test_nothing_is_written_by_opening_the_page(db, client):
    row = attempt(db)
    before = (row.status, row.updated_at)
    open_page(client, row)
    db.expire_all()
    fresh = db.query(IdentityVerification).filter_by(id=row.id).one()
    assert (fresh.status, fresh.updated_at) == before


# --- encoding ---------------------------------------------------------------------------------------------------------------
HOSTILE = '</script><script>alert(1)</script>"\'&<!--\u2028\u2029'


def test_hostile_public_values_cannot_break_out_of_the_page(db, client, monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_APP_ID", HOSTILE)
    monkeypatch.setattr(settings, "DOJAH_PUBLIC_KEY", HOSTILE)
    monkeypatch.setattr(settings, "DOJAH_WIDGET_ID", HOSTILE)
    r = open_page(client, attempt(db))
    assert r.text.count("<script") == 2 and r.text.count("</script>") == 2  # the config block and the one program, no more
    assert "<script>alert" not in r.text and "</script><script>" not in r.text
    assert "\u2028" not in r.text and "\u2029" not in r.text
    assert config_of(r.text)["app_id"] == HOSTILE  # and it survives the round trip intact


def test_the_return_link_is_escaped_in_html_and_in_json():
    page = kyc_widget.render_page(reference_id="OKL-" + "a" * 32, app_id="a", public_key="b", widget_id="c",
                                  return_url='oaklycarecaregivermobile://kyc/return?x="><script>1</script>', nonce="n" * 22)
    assert '"><script>1' not in page and "<script>1</script>" not in page
    assert page.count("<script") == 2


# --- the return link --------------------------------------------------------------------------------------------------------
def test_the_return_link_comes_from_config_and_is_a_plain_anchor_too(db, client):
    html = open_page(client, attempt(db)).text
    assert f'href="{settings.KYC_RETURN_URL}"' in html


def test_nothing_in_the_request_can_change_the_return_link(db, client):
    row = attempt(db)
    token = kyc_widget.issue_token(str(row.id))
    r = client.get(f"{PAGE}/{token}", params={"return_url": "https://evil.test/", "next": "https://evil.test/", "redirect": "//evil.test"},
                   headers={"Host": "evil.test", "Referer": "https://evil.test/", "X-Forwarded-Host": "evil.test"})
    assert "evil.test" not in r.text
    assert config_of(r.text)["return_url"] == settings.KYC_RETURN_URL


@pytest.mark.parametrize("value", [
    "oaklycarecaregivermobile://kyc/return", "oaklycaregiver://kyc/return", "exp://192.168.0.2:8081/--/kyc/return",
])
def test_allowed_return_schemes(value):
    assert Settings(KYC_RETURN_URL=value).KYC_RETURN_URL == value


@pytest.mark.parametrize("value", [
    "", "https://evil.test/", "http://evil.test", "javascript:alert(1)", "data:text/html,x", "//evil.test", "file:///etc/passwd",
    "intent://x#Intent;end", "oaklycarecaregivermobile://kyc/return\nSet-Cookie: x=1", 'oaklycarecaregivermobile://k"onload=1',
    "oaklycarecaregivermobile://k<b>", "oaklycarecaregivermobile:", "kyc/return", "OAKLYCAREGIVERMOBILE://x/" + "a" * 300,
    "oaklycarecaregivermobile://u:p@evil.test/",
])
def test_a_return_url_outside_the_scheme_allow_list_refuses_to_boot(value):
    with pytest.raises(ValueError):
        Settings(KYC_RETURN_URL=value)


def test_production_allows_only_the_apps_own_scheme_as_the_return_url():
    base = dict(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="sk_live_placeholder", TRUSTED_PROXIES=["10.0.0.0/8"], **PRODUCTION_KYC_KEYS)
    assert Settings(**base).KYC_RETURN_URL == "oaklycarecaregivermobile://kyc/return"
    with pytest.raises(ValueError):
        Settings(**base, KYC_RETURN_URL="exp://192.168.0.2:8081/--/kyc/return")


# --- config -----------------------------------------------------------------------------------------------------------------
PROD = dict(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="sk_live_placeholder", TRUSTED_PROXIES=["10.0.0.0/8"])


def test_production_refuses_to_boot_without_a_public_base_url():
    with pytest.raises(ValueError, match="KYC_WIDGET_PUBLIC_BASE_URL"):
        Settings(**PROD, **{**PRODUCTION_KYC_KEYS, "KYC_WIDGET_PUBLIC_BASE_URL": ""})


@pytest.mark.parametrize("value", ["http://api.oakly.test", "api.oakly.test", "ftp://api.oakly.test", "https://", "https:///x",
                                   "https://user:pw@api.oakly.test", "https://api.oakly.test?x=1", "https://api.oakly.test#f",
                                   "//api.oakly.test", "https://api.oakly.test/a b"])
def test_production_requires_an_absolute_https_origin(value):
    with pytest.raises(ValueError, match="KYC_WIDGET_PUBLIC_BASE_URL"):
        Settings(**PROD, **{**PRODUCTION_KYC_KEYS, "KYC_WIDGET_PUBLIC_BASE_URL": value})


def test_production_boots_with_an_https_origin():
    s = Settings(**PROD, **PRODUCTION_KYC_KEYS)
    assert s.KYC_WIDGET_PUBLIC_BASE_URL.startswith("https://")


def test_other_environments_may_leave_it_empty_or_use_http():
    assert Settings().KYC_WIDGET_PUBLIC_BASE_URL == ""
    assert Settings(KYC_WIDGET_PUBLIC_BASE_URL="http://192.168.0.10:8000").KYC_WIDGET_PUBLIC_BASE_URL.startswith("http://")


@pytest.mark.parametrize("value", ["javascript:alert(1)", "ftp://x.test", "http://", "https://u@x.test"])
def test_a_set_base_url_must_still_be_well_formed_outside_production(value):
    with pytest.raises(ValueError, match="KYC_WIDGET_PUBLIC_BASE_URL"):
        Settings(KYC_WIDGET_PUBLIC_BASE_URL=value)


# --- logging ----------------------------------------------------------------------------------------------------------------
def test_the_token_and_reference_are_never_logged(db, client, caplog):
    row = attempt(db)
    token = kyc_widget.issue_token(str(row.id))
    with caplog.at_level("DEBUG"):
        open_page(client, row, token)
        client.get(f"{PAGE}/{token[:-3]}000")
        open_page(client, attempt(db, status="passed"))
    assert token not in caplog.text and row.provider_session_id not in caplog.text and str(row.id) not in caplog.text and str(row.reference) not in caplog.text
