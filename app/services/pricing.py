"""Price calculation in integer kobo.

One function prices a booking preview, a booking and a prescription, so what the patient is
shown, what we store and what we send to Paystack can never drift apart. Arithmetic is
Decimal end to end; every amount that leaves this module is an integer number of kobo,
rounded half-up. ``total = service_fee + platform_fee`` always holds exactly.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def _dec(value: Any, default: str = "0") -> Decimal:
    # via str(): settings multipliers/percentages are floats in the DB; this drops binary noise
    return Decimal(str(value)) if value is not None else Decimal(default)


def _kobo(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class PriceBreakdown:
    base_calculation_kobo: int
    surge_adjustment_kobo: int
    discount_savings_kobo: int
    service_fee_kobo: int
    platform_fee_kobo: int
    total_kobo: int


def compute_price(
    *,
    base_fee_kobo: int,
    quantity: int = 1,
    multiplier: Any = 1.0,
    surge_multiplier: Optional[Any] = None,
    discount_percent: Optional[Any] = None,
    platform_fee_percent: Any = 0,
) -> PriceBreakdown:
    """Price ``quantity`` units of ``base_fee_kobo``.

    ``surge_multiplier`` is applied only when truthy (pass None when surge is off);
    ``discount_percent`` only when positive.
    """
    base = Decimal(int(base_fee_kobo)) * quantity * _dec(multiplier, "1")
    service = base

    surge_adjustment = Decimal("0")
    if surge_multiplier:
        surge = _dec(surge_multiplier)
        surge_adjustment = service * (surge - _ONE)
        service = service * surge

    discount_savings = Decimal("0")
    if discount_percent and _dec(discount_percent) > 0:
        discount_savings = service * (_dec(discount_percent) / _HUNDRED)
        service = service - discount_savings

    platform = service * (_dec(platform_fee_percent) / _HUNDRED)

    service_kobo = _kobo(service)
    platform_kobo = _kobo(platform)
    return PriceBreakdown(
        base_calculation_kobo=_kobo(base),
        surge_adjustment_kobo=_kobo(surge_adjustment),
        discount_savings_kobo=_kobo(discount_savings),
        service_fee_kobo=service_kobo,
        platform_fee_kobo=platform_kobo,
        total_kobo=service_kobo + platform_kobo,
    )


DEFAULT_MULTIPLIER = 1.0
MAX_MULTIPLIER = 10.0  # a typo (150 for 1.5) must not be sold at 150x; above this counts as not configured


def service_multiplier(settings: Any, service_type: str, *, channel: str = "app") -> float:
    """The price multiplier for ``service_type``: the one in the booking settings, else ``DEFAULT_MULTIPLIER``.

    The single lookup for every channel (the app's preview and booking, WhatsApp quotes and the Flow's price
    list), so they cannot quietly disagree. A service nobody has configured yet (admins will manage multipliers
    later) is sold at the default rather than refused; a missing, non-numeric, boolean or non-positive stored
    value, or one above ``MAX_MULTIPLIER``, counts as "not configured". Falling back is flagged with a structured warning (service and channel
    only, no personal data) so ops can see which services still run on the default.
    """
    multipliers = getattr(settings, "multipliers", None) or {}
    value = multipliers.get(service_type) if isinstance(multipliers, dict) else None
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_MULTIPLIER:
        logger.warning("pricing: no usable multiplier configured, using default %s service=%s channel=%s",
                       DEFAULT_MULTIPLIER, service_type, channel)
        return DEFAULT_MULTIPLIER
    return value


def price_service(settings: Any, service_type: str, hours: int, *, channel: str = "whatsapp") -> PriceBreakdown:
    """Price ``hours`` of ``service_type`` from a ``BookingSettings`` row: ``compute_price`` with the shared
    multiplier lookup and the same surge and discount rules as the app's preview and booking creation."""
    return compute_price(
        base_fee_kobo=settings.base_fee_kobo,
        quantity=hours,
        multiplier=service_multiplier(settings, service_type, channel=channel),
        surge_multiplier=settings.surge_multiplier if (settings.surge_pricing and settings.surge_multiplier) else None,
        discount_percent=settings.discount,
        platform_fee_percent=settings.platform_fee_percent,
    )
