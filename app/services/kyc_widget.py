"""KD2: the hosted Dojah widget page (caregiver KYC) and the signed link that opens it.

The caregiver app does not embed Dojah's native SDK. It opens ``widget_url`` (returned by ``POST .../identity-verification/sessions``)
in the in-app browser; that page loads Dojah's web widget with the PUBLIC values (app id, public key, widget id) and the server-minted
``OKL-`` reference, and sends the caregiver back to the app through a deep link when the widget calls back.

The link is the credential (the browser sends no auth header): ``<expiry>.<reference>.<mac>`` where ``mac`` is a hex HMAC-SHA256 over
a domain-separated message, keyed by a key DERIVED from ``SECRET_KEY`` (never ``SECRET_KEY`` itself, which signs JWTs). It names the
attempt by the attempt's id (not the Dojah reference, so a leaked link never reveals the reference), so no table is needed, lives 30 minutes and is checked in constant time. Opening the page never
writes anything and never reveals whether a reference exists: a bad, forged or unknown token is the same plain 404.

The widget's callbacks are NOT trusted for the outcome and the page sends nothing anywhere; the server learns the result from Dojah's
webhook, the pull and the reconcile cron (K3/K4). NEVER log the token or the reference, and the page carries no PII and no secret.
"""
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from typing import Optional

from app.core.config import settings

TOKEN_TTL_SECONDS = 30 * 60
_KEY_PURPOSE = b"oakly.kyc.widget.link.key.v1"
_MESSAGE_PURPOSE = b"oakly.kyc.widget.link.v1|"
# <expiry>.<attempt handle>.<mac>. The handle is the attempt's own id (``identity_verifications.id``, which the app already has as ``verification_id``), NOT the Dojah
# reference: a leaked or old link then never reveals the reference the widget can be run under. No leading zero in the expiry, so
# one token has exactly one spelling.
_TOKEN = re.compile(r"([1-9][0-9]{0,11})\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([0-9a-f]{64})")

WIDGET_SCRIPT = "https://widget.dojah.io/widget.js"
# [UNVERIFIED] The CSP was NOT checked against a live widget run (the sandbox pass used a page with no CSP). It therefore allows the
# whole of dojah.io for what the widget may fetch, frame or inject, rather than guessing narrower; narrow it after a live run.
_DOJAH = "https://*.dojah.io"


def _now() -> float:
    return time.time()


def signing_key() -> bytes:
    """The link key, derived from SECRET_KEY by a keyed hash with a fixed purpose label, so a JWT or any other use of SECRET_KEY can
    never produce a valid link (and the reverse)."""
    if not settings.SECRET_KEY:
        raise RuntimeError("SECRET_KEY is empty")
    return hmac.new(settings.SECRET_KEY.encode(), _KEY_PURPOSE, hashlib.sha256).digest()


def _mac(key: bytes, expires: int, handle: str) -> str:
    return hmac.new(key, _MESSAGE_PURPOSE + f"{expires}|{handle}".encode(), hashlib.sha256).hexdigest()


def issue_token(handle: str, now: Optional[float] = None) -> str:
    expires = int(_now() if now is None else now) + TOKEN_TTL_SECONDS
    return f"{expires}.{handle}.{_mac(signing_key(), expires, handle)}"


def verify_token(token: str, now: Optional[float] = None) -> Optional[str]:
    """The attempt handle the token was issued for, or None for anything else: malformed, forged, signed with another key, expired, or
    claiming a life longer than we ever issue. Never raises."""
    if not isinstance(token, str):
        return None
    match = _TOKEN.fullmatch(token)
    if match is None:
        return None
    expires_text, handle, mac = match.groups()
    try:
        expected = _mac(signing_key(), int(expires_text), handle)
    except RuntimeError:
        return None
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return None
    remaining = int(expires_text) - (_now() if now is None else now)
    if remaining < 0 or remaining > TOKEN_TTL_SECONDS:
        return None
    return handle


def token_expired(token: str, now: Optional[float] = None) -> bool:
    """True only for a token whose MAC is right and whose life is over, i.e. a link this server issued. Anything else (forged,
    malformed, unknown key) is False, so a forger learns nothing from the difference."""
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        return False
    expires_text, handle, mac = match.groups()
    try:
        expected = _mac(signing_key(), int(expires_text), handle)
    except RuntimeError:
        return False
    return hmac.compare_digest(mac.encode(), expected.encode()) and int(expires_text) < (_now() if now is None else now)


def widget_url(handle: str) -> str:
    """The absolute link the app opens. The base URL is checked at boot (https in production) and is never taken from a request."""
    return f"{settings.KYC_WIDGET_PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_STR}/kyc/widget/{issue_token(handle)}"


