"""Price calculation in integer kobo.

One function prices a booking preview, a booking and a prescription, so what the patient is
shown, what we store and what we send to Paystack can never drift apart. Arithmetic is
Decimal end to end; every amount that leaves this module is an integer number of kobo,
rounded half-up. ``total = service_fee + platform_fee`` always holds exactly.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

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


class MissingMultiplier(ValueError):
    """The service has no explicit multiplier in the booking settings, so it cannot be priced (WA-FR-21)."""


def explicit_multiplier(settings: Any, service_type: str) -> Any:
    """The multiplier configured for ``service_type``. Raises ``MissingMultiplier`` instead of defaulting to
    1.0: a service nobody priced must not be sold at the base rate by accident."""
    multipliers = getattr(settings, "multipliers", None) or {}
    value = multipliers.get(service_type)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MissingMultiplier(service_type)
    return value


def offered_services(settings: Any, candidates: "tuple[str, ...]") -> "list[str]":
    """The candidates that have an explicit multiplier, in the order given."""
    offered = []
    for service in candidates:
        try:
            explicit_multiplier(settings, service)
        except MissingMultiplier:
            continue
        offered.append(service)
    return offered


def price_service(settings: Any, service_type: str, hours: int) -> PriceBreakdown:
    """Price ``hours`` of ``service_type`` from a ``BookingSettings`` row. The same ``compute_price`` and the
    same surge and discount rules as the app's preview and booking creation, but strict about the multiplier."""
    return compute_price(
        base_fee_kobo=settings.base_fee_kobo,
        quantity=hours,
        multiplier=explicit_multiplier(settings, service_type),
        surge_multiplier=settings.surge_multiplier if (settings.surge_pricing and settings.surge_multiplier) else None,
        discount_percent=settings.discount,
        platform_fee_percent=settings.platform_fee_percent,
    )
