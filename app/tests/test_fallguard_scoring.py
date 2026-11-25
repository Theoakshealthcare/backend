"""FallGuard v2.0 scoring (pure functions): the PDF's worked example, its rules, its item table and its safety requirements.

Where every expected value comes from (the PDF is "Oakly FallGuard v2.0 Developer Handover Package", 10 November 2026):
- ``APPENDIX_A_*``: copied from Appendix A, the only worked example the PDF contains.
- ``PDF_ITEM_TABLE``, ``APPENDIX_B``: copied from section 4's table and Appendix B (the config-parity test).
- every other vector: worked out by hand from the numbered rule named in its comment (section 5 rules 1 to 12, section 6, section 7);
  none was produced by running this code.

The PDF says its own suite has vectors TV1 to TV17 plus a 10,000-case property test. Those files were never supplied and the PDF
prints none of the vectors, so they are NOT reproduced here; the sign-off should ask for them (SOP, open questions).
"""

import copy
import random
import re
from datetime import date

import pytest

from app.services import fallguard_recommendations as recs
from app.services import fallguard_scoring as fg
from app.services.fallguard_scoring import ScoringError, score_assessment

# --- Appendix A, copied verbatim from the PDF ---------------------------------------------------------------------------------

APPENDIX_A_INPUT = {
    "date_of_birth": "1946-05-20",
    "assessment_date": "2026-11-10",
    "part_a": {"A1": 1, "A2": 0, "A3": 0, "A4": 0, "A5": 1, "A6": 2, "A7": 0},
    "part_b": {"B2": 1, "B3": 1, "B4": 1, "B5": 2, "B6": 1, "B7": 2,
               "B8": ["DIABETES", "ARTHRITIS"], "B9": 1, "B10": 0, "B11": 1, "B12": 0,
               "B13": 1, "B14": ["CARDIO_BP_DIURETIC"], "B15": 1, "B16": 0,
               "B17": ["LOOSE_RUGS", "NO_BACKUP_LIGHT"], "B18": 1, "B19": 0},
    "part_c": {"C1": 1, "C2": "SOMETIMES", "C5": 0, "C6": 0},
    "red_flags": {},
}

APPENDIX_A_OUTPUT = {
    "questionnaire_version": "2.0.0",
    "algorithm_version": "2.0.0",
    "age": 80,
    "urgent": False,
    "red_flags_triggered": [],
    "eligible": True,
    "gate_tier": "INTERMEDIATE",
    "gate_reasons": ["GATE_I_ONE_FALL", "GATE_I_UNSTEADY", "GATE_I_WORRY"],
    "score": 18,
    "score_tier": "HIGH",
    "final_tier": "HIGH",
    "incomplete": False,
    "provisional": False,
    "unknown_items": [],
    "part_b_points": {"B1": 2, "B2": 1, "B3": 1, "B4": 1, "B5": 2, "B6": 1, "B7": 2,
                      "B8": 1, "B9": 1, "B10": 0, "B11": 1, "B12": 0, "B13": 1, "B14": 1,
                      "B15": 1, "B16": 0, "B17": 1, "B18": 1, "B19": 0},
    "top_factors": ["B5", "B7", "B2"],
    "prompt_attention": False,
    "consequence_flag": True,
    "pharmacist_review_flag": False,
}

# Section 4's item table: id -> {code: points}; multi items -> count bands. Typed here independently of the JSON file.
PDF_ITEM_TABLE = {
    "A1": {0: 0, 1: 1, 2: 2}, "A2": {0: 0, 1: 1}, "A3": {0: 0, 1: 1}, "A4": {0: 0, 1: 1},
    "A5": {0: 0, 1: 1, 2: 2}, "A6": {0: 0, 1: 1, 2: 2}, "A7": {0: 0, 1: 1},
    "B2": {0: 0, 1: 1}, "B3": {0: 0, 1: 1, 2: 2}, "B4": {0: 0, 1: 1, 2: 2}, "B5": {0: 0, 1: 1, 2: 2},
    "B6": {0: 0, 1: 1, 2: 2}, "B7": {0: 0, 1: 1, 2: 2}, "B9": {0: 0, 1: 1, 2: 2}, "B10": {0: 0, 1: 1, 2: 2},
    "B11": {0: 0, 1: 1, 2: 2}, "B12": {0: 0, 1: 1, 2: 2}, "B13": {0: 0, 1: 1}, "B15": {0: 0, 1: 1, 2: 2},
    "B16": {0: 0, 1: 1, 2: 2}, "B18": {0: 0, 1: 1, 2: 2}, "B19": {0: 0, 1: 1},
    "C1": {0: 0, 1: 1}, "C3": {0: 0, 1: 1}, "C4": {0: 0, 1: 1}, "C5": {0: None, 1: None, 9: None},
    "C6": {0: None, 1: None, 9: None},
}
PDF_MULTI_CODES = {
    "B8": ["STROKE", "PARKINSONS", "DIABETES", "ARTHRITIS", "HEART", "DEPRESSION", "KIDNEY", "LUNG", "NONE"],
    "B14": ["SEDATIVES", "PSYCH", "OPIOIDS", "EPILEPSY", "CARDIO_BP_DIURETIC", "GLUCOSE_LOWERING",
            "SEDATING_ALLERGY_COUGH", "ANY_DROWSY_DIZZY", "NONE", "NOT_SURE"],
    "B17": ["LOOSE_RUGS", "UNEVEN_FLOORS", "CLUTTER", "NO_HANDRAIL", "BATHROOM_NO_AID", "POOR_LIGHT_NIGHT",
            "NO_BACKUP_LIGHT", "NONE"],
}
PDF_AGE_BANDS = [(0, 59, 0), (60, 74, 1), (75, 84, 2), (85, None, 3)]  # B1; "under 60 = 0"
APPENDIX_B = {"score.intermediate_from": 6, "score.high_from": 12, "score.max_unknown_before_incomplete": 3,
              "eligibility.min_age": 60}
