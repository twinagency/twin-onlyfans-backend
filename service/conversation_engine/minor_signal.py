"""ONE minor / underage signal for every send-path safety check (G05-PR2).

The engine (journey fan signals, model_output draft judge) and the native detector (safety_detect.assess and
output_violation) all call ``minor_match``, so a phrase gets the same minor verdict on every path
(pinned by tests/test_g05pr2_minor_parity.py). The engine's own banned-theme regexes stay as they are on top.
"""
import re

from .safety_context import _AGE_ASSERTION, UNDER_18, sensitive_match

# Stated age (W5NR-5): matched on the full text; explicit disclosures are never exempt.
# A stated age needs a first-person subject; any continuation counts ("im 16 tbh", "16 & horny", "16 next month") unless
# the number carries a quantity unit or a height mark, so "I'm 15 minutes away", "I'm 16 days sober", "I'm 5'10" and
# "my car is 15 years old" are not ages. Fail-closed: an unlisted unit holds for a human, never sends.
_QUANTITY_UNIT = (r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?|pounds?|lbs?|kgs?|kilos?|"
                  r"grams?|miles?|mi|km|kms|kilomet(?:er|re)s?|met(?:er|re)s?|feet|foot|ft|inch(?:es)?|cm|mm|percent|dollars?|"
                  r"bucks|usd|euros?|quid|cents?|times|blocks?|stops?|exits?|drinks?|beers?|shots?|rounds?|episodes?|games?|"
                  r"points?|levels?|pages?|chapters?|steps?)")
_MINOR = (
    re.compile(r"\b(?:under[\s-]?aged?|under\s*(?:18|eighteen)|jailbait|loli|lolita|shota|pedo\w*|school\s?(?:girl|boy)s?|"
               r"(?:i'?m|i\s+am|im)\s+(?:(?:still|only|just)\s+)?a\s+minor)\b", re.I),
    re.compile(rf"\b(?:(?:i'?m|i\s+am|im)\s+(?:turning|about\s+to\s+turn)|i\s+just\s+turned|i'?m|i\s+am|im|my\s+age\s+is|age\s*:)"
               rf"\s+(?:a\s+)?(?:(?:only|just|barely|still|almost)\s+)?{UNDER_18}"
               r"(?:\s*(?:-\s*)?(?:(?:years?|yrs?)[\s-]*(?:old|of\s+age)|years?(?!\s*\w)|yrs?|yo|y/o|y\.o|f|m(?!\s+(?:away|out|from|ahead|behind|late))|"
               r"(?:fe)?male)\b|"
               rf"(?![\w'\"%$]|[.,]\d|\s*(?:-\s*)?{_QUANTITY_UNIT}\b|\s*m\s+(?:away|out|from|ahead|behind|late)\b))", re.I),
)
# Sibling/child step terms + teen words (step-PARENT roleplay is adult; the engine's banned set is broader by design).
# Never exempt: "my stepsister got a job" is held for a human (G05).
_STEP = re.compile(r"\b(?:step[ -]?(?:sis|sister|bro|brother|daughter|son)s?|teen(?:s|age|aged|ager|agers)?)\b", re.I)
# Ordinary child/family words: a signal only with sensitive context ("be my little girl, so horny"), per sensitive_match.
_CONTEXTUAL = re.compile(
    r"\b(?:minors?|(?:little|young) (?:girl|boy)|kiddo|child(?:ren)?|kids?|(?:my|your) (?:daughter|son|niece|nephew))\b", re.I)
_HARD = (*_MINOR, _STEP, _AGE_ASSERTION)


def minor_match(text, *related):
    """The first minor signal in ``text`` (``related`` texts only add sensitive context), else None."""
    t = (text or "").replace("’", "'")
    for rule in _HARD:
        match = rule.search(t)
        if match:
            return match
    return sensitive_match(_CONTEXTUAL, t, *(r.replace("’", "'") for r in related if r))


def is_minor(text, *related):
    return minor_match(text, *related) is not None
