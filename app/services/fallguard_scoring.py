"""FallGuard v2.0 scoring: pure functions, standard library only, no database and no network.

Source: "Oakly FallGuard v2.0 Developer Handover Package" (handover v1.0, 10 November 2026), sections 4 to 6 and 17 and
Appendix A and B. The PDF says it ships a reference ``fallguard_scoring.py``; that file was never supplied, so this module is
rebuilt from the PDF's own rules. Where the PDF's text and a rule disagree, "the rule wins" (section 5): the numbers in
``docs/sop/2026-11-25-fallguard-v2-backend.md`` map every rule to the code below.

The server is the only place a tier is decided (SR-3). The final tier is never below the gate tier (SR-4), and an unknown
Part B answer never scores zero (SR-7).

Every place the PDF is unclear is a named constant in the "CONSERVATIVE READINGS" block below, so a clinician's answer is a
one-line change and a versioned release (SR-14), not a hunt through the logic. Each is listed in the SOP under "Open
questions for the clinician".
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ALGORITHM_VERSION = "2.0.0"

DATA_DIR = Path(__file__).parent / "fallguard_data"

LOW, INTERMEDIATE, HIGH = "LOW", "INTERMEDIATE", "HIGH"
TIER_RANK = {LOW: 0, INTERMEDIATE: 1, HIGH: 2}

UNKNOWN = "UNKNOWN"  # section 4: "I don't know" and "Skip, ask my nurse" both map to UNKNOWN

# --- CONSERVATIVE READINGS (PDF unclear: see the SOP's open questions) -------------------------------------------------
# Section 6 says "R1 = yes and C5 = 0 -> Prompt attention" but rule 9 defines the prompt-attention flag without R1.
# True: the flag is also set in that case (the more cautious reading; it never changes the tier).
R1_WITHOUT_BLOOD_THINNER_RAISES_PROMPT_ATTENTION = True
# Rule 12: "up to three Part B items other than B1 with the highest points above 0". An unknown item scores 1 (rule 4), so by the
# letter it can be a top factor. True: it can (literal reading).
TOP_FACTORS_INCLUDE_UNKNOWN_ITEMS = True
# The PDF names only three gate-reason codes (GATE_I_ONE_FALL, GATE_I_UNSTEADY, GATE_I_WORRY, all in Appendix A). The HIGH
# gate rules have no published codes; these are placeholders until the clinical lead supplies them.
GATE_REASON_CODES = {
    "A1_TWICE_OR_MORE": "GATE_H_TWO_FALLS",
    "A2_INJURY": "GATE_H_INJURY",
    "A3_COULD_NOT_GET_UP": "GATE_H_COULD_NOT_GET_UP",
    "A4_FAINTED": "GATE_H_FAINTED",
    "A1_ONCE_AND_OFTEN_UNSTEADY": "GATE_H_FALL_AND_OFTEN_UNSTEADY",
    "A1_ONCE": "GATE_I_ONE_FALL",
    "A5_UNSTEADY": "GATE_I_UNSTEADY",
    "A6_WORRY": "GATE_I_WORRY",
}
# When the final tier is HIGH from the gate, gate_reasons lists only the HIGH rules that fired (False) or also every
# INTERMEDIATE rule that fired (True). The PDF's one example is an INTERMEDIATE gate, so it does not say.
GATE_REASONS_INCLUDE_LOWER_TIER_RULES = False
# Section 6 lists which R items make Urgent; "triggered" in the output is read as the items that caused Urgent.
RED_FLAGS_TRIGGERED_MEANS_CAUSED_URGENT = True


class ScoringError(Exception):
    """A request the scoring library refuses. Maps to HTTP 422 with ``code``, ``message`` and ``fields`` (section 8).

    The PDF says "the library's code" but publishes none; the codes below are this implementation's, prefixed like the
    existing FallGuard codes.
    """

    def __init__(self, code: str, message: str, fields: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.code, self.message, self.fields = code, message, dict(fields or {})

    @property
    def question_id(self) -> Optional[str]:
        """The one question the error is about, when it is about one (not for the date of birth or a body-level problem)."""
        only = next(iter(self.fields)) if len(self.fields) == 1 else None
        return None if only in _TOP_LEVEL_KEYS or only in ("part_a", "part_b", "part_c", "red_flags") else only


def load_json(name: str) -> Dict[str, Any]:
    with open(DATA_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


QUESTIONNAIRE: Dict[str, Any] = load_json("questionnaire_v2_0.json")
QUESTIONNAIRE_VERSION: str = QUESTIONNAIRE["questionnaire_version"]
_SCORING = QUESTIONNAIRE["scoring"]
ITEMS: Dict[str, Dict[str, Any]] = {i["id"]: i for i in QUESTIONNAIRE["items"]}
ITEM_ORDER: List[str] = [i["id"] for i in QUESTIONNAIRE["items"]]
RED_FLAG_IDS: List[str] = [r["id"] for r in QUESTIONNAIRE["red_flags"]]
PART_B_IDS: List[str] = [i for i in ITEM_ORDER if ITEMS[i]["part"] == "B"]

_TOP_LEVEL_KEYS = {"date_of_birth", "assessment_date", "part_a", "part_b", "part_c", "red_flags"}


# --- small helpers ------------------------------------------------------------------------------------------------------

def higher_tier(a: str, b: str) -> str:
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


def parse_date(value: Any, name: str) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ScoringError(f"fallguard_invalid_{name}", f"{name} must be a calendar date (YYYY-MM-DD).", {name: "invalid"})


def calculate_age(date_of_birth: date, on: date) -> int:
    """Completed years on ``on`` (section 17: the caller passes the Africa/Lagos date)."""
    return on.year - date_of_birth.year - ((on.month, on.day) < (date_of_birth.month, date_of_birth.day))


def tier_from_score(score: int) -> str:
    if score >= _SCORING["high_from"]:
        return HIGH
    if score >= _SCORING["intermediate_from"]:
        return INTERMEDIATE
    return LOW


def _band_points(bands: List[Dict[str, Any]], n: int) -> int:
    for band in bands:
        if n >= band["min"] and (band["max"] is None or n <= band["max"]):
            return band["points"]
    raise ValueError(f"no band for {n}")  # unreachable with the shipped config; the parity test checks full coverage


def _same(value: Any, code: Any) -> bool:
    return type(value) is type(code) and value == code  # True is not 1; 1.0 is not 1


def _invalid(item_id: str, reason: str = "invalid_option") -> ScoringError:
    return ScoringError("fallguard_invalid_option", "That answer is not one of the choices for the question.",
                        {item_id: reason})


# --- answer validation ---------------------------------------------------------------------------------------------------

def _validate_single(item: Dict[str, Any], value: Any) -> Any:
    if value == UNKNOWN and isinstance(value, str):
        if item["allows_unknown"]:
            return UNKNOWN
        raise _invalid(item["id"], "unknown_not_allowed")
    if any(_same(value, o["code"]) for o in item["options"]):
        return value
    raise _invalid(item["id"])


def _validate_multi(item: Dict[str, Any], value: Any) -> Any:
    if isinstance(value, str) and value == UNKNOWN:
        if item["allows_unknown"]:
            return UNKNOWN
        raise _invalid(item["id"], "unknown_not_allowed")
    if not isinstance(value, (list, tuple)):
        raise _invalid(item["id"])
    codes = [o["code"] for o in item["options"]]
    if not value:
        raise _invalid(item["id"], "empty_selection")  # section 17: empty is invalid, the user must pick NONE
    if any(not isinstance(v, str) or v not in codes for v in value):
        raise _invalid(item["id"])
    if len(set(value)) != len(value):
        raise _invalid(item["id"], "duplicate_codes")
    if len(value) > 1 and any(v in item["exclusive_codes"] for v in value):
        raise _invalid(item["id"], "exclusive_code_combined")
    return list(value)


def _validate_text_list(item: Dict[str, Any], value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise _invalid(item["id"])
    return [v.strip() for v in value if v.strip()]


def _validate_c8(item: Dict[str, Any], value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping) or any(k not in item["fields"] or v not in item["fields"][k]
                                             for k, v in value.items()):
        raise _invalid(item["id"])
    return dict(value)


def _shown(item: Dict[str, Any], answers: Mapping[str, Any]) -> bool:
    cond = item.get("show_if")
    if not cond:
        return True
    got = answers.get(cond["item"])
    if got is None or got == UNKNOWN:
        return False
    return got >= cond["value"] if cond["op"] == ">=" else _same(got, cond["value"])


def _validate_part(part: str, raw: Any) -> Dict[str, Any]:
    """Return the cleaned answers of one part. Unknown ids, wrong types and invalid codes raise; ``None`` is unanswered."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ScoringError("fallguard_invalid_payload", f"part_{part.lower()} must be an object.",
                           {f"part_{part.lower()}": "invalid"})
    clean: Dict[str, Any] = {}
    for item_id, value in raw.items():
        item = ITEMS.get(item_id)
        if item is None or item["part"] != part:
            raise ScoringError("fallguard_unknown_question", "One of the questions is not part of the assessment.",
                               {str(item_id): "unknown_question"})
        if value is None:
            continue
        kind = item["type"]
        if kind == "derived":
            raise ScoringError("fallguard_unknown_question", f"{item_id} is worked out from the date of birth.",
                               {item_id: "derived"})
        if kind == "auto":
            clean[item_id] = value
        elif item.get("analytics_only"):
            clean[item_id] = _validate_c8(item, value)
        elif kind == "single":
            clean[item_id] = _validate_single(item, value)
        elif kind == "multi":
            clean[item_id] = _validate_multi(item, value)
        elif kind == "text_list":
            clean[item_id] = _validate_text_list(item, value)
    return clean