PDF_MAX_PART_B = 36
PDF_BANNED_PATTERNS = ["you are safe", "is safe", "no risk", "will not fall", "clinically validated", "predict", "diagnos"]

ON = "2026-11-10"
DOB_70 = "1956-01-01"  # 70 on ON: B1 = 1 (60 to 74)
DOB_60 = "1966-11-10"  # exactly 60 on ON (birthday today)

ZERO_B = {"B2": 0, "B3": 0, "B4": 0, "B5": 0, "B6": 0, "B7": 0, "B8": ["NONE"], "B9": 0, "B10": 0, "B11": 0, "B12": 0,
          "B13": 0, "B14": ["NONE"], "B15": 0, "B16": 0, "B17": ["NONE"], "B18": 0, "B19": 0}
ZERO_A = {"A1": 0, "A4": 0, "A5": 0, "A6": 0}


def payload(dob=DOB_70, a=None, b=None, c=None, red_flags=None, on=ON):
    return {"date_of_birth": dob, "assessment_date": on, "part_a": copy.deepcopy(ZERO_A if a is None else a),
            "part_b": copy.deepcopy(ZERO_B if b is None else b), "part_c": copy.deepcopy(c or {}),
            "red_flags": red_flags or {}}


def with_b(**changes):
    return {**ZERO_B, **changes}


def err(p):
    with pytest.raises(ScoringError) as exc:
        score_assessment(p)
    return exc.value


# --- the worked example ----------------------------------------------------------------------------------------------------

def test_appendix_a_example_reproduces_the_pdfs_output_exactly():
    assert score_assessment(APPENDIX_A_INPUT) == APPENDIX_A_OUTPUT


def test_appendix_a_part_b_points_add_up_to_the_pdfs_score():
    assert sum(APPENDIX_A_OUTPUT["part_b_points"].values()) == APPENDIX_A_OUTPUT["score"] == 18


# --- config parity (section 4's table, Appendix B) ------------------------------------------------------------------------------

def test_every_single_choice_item_has_the_pdfs_codes_and_points():
    for item_id, table in PDF_ITEM_TABLE.items():
        item = fg.ITEMS[item_id]
        assert {o["code"]: o["points"] for o in item["options"]} == table, item_id


def test_multi_items_have_the_pdfs_codes_and_count_bands():
    for item_id, codes in PDF_MULTI_CODES.items():
        assert [o["code"] for o in fg.ITEMS[item_id]["options"]] == codes
    assert fg.ITEMS["B8"]["count_bands"] == fg.ITEMS["B17"]["count_bands"] == [
        {"min": 0, "max": 0, "points": 0}, {"min": 1, "max": 2, "points": 1}, {"min": 3, "max": None, "points": 2}]
    assert fg.ITEMS["B14"]["count_bands"] == [
        {"min": 0, "max": 0, "points": 0}, {"min": 1, "max": 1, "points": 1}, {"min": 2, "max": None, "points": 2}]
    assert fg.ITEMS["B14"]["not_sure_points"] == 1


def test_c2_codes_and_c8_enumerations_match_the_pdf():
    assert [o["code"] for o in fg.ITEMS["C2"]["options"]] == ["DAILY", "SOMETIMES", "RARELY"]
    assert fg.ITEMS["C8"]["fields"] == {"sex": ["F", "M", "OTHER", "NO_ANSWER"],
                                        "education": ["NONE", "PRIMARY", "SECONDARY", "TERTIARY", "NO_ANSWER"]}


def test_age_bands_and_conditions_match_the_pdf():
    assert [(b["min"], b["max"], b["points"]) for b in fg.ITEMS["B1"]["age_bands"]] == PDF_AGE_BANDS
    assert fg.ITEMS["A2"]["show_if"] == fg.ITEMS["A3"]["show_if"] == fg.ITEMS["A7"]["show_if"] == \
        {"item": "A1", "op": ">=", "value": 1}
    assert fg.ITEMS["C3"]["show_if"] == {"item": "B2", "op": "==", "value": 1}
    assert [i for i in fg.ITEM_ORDER if fg.ITEMS[i]["required"]] == ["A1", "A4", "A5", "A6"]
    assert [i for i in fg.ITEM_ORDER if fg.ITEMS[i]["allows_unknown"]] == [
        i for i in fg.PART_B_IDS if i != "B1"]  # Part B except the derived age allows UNKNOWN


