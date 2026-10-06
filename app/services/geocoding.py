"""Turn a typed address into coordinates, only when the answer is good enough to send a caregiver to.

``Geocoder`` is the seam: ``GoogleGeocoder`` (Google Geocoding API), ``NoopGeocoder`` (no key configured, so
every address is "no result" and the caller falls back to its own rule) and a fake in ``app/tests``.

Rules that hold for every implementation
- Only the address text (street and area) and the city, state and country are sent. Never a name, a phone
  number, a condition or anything else about the person.
- Nothing raises. A timeout, an HTTP error, a quota error or a body we cannot read is "no result" (``None``),
  logged by kind only: never the address, never the key.
- Only a confident answer is returned: street or premise level, not a guess for the city or an area, and not
  a partial match of what was typed. Whether the point is inside the right city is the caller's decision.

The Geocoding API takes the key and the address in the query string and httpx logs request URLs at INFO, so
this module raises the httpx logger to WARNING when it is imported.
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Google's ``location_type`` for the points we trust (APPROXIMATE is a guess, usually a city or an area centre),
# and the result ``types`` that mean a street or a building rather than an area.
ACCEPTED_LOCATION_TYPES = frozenset({"ROOFTOP", "RANGE_INTERPOLATED", "GEOMETRIC_CENTER"})
ACCEPTED_RESULT_TYPES = frozenset({"street_address", "premise", "subpremise", "route", "intersection"})
# A bare road comes back as GEOMETRIC_CENTER, the midpoint of a possibly very long road: only a house-level
# location type is good enough for it.
ROUTE_LOCATION_TYPES = frozenset({"ROOFTOP", "RANGE_INTERPOLATED"})
MAX_VIEWPORT_DIAGONAL_M = 500.0  # a result that covers more than this is an area, not a place to send someone

# Test hook only. None in production, where httpx uses its real transport.
_transport: Optional[httpx.BaseTransport] = None


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lng: float
    precision: str  # Google's location_type, lower-cased: rooftop / range_interpolated / geometric_center
    admin_area: Optional[str] = None  # the state Google puts the point in (administrative_area_level_1)


class Geocoder(Protocol):
    enabled: bool

    def geocode(self, address: str, *, city: str, state: str) -> Optional[GeocodeResult]:
        """The coordinates of a typed address in ``city``, ``state``, Nigeria, or None. Never raises."""


class NoopGeocoder:
    """No key configured: always no result."""

    enabled = False

    def geocode(self, address: str, *, city: str, state: str) -> Optional[GeocodeResult]:
        return None


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class GoogleGeocoder:
    enabled = True

    def __init__(self, api_key: str, *, timeout: float = 3.0, transport: Optional[httpx.BaseTransport] = None):
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport if transport is not None else _transport
        self._deadline = timeout + 1.0  # the whole call, whatever the per-phase timeouts add up to

    def __repr__(self) -> str:  # the key stays out of tracebacks and logs
        return "GoogleGeocoder()"

    def geocode(self, address: str, *, city: str, state: str) -> Optional[GeocodeResult]:
        params = {"address": f"{address}, {city}, {state}, Nigeria", "components": "country:NG", "region": "ng",
                  "key": self._api_key}
        try:
            body = _POOL.submit(self._fetch, params).result(timeout=self._deadline)
        except FutureTimeout:
            logger.warning("geocoding: no answer within %.1fs; no result", self._deadline)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("geocoding: provider answered HTTP %s; no result", exc.response.status_code)
            return None
        except (httpx.HTTPError, ValueError) as exc:  # timeouts, connection errors, a body that is not JSON
            logger.warning("geocoding: request failed (%s); no result", type(exc).__name__)
            return None
        return _accept(body)

    def _fetch(self, params: dict) -> Any:
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            response = client.get(GEOCODE_URL, params=params)
        response.raise_for_status()
        return response.json()


_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="geocode")  # lets a call be abandoned at its deadline


def _diagonal_m(viewport: Any) -> Optional[float]:
    try:
        ne, sw = viewport["northeast"], viewport["southwest"]
        lat1, lng1, lat2, lng2 = (_number(v) for v in (ne["lat"], ne["lng"], sw["lat"], sw["lng"]))
    except (KeyError, TypeError):
        return None
    if None in (lat1, lng1, lat2, lng2):
        return None
    dy = math.radians(lat1 - lat2) * 6371000
    dx = math.radians(lng1 - lng2) * 6371000 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def _admin_area(best: dict) -> Optional[str]:
    for part in best.get("address_components") or []:
        if isinstance(part, dict) and "administrative_area_level_1" in (part.get("types") or []):
            name = part.get("long_name")
            return name if isinstance(name, str) else None
    return None


def _accept(body: Any) -> Optional[GeocodeResult]:
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    if status != "OK":
        if status != "ZERO_RESULTS":  # quota, denied key, bad request: ops should see these
            logger.warning("geocoding: provider status %s; no result", status)
        return None
    results = body.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    best = results[0]
    geometry = best.get("geometry") if isinstance(best.get("geometry"), dict) else {}
    location_type = geometry.get("location_type")
    types = best.get("types") if isinstance(best.get("types"), list) else []
    location = geometry.get("location") if isinstance(geometry.get("location"), dict) else {}
    lat, lng = _number(location.get("lat")), _number(location.get("lng"))
    if (location_type not in ACCEPTED_LOCATION_TYPES or not ACCEPTED_RESULT_TYPES.intersection(types)
            or best.get("partial_match") or lat is None or lng is None):
        return None
    if "route" in types and not {"street_address", "premise", "subpremise"}.intersection(types) \
            and location_type not in ROUTE_LOCATION_TYPES:
        return None
    diagonal = _diagonal_m(geometry.get("viewport"))
    if diagonal is None or diagonal > MAX_VIEWPORT_DIAGONAL_M:
        return None
    return GeocodeResult(lat=lat, lng=lng, precision=str(location_type).lower(), admin_area=_admin_area(best))


def build_geocoder() -> Geocoder:
    key = (settings.GOOGLE_MAPS_API_KEY or "").strip()
    if not key:
        return NoopGeocoder()
    return GoogleGeocoder(key, timeout=settings.GEOCODING_TIMEOUT_SECONDS)
