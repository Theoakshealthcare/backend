"""The vitals flags engine (app/services/vital_flags.py): a pure function from one reading and what is known about the
patient to normal / high / low / critical, with the reason.

Every threshold below is the clinical lead's, from the clinician review pack (section 3, 2026-11): heart rate 60-100
(critical below 50), blood pressure 120/80 to 140/90 (low below 100/60, critical systolic below 90, flag above 130/80 for
diabetes or kidney disease), temperature 36.5-37.5 C, respiratory rate 12-20, glucose fasting 126 / random 200. Weight and
sleep are never flagged. No database here: these tests run without Postgres."""

import pytest

from app.services.vital_flags import (
    EMERGENCY_MESSAGE,
    PatientVitalContext,
    context_from_text,
    evaluate_reading,
    glucose_context_from_notes,
    is_out_of_range,
)

PLAIN = PatientVitalContext()
DIABETIC = PatientVitalContext(lowers_bp_ceiling=True)
BETA = PatientVitalContext(beta_blocker_for_hypertension=True)


def status(vital_type, value, unit=None, glucose_context=None, context=PLAIN):
    flag = evaluate_reading(vital_type, value, unit, glucose_context, context)
    return None if flag is None else flag.status


# --- heart rate ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("49", "critical"), ("49.9", "critical"), ("50", "low"), ("59", "low"), ("60", "normal"), ("72", "normal"),
    ("100", "normal"), ("101", "high"), ("180", "high"),
])
def test_heart_rate_bands(value, expected):
    assert status("heart_rate", value, "bpm") == expected


def test_heart_rate_critical_is_an_emergency_with_the_112_message():
    flag = evaluate_reading("heart_rate", "45", "bpm", None, PLAIN)
    assert flag.emergency is True and flag.message == EMERGENCY_MESSAGE and "112" in flag.message
    assert "50" in flag.reason


def test_heart_rate_high_and_low_are_not_emergencies():
    for value in ("55", "120"):
        flag = evaluate_reading("heart_rate", value, "bpm", None, PLAIN)
        assert flag.emergency is False and flag.message is None


def test_beta_blocker_patient_flags_a_heart_rate_below_60_without_naming_the_medicine():
    flag = evaluate_reading("heart_rate", "58", "bpm", None, BETA)
    assert flag.status == "low" and "beta" not in flag.reason.lower()
    assert status("heart_rate", "60", "bpm", context=BETA) == "normal"


# --- blood pressure --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("120/80", "normal"), ("140/90", "normal"), ("110/70", "normal"), ("100/60", "normal"),
    ("141/85", "high"), ("130/91", "high"), ("160/100", "high"),
    ("99/70", "low"), ("110/59", "low"), ("95/55", "low"),
    ("89/50", "critical"), ("70/40", "critical"), ("89.9/55", "critical"),
])
def test_blood_pressure_bands(value, expected):
    assert status("blood_pressure", value, "mmHg") == expected


def test_isolated_systolic_hypertension_with_a_low_bottom_number_is_high_not_low():
    assert status("blood_pressure", "160/55", "mmHg") == "high"


def test_blood_pressure_critical_is_an_emergency():
    flag = evaluate_reading("blood_pressure", "85/50", "mmHg", None, PLAIN)
    assert flag.emergency and flag.message == EMERGENCY_MESSAGE and "90" in flag.reason


def test_diabetes_or_kidney_disease_lowers_the_high_line_to_130_over_80():
    assert status("blood_pressure", "135/78", "mmHg", context=PLAIN) == "normal"
    assert status("blood_pressure", "135/78", "mmHg", context=DIABETIC) == "high"
    assert status("blood_pressure", "128/82", "mmHg", context=DIABETIC) == "high"
    assert status("blood_pressure", "130/80", "mmHg", context=DIABETIC) == "normal"
    assert "130/80" in evaluate_reading("blood_pressure", "135/78", "mmHg", None, DIABETIC).reason


def test_a_malformed_pressure_is_not_flagged():
    for bad in ("120", "banana", "300/20x", ""):
        assert status("blood_pressure", bad, "mmHg") is None


# --- temperature -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,unit,expected", [
    ("36.4", "°C", "low"), ("36.5", "°C", "normal"), ("37.0", "°C", "normal"), ("37.5", "°C", "normal"),
    ("37.6", "°C", "high"), ("39.2", "°C", "high"), ("35.0", "°C", "low"),
    ("97.7", "°F", "normal"), ("99.5", "°F", "normal"), ("97.0", "°F", "low"), ("100.4", "°F", "high"),
    ("36.8", None, "normal"),
])
def test_temperature_bands_in_celsius_and_fahrenheit(value, unit, expected):
    assert status("temperature", value, unit) == expected


def test_temperature_has_no_critical_level():
    assert status("temperature", "41", "°C") == "high"
    assert status("temperature", "32", "°C") == "low"


# --- respiratory rate ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("11", "low"), ("12", "normal"), ("16", "normal"), ("20", "normal"),
                                            ("21", "high"), ("30", "high"), ("6", "low")])
