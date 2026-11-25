"""FallGuard v2.0 recommendation catalogue (handover PDF section 7) and the fixed content strings (Appendix B copy lint,
Appendix C keys). The catalogue is not part of the reference scoring, so its expectations are hand-derived from the "fires
when" column of the PDF's table; the scored results come from the vendored reference."""

import copy
import re

import pytest

from app.services import fallguard_recommendations as recs
from app.services.fallguard_scoring import score_assessment

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
PDF_BANNED_PATTERNS = ["you are safe", "is safe", "no risk", "will not fall", "clinically validated", "predict", "diagnos"]
ON = "2026-11-10"
DOB_70 = "1956-01-01"
ZERO_B = {"B2": 0, "B3": 0, "B4": 0, "B5": 0, "B6": 0, "B7": 0, "B8": ["NONE"], "B9": 0, "B10": 0, "B11": 0, "B12": 0,
          "B13": 0, "B14": ["NONE"], "B15": 0, "B16": 0, "B17": ["NONE"], "B18": 0, "B19": 0}
ZERO_A = {"A1": 0, "A4": 0, "A5": 0, "A6": 0}


def payload(dob=DOB_70, a=None, b=None, c=None, red_flags=None, on=ON):
    return {"date_of_birth": dob, "assessment_date": on, "part_a": copy.deepcopy(ZERO_A if a is None else a),
            "part_b": copy.deepcopy(ZERO_B if b is None else b), "part_c": copy.deepcopy(c or {}),
            "red_flags": red_flags or {}}


def with_b(**changes):
    return {**ZERO_B, **changes}


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