def test_item_set_is_a1_to_c9_in_order():
    expected = [f"A{n}" for n in range(1, 8)] + [f"B{n}" for n in range(1, 20)] + [f"C{n}" for n in range(1, 10)]
    assert fg.ITEM_ORDER == expected
    assert fg.RED_FLAG_IDS == [f"R{n}" for n in range(1, 8)]  # R1 to R7


def test_thresholds_match_appendix_b_and_max_part_b_is_36():
    s = fg.QUESTIONNAIRE["scoring"]
    assert (s["intermediate_from"], s["high_from"], s["max_unknown_before_incomplete"], s["min_age"]) == (
        APPENDIX_B["score.intermediate_from"], APPENDIX_B["score.high_from"],
        APPENDIX_B["score.max_unknown_before_incomplete"], APPENDIX_B["eligibility.min_age"])
    assert s["max_part_b"] == PDF_MAX_PART_B
    worst = fg.ITEMS  # every Part B item at its highest points
    top = 0
    for item_id in fg.PART_B_IDS:
        item = worst[item_id]
        if item["type"] == "derived":
            top += max(b["points"] for b in item["age_bands"])
        elif item["type"] == "multi":
            top += max(b["points"] for b in item["count_bands"])
        else:
            top += max(o["points"] for o in item["options"])
    assert top == PDF_MAX_PART_B


def test_the_bands_cover_every_count_so_scoring_cannot_hit_a_gap():
    for item_id in ("B8", "B14", "B17"):
        for n in range(0, 12):
            assert fg._band_points(fg.ITEMS[item_id]["count_bands"], n) in (0, 1, 2)
    for age in range(0, 121):
        assert fg._band_points(fg.ITEMS["B1"]["age_bands"], age) in (0, 1, 2, 3)


# --- rule 1: eligibility and age -------------------------------------------------------------------------------------------------

def test_under_60_has_no_score_and_no_tier_but_red_flags_still_work():
    r = score_assessment(payload(dob="1966-11-11", red_flags={"R2": True}))  # 59 on ON: birthday is tomorrow
    assert r["age"] == 59 and r["eligible"] is False
    assert (r["score"], r["gate_tier"], r["score_tier"], r["final_tier"]) == (None, None, None, None)
    assert r["urgent"] is True and r["red_flags_triggered"] == ["R2"]


def test_exactly_60_on_the_assessment_date_is_eligible():
    r = score_assessment(payload(dob=DOB_60))
    assert r["age"] == 60 and r["eligible"] is True and r["part_b_points"]["B1"] == 1


def test_age_is_completed_years_including_a_leap_day_birthday():
    assert fg.calculate_age(date(1960, 2, 29), date(2026, 2, 28)) == 65
    assert fg.calculate_age(date(1960, 2, 29), date(2026, 3, 1)) == 66


@pytest.mark.parametrize("age,points", [(60, 1), (74, 1), (75, 2), (84, 2), (85, 3), (99, 3)])
def test_b1_bands_from_age(age, points):
    r = score_assessment(payload(dob=f"{2026 - age}-01-01"))  # the birthday has already passed on 2026-11-10
    assert r["age"] == age and r["part_b_points"]["B1"] == points


# --- rule 2: required answers ----------------------------------------------------------------------------------------------------

def test_a1_a4_a5_a6_are_required():
    assert err(payload(a={})).code == "fallguard_missing_required"
    assert err(payload(a={})).fields == {"A1": "required", "A4": "required", "A5": "required", "A6": "required"}


def test_a2_a3_a7_are_required_only_when_a1_is_1_or_more():
    assert err(payload(a={**ZERO_A, "A1": 1})).fields == {"A2": "required", "A3": "required", "A7": "required"}
    assert err(payload(a={**ZERO_A, "A1": 2, "A2": 0})).fields == {"A3": "required", "A7": "required"}
    score_assessment(payload(a={**ZERO_A, "A1": 1, "A2": 0, "A3": 0, "A7": 0}))  # complete: no error


def test_an_answer_to_a_hidden_item_is_ignored_and_counts_as_zero():
    r = score_assessment(payload(a={**ZERO_A, "A2": 1, "A3": 1, "A7": 1}))  # A1 = 0 hides A2, A3, A7
    assert r["gate_tier"] == "LOW" and r["gate_reasons"] == [] and r["prompt_attention"] is False


# --- rule 3: gate tier --------------------------------------------------------------------------------------------------------------

def gate(**a):
    base = {**ZERO_A, **a}
    if base["A1"] >= 1:
        base = {"A2": 0, "A3": 0, "A7": 0, **base}
    r = score_assessment(payload(a=base))
    return r["gate_tier"], r["gate_reasons"]


@pytest.mark.parametrize("answers,tier", [
    ({"A1": 2}, "HIGH"),
    ({"A1": 1, "A2": 1}, "HIGH"),
    ({"A1": 1, "A3": 1}, "HIGH"),
    ({"A4": 1}, "HIGH"),
    ({"A1": 1, "A5": 2}, "HIGH"),
    ({"A1": 1, "A5": 1}, "INTERMEDIATE"),
    ({"A1": 1}, "INTERMEDIATE"),
    ({"A5": 1}, "INTERMEDIATE"),
    ({"A5": 2}, "INTERMEDIATE"),  # HIGH needs a fall as well as A5 = 2
    ({"A6": 2}, "INTERMEDIATE"),
    ({"A6": 1}, "LOW"),
    ({}, "LOW"),
])
def test_gate_tier_table_from_rule_3(answers, tier):
    assert gate(**answers)[0] == tier


