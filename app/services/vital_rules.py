"""What a vital reading may look like when it is saved (POST /vitals/, PUT /vitals/{id}).

The caregiver app and the patient app share one vocabulary: six ``vital_type`` keys, the unit they print next to
the number, glucose context (RBS / FBS / PP) and sleep times in ``notes``. This module accepts exactly that and refuses
what is plainly a typing mistake; it never judges whether a reading is normal. The bounds are the patient app's entry
ranges (the caregiver app only checks "a positive number", so these are tighter than what it checked before, by design),
except sleep, which keeps the caregiver app's rule (more than 0, at most 24 hours).

Only writes are checked. Rows saved before this existed (a ``banana``, a ``300/20``) are still listed, downloaded and
deleted as before.
"""
import re
from dataclasses import dataclass
from typing import Optional, Tuple

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_PRESSURE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*")


class VitalRuleError(ValueError):
    """The reading is refused; ``str(error)`` is a sentence for the person who typed it."""


@dataclass(frozen=True)
class VitalSpec:
    label: str
    units: Tuple[str, ...]  # the first is the default when no unit is sent
    low: dict  # unit -> smallest plausible value
    high: dict  # unit -> largest plausible value
    low_open: bool = False  # True: the value must be MORE than low (sleep)


VITAL_SPECS = {
    "heart_rate": VitalSpec("Heart rate", ("bpm",), {"bpm": 20}, {"bpm": 250}),
    "blood_pressure": VitalSpec("Blood pressure", ("mmHg",), {"mmHg": 50}, {"mmHg": 260}),  # systolic; see below
    "temperature": VitalSpec("Temperature", ("°C", "°F"), {"°C": 30, "°F": 86}, {"°C": 45, "°F": 113}),
    "respiratory_rate": VitalSpec("Respiratory rate", ("breaths/min",), {"breaths/min": 4}, {"breaths/min": 60}),
    "blood_glucose": VitalSpec("Blood glucose", ("mg/dL", "mmol/L"), {"mg/dL": 20, "mmol/L": 1.1},
                               {"mg/dL": 600, "mmol/L": 33.3}),
    "sleep_duration": VitalSpec("Sleep duration", ("hrs",), {"hrs": 0}, {"hrs": 24}, low_open=True),
}
VITAL_TYPES = tuple(sorted(VITAL_SPECS))

DIASTOLIC_RANGE = (30, 160)
NOTES_MAX = 500


def _show(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def check_reading(vital_type: str, value: str, unit: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """Returns the type, the value (trimmed; blood pressure as ``120/80``) and the unit to store, or raises VitalRuleError."""
    vital_type = (vital_type or "").strip()
    spec = VITAL_SPECS.get(vital_type)
    if spec is None:
        raise VitalRuleError(f"vital_type must be one of: {', '.join(VITAL_TYPES)}.")
    unit = unit.strip() if isinstance(unit, str) and unit.strip() else None
    if unit is not None and unit not in spec.units:
        raise VitalRuleError(f"{spec.label} is measured in {' or '.join(spec.units)}, not '{unit}'.")
    unit_for_range = unit or spec.units[0]
    text = (value or "").strip()

    if vital_type == "blood_pressure":
        match = _PRESSURE.fullmatch(text)
        if not match:
            raise VitalRuleError("Blood pressure must be two numbers written like 120/80 (top number first).")
        systolic, diastolic = float(match.group(1)), float(match.group(2))
        if not spec.low["mmHg"] <= systolic <= spec.high["mmHg"]:
            raise VitalRuleError(f"The top (systolic) number must be between {spec.low['mmHg']} and {spec.high['mmHg']} mmHg.")
        if not DIASTOLIC_RANGE[0] <= diastolic <= DIASTOLIC_RANGE[1]:
            raise VitalRuleError(f"The bottom (diastolic) number must be between {DIASTOLIC_RANGE[0]} and {DIASTOLIC_RANGE[1]} mmHg.")
        if systolic <= diastolic:
            raise VitalRuleError("The top (systolic) number must be higher than the bottom (diastolic) number.")
        return vital_type, f"{match.group(1)}/{match.group(2)}", unit

    if not _NUMBER.fullmatch(text):
        raise VitalRuleError(f"{spec.label} must be a number such as 72 or 36.8.")
    number = float(text)
    low, high = spec.low[unit_for_range], spec.high[unit_for_range]
    below = number <= low if spec.low_open else number < low
    if below or number > high:
        lower = f"more than {_show(low)}" if spec.low_open else f"at least {_show(low)}"
        raise VitalRuleError(f"{spec.label} must be {lower} and at most {_show(high)} {unit_for_range}.")
    return vital_type, text, unit


def check_notes(notes: Optional[str]) -> None:
    if notes is not None and len(notes) > NOTES_MAX:
        raise VitalRuleError(f"notes must be at most {NOTES_MAX} characters.")
