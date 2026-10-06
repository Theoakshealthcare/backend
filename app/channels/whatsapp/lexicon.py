"""Emergency and clinical-advice detection (WA-FR-40, WA-FR-42).

Deliberately biased towards false positives: a needless emergency reply costs one message and a
ticket an agent closes in a minute; a missed one can cost a life. So there is no negation handling
("no chest pain" still matches). The word list is data; extend it here, with a test case each time.

Text is normalised first: lower-cased, accents folded, apostrophes dropped (``can't`` -> ``cant``),
every other symbol turned into a space.
"""

import difflib
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
    r"(?:having|has|have) (?:a )?stroke", r"(?:had|has had) (?:a )?stroke (?:now|just|today|this morning|just now)",
    r"stroke (?:symptoms?|signs?|now)", r"face (?:is )?droop(?:ing|s)", r"(?:its|it is) (?:a )?stroke",
    r"think (?:he|she|they|mum|dad|mom|my \w+) (?:has |had |is having )(?:a )?stroke",
    r"heart attack", r"cardiac arrest",
    r"suicid(?:e|al)", r"kill (?:myself|my self)", r"end my life", r"take my own life", r"want to die",
    r"wanna die", r"harm myself", r"hang(?:ing)? (?:myself|my self)", r"end it all", r"tired of (?:life|living)",
    r"sniper", r"no wan live",
    r"overdos(?:e|ed|ing)", r"took too many (?:pills|tablets)", r"poison(?:ed|ing)?",
    r"chok(?:e|ed|ing)", r"snake ?bite", r"anaphyla\w*",
    # --- gaps closed after the security review ---
    r"shortness of breath", r"(?:difficulty|trouble) in breath(?:e|ing)?", r"breathing (?:difficulty|hard|problem)",
    r"gasping", r"heart (?:is |dey )?(?:paining|pain|hurt|hurting)", r"chest (?:is )?(?:too |very )?tight",
    r"tight chest",
    r"(?:not|wont|will not|cant|cannot|doesnt|isnt|never) wak(?:e|ing) up", r"not waking", r"slump(?:ed|ing)?",
    r"(?:is|was|am|are|keeps|keep|still|started|starts|she|he) bleeding(?! gums?)", r"^bleeding$",
    r"pregnant and bleeding", r"blood (?:is )?coming out", r"blood (?:is )?(?:flowing|pouring)",
    r"having (?:a )?fits?(?! of)", r"foam(?:ing)? (?:at|from|in) (?:the |his |her |my |their )?mouth",
    r"(?:is|are|was|am) dead", r"(?:has|have) died", r"stabbed",
    r"(?:had|has|was|is|in|been in|were in|just had) an? (?:\w+ )?accident", r"accident (?:now|just happened|scene)",
    # --- Nigerian Pidgin ---
    r"(?:e|he|she|im|dem|person) dey fitting", r"(?:e|he|she|im) dey fit$",
    r"i no wan live", r"no wan live again",
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


# Single words that are emergencies on their own; matched with a small typo tolerance so "unconcious",
# "siezure" and "sucide" are caught. Only longer words are fuzzed so short ordinary words are left alone.
_FUZZY_KEYWORDS = ("unconscious", "unresponsive", "seizure", "seizures", "suicide", "suicidal")
# Only typos of these words, by their first letters, so bare "conscious" / "responsive" are never fuzzed
# into "unconscious" / "unresponsive".
_FUZZY_PREFIXES = ("un", "se", "si", "su")
_FUZZY_MIN_LENGTH = 6
_FUZZY_CUTOFF = 0.84


def _fuzzy_hit(normalized: str) -> bool:
    return any(
        len(token) >= _FUZZY_MIN_LENGTH and token.startswith(_FUZZY_PREFIXES) and difflib.get_close_matches(token, _FUZZY_KEYWORDS, n=1,
                                                                      cutoff=_FUZZY_CUTOFF)
        for token in normalized.split()
    )


def is_emergency(text: str) -> bool:
    normalized = normalize(text)
    return bool(_EMERGENCY.search(normalized)) or _fuzzy_hit(normalized)


def is_clinical_advice_request(text: str) -> bool:
    return bool(_CLINICAL.search(normalize(text)))
