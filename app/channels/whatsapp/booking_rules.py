"""Booking rules the chat and the Flow share: services, cities, durations, slots, dates and money formats.

Everything here is a pure function of the settings and an explicit ``now`` (UTC, naive: the way the rest of the
backend stores times), so the Flow endpoint, the quote and the tests all agree and none of them reads a clock.
Times the person sees and types are Lagos time (WAT, UTC+1, no daylight saving).
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings

LAGOS = ZoneInfo("Africa/Lagos")

# The order services are offered in, and what the person sees. Every service is offered; one without a configured
# multiplier is priced at the default of 1.0, the same as in the app (see services/pricing.service_multiplier).
SERVICE_ORDER: Tuple[str, ...] = ("nurse", "caregiver", "physiotherapist", "doctor", "nutritionist")
SERVICE_LABELS: Dict[str, str] = {
    "nurse": "Nurse",
    "caregiver": "Caregiver",
    "physiotherapist": "Physiotherapist",
    "doctor": "Doctor home visit",
    "nutritionist": "Nutritionist",
}

# The city list is a setting; these tables only say how a served city is stored on a booking and where the
# placeholder pin goes when nobody shares a location (the booking is then held for an address check).
CITY_STATE: Dict[str, str] = {"Lagos": "Lagos", "Abuja": "FCT"}
CITY_CENTRE: Dict[str, Tuple[float, float]] = {"Lagos": (6.5244, 3.3792), "Abuja": (9.0765, 7.3986)}

# Where a geocoded address must fall to count as being in the served city: (south, north, west, east), generous
# enough for the built-up area and its suburbs, tight enough that another city or state is outside.
CITY_BOUNDS: Dict[str, Tuple[float, float, float, float]] = {
    "Lagos": (6.38, 6.72, 3.10, 3.72),
    "Abuja": (8.85, 9.25, 7.00, 7.65),
}

BOOKING_WINDOW_DAYS = 30  # WA-FR-13: today and up to 30 days ahead
NIGERIA_LAT = (4.0, 14.0)
NIGERIA_LNG = (2.5, 15.0)


def service_cities() -> List[str]:
    return [c.strip() for c in settings.WHATSAPP_SERVICE_CITIES.split(",") if c.strip()]


def canonical_city(raw: Optional[str]) -> Optional[str]:
    """The served city ``raw`` names (case-insensitive), or None when it is not one of them."""
    if not isinstance(raw, str):
        return None
    wanted = raw.strip().lower()
    return next((c for c in service_cities() if c.lower() == wanted), None)


def max_hours(service: str) -> int:
    for item in settings.WHATSAPP_SERVICE_MAX_HOURS.split(","):
        name, _, value = item.partition(":")
        if name.strip() == service and value.strip().isdigit():
            return max(1, min(24, int(value)))
    return max(1, min(24, settings.WHATSAPP_DEFAULT_MAX_HOURS))


def pin_is_plausible(lat: object, lng: object) -> bool:
    return (isinstance(lat, (int, float)) and isinstance(lng, (int, float))
            and not isinstance(lat, bool) and not isinstance(lng, bool)
            and NIGERIA_LAT[0] <= lat <= NIGERIA_LAT[1] and NIGERIA_LNG[0] <= lng <= NIGERIA_LNG[1])


def in_city(city: Optional[str], lat: object, lng: object) -> bool:
    """Is the point inside the box of a served city? False for a city we have no box for."""
    box = CITY_BOUNDS.get(city or "")
    if box is None or not pin_is_plausible(lat, lng):
        return False
    south, north, west, east = box
    return south <= lat <= north and west <= lng <= east


# --- time ------------------------------------------------------------------------------------
def to_lagos(utc_naive: datetime) -> datetime:
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(LAGOS)


def lagos_to_utc(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    local = datetime.combine(day, time(hour, minute), tzinfo=LAGOS)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def today_lagos(now: datetime) -> date:
    return to_lagos(now).date()


def date_range(now: datetime) -> Tuple[date, date]:
    start = today_lagos(now)
    return start, start + timedelta(days=BOOKING_WINDOW_DAYS)


def slots_for(day: date, now: datetime) -> List[str]:
    """Start times (HH:MM, Lagos time) offered on ``day``: hourly, within opening hours, at least
    WHATSAPP_MIN_LEAD_HOURS from now. Empty for a day outside the booking window."""
    first, last = date_range(now)
    if not (first <= day <= last):
        return []
    earliest = now + timedelta(hours=settings.WHATSAPP_MIN_LEAD_HOURS)
    out = []
    for hour in range(settings.WHATSAPP_SLOT_START_HOUR, settings.WHATSAPP_SLOT_END_HOUR + 1):
        hhmm = f"{hour:02d}:00"
        if lagos_to_utc(day, hhmm) >= earliest:
            out.append(hhmm)
    return out


def first_bookable_day(now: datetime) -> Optional[date]:
    first, last = date_range(now)
    day = first
    while day <= last:
        if slots_for(day, now):
            return day
        day += timedelta(days=1)
    return None


def slot_title(hhmm: str) -> str:
    hour = int(hhmm.split(":")[0])
    return f"{hour % 12 or 12}:00 {'AM' if hour < 12 else 'PM'}"


def parse_day(raw: object) -> Optional[date]:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


# --- display -----------------------------------------------------------------------------------
def naira(kobo: int) -> str:
    """₦44,000 (whole naira) or ₦44,000.50 (when there are kobo). Money is integer kobo everywhere else."""
    whole, rest = divmod(int(kobo), 100)
    return f"₦{whole:,}" + (f".{rest:02d}" if rest else "")


def format_when(start_utc: datetime, hours: int) -> str:
    """Sat 4 Oct, 10:00-14:00 (Lagos time)."""
    start = to_lagos(start_utc)
    end = start + timedelta(hours=hours)
    day = f"{start:%a} {start.day} {start:%b}"
    return f"{day}, {start:%H:%M}–{end:%H:%M}"