def _check_required_part_a(a: Mapping[str, Any]) -> None:
    """Rule 2: A1, A4, A5, A6 always; A2, A3, A7 only when A1 >= 1."""
    missing = [i for i in ITEM_ORDER if ITEMS[i]["part"] == "A" and i not in a
               and (ITEMS[i]["required"] or (ITEMS[i]["show_if"] and _shown(ITEMS[i], a)))]
    if missing:
        raise ScoringError("fallguard_missing_required", "Answer every required question.",
                           {i: "required" for i in missing})


# --- red flags (section 6) -----------------------------------------------------------------------------------------------

def _red_flag_yes(item_id: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "no"):
        return value.strip().lower() == "yes"
    raise ScoringError("fallguard_invalid_option", "A red-flag answer is yes or no.", {item_id: "invalid_option"})


def evaluate_red_flags(red_flags: Optional[Mapping[str, Any]], c5: Any = None) -> Dict[str, Any]:
    """Section 6. ``urgent``: any of R2..R7 yes, or R1 yes with C5 = 1, 9 or unanswered. R1 yes with C5 = 0 is prompt attention.

    An unanswered R item is not yes (the check only runs when it is shown). ``c5`` is the raw Part C answer or ``None``.
    """
    flags = red_flags or {}
    if not isinstance(flags, Mapping):
        raise ScoringError("fallguard_invalid_payload", "red_flags must be an object.", {"red_flags": "invalid"})
    yes: List[str] = []
    for item_id, value in flags.items():
        if item_id not in RED_FLAG_IDS:
            raise ScoringError("fallguard_unknown_question", "One of the questions is not part of the assessment.",
                               {str(item_id): "unknown_question"})
        if value is not None and _red_flag_yes(item_id, value):
            yes.append(item_id)
    yes.sort(key=RED_FLAG_IDS.index)
    caused = [i for i in yes if i != "R1"]
    prompt = False
    if "R1" in yes:
        if type(c5) is int and c5 == 0:
            prompt = True  # head blow, not on a blood thinner
        else:
            caused.append("R1")  # C5 is 1, 9 or unanswered: the cautious default (section 17)
    caused.sort(key=RED_FLAG_IDS.index)
    return {"urgent": bool(caused), "triggered": caused if RED_FLAGS_TRIGGERED_MEANS_CAUSED_URGENT else yes,
            "prompt_attention": prompt}


