"""FallGuard v2.0 questionnaire and content as data, plus the checks and views the API layer needs.

``fallguard_data/questionnaire_v2_0.json`` is the file supplied with the handover package, byte for byte. It is the source of
truth for wording, codes, points and conditions; ``fallguard_scoring.py`` (also supplied, also unmodified) is the reference
scoring. ``content_v2_0.json`` holds what the supplied JSON has no equivalent for: the recommendation catalogue, the Appendix C
messages and the welcome text, copied from the handover PDF.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(__file__).parent / "fallguard_data"
LANG = "en"  # the only language that exists; every string falls back to it (PDF section 17)
# Appendix E of the handover is the clinical lead's sign-off; it is blank, so nothing here is signed.
WORDING_STATUS = "PENDING_CLINICAL_SIGN_OFF"


def load_json(name: str) -> Dict[str, Any]:
    with open(DATA_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


QUESTIONNAIRE: Dict[str, Any] = load_json("questionnaire_v2_0.json")
CONTENT: Dict[str, Any] = load_json("content_v2_0.json")
QUESTIONNAIRE_VERSION: str = QUESTIONNAIRE["questionnaire_version"]
CONTENT_VERSION: str = CONTENT["content_version"]
ITEMS: Dict[str, Dict[str, Any]] = {i["id"]: i for i in QUESTIONNAIRE["items"]}
ITEM_ORDER: List[str] = [i["id"] for i in QUESTIONNAIRE["items"]]
RED_FLAG_IDS: List[str] = [r["id"] for r in QUESTIONNAIRE["red_flags"]]


def say(node: Optional[Dict[str, str]], lang: str = LANG) -> Optional[str]:
    """A localised string from the questionnaire JSON, falling back to English."""
    if not node:
        return None
    return node.get(lang) or node.get(LANG)


def question_view(item: Dict[str, Any], order: int, lang: str = LANG) -> Dict[str, Any]:
    """One item as the app receives it. ``id``, ``question``, ``options`` (``text``, ``score``), ``order`` and ``is_active`` are
    the older field names; the rest is additive. ``score`` is the points of the choice, null where a choice carries none."""
    return {
        "id": item["id"],
        "question": say(item.get("text"), lang),
        "options": [{"text": say(o.get("label"), lang) or str(o["code"]), "score": o.get("points"), "code": o["code"]}
                    for o in item.get("options", [])],
        "order": order,
        "is_active": True,
        "part": item["part"],
        "type": item["type"],
        "required": item["required"],
        "allows_unknown": item["allow_unknown"],
        "show_if": item["show_if"],
        "icon": item.get("icon"),
        "explain": say(item.get("explain"), lang),
        "exclusive_codes": item.get("exclusive"),
        "scoring": item.get("scoring"),
        "fields": item.get("fields"),
        "wording_status": WORDING_STATUS,
    }


def questions_view(lang: str = LANG) -> List[Dict[str, Any]]:
    return [question_view(item, n, lang) for n, item in enumerate(QUESTIONNAIRE["items"], start=1)]


def red_flags_view(lang: str = LANG) -> List[Dict[str, Any]]:
    return [{"id": r["id"], "text": say(r.get("text"), lang), "rule": r.get("rule")} for r in QUESTIONNAIRE["red_flags"]]


def config_view(lang: str = LANG) -> Dict[str, Any]:
    """Everything the app needs to render the questionnaire, the red-flag items and the fixed messages."""
    return {
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "algorithm_version": QUESTIONNAIRE["algorithm_version"],
        "content_version": CONTENT_VERSION,
        "language": LANG,
        "wording_status": WORDING_STATUS,
        "content_sign_off_status": CONTENT["sign_off_status"],
        "emergency_number": CONTENT["emergency_number"]["default"],
        "assessment_minutes": CONTENT["assessment_minutes"],
        "min_age": QUESTIONNAIRE["score_config"]["min_age"],
        "unknown_codes": QUESTIONNAIRE["unknown_codes"],
        "items": questions_view(lang),
        "red_flags": red_flags_view(lang),
        "messages": CONTENT["messages"],
        "welcome": CONTENT["welcome"],
    }
