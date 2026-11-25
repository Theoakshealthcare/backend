"""Hand-derived checks of the handover PDF's rules against the vendored reference scoring (``app.services.fallguard_scoring``).

The package's own suite (TV1-TV17, properties, parity) is ``test_fallguard_scoring.py``. This file adds what it does not
cover, each expected value copied from the PDF (Appendix A, section 4's table, Appendix B) or worked out by hand from the
numbered rule named in the comment; none was produced by running the code. Where a hand-derived expectation first written
against our own re-implementation disagreed with the reference, the reference won (see the SOP, "What changed when the
reference arrived").
"""

import copy
from datetime import date

import pytest

from app.services import fallguard_scoring as fg
from app.services.fallguard_scoring import ValidationError, score_assessment

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

# Section 4's table typed independently of the JSON: id -> codes. Points equal the codes for the Part B single items.
PDF_B_SINGLE_MAX = {"B2": 1, "B3": 2, "B4": 2, "B5": 2, "B6": 2, "B7": 2, "B9": 2, "B10": 2, "B11": 2, "B12": 2,
                    "B13": 1, "B15": 2, "B16": 2, "B18": 2, "B19": 1}
PDF_A_MAX = {"A1": 2, "A2": 1, "A3": 1, "A4": 1, "A5": 2, "A6": 2, "A7": 1}
PDF_C_CODES = {"C1": [0, 1], "C2": ["DAILY", "SOMETIMES", "RARELY"], "C3": [0, 1], "C4": [0, 1], "C5": [0, 1, 9],
               "C6": [0, 1, 9]}

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
    with pytest.raises(ValidationError) as exc:
        score_assessment(p)
    return exc.value


# --- the worked example --------------------------------------------------------------------------------------------------------

def test_appendix_a_example_reproduces_the_pdfs_output_exactly():
    assert score_assessment(APPENDIX_A_INPUT) == APPENDIX_A_OUTPUT


def test_appendix_a_part_b_points_add_up_to_the_pdfs_score():
    assert sum(APPENDIX_A_OUTPUT["part_b_points"].values()) == APPENDIX_A_OUTPUT["score"] == 18


# --- the supplied JSON against the PDF's section 4 table (the package's parity test checks it against the engine) ---------------------

def test_the_supplied_questionnaire_has_the_pdfs_items_codes_and_points():
    items = {i["id"]: i for i in fg_questionnaire()["items"]}
    for item_id, top in {**PDF_A_MAX, **PDF_B_SINGLE_MAX}.items():
        assert [o["code"] for o in items[item_id]["options"]] == list(range(top + 1)), item_id
    for item_id, top in PDF_B_SINGLE_MAX.items():
        assert [o["points"] for o in items[item_id]["options"]] == list(range(top + 1)), item_id
    for item_id, codes in PDF_C_CODES.items():
        assert [o["code"] for o in items[item_id]["options"]] == codes, item_id
    assert items["B14"]["scoring"]["bands"] == [{"min_count": 0, "points": 0}, {"min_count": 1, "points": 1},
                                                 {"min_count": 2, "points": 2}]  # 0 = 0, 1 = 1, 2+ = 2
    assert items["B8"]["scoring"]["bands"] == items["B17"]["scoring"]["bands"] == [
        {"min_count": 0, "points": 0}, {"min_count": 1, "points": 1}, {"min_count": 3, "points": 2}]  # 0, 1-2, 3+
    assert [b["min_age"] for b in items["B1"]["scoring"]["bands"]] == [0, 60, 75, 85]
    assert items["C3"]["show_if"] == {"item": "B2", "op": "==", "value": 1}
    assert [i for i, it in items.items() if it["required"] and it["part"] == "A"] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    assert fg_questionnaire()["score_config"] == {"intermediate_from": 6, "high_from": 12,
                                                  "max_unknown_before_incomplete": 3, "min_age": 60}  # Appendix B


def fg_questionnaire():
    from app.services import fallguard_questionnaire

    return fallguard_questionnaire.QUESTIONNAIRE


# --- rule 1: eligibility and age -----------------------------------------------------------------------------------------------------

def test_under_60_has_no_score_and_no_tier_but_red_flags_still_work():
    r = score_assessment(payload(dob="1966-11-11", red_flags={"R2": True}))  # 59 on ON: birthday is tomorrow
    assert r["age"] == 59 and r["eligible"] is False
    assert (r["score"], r["gate_tier"], r["score_tier"], r["final_tier"]) == (None, None, None, None)
    assert r["urgent"] is True and r["red_flags_triggered"] == ["R2"]
    assert r["referral_message_key"] == "UNDER_60_REFERRAL"


