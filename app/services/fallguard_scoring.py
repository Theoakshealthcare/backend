"""Oakly FallGuard v2.0 - reference scoring implementation.

Questionnaire 2.0.0 / algorithm 2.0.0. Pure functions, standard library only.
If this code and Section 5 of the developer handover disagree, Section 5 wins
and this code is a defect.
"""
from datetime import date, datetime

QUESTIONNAIRE_VERSION = "2.0.0"
ALGORITHM_VERSION = "2.0.0"
UNKNOWN = "UNKNOWN"

# Versioned with the algorithm. Change only through a configuration release (SR-14).
CONFIG = {
    "min_age": 60,
    "intermediate_from": 6,   # score tier: 0-5 LOW, 6-11 INTERMEDIATE, 12+ HIGH
    "high_from": 12,
    "max_unknown_before_incomplete": 3,
}

TIERS = ("LOW", "INTERMEDIATE", "HIGH")
RANK = {t: i for i, t in enumerate(TIERS)}

PART_B_ITEMS = ["B%d" % i for i in range(1, 20)]
# Single-choice Part B items: answer code equals points; value is the maximum code.
SINGLE_MAX = {
    "B2": 1, "B3": 2, "B4": 2, "B5": 2, "B6": 2, "B7": 2, "B9": 2, "B10": 2,
    "B11": 2, "B12": 2, "B13": 1, "B15": 2, "B16": 2, "B18": 2, "B19": 1,
}
# Multi-select Part B items: allowed codes and count bands (min_count, points), highest first.
MULTI = {
    "B8": {"options": {"STROKE", "PARKINSONS", "DIABETES", "ARTHRITIS", "HEART",
                       "DEPRESSION", "KIDNEY", "LUNG"},
           "bands": ((3, 2), (1, 1))},
    "B14": {"options": {"SEDATIVES", "PSYCH", "OPIOIDS", "EPILEPSY",
                        "CARDIO_BP_DIURETIC", "GLUCOSE_LOWERING",
                        "SEDATING_ALLERGY_COUGH", "ANY_DROWSY_DIZZY"},
            "bands": ((2, 2), (1, 1))},
    "B17": {"options": {"LOOSE_RUGS", "UNEVEN_FLOORS", "CLUTTER", "NO_HANDRAIL",
                        "BATHROOM_NO_AID", "POOR_LIGHT_NIGHT", "NO_BACKUP_LIGHT"},
            "bands": ((3, 2), (1, 1))},
}
RED_FLAG_IDS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
OVERRIDE_REASONS = (
    "RECENT_FALL_NOT_CAPTURED", "COGNITIVE_IMPAIRMENT_AFFECTED_ANSWERS",
    "INFORMANT_DISAGREES", "SCORE_UNDERSTATED_RISK", "SCORE_OVERSTATED_RISK",
    "OBJECTIVE_TEST_FINDINGS", "OTHER",
)


class ValidationError(ValueError):
    def __init__(self, code, message=""):
        super().__init__("%s: %s" % (code, message))
        self.code = code


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise ValidationError("INVALID_DATE", repr(v))


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def age_points(age):
    if age < 60:
        return 0
    if age < 75:
        return 1
    if age < 85:
        return 2
    return 3


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _single_points(item, ans):
    """Returns (points, unknown)."""
    if ans is None or ans == UNKNOWN:
        return 1, True
    if not _is_int(ans) or ans < 0 or ans > SINGLE_MAX[item]:
        raise ValidationError("INVALID_ANSWER", "%s=%r" % (item, ans))
    return ans, False


def _band_points(count, bands):
    for min_count, pts in bands:
        if count >= min_count:
            return pts
    return 0


def _multi_points(item, ans):
    """Returns (points, unknown, not_sure)."""
    if ans is None or ans == UNKNOWN:
        return 1, True, False
    if not isinstance(ans, (list, tuple)) or len(ans) == 0:
        raise ValidationError("INVALID_ANSWER", "%s=%r" % (item, ans))
    codes = set(ans)
    allowed = set(MULTI[item]["options"]) | {"NONE"}
    if item == "B14":
        allowed |= {"NOT_SURE"}
    if codes - allowed:
        raise ValidationError("INVALID_ANSWER", "%s=%r" % (item, sorted(codes - allowed)))
    if "NONE" in codes:
        if len(codes) > 1:
            raise ValidationError("EXCLUSIVE_ANSWER", item)
        return 0, False, False
    if "NOT_SURE" in codes:
        if len(codes) > 1:
            raise ValidationError("EXCLUSIVE_ANSWER", item)
        return 1, False, True
    return _band_points(len(codes), MULTI[item]["bands"]), False, False


def evaluate_red_flags(red_flags, c5=None):
    """Red-flag logic (SR-1). Also used by check-ins and the offline client.

    R1 (head blow) is urgent unless the person has explicitly said they do NOT
    take blood thinners (c5 == 0); otherwise it triggers prompt attention.
    R2 to R7 are urgent on their own.
    """
    flags = red_flags or {}
    unknown_keys = set(flags) - set(RED_FLAG_IDS)
    if unknown_keys:
        raise ValidationError("INVALID_RED_FLAG", str(sorted(unknown_keys)))
    triggered = [k for k in RED_FLAG_IDS if flags.get(k) is True]
    urgent = any(k in triggered for k in RED_FLAG_IDS[1:]) or ("R1" in triggered and c5 != 0)
    prompt = ("R1" in triggered) and not urgent
    return {"urgent": urgent, "prompt_attention": prompt, "triggered": triggered}


