"""Vitals flags: is one stored reading normal, high, low or critical for this patient, and why.

The thresholds are the clinical lead's, from the clinician review pack (section 3, signed off 2026-11) and mirrored in
``docs/plans/2026-11-25-clinician-phase2-build-map.md``. Change a number only on a clinician's say-so.

========================  ============  ==========================  =====================================
Vital                     Normal        Flag                        Critical (emergency, call 112)
========================  ============  ==========================  =====================================
Heart rate (bpm)          60-100        high > 100, low < 60        < 50
Blood pressure (mmHg)     120/80-140/90 high > 140/90, low < 100/60 systolic < 90
Temperature (C)           36.5-37.5     high > 37.5, low < 36.5     none
Respiratory rate (/min)   12-20         high > 20, low < 12         none
Blood glucose (mg/dL)     70-...        fasting >= 126, random >= 200  none
Weight, sleep             never flagged
========================  ============  ==========================  =====================================

Patient context (read from the profile and the active care plan, see ``patient_context``):
- diabetes or kidney disease: blood pressure above 130/80 is high;
- hypertension treated with a beta blocker: a heart rate below 60 is flagged (it already is for everyone; the reason says why).

Decisions where the pack was silent (each is a question for the clinical lead, listed in the SOP):
- glucose below 70 mg/dL (3.9 mmol/L) is Low, because the pack's normal band starts at 70;
- an unlabelled glucose reading, and an After-meal one, are judged by the Random rule (>= 200), so a normal after-meal
  value is never shown as High;
- a pressure that is both above the high line and below the low line (160/55) is High.

The module is pure: no database, no clock, no I/O, except ``patient_context`` and ``reading_flag``, which read the
patient's record. Weight, sleep, a type this module does not know, and a value that cannot be read as a number get
``None``: no flag, not even Normal. ``is_out_of_range`` is the hook the alerts slice (1c) calls.
"""
import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

NORMAL, HIGH, LOW, CRITICAL = "normal", "high", "low", "critical"
LABELS = {NORMAL: "Normal", HIGH: "High", LOW: "Low", CRITICAL: "Critical"}
EMERGENCY_MESSAGE = "This may be an emergency. Call 112 now."

# --- thresholds (the clinical lead's numbers) --------------------------------------------------------------------------
HEART_RATE_NORMAL = (60, 100)
HEART_RATE_CRITICAL_BELOW = 50
BP_HIGH_ABOVE = (140, 90)  # systolic, diastolic
BP_HIGH_ABOVE_DIABETES_RENAL = (130, 80)
BP_LOW_BELOW = (100, 60)
BP_CRITICAL_SYSTOLIC_BELOW = 90
TEMPERATURE_NORMAL_C = (36.5, 37.5)
RESPIRATORY_NORMAL = (12, 20)
# glucose: (fasting high at or above, random high at or above, low below), per unit
GLUCOSE = {"mg/dL": (126, 200, 70), "mmol/L": (7.0, 11.1, 3.9)}

GLUCOSE_CONTEXTS = ("random", "fasting", "after_meal")
_GLUCOSE_NOTE = {"rbs": "random", "random": "random", "fbs": "fasting", "fasting": "fasting", "pp": "after_meal",
                 "after meal": "after_meal", "after_meal": "after_meal"}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_PRESSURE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*")


@dataclass(frozen=True)
class PatientVitalContext:
    """What the engine needs to know about the patient. The default is "nothing known": the standard thresholds."""
    lowers_bp_ceiling: bool = False  # diabetes mellitus or kidney disease: high blood pressure starts above 130/80
    beta_blocker_for_hypertension: bool = False  # hypertension treated with a beta blocker


@dataclass(frozen=True)
class VitalFlag:
    status: str  # normal | high | low | critical
    label: str  # Normal | High | Low | Critical
    reason: Optional[str] = None  # null for normal
    emergency: bool = False  # true only for critical
    message: Optional[str] = None  # the emergency message, null unless emergency


def _flag(status: str, reason: Optional[str] = None) -> VitalFlag:
    emergency = status == CRITICAL
    return VitalFlag(status, LABELS[status], reason, emergency, EMERGENCY_MESSAGE if emergency else None)


def is_out_of_range(flag: Optional[VitalFlag]) -> bool:
    """True for high, low and critical. The alerts slice calls this on a stored reading's flag; normal and no flag are False."""
    return flag is not None and flag.status != NORMAL


def glucose_context_from_notes(notes: Optional[str]) -> Optional[str]:
    """The label of a glucose reading. Both apps store it as the reading's whole ``notes``: RBS (Random), FBS (Fasting) or
    PP (After meal). The long names are accepted too. Anything else (free text, empty) is no label."""
    return _GLUCOSE_NOTE.get((notes or "").strip().lower())