def test_exactly_60_on_the_assessment_date_is_eligible():
    r = score_assessment(payload(dob=DOB_60))
    assert r["age"] == 60 and r["eligible"] is True and r["part_b_points"]["B1"] == 1


def test_age_is_completed_years_including_a_leap_day_birthday():
    assert fg.age_on(date(1960, 2, 29), date(2026, 2, 28)) == 65
    assert fg.age_on(date(1960, 2, 29), date(2026, 3, 1)) == 66


@pytest.mark.parametrize("age,points", [(60, 1), (74, 1), (75, 2), (84, 2), (85, 3), (99, 3)])
def test_b1_bands_from_age(age, points):
    r = score_assessment(payload(dob=f"{2026 - age}-01-01"))  # the birthday has already passed on 2026-11-10
    assert r["age"] == age and r["part_b_points"]["B1"] == points


# --- rule 2: required answers ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["A1", "A4", "A5", "A6"])
def test_a1_a4_a5_a6_are_required(missing):
    a = {k: v for k, v in ZERO_A.items() if k != missing}
    e = err(payload(a=a))
    assert e.code == "MISSING_REQUIRED" and str(e).endswith(missing)


def test_a2_a3_a7_are_required_only_when_a1_is_1_or_more():
    for item in ("A2", "A3", "A7"):
        full = {**ZERO_A, "A1": 1, "A2": 0, "A3": 0, "A7": 0}
        del full[item]
        assert err(payload(a=full)).code == "MISSING_REQUIRED"
    score_assessment(payload(a={**ZERO_A, "A1": 1, "A2": 0, "A3": 0, "A7": 0}))  # complete: no error


def test_an_answer_to_a_hidden_item_is_ignored_and_counts_as_zero():
    r = score_assessment(payload(a={**ZERO_A, "A2": 1, "A3": 1, "A7": 1}))  # A1 = 0 hides A2, A3, A7
    assert r["gate_tier"] == "LOW" and r["gate_reasons"] == [] and r["prompt_attention"] is False


# --- rule 3: gate tier, with the reference's own reason codes -------------------------------------------------------------------------

def gate(**a):
    base = {**ZERO_A, **a}
    if base["A1"] >= 1:
        base = {"A2": 0, "A3": 0, "A7": 0, **base}
    r = score_assessment(payload(a=base))
    return r["gate_tier"], r["gate_reasons"]


@pytest.mark.parametrize("answers,tier,reasons", [
    ({"A1": 2}, "HIGH", ["GATE_H_FALLS_2PLUS"]),
    ({"A1": 1, "A2": 1}, "HIGH", ["GATE_H_INJURY"]),
    ({"A1": 1, "A3": 1}, "HIGH", ["GATE_H_COULD_NOT_GET_UP"]),
    ({"A4": 1}, "HIGH", ["GATE_H_FAINTING"]),
    ({"A1": 1, "A5": 2}, "HIGH", ["GATE_H_FALL_PLUS_OFTEN_UNSTEADY"]),
    ({"A1": 2, "A4": 1}, "HIGH", ["GATE_H_FALLS_2PLUS", "GATE_H_FAINTING"]),     # only the HIGH rules, in rule order
    ({"A1": 1, "A5": 1}, "INTERMEDIATE", ["GATE_I_ONE_FALL", "GATE_I_UNSTEADY"]),
    ({"A1": 1}, "INTERMEDIATE", ["GATE_I_ONE_FALL"]),
    ({"A5": 1}, "INTERMEDIATE", ["GATE_I_UNSTEADY"]),
    ({"A5": 2}, "INTERMEDIATE", ["GATE_I_UNSTEADY"]),  # HIGH needs a fall as well as A5 = 2
    ({"A6": 2}, "INTERMEDIATE", ["GATE_I_WORRY"]),
    ({"A6": 1}, "LOW", []),
    ({}, "LOW", []),
])
def test_gate_tier_table_from_rule_3(answers, tier, reasons):
    assert gate(**answers) == (tier, reasons)


# --- rules 4 to 7: Part B total, score tier, incomplete, final tier -----------------------------------------------------------------------

def test_the_best_possible_answers_score_only_the_age_point():
    r = score_assessment(payload())
    assert r["score"] == 1 and r["score_tier"] == "LOW" and r["final_tier"] == "LOW" and r["unknown_items"] == []
    assert r["top_factors"] == []  # B1 is never a top factor


