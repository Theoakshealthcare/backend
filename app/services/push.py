"""Push notifications through the Expo Push API.

Best effort by design: a push is never the source of truth (the app also polls), so nothing in here
raises. A failure is logged and the caller carries on. Tokens Expo reports as ``DeviceNotRegistered``
(app uninstalled, token rotated) are deleted so they are not retried forever.

Tests never reach Expo: they set ``_transport`` to an ``httpx.MockTransport`` (see
``app/tests/fakes.py``), and without one the suite's network guard blocks the call.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.push_token import PushToken

logger = logging.getLogger(__name__)

EXPO_BATCH_SIZE = 100  # Expo accepts at most 100 messages per request

# Test hook only. None in production, where httpx uses its real transport.
_transport: Optional[httpx.BaseTransport] = None


def mask_token(token: str) -> str:
    """Tokens are device addresses; keep them out of logs."""
    return f"{token[:18]}..." if len(token) > 18 else "..."


class PushService:
    def send_to_users(self, db: Session, user_ids: Iterable[UUID], *, title: str, body: str,
                      data: Dict[str, Any]) -> int:
        """Push the same message to every device of the given users. Returns how many messages
        Expo accepted. Never raises."""
        ids = list(user_ids)
        try:
            return self._send_to_users(db, ids, title, body, data)
        except Exception:
            logger.exception("push: unexpected failure sending to %d user(s)", len(ids))
            try:
                db.rollback()
            except Exception:
                logger.exception("push: rollback failed")
            return 0

    def _send_to_users(self, db: Session, user_ids: List[UUID], title: str, body: str,
                       data: Dict[str, Any]) -> int:
        if not user_ids:
            return 0
        tokens = [
            row[0] for row in db.query(PushToken.token).filter(PushToken.user_id.in_(user_ids))
            .order_by(PushToken.created_at, PushToken.token)
        ]
        if not tokens:
            return 0

        accepted = 0
        dead: List[str] = []
        with httpx.Client(timeout=settings.EXPO_PUSH_TIMEOUT_SECONDS, transport=_transport) as client:
            for start in range(0, len(tokens), EXPO_BATCH_SIZE):
                batch = tokens[start:start + EXPO_BATCH_SIZE]
                tickets = self._post_batch(client, [
                    {"to": t, "title": title, "body": body, "data": data, "sound": "default",
                     "priority": "high"}
                    for t in batch
                ])
                if tickets is None:
                    continue
                for token, ticket in zip(batch, tickets):
                    if ticket.get("status") == "ok":
                        accepted += 1
                    elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                        dead.append(token)
                    else:
                        logger.warning("push: Expo rejected %s: %s", mask_token(token),
                                       (ticket.get("details") or {}).get("error") or ticket.get("message"))

        if dead:
            db.query(PushToken).filter(PushToken.token.in_(dead)).delete(synchronize_session=False)
            db.commit()
            logger.info("push: pruned %d unregistered token(s)", len(dead))
        return accepted

    def _post_batch(self, client: httpx.Client, messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.EXPO_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"
        try:
            response = client.post(settings.EXPO_PUSH_URL, json=messages, headers=headers)
            response.raise_for_status()
            tickets = response.json()["data"]
            if not isinstance(tickets, list) or len(tickets) != len(messages):
                raise ValueError("ticket count does not match the batch")
            return tickets
        except Exception as exc:  # network, timeout, HTTP error, bad JSON: all non-fatal
            logger.warning("push: batch of %d failed (%s: %s)", len(messages), type(exc).__name__, exc)
            return None


def get_push_service() -> PushService:
    return PushService()
