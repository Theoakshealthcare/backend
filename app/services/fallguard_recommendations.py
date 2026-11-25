"""FallGuard v2.0 recommendation catalogue (handover PDF section 7): which entries apply to a scored assessment.

Pure functions. The wording lives in ``fallguard_data/content_v2_0.json`` (the PDF's default English text, verbatim, pending
the clinical lead's sign-off) and changes only through clinical sign-off (E3).

Rules from the PDF: sort by priority number, de-duplicate by id, never show REC-LOW with another tier, every user sees every
entry that applies, and the result screen shows the top three with "See all" for the rest.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping

from app.services.fallguard_questionnaire import CONTENT

LOW, INTERMEDIATE, HIGH = "LOW", "INTERMEDIATE", "HIGH"
CATALOGUE: List[Dict[str, Any]] = sorted(CONTENT["recommendations"], key=lambda r: r["priority"])
TOP_N = 3  # section 7: "the result screen shows the top three by priority"

# --- CONSERVATIVE READING (PDF unclear: see the SOP's open questions) ---------------------------------------------------
# "B4 >= 1", "B13 = 1" and similar read the item's points. An unknown Part B answer scores 1 (rule 4, SR-7), so True means an
# unknown answer fires the entry. False would read the answer itself and let an unknown answer fire nothing.
UNKNOWN_PART_B_FIRES_ENTRIES = True


def _points(result: Mapping[str, Any], item_id: str) -> int:
    value = result["part_b_points"][item_id]
    if not UNKNOWN_PART_B_FIRES_ENTRIES and item_id in result["unknown_items"]:
        return 0
    return value


def applicable(payload: Mapping[str, Any], result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The catalogue entries that fire for ``result`` (the output of ``score_assessment`` for ``payload``), by priority.

    ``payload`` is the same dict that was scored. An ineligible (under-age) result has no tier and no entries.
    """
    if not result["eligible"]:
        return []
    a = payload.get("part_a") or {}
    b = payload.get("part_b") or {}
    c = payload.get("part_c") or {}
    tier, score = result["final_tier"], result["score"]
    pb = lambda i: _points(result, i)  # noqa: E731
    c3 = c.get("C3", 0) if b.get("B2") == 1 else 0  # a hidden or skipped item counts as 0 (section 4)
    a6 = a.get("A6", 0)
    c7 = c.get("C7")
    c7_present = any(s.strip() for s in ([c7] if isinstance(c7, str) else (c7 or [])))

    fires: Dict[str, Callable[[], bool]] = {
        "REC-PROMPT": lambda: result["prompt_attention"],
        "REC-CLINICAL": lambda: tier == HIGH,
        "REC-EXERCISE": lambda: tier in (INTERMEDIATE, HIGH),
        "REC-BOOK-NURSE": lambda: tier == HIGH or (tier == INTERMEDIATE and 9 <= score <= 11),
        "REC-MEDS": lambda: pb("B13") == 1 or pb("B14") >= 1 or c7_present,
        "REC-DIZZY": lambda: pb("B4") >= 1,
        "REC-FALLPLAN": lambda: result["consequence_flag"],
        "REC-HOME": lambda: pb("B17") >= 1,
        "REC-VISION": lambda: pb("B11") >= 1 or c.get("C4") == 1,
        "REC-FOOT": lambda: pb("B6") >= 1 or pb("B7") >= 1,
        "REC-BLADDER": lambda: pb("B9") >= 1,
        "REC-PAIN": lambda: pb("B5") >= 1,
        "REC-HEARING": lambda: pb("B12") >= 1,
        "REC-AID": lambda: pb("B2") == 1 and c3 == 0,
        "REC-FEAR": lambda: a6 >= 1,
        "REC-MEMORY": lambda: pb("B10") >= 1,
        "REC-OUTDOOR": lambda: pb("B18") >= 1,
        "REC-ACTIVE": lambda: pb("B15") >= 1,
        "REC-NUTRITION": lambda: pb("B19") == 1,
        "REC-ALCOHOL": lambda: pb("B16") == 2,
        "REC-LOW": lambda: tier == LOW,
    }
    seen, out = set(), []
    for entry in CATALOGUE:
        if entry["id"] in seen or not fires[entry["id"]]():
            continue
        seen.add(entry["id"])
        out.append({"id": entry["id"], "priority": entry["priority"], "text": entry["text"]})
    return out


def split_top(entries: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """``top`` is what the result screen shows first; ``all`` is the full list behind "See all"."""
    return {"top": entries[:TOP_N], "all": entries}