def test_gate_reasons_are_the_rules_that_fired_in_rule_order():
    assert gate(A1=1, A5=1, A6=2) == ("INTERMEDIATE", ["GATE_I_ONE_FALL", "GATE_I_UNSTEADY", "GATE_I_WORRY"])  # Appendix A
    assert gate(A5=1)[1] == ["GATE_I_UNSTEADY"]
    assert gate(A6=2)[1] == ["GATE_I_WORRY"]
    assert gate() == ("LOW", [])
    tier, reasons = gate(A1=2, A4=1)
    assert tier == "HIGH" and len(reasons) == 2  # two HIGH rules fired; their code names are placeholders (SOP)


# --- rules 4 to 7: Part B total, score tier, incomplete, final tier ---------------------------------------------------------------------

def test_the_best_possible_answers_score_only_the_age_point():
    r = score_assessment(payload())
    assert r["score"] == 1 and r["score_tier"] == "LOW" and r["final_tier"] == "LOW" and r["unknown_items"] == []
    assert r["top_factors"] == []  # B1 is never a top factor


@pytest.mark.parametrize("b,score,tier", [
    (with_b(B3=1, B4=1, B5=1, B6=1), 5, "LOW"),                       # 1 (age) + 4
    (with_b(B3=1, B4=1, B5=1, B6=1, B7=1), 6, "INTERMEDIATE"),        # cut-off 6
    (with_b(B3=2, B4=2, B5=2, B6=2, B7=2), 11, "INTERMEDIATE"),       # 1 + 10
    (with_b(B3=2, B4=2, B5=2, B6=2, B7=2, B9=1), 12, "HIGH"),         # cut-off 12
])
def test_score_tier_cut_offs_6_and_12(b, score, tier):
    r = score_assessment(payload(b=b))
    assert (r["score"], r["score_tier"]) == (score, tier)


def test_the_worst_possible_answers_score_36():
    worst = {"B2": 1, "B3": 2, "B4": 2, "B5": 2, "B6": 2, "B7": 2, "B8": ["STROKE", "PARKINSONS", "DIABETES"], "B9": 2,
             "B10": 2, "B11": 2, "B12": 2, "B13": 1, "B14": ["SEDATIVES", "PSYCH"], "B15": 2, "B16": 2,
             "B17": ["LOOSE_RUGS", "CLUTTER", "NO_HANDRAIL"], "B18": 2, "B19": 1}
    r = score_assessment(payload(dob="1930-01-01", b=worst))  # 96: B1 = 3
    assert r["score"] == 36 and r["score_tier"] == "HIGH"


@pytest.mark.parametrize("codes,points", [
    (["NONE"], 0), (["STROKE"], 1), (["STROKE", "LUNG"], 1), (["STROKE", "LUNG", "HEART"], 2),
    (["STROKE", "PARKINSONS", "DIABETES", "ARTHRITIS", "HEART", "DEPRESSION", "KIDNEY", "LUNG"], 2)])
def test_b8_points_by_count(codes, points):
    assert score_assessment(payload(b=with_b(B8=codes)))["part_b_points"]["B8"] == points


@pytest.mark.parametrize("codes,points,flag", [
    (["NONE"], 0, False), (["OPIOIDS"], 1, False), (["OPIOIDS", "PSYCH"], 2, False), (["SEDATIVES", "PSYCH", "OPIOIDS"], 2, False),
    (["NOT_SURE"], 1, True)])
def test_b14_points_by_count_and_not_sure_scores_1_and_sets_the_pharmacist_flag(codes, points, flag):
    r = score_assessment(payload(b=with_b(B14=codes)))
    assert r["part_b_points"]["B14"] == points and r["pharmacist_review_flag"] is flag
    assert "B14" not in r["unknown_items"]  # NOT_SURE is not an unknown answer for the incomplete count (section 17)


@pytest.mark.parametrize("codes,points", [(["NONE"], 0), (["CLUTTER"], 1), (["CLUTTER", "NO_HANDRAIL"], 1),
                                          (["CLUTTER", "NO_HANDRAIL", "LOOSE_RUGS"], 2)])
def test_b17_points_by_count(codes, points):
    assert score_assessment(payload(b=with_b(B17=codes)))["part_b_points"]["B17"] == points


def test_a_skipped_unknown_or_unanswered_part_b_item_scores_1_never_0():  # rule 4, SR-7
    omitted = {k: v for k, v in ZERO_B.items() if k != "B3"}
    for b in (omitted, with_b(B3="UNKNOWN")):
        r = score_assessment(payload(b=b))
        assert r["part_b_points"]["B3"] == 1 and r["unknown_items"] == ["B3"] and r["score"] == 2