def _number(value: str) -> Optional[float]:
    text = (value or "").strip()
    return float(text) if _NUMBER.fullmatch(text) else None


def _shown(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _heart_rate(value: str, context: PatientVitalContext) -> Optional[VitalFlag]:
    bpm = _number(value)
    if bpm is None:
        return None
    if bpm < HEART_RATE_CRITICAL_BELOW:
        return _flag(CRITICAL, f"Heart rate below {HEART_RATE_CRITICAL_BELOW} bpm. Contact the family and arrange an ambulance.")
    low, high = HEART_RATE_NORMAL
    if bpm < low:
        why = f"Heart rate below {low} bpm."
        if context.beta_blocker_for_hypertension:
            why += " The patient takes a beta blocker for hypertension: tell the prescriber."
        return _flag(LOW, why)
    if bpm > high:
        return _flag(HIGH, f"Heart rate above {high} bpm. Tell the family or the patient's doctor for a medical review.")
    return _flag(NORMAL)


def _blood_pressure(value: str, context: PatientVitalContext) -> Optional[VitalFlag]:
    match = _PRESSURE.fullmatch(value or "")
    if not match:
        return None
    systolic, diastolic = float(match.group(1)), float(match.group(2))
    if systolic < BP_CRITICAL_SYSTOLIC_BELOW:
        return _flag(CRITICAL, f"Top number below {BP_CRITICAL_SYSTOLIC_BELOW} mmHg. Contact the family and arrange an ambulance.")
    high_sys, high_dia = BP_HIGH_ABOVE_DIABETES_RENAL if context.lowers_bp_ceiling else BP_HIGH_ABOVE
    if systolic > high_sys or diastolic > high_dia:
        why = f"Blood pressure above {high_sys}/{high_dia} mmHg."
        if context.lowers_bp_ceiling:
            why += " The limit is lower for diabetes or kidney disease."
        return _flag(HIGH, why + " Tell the family or the patient's doctor for a medical review.")
    low_sys, low_dia = BP_LOW_BELOW
    if systolic < low_sys or diastolic < low_dia:
        return _flag(LOW, f"Blood pressure below {low_sys}/{low_dia} mmHg.")
    return _flag(NORMAL)


def _celsius(value: float, unit: Optional[str]) -> float:
    # The apps still accept °F; the pack's band is in °C. One decimal, so 99.5 °F is exactly 37.5 °C.
    return round((value - 32) * 5 / 9, 1) if unit == "°F" else value


def _temperature(value: str, unit: Optional[str]) -> Optional[VitalFlag]:
    number = _number(value)
    if number is None:
        return None
    celsius = _celsius(number, unit)
    low, high = TEMPERATURE_NORMAL_C
    if celsius < low:
        return _flag(LOW, f"Temperature below {low} °C.")
    if celsius > high:
        return _flag(HIGH, f"Temperature above {high} °C.")
    return _flag(NORMAL)


def _respiratory_rate(value: str) -> Optional[VitalFlag]:
    rate = _number(value)
    if rate is None:
        return None
    low, high = RESPIRATORY_NORMAL
    if rate < low:
        return _flag(LOW, f"Breathing rate below {low} per minute.")
    if rate > high:
        return _flag(HIGH, f"Breathing rate above {high} per minute.")
    return _flag(NORMAL)


def _blood_glucose(value: str, unit: Optional[str], glucose_context: Optional[str]) -> Optional[VitalFlag]:
    number = _number(value)
    if number is None:
        return None
    unit = unit if unit in GLUCOSE else "mg/dL"
    fasting_high, random_high, low = GLUCOSE[unit]
    fasting = glucose_context == "fasting"
    limit = fasting_high if fasting else random_high
    if number >= limit:
        kind = "fasting" if fasting else "random"
        return _flag(HIGH, f"Blood glucose {_shown(limit)} {unit} or more ({kind}). "
                           "Tell the patient's doctor or book a review, and inform the family.")
    if number < low:
        return _flag(LOW, f"Blood glucose below {_shown(low)} {unit}.")
    return _flag(NORMAL)


def evaluate_reading(vital_type: str, value: str, unit: Optional[str], glucose_context: Optional[str],
                     context: PatientVitalContext) -> Optional[VitalFlag]:
    """The flag for one reading, or None when the vital is not flagged (weight, sleep, unknown) or the value is unreadable."""
    if vital_type == "heart_rate":
        return _heart_rate(value, context)
    if vital_type == "blood_pressure":
        return _blood_pressure(value, context)
    if vital_type == "temperature":
        return _temperature(value, unit)
    if vital_type == "respiratory_rate":
        return _respiratory_rate(value)
    if vital_type == "blood_glucose":
        return _blood_glucose(value, unit, glucose_context)
    return None


# --- what the patient's record says --------------------------------------------------------------------------------------

_DIABETES_OR_RENAL = re.compile(
    r"diabet|\bdm\b|\bt[12]dm\b|\brenal\b|kidney|\bckd\b|\besrd\b|\besrf\b|nephro|dialysis", re.IGNORECASE)
_HYPERTENSION = re.compile(r"hypertens|high blood pressure|\bhtn\b", re.IGNORECASE)
_BETA_BLOCKER = re.compile(
    r"\b(?:metoprolol|atenolol|bisoprolol|propranolol|carvedilol|nebivolol|labetalol|nadolol|sotalol|timolol|"
    r"pindolol|acebutolol|betaxolol|esmolol)\b|beta[\s-]?blocker", re.IGNORECASE)


def context_from_text(conditions: Optional[str], medicines: Iterable[str]) -> PatientVitalContext:
    """Reads the two rules' inputs from free text (the profile's conditions and medicines, the care plan's diagnosis and
    medicine names). A keyword match, deliberately: the record is typed by people, and a missed match only means the
    standard thresholds apply."""
    conditions = conditions or ""
    medicine_text = " ".join(m for m in medicines if m)
    return PatientVitalContext(
        lowers_bp_ceiling=bool(_DIABETES_OR_RENAL.search(conditions)),
        beta_blocker_for_hypertension=bool(_HYPERTENSION.search(conditions)
                                           and _BETA_BLOCKER.search(f"{conditions} {medicine_text}")),
    )


def patient_context(db: Session, patient_id: UUID) -> PatientVitalContext:
    """The context for a patient: the profile's conditions and medicines plus the active care plan's diagnosis and active
    medicines. Never raises: a record that cannot be read gives the standard thresholds (and a log line), because a vitals
    list must not fail over a context lookup."""
    from app.core.security import decrypt
    from app.db.models.care_plan import CarePlan, CarePlanMedication
    from app.db.models.patient import Patient

    try:
        patient = db.query(Patient).filter(Patient.id == patient_id).first()
        if patient is None:
            return PatientVitalContext()
        conditions = [patient.medical_conditions or ""]
        medicines = [patient.medications or ""]
        plan = (db.query(CarePlan).filter(CarePlan.patient_id == patient_id, CarePlan.status == "active").first())
        if plan is not None:
            if plan.diagnosis_encrypted:
                conditions.append(decrypt(plan.diagnosis_encrypted))
            for med in (db.query(CarePlanMedication)
                        .filter(CarePlanMedication.plan_id == plan.id, CarePlanMedication.status == "active")):
                if med.detail_encrypted:
                    medicines.append(str(json.loads(decrypt(med.detail_encrypted)).get("name") or ""))
        return context_from_text(" ; ".join(conditions), medicines)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("vital flags: patient context unavailable patient=%s", patient_id)
        return PatientVitalContext()


def reading_flag(vital, context: PatientVitalContext) -> Optional[VitalFlag]:
    """The flag of a stored ``PatientVital`` (or anything with vital_type, value, unit, notes)."""
    glucose = glucose_context_from_notes(vital.notes) if vital.vital_type == "blood_glucose" else None
    return evaluate_reading(vital.vital_type, vital.value, vital.unit, glucose, context)


def stored_reading_is_out_of_range(db: Session, vital) -> bool:
    """The hook for slice 1c (alerts): is this stored reading high, low or critical for this patient?"""
    return is_out_of_range(reading_flag(vital, patient_context(db, vital.patient_id)))


def flags_for(vitals: Sequence, context: PatientVitalContext) -> list:
    return [reading_flag(v, context) for v in vitals]


def vital_response(vital, context: PatientVitalContext) -> dict:
    """A stored ``PatientVital`` as the API returns it: the stored fields plus ``flag`` and ``glucose_context`` (additive)."""
    glucose = glucose_context_from_notes(vital.notes) if vital.vital_type == "blood_glucose" else None
    flag = evaluate_reading(vital.vital_type, vital.value, vital.unit, glucose, context)
    return {
        "id": vital.id, "patient_id": vital.patient_id, "recorded_by_user_id": vital.recorded_by_user_id,
        "source": vital.source.value if hasattr(vital.source, "value") else vital.source,
        "vital_type": vital.vital_type, "value": vital.value, "unit": vital.unit, "recorded_at": vital.recorded_at,
        "notes": vital.notes, "glucose_context": glucose, "flag": None if flag is None else asdict(flag),
    }