# --- the rules (section 5) -----------------------------------------------------------------------------------------------

def gate_tier(a: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """Rule 3. Returns (tier, reasons). Hidden or skipped A2, A3 and A7 count as 0 (section 4)."""
    a1, a4, a5, a6 = a["A1"], a["A4"], a["A5"], a["A6"]
    a2 = a.get("A2", 0) if a1 >= 1 else 0
    a3 = a.get("A3", 0) if a1 >= 1 else 0
    c = GATE_REASON_CODES
    high = [code for hit, code in (
        (a1 == 2, c["A1_TWICE_OR_MORE"]), (a2 == 1, c["A2_INJURY"]), (a3 == 1, c["A3_COULD_NOT_GET_UP"]),
        (a4 == 1, c["A4_FAINTED"]), (a1 == 1 and a5 == 2, c["A1_ONCE_AND_OFTEN_UNSTEADY"])) if hit]
    inter = [code for hit, code in ((a1 == 1, c["A1_ONCE"]), (a5 >= 1, c["A5_UNSTEADY"]), (a6 == 2, c["A6_WORRY"])) if hit]
    if high:
        return HIGH, high + (inter if GATE_REASONS_INCLUDE_LOWER_TIER_RULES else [])
    if inter:
        return INTERMEDIATE, inter
    return LOW, []


def _part_b_points(age: int, b: Mapping[str, Any]) -> Tuple[Dict[str, int], List[str]]:
    """Rule 4. Returns (points per item B1..B19, unknown or skipped item ids).

    A skipped, unknown or unanswered item other than B1 scores 1 and counts as unknown, except B14 NOT_SURE, which scores 1
    (``not_sure_points``), sets the pharmacist flag and does not count towards the incomplete threshold (section 17).
    """
    points: Dict[str, int] = {}
    unknown: List[str] = []
    for item_id in PART_B_IDS:
        item = ITEMS[item_id]
        if item["type"] == "derived":
            points[item_id] = _band_points(item["age_bands"], age)
            continue
        value = b.get(item_id, UNKNOWN)
        if value == UNKNOWN:
            points[item_id] = 1
            unknown.append(item_id)
        elif item["type"] == "multi":
            if item.get("not_sure_code") in value:
                points[item_id] = item["not_sure_points"]
            else:
                points[item_id] = _band_points(item["count_bands"], len([v for v in value if v != "NONE"]))
        else:
            points[item_id] = next(o["points"] for o in item["options"] if _same(value, o["code"]))
    return points, unknown


def score_assessment(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Score one assessment. ``payload`` has the Appendix A shape; the return value has the Appendix A keys.

    Raises ``ScoringError`` for a malformed or incomplete request (no result, no tier). Under the minimum age there is no
    score and no tier, but ``urgent`` is still decided (section 5, rule 1).
    """
    if not isinstance(payload, Mapping):
        raise ScoringError("fallguard_invalid_payload", "The assessment must be an object.")
    stray = sorted(k for k in payload if k not in _TOP_LEVEL_KEYS)
    if stray:
        raise ScoringError("fallguard_invalid_payload", "Unexpected field in the assessment.", {k: "unexpected" for k in stray})
    if payload.get("date_of_birth") is None:
        raise ScoringError("fallguard_missing_required", "Date of birth is required.", {"date_of_birth": "required"})
    if payload.get("assessment_date") is None:
        raise ScoringError("fallguard_missing_required", "The assessment date is required.", {"assessment_date": "required"})
    dob = parse_date(payload["date_of_birth"], "date_of_birth")
    on = parse_date(payload["assessment_date"], "assessment_date")
    if dob > on:
        raise ScoringError("fallguard_invalid_date_of_birth", "Date of birth is in the future.", {"date_of_birth": "future"})
    age = calculate_age(dob, on)

    base: Dict[str, Any] = {"questionnaire_version": QUESTIONNAIRE_VERSION, "algorithm_version": ALGORITHM_VERSION, "age": age}
    empty = {"gate_tier": None, "gate_reasons": [], "score": None, "score_tier": None, "final_tier": None,
             "incomplete": False, "provisional": False, "unknown_items": [], "part_b_points": {}, "top_factors": [],
             "prompt_attention": False, "consequence_flag": False, "pharmacist_review_flag": False}

    if age < _SCORING["min_age"]:
        raw_c5 = (payload.get("part_c") or {}).get("C5") if isinstance(payload.get("part_c"), Mapping) else None
        c5 = raw_c5 if type(raw_c5) is int and raw_c5 in (0, 1, 9) else None  # unusable counts as unanswered (cautious)
        rf = evaluate_red_flags(payload.get("red_flags"), c5)
        return {**base, "urgent": rf["urgent"], "red_flags_triggered": rf["triggered"], "eligible": False, **empty,
                "prompt_attention": rf["prompt_attention"] and R1_WITHOUT_BLOOD_THINNER_RAISES_PROMPT_ATTENTION}

    a = _validate_part("A", payload.get("part_a"))
    b = _validate_part("B", payload.get("part_b"))
    c = _validate_part("C", payload.get("part_c"))
    _check_required_part_a(a)
    for item_id in ("C3",):  # a hidden item is ignored, not an error (section 4: skipped items count as 0)
        if item_id in c and not _shown(ITEMS[item_id], {**a, **b, **c}):
            del c[item_id]
    for item_id in ("A2", "A3", "A7"):
        if item_id in a and not _shown(ITEMS[item_id], a):
            del a[item_id]
    rf = evaluate_red_flags(payload.get("red_flags"), c.get("C5"))

    g_tier, g_reasons = gate_tier(a)
    points, unknown = _part_b_points(age, b)
    score = sum(points.values())
    s_tier = tier_from_score(score)
    incomplete = len(unknown) > _SCORING["max_unknown_before_incomplete"]  # rule 6; B1 is never unknown
    final = higher_tier(g_tier, s_tier)  # rule 7 and SR-4, including when incomplete

    ranked = [i for i in PART_B_IDS if i != "B1" and points[i] > 0
              and (TOP_FACTORS_INCLUDE_UNKNOWN_ITEMS or i not in unknown)]
    ranked.sort(key=lambda i: (-points[i], PART_B_IDS.index(i)))  # rule 12: ties by item order

    prompt = (a.get("A7") == 1 and a.get("A2") == 1) or a["A4"] == 1 or (
        rf["prompt_attention"] and R1_WITHOUT_BLOOD_THINNER_RAISES_PROMPT_ATTENTION)  # rule 9
    c_flag = (c.get("C5") in (1, 9) or c.get("C6") in (1, 9)
              or (c.get("C1") == 1 and c.get("C2") != "DAILY"))  # rule 10: a missing C2 is not daily
    b14 = b.get("B14")
    pharmacist = (isinstance(b14, list) and "NOT_SURE" in b14) or bool(c.get("C7"))  # rule 11

    return {**base, "urgent": rf["urgent"], "red_flags_triggered": rf["triggered"], "eligible": True,
            "gate_tier": g_tier, "gate_reasons": g_reasons, "score": score, "score_tier": s_tier, "final_tier": final,
            "incomplete": incomplete, "provisional": incomplete, "unknown_items": unknown, "part_b_points": points,
            "top_factors": ranked[:_SCORING["top_factors"]], "prompt_attention": bool(prompt),
            "consequence_flag": bool(c_flag), "pharmacist_review_flag": bool(pharmacist)}
