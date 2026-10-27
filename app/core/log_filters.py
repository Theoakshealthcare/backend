"""Logging filters that keep secrets out of log lines.

Uvicorn's access logger prints each request's full path *including the query string*. Meta's webhook
subscription handshake is ``GET /whatsapp/webhook?hub.mode=subscribe&hub.verify_token=<shared secret>&...``,
so without this filter the verify token is written to the logs on every (re)subscription.
"""

import logging
from urllib.parse import unquote_plus

from app.core.config import settings

REDACTED = "[redacted]"
_SECRET_PARAMS = frozenset({"hub.verify_token"})
_ACCESS_LOGGER = "uvicorn.access"


def _redact_query(query: str) -> str:
    """``query`` with the value of every secret parameter replaced; everything else is kept byte for byte."""
    parts = []
    for part in query.split("&"):
        key, sep, _value = part.partition("=")
        parts.append(f"{key}={REDACTED}" if unquote_plus(key) in _SECRET_PARAMS else part)
    return "&".join(parts)


class AccessLogRedactionFilter(logging.Filter):
    """Blank the webhook verify token in uvicorn access records (``args`` = client, method, path, http, status).

    It never drops a record and never raises: a record of any other shape passes through untouched."""

    def __init__(self) -> None:
        super().__init__()
        self._webhook_path = f"{settings.API_V1_STR}/whatsapp/webhook"

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, sep, query = args[2].partition("?")
            if sep and path == self._webhook_path:
                record.args = (*args[:2], f"{path}?{_redact_query(query)}", *args[3:])
        return True


def install_access_log_redaction() -> None:
    """Attach the filter to uvicorn's access logger once (safe to call again)."""
    logger = logging.getLogger(_ACCESS_LOGGER)
    if not any(isinstance(f, AccessLogRedactionFilter) for f in logger.filters):
        logger.addFilter(AccessLogRedactionFilter())