def test_unknown_works_for_multi_select_items_too():
    r = score_assessment(payload(b=with_b(B8="UNKNOWN", B17="UNKNOWN")))
    assert r["part_b_points"]["B8"] == r["part_b_points"]["B17"] == 1 and r["unknown_items"] == ["B8", "B17"]


def test_no_part_b_at_all_is_every_item_unknown_and_provisional():
    r = score_assessment(payload(b={}))
    assert len(r["unknown_items"]) == 18 and r["incomplete"] is True and r["score"] == 1 + 18  # B1 is never unknown
    assert r["score_tier"] == "HIGH"


def test_incomplete_means_more_than_3_unknown_items():  # rule 6
    three = {k: v for k, v in ZERO_B.items() if k not in ("B3", "B4", "B5")}
    four = {k: v for k, v in ZERO_B.items() if k not in ("B3", "B4", "B5", "B6")}
    r3, r4 = score_assessment(payload(b=three)), score_assessment(payload(b=four))
    assert r3["incomplete"] is False and r3["provisional"] is False
    assert r4["incomplete"] is True and r4["provisional"] is True


def test_not_sure_on_b14_does_not_count_towards_incomplete():
    three = {k: v for k, v in ZERO_B.items() if k not in ("B3", "B4", "B5")}
    r = score_assessment(payload(b={**three, "B14": ["NOT_SURE"]}))
    assert r["unknown_items"] == ["B3", "B4", "B5"] and r["incomplete"] is False


def test_an_incomplete_result_is_never_lowered_below_the_gate_tier():  # rules 6 and 7
    four = {k: v for k, v in ZERO_B.items() if k not in ("B3", "B4", "B5", "B6")}
    r = score_assessment(payload(a={**ZERO_A, "A4": 1}, b=four))  # score 1 + 4 = 5 (LOW), gate HIGH
    assert r["incomplete"] and r["score_tier"] == "LOW" and r["gate_tier"] == "HIGH" and r["final_tier"] == "HIGH"


def test_final_tier_is_the_higher_of_gate_and_score():  # rule 7
    r = score_assessment(payload(a={**ZERO_A, "A5": 1}))
    assert (r["gate_tier"], r["score_tier"], r["final_tier"]) == ("INTERMEDIATE", "LOW", "INTERMEDIATE")
    r = score_assessment(payload(b=with_b(B3=2, B4=2, B5=2, B6=2, B7=2, B9=1)))
    assert (r["gate_tier"], r["score_tier"], r["final_tier"]) == ("LOW", "HIGH", "HIGH")


# --- rule 12: top factors ---------------------------------------------------------------------------------------------------

def test_top_factors_are_the_three_highest_part_b_items_by_points_ties_by_item_order():
    r = score_assessment(payload(dob="1930-01-01", b=with_b(B3=1, B5=2, B9=2, B12=2, B16=2, B18=1)))
    assert r["top_factors"] == ["B5", "B9", "B12"]  # B1 (age, 3 points) is excluded; B16 ties but comes later
    r = score_assessment(payload(b=with_b(B7=1, B2=1)))
    assert r["top_factors"] == ["B2", "B7"]  # only items above 0, in item order on a tie


# --- rules 8 to 11 and section 6: red flags and the flags -------------------------------------------------------------------------------

@pytest.mark.parametrize("item", ["R2", "R3", "R4", "R5", "R6", "R7"])
def test_any_of_r2_to_r7_makes_urgent_and_never_changes_the_tier(item):
    base = score_assessment(payload())
    r = score_assessment(payload(red_flags={item: True}))
    assert r["urgent"] is True and r["red_flags_triggered"] == [item]
    assert (r["gate_tier"], r["score_tier"], r["final_tier"]) == (base["gate_tier"], base["score_tier"], base["final_tier"])


@pytest.mark.parametrize("c5", [1, 9, None])
def test_r1_with_blood_thinners_yes_unsure_or_unanswered_is_urgent(c5):
    c = {} if c5 is None else {"C5": c5}
    r = score_assessment(payload(c=c, red_flags={"R1": True}))
    assert r["urgent"] is True and r["red_flags_triggered"] == ["R1"]


def test_r1_with_no_blood_thinners_is_prompt_attention_not_urgent():
    r = score_assessment(payload(c={"C5": 0}, red_flags={"R1": True}))
    assert r["urgent"] is False and r["red_flags_triggered"] == [] and r["prompt_attention"] is True


def test_red_flags_answered_no_or_left_out_do_nothing():
    r = score_assessment(payload(red_flags={"R1": False, "R2": "no", "R3": None}))
    assert r["urgent"] is False and r["prompt_attention"] is False and r["red_flags_triggered"] == []
    assert score_assessment(payload(red_flags={"R4": "YES"}))["urgent"] is True


def test_evaluate_red_flags_matches_the_section_6_table():
    assert fg.evaluate_red_flags({"R1": True}, 0) == {"urgent": False, "triggered": [], "prompt_attention": True}
    assert fg.evaluate_red_flags({"R1": True, "R6": True}, 0) == {"urgent": True, "triggered": ["R6"], "prompt_attention": True}
    assert fg.evaluate_red_flags(None, None) == {"urgent": False, "triggered": [], "prompt_attention": False}


