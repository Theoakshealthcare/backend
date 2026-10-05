from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional


def to_kobo(amount_naira: Any) -> int:
    """Naira -> integer kobo, rounded half-up.

    The single conversion used both when we ask Paystack to charge (initialize) and when we
    compare what Paystack says was paid (settlement), so the two can never disagree.
    Goes through str() so float noise (0.1 + 0.2) doesn't shift the result.
    """
    return int((Decimal(str(amount_naira)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_kobo_or_none(amount_naira: Any) -> Optional[int]:
    if amount_naira is None:
        return None
    try:
        return to_kobo(amount_naira)
    except Exception:
        return None
