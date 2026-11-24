"""The page Paystack sends a person to after they pay (``GET /api/v1/payment/callback``).

Paystack redirects the in-app browser to ``PAYMENT_CALLBACK_URL`` with ``?trxref=...&reference=...`` appended. This page tells
the patient the payment is being confirmed and hands them back to the Oakly app. It decides nothing: whether the booking is paid
is settled only by the signed Paystack webhook (and the app's own verify call), never by someone arriving here, so the page
reads nothing from the request at all. In particular it never prints ``reference`` / ``trxref`` (reflected XSS) and the same
bytes come back for every query string.

Self-contained on purpose: no script, no external font, image or stylesheet, so a strict CSP can forbid everything but the
one inline style block (allowed by its hash, not by ``'unsafe-inline'``).
"""

import base64
import hashlib
from typing import Dict

BRAND_TEAL = "#168283"
APP_RETURN_URL = "oaklypatient://payment-complete"  # the patient app's deep link (the caregiver app has its own scheme)

_STYLE = f"""
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
background:#f4f9f9;color:#12303a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-text-size-adjust:100%}}
main{{width:100%;max-width:420px;background:#fff;border-radius:20px;padding:36px 28px;text-align:center;
box-shadow:0 6px 24px rgba(22,130,131,.12)}}
.mark{{width:64px;height:64px;margin:0 auto 20px;border-radius:50%;background:{BRAND_TEAL};color:#fff;font-size:32px;
line-height:64px;font-weight:700}}
h1{{margin:0 0 12px;font-size:22px;line-height:1.3;color:{BRAND_TEAL}}}
p{{margin:0 0 12px;font-size:16px;line-height:1.5}}
a.button{{display:block;margin-top:24px;padding:16px 20px;border-radius:14px;background:{BRAND_TEAL};color:#fff;
font-size:17px;font-weight:600;text-decoration:none}}
a.button:active{{opacity:.85}}
@media (prefers-color-scheme:dark){{body{{background:#0d1b1e;color:#e6f1f2}}main{{background:#14282c;box-shadow:none}}}}
"""

_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light dark">
<title>Confirming your payment - Oakly</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<div class="mark" aria-hidden="true">&#10003;</div>
<h1>We're confirming your payment</h1>
<p>This only takes a few moments. You can close this page and return to the Oakly app, where your booking will update.</p>
<a class="button" href="{APP_RETURN_URL}">Return to the app</a>
</main>
</body>
</html>
"""

# Computed from the exact bytes above, so editing the style can never leave the CSP out of step with it.
_STYLE_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")

HTML = _HTML

HEADERS: Dict[str, str] = {
    "Content-Security-Policy": (f"default-src 'none'; style-src '{_STYLE_HASH}'; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'"),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