def test_respiratory_rate_bands(value, expected):
    assert status("respiratory_rate", value, "breaths/min") == expected


# --- blood glucose ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,unit,label,expected", [
    ("125", "mg/dL", "fasting", "normal"), ("126", "mg/dL", "fasting", "high"), ("150", "mg/dL", "fasting", "high"),
    ("199", "mg/dL", "random", "normal"), ("200", "mg/dL", "random", "high"), ("126", "mg/dL", "random", "normal"),
    ("199", "mg/dL", "after_meal", "normal"), ("200", "mg/dL", "after_meal", "high"),
    ("6.9", "mmol/L", "fasting", "normal"), ("7.0", "mmol/L", "fasting", "high"),
    ("11.0", "mmol/L", "random", "normal"), ("11.1", "mmol/L", "random", "high"),
    ("69", "mg/dL", "random", "low"), ("70", "mg/dL", "random", "normal"), ("3.8", "mmol/L", "fasting", "low"),
    ("92", "mg/dL", "fasting", "normal"),
])
def test_glucose_depends_on_how_the_reading_was_labelled(value, unit, label, expected):
    assert status("blood_glucose", value, unit, label) == expected


def test_an_unlabelled_glucose_reading_is_judged_as_a_random_one():
    # 150 fasting would be high; unlabelled it could be an after-meal value, so it must not raise a flag.
    assert status("blood_glucose", "150", "mg/dL", None) == "normal"
    assert status("blood_glucose", "210", "mg/dL", None) == "high"


def test_glucose_has_no_critical_level():
    assert status("blood_glucose", "550", "mg/dL", "random") == "high"
    assert status("blood_glucose", "30", "mg/dL", "random") == "low"


@pytest.mark.parametrize("notes,expected", [
    ("RBS", "random"), ("FBS", "fasting"), ("PP", "after_meal"), ("rbs", "random"), (" FBS ", "fasting"),
    ("random", "random"), ("Fasting", "fasting"), ("after meal", "after_meal"),
    (None, None), ("", None), ("felt dizzy", None), ("FBS and tired", None),
])
def test_the_glucose_label_is_read_from_the_notes_the_apps_already_write(notes, expected):
    assert glucose_context_from_notes(notes) == expected


# --- never flagged ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("vital_type,value", [("sleep_duration", "2"), ("sleep_duration", "7.5"), ("weight", "300"),
                                              ("banana", "1")])
def test_sleep_weight_and_unknown_types_get_no_flag(vital_type, value):
    assert evaluate_reading(vital_type, value, None, None, PLAIN) is None


def test_a_value_that_is_not_a_number_is_not_flagged():
    for vital_type in ("heart_rate", "temperature", "respiratory_rate", "blood_glucose"):
        assert status(vital_type, "banana") is None


# --- shape and the 1c hook -------------------------------------------------------------------------------------------

def test_a_flag_carries_a_label_a_reason_and_the_emergency_switch():
    flag = evaluate_reading("heart_rate", "110", "bpm", None, PLAIN)
    assert (flag.status, flag.label, flag.emergency) == ("high", "High", False)
    assert "100" in flag.reason
    normal = evaluate_reading("heart_rate", "72", "bpm", None, PLAIN)
    assert (normal.status, normal.label, normal.reason, normal.emergency) == ("normal", "Normal", None, False)


def test_is_out_of_range_is_true_for_high_low_and_critical_only():
    assert is_out_of_range(evaluate_reading("heart_rate", "110", "bpm", None, PLAIN))
    assert is_out_of_range(evaluate_reading("heart_rate", "55", "bpm", None, PLAIN))
    assert is_out_of_range(evaluate_reading("heart_rate", "40", "bpm", None, PLAIN))
    assert not is_out_of_range(evaluate_reading("heart_rate", "72", "bpm", None, PLAIN))
    assert not is_out_of_range(None)


# --- what the patient's record says ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Type 2 diabetes mellitus", True), ("Diabetes", True), ("DM, hypertension", True), ("T2DM", True),
    ("Chronic kidney disease stage 3", True), ("CKD", True), ("renal failure", True), ("on dialysis", True),
    ("Hypertension", False), ("arthritis", False), ("", False), (None, False), ("Admitted", False),
])
def test_diabetes_and_kidney_disease_are_read_from_the_conditions_text(text, expected):
    assert context_from_text(text, []).lowers_bp_ceiling is expected


@pytest.mark.parametrize("conditions,medicines,expected", [
    ("Hypertension", ["Metoprolol 50mg"], True),
    ("high blood pressure", ["atenolol"], True),
    ("HTN", ["Bisoprolol"], True),
    ("Hypertension", ["Carvedilol"], True),
    ("Hypertension", ["Amlodipine"], False),
    ("Arthritis", ["Metoprolol"], False),  # a beta blocker, but not for hypertension: the rule says hypertension
    ("Hypertension", [], False),
    (None, ["Metoprolol"], False),
])
def test_a_beta_blocker_for_hypertension_is_read_from_conditions_and_medicines(conditions, medicines, expected):
    assert context_from_text(conditions, medicines).beta_blocker_for_hypertension is expected