def pa(**a):
    return {**ZERO_A, "A1": 1, "A2": 0, "A3": 0, "A7": 0, **a}


@pytest.mark.parametrize("a,expected", [
    (pa(A7=1, A2=1), True),   # rule 9
    (pa(A7=1), False),        # a recent fall without an injury
    (pa(A2=1), False),        # an injury without a fall in the last 4 weeks
    (pa(A4=1), True),
    (ZERO_A, False)])
def test_prompt_attention_flag_rule_9(a, expected):
    assert score_assessment(payload(a=a))["prompt_attention"] is expected


@pytest.mark.parametrize("c,expected", [
    ({"C5": 1}, True), ({"C5": 9}, True), ({"C6": 1}, True), ({"C6": 9}, True), ({"C5": 0, "C6": 0}, False),
    ({"C1": 1}, True),                       # a missing C2 counts as not daily
    ({"C1": 1, "C2": "SOMETIMES"}, True), ({"C1": 1, "C2": "RARELY"}, True), ({"C1": 1, "C2": "DAILY"}, False),
    ({"C1": 0, "C2": "RARELY"}, False), ({"C2": "RARELY"}, False), ({}, False)])
def test_consequence_flag_rule_10_never_changes_the_tier(c, expected):
    r = score_assessment(payload(c=c))
    assert r["consequence_flag"] is expected and r["final_tier"] == "LOW"


def test_pharmacist_review_flag_rule_11():
    assert score_assessment(payload(c={"C7": ["Placeholder herbal tea"]}))["pharmacist_review_flag"] is True
    assert score_assessment(payload(c={"C7": []}))["pharmacist_review_flag"] is False
    assert score_assessment(payload(c={"C7": ["  "]}))["pharmacist_review_flag"] is False
    assert score_assessment(payload(b=with_b(B14=["NOT_SURE"])))["pharmacist_review_flag"] is True
    assert score_assessment(payload(b=with_b(B14=["OPIOIDS"])))["pharmacist_review_flag"] is False


def test_c3_is_hidden_unless_b2_is_yes_and_c8_c9_never_affect_the_result():
    base = score_assessment(payload())
    r = score_assessment(payload(c={"C3": 1, "C8": {"sex": "F", "education": "TERTIARY"}, "C9": {"mode": "SELF"}}))
    assert {k: v for k, v in r.items()} == base


# --- validation: no result, no tier -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,code,field", [
    (lambda p: p["part_a"].update(A1=3), "fallguard_invalid_option", "A1"),
    (lambda p: p["part_a"].update(A1=True), "fallguard_invalid_option", "A1"),
    (lambda p: p["part_a"].update(A1=1.0), "fallguard_invalid_option", "A1"),
    (lambda p: p["part_a"].update(A5="UNKNOWN"), "fallguard_invalid_option", "A5"),
    (lambda p: p["part_a"].update(A8=0), "fallguard_unknown_question", "A8"),
    (lambda p: p["part_a"].update(B2=0), "fallguard_unknown_question", "B2"),
    (lambda p: p["part_b"].update(B1=1), "fallguard_unknown_question", "B1"),
    (lambda p: p["part_b"].update(B20=1), "fallguard_unknown_question", "B20"),
    (lambda p: p["part_b"].update(B3=5), "fallguard_invalid_option", "B3"),
    (lambda p: p["part_b"].update(B8=[]), "fallguard_invalid_option", "B8"),
    (lambda p: p["part_b"].update(B8=["NONE", "STROKE"]), "fallguard_invalid_option", "B8"),
    (lambda p: p["part_b"].update(B8=["STROKE", "STROKE"]), "fallguard_invalid_option", "B8"),
    (lambda p: p["part_b"].update(B8=["NOT_SURE"]), "fallguard_invalid_option", "B8"),  # only B14 has NOT_SURE
    (lambda p: p["part_b"].update(B8="STROKE"), "fallguard_invalid_option", "B8"),
    (lambda p: p["part_b"].update(B14=["NOT_SURE", "OPIOIDS"]), "fallguard_invalid_option", "B14"),
    (lambda p: p["part_c"].update(C2="WEEKLY"), "fallguard_invalid_option", "C2"),
    (lambda p: p["part_c"].update(C5=2), "fallguard_invalid_option", "C5"),
    (lambda p: p["part_c"].update(C8={"sex": "X"}), "fallguard_invalid_option", "C8"),
    (lambda p: p["red_flags"].update(R9=True), "fallguard_unknown_question", "R9"),
    (lambda p: p["red_flags"].update(R2="maybe"), "fallguard_invalid_option", "R2"),
    (lambda p: p.update(date_of_birth="not-a-date"), "fallguard_invalid_date_of_birth", "date_of_birth"),
    (lambda p: p.update(date_of_birth="2030-01-01"), "fallguard_invalid_date_of_birth", "date_of_birth"),
    (lambda p: p.update(date_of_birth=None), "fallguard_missing_required", "date_of_birth"),
    (lambda p: p.update(assessment_date="31/12/2026"), "fallguard_invalid_assessment_date", "assessment_date"),
    (lambda p: p.update(surprise=1), "fallguard_invalid_payload", "surprise"),
])
def test_invalid_requests_are_refused_with_a_code_and_the_field(mutate, code, field):
    p = payload()
    mutate(p)
    e = err(p)
    assert e.code == code and field in e.fields
    assert e.question_id == (None if field in ("date_of_birth", "assessment_date") else field)