@pytest.mark.parametrize("b,score,tier", [
    (with_b(B3=1, B4=1, B5=1, B6=1), 5, "LOW"),
    (with_b(B3=1, B4=1, B5=1, B6=1, B7=1), 6, "INTERMEDIATE"),
    (with_b(B3=2, B4=2, B5=2, B6=2, B7=2), 11, "INTERMEDIATE"),
    (with_b(B3=2, B4=2, B5=2, B6=2, B7=2, B9=1), 12, "HIGH"),
])
def test_score_tier_cut_offs_6_and_12(b, score, tier):
    r = score_assessment(payload(b=b))
    assert (r["score"], r["score_tier"]) == (score, tier)


@pytest.mark.parametrize("codes,points", [
    (["NONE"], 0), (["STROKE"], 1), (["STROKE", "LUNG"], 1), (["STROKE", "LUNG", "HEART"], 2),
    (["STROKE", "PARKINSONS", "DIABETES", "ARTHRITIS", "HEART", "DEPRESSION", "KIDNEY", "LUNG"], 2)])
def test_b8_points_by_count(codes, points):
    assert score_assessment(payload(b=with_b(B8=codes)))["part_b_points"]["B8"] == points


@pytest.mark.parametrize("codes,points,flag", [
    (["NONE"], 0, False), (["OPIOIDS"], 1, False), (["OPIOIDS", "PSYCH"], 2, False),
    (["SEDATIVES", "PSYCH", "OPIOIDS"], 2, False), (["NOT_SURE"], 1, True)])
def test_b14_points_by_count_and_not_sure_scores_1_and_sets_the_pharmacist_flag(codes, points, flag):
    r = score_assessment(payload(b=with_b(B14=codes)))
    assert r["part_b_points"]["B14"] == points and r["pharmacist_review_flag"] is flag
    assert "B14" not in r["unknown_items"]


@pytest.mark.parametrize("codes,points", [(["NONE"], 0), (["CLUTTER"], 1), (["CLUTTER", "NO_HANDRAIL"], 1),
                                          (["CLUTTER", "NO_HANDRAIL", "LOOSE_RUGS"], 2)])
def test_b17_points_by_count(codes, points):
    assert score_assessment(payload(b=with_b(B17=codes)))["part_b_points"]["B17"] == points


def test_a_skipped_unknown_or_unanswered_part_b_item_scores_1_never_0():  # rule 4, SR-7
    omitted = {k: v for k, v in ZERO_B.items() if k != "B3"}
    for b in (omitted, with_b(B3="UNKNOWN"), with_b(B3=None)):
        r = score_assessment(payload(b=b))
        assert r["part_b_points"]["B3"] == 1 and r["unknown_items"] == ["B3"] and r["score"] == 2


def test_every_single_choice_part_b_item_scores_1_when_unknown():
    for item in PDF_B_SINGLE_MAX:
        r = score_assessment(payload(b=with_b(**{item: "UNKNOWN"})))
        assert r["part_b_points"][item] == 1 and r["unknown_items"] == [item]


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


# --- rule 12: top factors ------------------------------------------------------------------------------------------------------------

def test_top_factors_are_the_three_highest_part_b_items_by_points_ties_by_item_order():
    r = score_assessment(payload(dob="1930-01-01", b=with_b(B3=1, B5=2, B9=2, B12=2, B16=2, B18=1)))
    assert r["top_factors"] == ["B5", "B9", "B12"]  # B1 (age, 3 points) is excluded; B16 ties but comes later
    r = score_assessment(payload(b=with_b(B7=1, B2=1)))
    assert r["top_factors"] == ["B2", "B7"]


def test_an_unknown_item_can_be_a_top_factor_because_it_scores_1():  # the reference's reading of rule 12
    r = score_assessment(payload(b={k: v for k, v in ZERO_B.items() if k != "B4"}))
    assert r["top_factors"] == ["B4"]


# --- rules 8 to 11 and section 6: red flags and the flags -----------------------------------------------------------------------------

@pytest.mark.parametrize("item", ["R2", "R3", "R4", "R5", "R6", "R7"])
def test_any_of_r2_to_r7_makes_urgent_and_never_changes_the_tier(item):
    base = score_assessment(payload())
    r = score_assessment(payload(red_flags={item: True}))
    assert r["urgent"] is True and r["red_flags_triggered"] == [item]
    assert (r["gate_tier"], r["score_tier"], r["final_tier"]) == (base["gate_tier"], base["score_tier"], base["final_tier"])