# --- the page ---------------------------------------------------------------------------------------------------------------
def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def security_headers(nonce: str) -> dict:
    """Headers for every response of the page, including its 404 and 410 (the camera is delegated to Dojah's frame only; ``self`` is
    required or Dojah's frame cannot use it, Weysure SOP 2026-10-09)."""
    csp = "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}' {_DOJAH}",
        f"connect-src {_DOJAH} wss://*.dojah.io",
        f"frame-src {_DOJAH}",
        f"img-src data: blob: {_DOJAH}",
        f"media-src blob: data: {_DOJAH}",
        f"font-src data: {_DOJAH}",
        f"style-src 'unsafe-inline' {_DOJAH}",
        f"worker-src blob: {_DOJAH}",
        "base-uri 'none'", "form-action 'none'", "object-src 'none'", "frame-ancestors 'none'",
    ])
    identity = '"https://identity.dojah.io"'
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": csp,
        "Permissions-Policy": f"camera=(self {identity}), microphone=(self {identity}), geolocation=(self {identity})",
    }


def _json_for_html(value: dict) -> str:
    """JSON that is safe inside a <script type="application/json"> block: pure ASCII (so U+2028/9 are escaped) and no ``<``, ``>``
    or ``&``, so nothing in a value can close the block or open a comment."""
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_STYLE = """
body{font:16px/1.45 -apple-system,system-ui,sans-serif;margin:0 auto;padding:24px 20px;max-width:520px;color:#111;background:#fff}
h1{font-size:20px;margin:0 0 12px}
#status{padding:12px;border-radius:10px;background:#f2f4f7;font-weight:600}
button{width:100%;font-size:18px;padding:16px;border:0;border-radius:12px;background:#0a7d4f;color:#fff;margin:16px 0}
button:disabled{background:#9aa3ad}
a{color:#0a7d4f;font-weight:600}
[hidden]{display:none}
@media (prefers-color-scheme:dark){body{background:#101418;color:#eef1f4}#status{background:#1c232b}}
"""

_PROGRAM = """
(function(){
var cfg=JSON.parse(document.getElementById('oakly-config').textContent);
var statusEl=document.getElementById('status'),go=document.getElementById('go'),done=document.getElementById('done');
var opened=false,finished=false;
function say(t){statusEl.textContent=t}
function finish(){
 if(finished)return;finished=true;go.hidden=true;done.hidden=false;say('Done. You can return to the app.');
 window.setTimeout(function(){window.location.replace(cfg.return_url)},600);
}
function openWidget(){
 if(opened||finished)return;
 if(!window.Connect){load();return}
 try{
  var w=new window.Connect({app_id:cfg.app_id,p_key:cfg.public_key,type:'custom',config:{widget_id:cfg.widget_id},
   reference_id:cfg.reference_id,onSuccess:finish,onClose:finish,onError:finish});
  w.setup();w.open();opened=true;go.disabled=true;say('Follow the steps on screen.');
 }catch(e){say('Could not start the check. Tap Start verification to try again.');go.disabled=false}
}
function load(){
 say('Loading the verification widget...');go.disabled=true;
 var s=document.createElement('script');s.src='__WIDGET_SCRIPT__';s.async=true;
 s.onload=function(){go.disabled=false;openWidget()};
 s.onerror=function(){say('Could not load the widget. Check your connection and tap Start verification.');go.disabled=false};
 document.head.appendChild(s);
}
go.onclick=openWidget;
load();
})();
"""

_FINISHED_PROGRAM = """
(function(){
var cfg=JSON.parse(document.getElementById('oakly-config').textContent);
window.setTimeout(function(){window.location.replace(cfg.return_url)},1500);
})();
"""


def _shell(body: str, config: dict, program: str, nonce: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
        f"<title>Oakly identity check</title><style>{_STYLE}</style></head><body>\n{body}\n"
        f'<script type="application/json" id="oakly-config">{_json_for_html(config)}</script>\n'
        f'<script nonce="{nonce}">{program}</script>\n</body></html>\n'
    )


def render_page(*, reference_id: str, app_id: str, public_key: str, widget_id: str, return_url: str, nonce: str) -> str:
    """The page that opens the widget. Only public values go into it, all through ``_json_for_html``; the return link is also an
    HTML-escaped anchor for the case the automatic redirect is blocked."""
    body = (
        "<h1>Verify your identity</h1>\n"
        '<p id="status" role="status" aria-live="polite">Loading the verification widget...</p>\n'
        '<button id="go" type="button" disabled>Start verification</button>\n'
        f'<p id="done" hidden>Done. You can return to the app. <a href="{html.escape(return_url, quote=True)}">Return to the app</a></p>\n'
        "<noscript><p>This check needs JavaScript. Turn it on and reload this page.</p></noscript>"
    )
    config = {"app_id": app_id, "public_key": public_key, "widget_id": widget_id, "reference_id": reference_id,
              "return_url": return_url}
    return _shell(body, config, _PROGRAM.replace("__WIDGET_SCRIPT__", WIDGET_SCRIPT), nonce)


def render_finished(*, return_url: str, nonce: str) -> str:
    """For a link whose attempt is over (or whose token ran out): says so and never opens the widget."""
    body = (
        "<h1>Oakly identity check</h1>\n"
        '<p id="status" role="status">This verification link has finished. Return to the app.</p>\n'
        f'<p><a href="{html.escape(return_url, quote=True)}">Return to the app</a></p>'
    )
    return _shell(body, {"return_url": return_url}, _FINISHED_PROGRAM, nonce)