def test_a_payload_that_is_not_an_object_is_refused():
    assert err([]).code == "fallguard_invalid_payload"


# --- safety requirements ------------------------------------------------------------------------------------------------------------------------

def _random_payload(rng):
    a1 = rng.choice([0, 1, 2])
    a = {"A1": a1, "A4": rng.choice([0, 1]), "A5": rng.choice([0, 1, 2]), "A6": rng.choice([0, 1, 2])}
    if a1 >= 1:
        a.update(A2=rng.choice([0, 1]), A3=rng.choice([0, 1]), A7=rng.choice([0, 1]))
    b = {}
    for item_id in fg.PART_B_IDS:
        item = fg.ITEMS[item_id]
        if item["type"] == "derived" or rng.random() < 0.15:
            continue  # left out = unanswered
        if item["type"] == "multi":
            codes = [o["code"] for o in item["options"]]
            if rng.random() < 0.1:
                b[item_id] = "UNKNOWN"
            elif rng.random() < 0.3:
                b[item_id] = [rng.choice([c for c in codes if c in item["exclusive_codes"]] or ["NONE"])]
            else:
                pool = [c for c in codes if c not in item["exclusive_codes"]]
                b[item_id] = rng.sample(pool, rng.randint(1, len(pool)))
        else:
            b[item_id] = "UNKNOWN" if rng.random() < 0.1 else rng.choice([o["code"] for o in item["options"]])
    year = rng.randint(1900, 1966)
    return payload(dob=f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}", a=a, b=b,
                   c={"C5": rng.choice([0, 1, 9])}, red_flags={f"R{n}": rng.random() < 0.2 for n in range(1, 8)})


def test_sr4_the_final_tier_is_never_below_the_gate_tier_in_10000_random_cases():
    rng = random.Random(20261110)
    for _ in range(10_000):
        r = score_assessment(_random_payload(rng))
        if r["eligible"]:
            assert fg.TIER_RANK[r["final_tier"]] >= fg.TIER_RANK[r["gate_tier"]]
            assert fg.TIER_RANK[r["final_tier"]] >= fg.TIER_RANK[r["score_tier"]]
            assert r["final_tier"] in (r["gate_tier"], r["score_tier"])
            assert r["provisional"] is r["incomplete"] is (len(r["unknown_items"]) > 3)
            assert r["score"] == sum(r["part_b_points"].values()) <= PDF_MAX_PART_B


def test_sr7_an_unknown_part_b_answer_never_scores_zero():
    rng = random.Random(7)
    for _ in range(2_000):
        r = score_assessment(_random_payload(rng))
        for item_id in r["unknown_items"]:
            assert r["part_b_points"][item_id] == 1


def test_sr3_scoring_is_deterministic_and_does_not_change_its_input():
    p = copy.deepcopy(APPENDIX_A_INPUT)
    assert score_assessment(p) == score_assessment(p) and p == APPENDIX_A_INPUT


# --- section 7: recommendations ------------------------------------------------------------------------------------------------------------------------

def rec_ids(p):
    result = score_assessment(p)
    return [r["id"] for r in recs.applicable(p, result)]


def test_appendix_a_example_gets_the_catalogue_entries_its_answers_fire():
    # Worked by hand from section 7's "fires when" column for Appendix A's input: HIGH, score 18, consequence flag, no prompt.
    assert rec_ids(APPENDIX_A_INPUT) == [
        "REC-CLINICAL", "REC-EXERCISE", "REC-BOOK-NURSE", "REC-MEDS", "REC-DIZZY", "REC-FALLPLAN", "REC-HOME",
        "REC-VISION", "REC-FOOT", "REC-BLADDER", "REC-PAIN", "REC-AID", "REC-FEAR", "REC-OUTDOOR", "REC-ACTIVE"]


def test_a_low_result_with_nothing_else_gets_only_rec_low_and_it_is_never_with_other_tiers():
    assert rec_ids(payload()) == ["REC-LOW"]
    for p in (payload(a={**ZERO_A, "A5": 1}), payload(a={**ZERO_A, "A4": 1})):
        assert "REC-LOW" not in rec_ids(p)


def test_recommendations_are_sorted_by_priority_and_de_duplicated():
    p = APPENDIX_A_INPUT
    out = recs.applicable(p, score_assessment(p))
    assert [r["priority"] for r in out] == sorted(r["priority"] for r in out)
    assert len({r["id"] for r in out}) == len(out)