@pytest.mark.parametrize("c", [{"C5": 1}, {"C5": 9}, {}])
def test_r1_with_blood_thinners_yes_unsure_or_unanswered_is_urgent(c):
    r = score_assessment(payload(c=c, red_flags={"R1": True}))
    assert r["urgent"] is True and r["red_flags_triggered"] == ["R1"]


def test_r1_with_no_blood_thinners_is_prompt_attention_not_urgent():
    r = score_assessment(payload(c={"C5": 0}, red_flags={"R1": True}))
    assert r["urgent"] is False and r["prompt_attention"] is True
    assert r["red_flags_triggered"] == ["R1"]  # the reference lists every item answered yes, urgent or not


def test_only_a_true_red_flag_counts_in_the_library():
    r = score_assessment(payload(red_flags={"R1": False, "R2": False, "R3": None}))
    assert r["urgent"] is False and r["prompt_attention"] is False and r["red_flags_triggered"] == []
    assert score_assessment(payload(red_flags={"R4": "yes"}))["urgent"] is False  # the API layer refuses non-booleans


def test_an_unknown_red_flag_id_is_refused():
    assert err(payload(red_flags={"R9": True})).code == "INVALID_RED_FLAG"


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


def test_part_c_extras_never_change_the_result():
    base = score_assessment(payload())
    r = score_assessment(payload(c={"C3": 1, "C4": 1, "C8": {"sex": "F", "education": "TERTIARY"}}))
    assert r == base


# --- library validation (what the reference refuses) --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,code", [
    (lambda p: p["part_a"].update(A1=3), "INVALID_ANSWER"),
    (lambda p: p["part_a"].update(A1=True), "INVALID_ANSWER"),
    (lambda p: p["part_a"].update(A1=1.0), "INVALID_ANSWER"),
    (lambda p: p["part_a"].update(A5="UNKNOWN"), "INVALID_ANSWER"),
    (lambda p: p["part_b"].update(B3=5), "INVALID_ANSWER"),
    (lambda p: p["part_b"].update(B8=[]), "INVALID_ANSWER"),
    (lambda p: p["part_b"].update(B8="STROKE"), "INVALID_ANSWER"),
    (lambda p: p["part_b"].update(B8=["NOT_SURE"]), "INVALID_ANSWER"),  # only B14 has NOT_SURE
    (lambda p: p["part_b"].update(B8=["NONE", "STROKE"]), "EXCLUSIVE_ANSWER"),
    (lambda p: p["part_b"].update(B14=["NOT_SURE", "OPIOIDS"]), "EXCLUSIVE_ANSWER"),
    (lambda p: p.update(date_of_birth="not-a-date"), "INVALID_DATE"),
    (lambda p: p.update(date_of_birth=None), "INVALID_DATE"),
    (lambda p: p.update(assessment_date="31/12/2026"), "INVALID_DATE"),
])
def test_invalid_requests_are_refused_with_the_librarys_code(mutate, code):
    p = payload()
    mutate(p)
    assert err(p).code == code


def test_the_library_does_not_check_these_so_the_api_layer_does():
    """Documented gaps of the reference: they are closed in app/services/fallguard.py (see test_fallguard_submit.py)."""
    assert score_assessment(payload(dob="2030-01-01"))["eligible"] is False   # a future date of birth reads as "under 60"
    p = payload()
    p["part_b"]["B20"] = 1  # an item that does not exist is ignored
    p["part_b"]["B1"] = 1
    assert score_assessment(p)["part_b_points"]["B1"] == 1
    assert score_assessment({**payload(), "surprise": 1})["eligible"] is True


def test_scoring_is_deterministic_and_does_not_change_its_input():
    p = copy.deepcopy(APPENDIX_A_INPUT)
    assert score_assessment(p) == score_assessment(p) and p == APPENDIX_A_INPUT


def test_apply_override_keeps_the_original_and_flags_lowering_below_the_gate():  # SR-9, rule 14 (library only; no endpoint yet)
    r = score_assessment(payload(a={**ZERO_A, "A4": 1}))
    o = fg.apply_override(r, "LOW", "SCORE_OVERSTATED_RISK", " reviewed ", {"credentialed": True, "id": "clinician-placeholder"})
    assert o["original"] == r and o["display_tier"] == "LOW"
    assert o["override"]["lowered_below_gate"] is True and o["override"]["note"] == "reviewed"
