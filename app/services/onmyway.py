"""Did the caregiver really set out when they tapped "on my way"?

A late cancellation fee is paid in part to the caregiver only when the tap was *qualifying* (A3 pays it; this module
only decides). Two conditions, both about what we can see:

1. the tap came no earlier than ``onmyway_compensation_window_minutes`` before the start (a tap hours ahead of a visit
   shows nothing about whether they are really on the way), and
2. a location ping *after* the tap shows them closer to the patient by at least ``MIN_PROGRESS_M`` than they were at
   the tap.

Both are pure functions of stored facts (the tap time, the distance at the tap, the latest ping), so the decision can
be made under the booking lock without any other call.
"""

import math
from datetime import datetime, timedelta
from typing import Optional

MIN_PROGRESS_M = 300  # "moved toward the patient": at least this much closer than at the tap


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (Haversine)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def moved_toward(start_distance_m: float, now_distance_m: float, min_progress_m: float = MIN_PROGRESS_M) -> bool:
    return (start_distance_m - now_distance_m) >= min_progress_m


def qualifies(*, on_my_way_at: Optional[datetime], scheduled_date: datetime, window_minutes: int,
              start_distance_m: Optional[float], ping_at: Optional[datetime], ping_distance_m: Optional[float],
              min_progress_m: float = MIN_PROGRESS_M) -> bool:
    if on_my_way_at is None or start_distance_m is None or ping_at is None or ping_distance_m is None:
        return False
    if on_my_way_at < scheduled_date - timedelta(minutes=window_minutes):
        return False
    if ping_at <= on_my_way_at:
        return False
    return moved_toward(start_distance_m, ping_distance_m, min_progress_m)