def test_rec_book_nurse_fires_for_intermediate_only_when_the_score_is_9_to_11():
    nine = with_b(B3=2, B4=2, B5=2, B6=1, B7=1)         # 1 + 8 = 9
    eleven = with_b(B3=2, B4=2, B5=2, B6=2, B7=2)       # 1 + 10 = 11
    five = with_b(B3=1, B4=1, B5=1, B6=1)               # 1 + 4 = 5 with a gate-raised INTERMEDIATE
    six = with_b(B3=1, B4=1, B5=1, B6=1, B7=1)
    assert "REC-BOOK-NURSE" in rec_ids(payload(b=nine)) and "REC-BOOK-NURSE" in rec_ids(payload(b=eleven))
    assert "REC-BOOK-NURSE" not in rec_ids(payload(b=six))
    assert "REC-BOOK-NURSE" not in rec_ids(payload(a={**ZERO_A, "A5": 1}, b=five))
    assert "REC-BOOK-NURSE" in rec_ids(payload(a={**ZERO_A, "A4": 1}))  # HIGH from the gate alone


def test_prompt_and_fall_plan_entries_follow_the_flags():
    ids = rec_ids(payload(a={**ZERO_A, "A4": 1}, c={"C5": 1}))
    assert ids[0] == "REC-PROMPT" and "REC-FALLPLAN" in ids and "REC-CLINICAL" in ids


@pytest.mark.parametrize("b,c,entry", [
    (with_b(B13=1), {}, "REC-MEDS"), (with_b(B14=["OPIOIDS"]), {}, "REC-MEDS"), (with_b(B14=["NOT_SURE"]), {}, "REC-MEDS"),
    (ZERO_B, {"C7": ["Placeholder remedy"]}, "REC-MEDS"), (with_b(B4=1), {}, "REC-DIZZY"),
    (with_b(B17=["CLUTTER"]), {}, "REC-HOME"), (with_b(B11=1), {}, "REC-VISION"), (ZERO_B, {"C4": 1}, "REC-VISION"),
    (with_b(B6=1), {}, "REC-FOOT"), (with_b(B7=1), {}, "REC-FOOT"), (with_b(B9=1), {}, "REC-BLADDER"),
    (with_b(B5=1), {}, "REC-PAIN"), (with_b(B12=1), {}, "REC-HEARING"), (with_b(B2=1), {"C3": 0}, "REC-AID"),
    (with_b(B2=1), {}, "REC-AID"), (with_b(B10=1), {}, "REC-MEMORY"), (with_b(B18=1), {}, "REC-OUTDOOR"),
    (with_b(B15=1), {}, "REC-ACTIVE"), (with_b(B19=1), {}, "REC-NUTRITION"), (with_b(B16=2), {}, "REC-ALCOHOL")])
def test_entries_fire_on_their_section_7_conditions(b, c, entry):
    assert entry in rec_ids(payload(b=b, c=c))


def test_entries_stay_quiet_when_their_condition_is_not_met():
    quiet = rec_ids(payload(b=with_b(B2=1), c={"C3": 1}))  # a fitted walking aid: no REC-AID
    assert "REC-AID" not in quiet
    assert "REC-ALCOHOL" not in rec_ids(payload(b=with_b(B16=1)))  # only "most days" (2) fires
    assert "REC-NUTRITION" not in rec_ids(payload())
    assert "REC-FEAR" not in rec_ids(payload(a={**ZERO_A, "A6": 0}))
    assert "REC-FEAR" in rec_ids(payload(a={**ZERO_A, "A6": 1}))


def test_an_unknown_part_b_answer_fires_its_entries_under_the_conservative_reading():
    omitted = {k: v for k, v in ZERO_B.items() if k != "B4"}
    assert recs.UNKNOWN_PART_B_FIRES_ENTRIES is True and "REC-DIZZY" in rec_ids(payload(b=omitted))


def test_an_under_age_result_has_no_recommendations():
    p = payload(dob="1990-01-01")
    assert recs.applicable(p, score_assessment(p)) == []


def test_split_top_gives_three_and_all():
    p = APPENDIX_A_INPUT
    out = recs.split_top(recs.applicable(p, score_assessment(p)))
    assert [r["id"] for r in out["top"]] == ["REC-CLINICAL", "REC-EXERCISE", "REC-BOOK-NURSE"] and len(out["all"]) == 15


def test_every_catalogue_entry_has_a_rule_and_a_priority_and_the_pdfs_20_plus_low():
    ids = [r["id"] for r in recs.CATALOGUE]
    assert len(ids) == len(set(ids)) == 21 and "REC-LOW" in ids


# --- content: copy lint (Appendix B copy.banned_patterns) --------------------------------------------------------------------------------------------

def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def test_no_user_facing_string_matches_a_banned_pattern():  # SR-6
    shown = {k: recs.CONTENT[k] for k in ("messages", "welcome", "recommendations")}
    for s in _strings(shown):
        for pattern in PDF_BANNED_PATTERNS:
            assert not re.search(re.escape(pattern), s, re.IGNORECASE), (pattern, s)


def test_content_carries_the_pdfs_appendix_c_keys_and_the_default_emergency_number():
    assert set(recs.CONTENT["messages"]) == {
        "result.low", "result.intermediate", "result.high", "provisional.label", "overridden.label", "prompt.message",
        "urgent.message", "error.safety", "referral.under60", "disclaimer", "carer.alert", "notify.checkin", "notify.reassess"}
    assert recs.CONTENT["emergency_number"]["default"] == "112"
    assert recs.CONTENT["messages"]["notify.checkin"] == "You have a FallGuard check-in."  # SR-11: generic text only