def _gate(a):
    reasons = []
    if a["A1"] == 2:
        reasons.append("GATE_H_FALLS_2PLUS")
    if a["A2"] == 1:
        reasons.append("GATE_H_INJURY")
    if a["A3"] == 1:
        reasons.append("GATE_H_COULD_NOT_GET_UP")
    if a["A4"] == 1:
        reasons.append("GATE_H_FAINTING")
    if a["A1"] == 1 and a["A5"] == 2:
        reasons.append("GATE_H_FALL_PLUS_OFTEN_UNSTEADY")
    if reasons:
        return "HIGH", reasons
    if a["A1"] == 1:
        reasons.append("GATE_I_ONE_FALL")
    if a["A5"] >= 1:
        reasons.append("GATE_I_UNSTEADY")
    if a["A6"] == 2:
        reasons.append("GATE_I_WORRY")
    return ("INTERMEDIATE", reasons) if reasons else ("LOW", [])


def _validate_part_a(raw):
    raw = raw or {}
    out = {}
    rules = {"A1": 2, "A2": 1, "A3": 1, "A4": 1, "A5": 2, "A6": 2, "A7": 1}
    for k in ("A1", "A4", "A5", "A6"):
        if k not in raw or raw[k] is None:
            raise ValidationError("MISSING_REQUIRED", k)
    for k, mx in rules.items():
        v = raw.get(k)
        if k in ("A2", "A3", "A7") and raw.get("A1") == 0:
            out[k] = 0  # not asked
            continue
        if k in ("A2", "A3", "A7") and v is None:
            raise ValidationError("MISSING_REQUIRED", k)
        if not _is_int(v) or v < 0 or v > mx:
            raise ValidationError("INVALID_ANSWER", "%s=%r" % (k, v))
        out[k] = v
    return out


def _consequence(c):
    c5, c6 = c.get("C5"), c.get("C6")
    if c5 in (1, 9) or c6 in (1, 9):
        return True
    return c.get("C1") == 1 and c.get("C2") != "DAILY"


def score_assessment(payload):
    """Pure function. Input keys: date_of_birth, assessment_date, part_a, part_b,
    part_c, red_flags. Returns a JSON-serialisable result dict."""
    dob = _to_date(payload.get("date_of_birth"))
    on = _to_date(payload.get("assessment_date"))
    age = age_on(dob, on)
    c = payload.get("part_c") or {}
    red = evaluate_red_flags(payload.get("red_flags"), c.get("C5"))
    base = {
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "age": age,
        "urgent": red["urgent"],
        "red_flags_triggered": red["triggered"],
    }
    if age < CONFIG["min_age"]:
        base.update({"eligible": False, "final_tier": None, "gate_tier": None,
                     "score_tier": None, "score": None,
                     "referral_message_key": "UNDER_60_REFERRAL",
                     "prompt_attention": red["prompt_attention"]})
        return base

    a = _validate_part_a(payload.get("part_a"))
    gate_tier, gate_reasons = _gate(a)

    b_in = payload.get("part_b") or {}
    points, unknown_items, not_sure_b14 = {"B1": age_points(age)}, [], False
    for item in PART_B_ITEMS[1:]:
        ans = b_in.get(item)
        if item in SINGLE_MAX:
            p, unk = _single_points(item, ans)
        else:
            p, unk, ns = _multi_points(item, ans)
            if item == "B14" and ns:
                not_sure_b14 = True
        points[item] = p
        if unk:
            unknown_items.append(item)

    score = sum(points.values())
    if score >= CONFIG["high_from"]:
        score_tier = "HIGH"
    elif score >= CONFIG["intermediate_from"]:
        score_tier = "INTERMEDIATE"
    else:
        score_tier = "LOW"

    incomplete = len(unknown_items) > CONFIG["max_unknown_before_incomplete"]
    # Final tier is always the higher of gate and score (SR-4). Incomplete results are
    # labelled provisional; they are never lowered to the gate tier.
    final = max(gate_tier, score_tier, key=RANK.get)

    ranked = sorted((i for i in PART_B_ITEMS[1:] if points[i] > 0),
                    key=lambda i: (-points[i], PART_B_ITEMS.index(i)))
    herbal = [x for x in (c.get("C7") or []) if str(x).strip()]

    base.update({
        "eligible": True,
        "gate_tier": gate_tier,
        "gate_reasons": gate_reasons,
        "score": score,
        "score_tier": score_tier,
        "final_tier": final,
        "incomplete": incomplete,
        "provisional": incomplete,
        "unknown_items": unknown_items,
        "part_b_points": points,
        "top_factors": ranked[:3],
        "prompt_attention": bool((a["A7"] == 1 and a["A2"] == 1) or a["A4"] == 1
                                 or red["prompt_attention"]),
        "consequence_flag": _consequence(c),
        "pharmacist_review_flag": bool(not_sure_b14 or herbal),
    })
    return base


def apply_override(result, new_tier, reason_code, note, clinician):
    """SR-9: credentialed clinician, mandatory reason code and note; original kept."""
    if not (clinician or {}).get("credentialed") is True:
        raise ValidationError("NOT_AUTHORISED", "clinician credential required")
    if new_tier not in TIERS:
        raise ValidationError("INVALID_TIER", str(new_tier))
    if reason_code not in OVERRIDE_REASONS:
        raise ValidationError("INVALID_REASON", str(reason_code))
    if not (note or "").strip():
        raise ValidationError("NOTE_REQUIRED", "")
    if result.get("final_tier") is None:
        raise ValidationError("NOT_OVERRIDABLE", "no tier to override")
    gate = result.get("gate_tier")
    return {
        "original": result,
        "override": {"tier": new_tier, "reason_code": reason_code, "note": note.strip(),
                     "clinician_id": clinician.get("id"),
                     "lowered_below_gate": RANK[new_tier] < RANK[gate]},
        "display_tier": new_tier,
    }
