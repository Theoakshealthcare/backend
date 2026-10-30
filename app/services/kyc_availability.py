"""Is Dojah usable right now? One shared flag, read by the caregiver KYC API and written by the K4 reconcile/decision code.

The flag lives in Redis (``kyc:provider_unavailable`` with a TTL, design 7.3) so every API worker and the ARQ worker agree, and it
heals by itself. A Redis problem must not lock caregivers out of onboarding, so a failed read counts as *available* (the app then
opens the widget and finds out; note that with Redis AND Dojah both down, ``defer`` is refused); a failed write is logged. ``get_provider_availability`` is a FastAPI
dependency so tests replace it with an in-memory fake (``app/tests/fake_dojah.py``).
"""
import logging
from typing import Optional, Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)

UNAVAILABLE_KEY = "kyc:provider_unavailable"
ERRORS_KEY = "kyc:provider_errors"  # the consecutive-error counter (K4); it expires on its own so old errors are forgotten
ALERT_KEY_PREFIX = "kyc:alert:"  # one ops alert per kind per window (K4)
DEFAULT_UNAVAILABLE_TTL_SECONDS = 15 * 60  # design 7.3: three consecutive provider errors flag it for 15 minutes
CONSECUTIVE_ERRORS_TO_FLAG = 3
DEFAULT_ALERT_WINDOW_SECONDS = 60 * 60


class ProviderAvailability(Protocol):
    def is_available(self) -> bool: ...
    def mark_unavailable(self, ttl_seconds: int = DEFAULT_UNAVAILABLE_TTL_SECONDS) -> None: ...
    def mark_available(self) -> None: ...
    def record_error(self) -> bool:
        """One more provider error. Returns True when this one tripped the flag (three in a row)."""
    def record_success(self) -> None:
        """A good provider answer: forget the error streak and lift the flag."""
    def claim_alert(self, kind: str, window_seconds: int = DEFAULT_ALERT_WINDOW_SECONDS) -> bool:
        """True for the first caller per ``kind`` per window (so a wallet that stays empty alerts once an hour, not once a pull)."""


class RedisProviderAvailability:
    def __init__(self) -> None:
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis  # a dependency of arq; imported late so importing this module never needs a server

            self._client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        return self._client

    def is_available(self) -> bool:
        try:
            return not self._redis().exists(UNAVAILABLE_KEY)
        except Exception:  # noqa: BLE001 - any Redis trouble means "do not block the caregiver"
            logger.warning("kyc availability flag unreadable; treating Dojah as available", exc_info=True)
            return True

    def mark_unavailable(self, ttl_seconds: int = DEFAULT_UNAVAILABLE_TTL_SECONDS) -> None:
        try:
            self._redis().set(UNAVAILABLE_KEY, "1", ex=ttl_seconds)
        except Exception:  # noqa: BLE001
            logger.warning("kyc availability flag could not be set", exc_info=True)

    def mark_available(self) -> None:
        try:
            self._redis().delete(UNAVAILABLE_KEY)
        except Exception:  # noqa: BLE001
            logger.warning("kyc availability flag could not be cleared", exc_info=True)


    def record_error(self) -> bool:
        try:
            redis = self._redis()
            count = redis.incr(ERRORS_KEY)
            if count == 1:
                redis.expire(ERRORS_KEY, DEFAULT_UNAVAILABLE_TTL_SECONDS)
            if count >= CONSECUTIVE_ERRORS_TO_FLAG:
                self.mark_unavailable()
                return True
        except Exception:  # noqa: BLE001
            logger.warning("kyc provider error counter could not be updated", exc_info=True)
        return False

    def record_success(self) -> None:
        try:
            self._redis().delete(ERRORS_KEY, UNAVAILABLE_KEY)
        except Exception:  # noqa: BLE001
            logger.warning("kyc provider error counter could not be cleared", exc_info=True)

    def claim_alert(self, kind: str, window_seconds: int = DEFAULT_ALERT_WINDOW_SECONDS) -> bool:
        try:
            return bool(self._redis().set(ALERT_KEY_PREFIX + kind, "1", nx=True, ex=window_seconds))
        except Exception:  # noqa: BLE001 - alert anyway: a duplicate alert beats a missed one
            logger.warning("kyc alert throttle unreadable; alerting", exc_info=True)
            return True


_availability: Optional[ProviderAvailability] = None


def get_provider_availability() -> ProviderAvailability:
    global _availability
    if _availability is None:
        _availability = RedisProviderAvailability()
    return _availability
