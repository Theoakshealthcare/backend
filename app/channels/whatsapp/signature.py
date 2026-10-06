"""Verify Meta's ``X-Hub-Signature-256`` header.

Meta signs the RAW request body with HMAC-SHA256, keyed by the app secret, and sends
``sha256=<hex digest>``. Verify before parsing anything, over the exact bytes received (re-serialised
JSON would hash differently), and compare in constant time.
https://developers.facebook.com/docs/graph-api/webhooks/getting-started#validate-payloads

An empty app secret is refused outright: HMAC with an empty key is something anyone can compute, so
"verifying" with it would be a forgery hole disguised as a check.
"""

import hashlib
import hmac
from typing import Optional

PREFIX = "sha256="


def verify_signature(raw_body: bytes, header: Optional[str], app_secret: str) -> bool:
    if not app_secret or not header or not header.startswith(PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header[len(PREFIX):].strip().lower().encode())


def verify_token_matches(supplied: Optional[str], configured: str) -> bool:
    if not configured or not supplied:
        return False
    return hmac.compare_digest(supplied.encode(), configured.encode())
