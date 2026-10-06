"""Emergency and clinical-advice detection (WA-FR-40, WA-FR-42).

Deliberately biased towards false positives: a needless emergency reply costs one message and a
ticket an agent closes in a minute; a missed one can cost a life. So there is no negation handling
("no chest pain" still matches). The word list is data; extend it here, with a test case each time.

Text is normalised first: lower-cased, accents folded, apostrophes dropped (``can't`` -> ``cant``),
every other symbol turned into a space.
"""

import re
import unicodedata

_APOSTROPHES = "'’‘`´"


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    folded = folded.lower()
    for ch in _APOSTROPHES:
        folded = folded.replace(ch, "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", folded)).strip()


def _compile(patterns: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b")


_EMERGENCY = _compile([
    # --- English ---
    r"chest (?:pain|pains|tightness|is paining)",
    r"(?:pain|pains|tightness) in (?:my|the|his|her|their) chest",
    r"(?:can ?not|cant|unable to|struggling to|struggles to|struggle to|gasping for) breath(?:e|ing)?",
    r"(?:difficulty|trouble|hard to|short of) breath(?:e|ing)?",
    r"(?:not|stopped|stop|isnt|aint|no longer) breathing",
    r"unconscious", r"unresponsive", r"passed out", r"collaps(?:e|ed|ing)", r"faint(?:ed|ing)",
    r"seizures?", r"convuls(?:ion|ions|ing|e|ed)", r"epileptic fit", r"having a fit",
    r"heavy bleeding", r"bleeding (?:heavily|badly|profusely|a lot|too much|non stop)",
    r"(?:wont|will not|cant|cannot) stop bleeding", r"bleeding (?:wont|will not) stop", r"blood everywhere",
    r"vomit(?:ing|ed)? blood", r"coughing (?:up )?blood",
    r"strokes?", r"heart attack", r"cardiac arrest",
    r"suicid(?:e|al)", r"kill (?:myself|my self)", r"end my life", r"take my own life", r"want to die",
    r"wanna die", r"harm myself",
    r"overdos(?:e|ed|ing)", r"took too many (?:pills|tablets)", r"poison(?:ed|ing)?",
    r"chok(?:e|ed|ing)", r"snake ?bite", r"anaphyla\w*",
    # --- Nigerian Pidgin ---
    r"(?:i|e|he|she|we|dem|im) no fit breath(?:e|ing)?", r"no fit breath(?:e|ing)?",
    r"breath(?:e|ing)? no dey (?:come|enter)(?: out| in)?",
    r"(?:e|he|she|im|dem) no dey breath(?:e|ing)?", r"no dey breath(?:e|ing)?",
    r"(?:my |im |her |his )?chest dey (?:pain|hurt|catch)(?: me| am| her| him)?",
    r"blood dey (?:commot|comot|flow|pour)(?: well well| plenty| plenti)?",
    r"dey bleed(?: well well| plenty| plenti)?",
    r"(?:e|he|she|im|dem|person) don (?:faint|collapse|pass out|die)",
    r"(?:i|me) wan (?:die|kill myself|kill my self)", r"make i die",
    r"no dey (?:wake|wake up|respond|answer)",
    r"dey convuls(?:e|ing)", r"dey (?:shake|jerk) (?:well well|anyhow)",
])

_CLINICAL = _compile([
    r"what (?:should|can|could|do|must) i (?:take|drink|use|eat|give)(?: for| to)?",
    r"what (?:should|can|could) (?:we|i) give (?:him|her|them|my \w+)",
    r"which (?:drug|drugs|medicine|medicines|medication|medications|tablet|tablets|pill|pills|injection)",
    r"(?:dose|dosage|overdose limit)",
    r"how (?:much|many) (?:mg|tablets|pills|drops|ml)",
    r"(?:can|may) i (?:take|mix)",
    r"is it safe to (?:take|use|mix|give)",
    r"(?:diagnose|diagnosis|what is wrong with|what s wrong with|what disease)",
    r"(?:medicine|drug|drugs|treatment|remedy) for",
    r"wetin i (?:go|fit|suppose) (?:take|drink|use)",
])


def is_emergency(text: str) -> bool:
    return bool(_EMERGENCY.search(normalize(text)))


def is_clinical_advice_request(text: str) -> bool:
    return bool(_CLINICAL.search(normalize(text)))
