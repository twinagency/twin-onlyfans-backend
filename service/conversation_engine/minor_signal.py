"""ONE minor / underage signal for every safety detector on the send paths (G05-PR2).

The engine (journey fan signals, model_output draft judge) and the native detector (safety_detect.assess and
output_violation, not wired to a sender until G14) all call ``minor_match``, so a phrase gets the same minor verdict on
every detector (pinned by tests/test_g05pr2_minor_parity.py). The engine's own banned-theme regexes stay as they are on top.
"""
import re

from .safety_context import _AGE_ASSERTION, UNDER_18, sensitive_match

# Stated age (W5NR-5, G05-G): matched on the full text; explicit disclosures are never exempt.
# A stated age needs a person subject (first, second or third person: "im 16", "pretend you're 16", "she's 16") or an age
# suffix ("16f here", "16 years old", "16yo"). After a strong subject any continuation counts ("im 16 tbh", "she's 16 lol")
# unless the number carries a quantity unit, a height or score mark, so "I'm 15 minutes away", "I'm 16 days sober",
# "she's 5'6", "ur 10/10" and "my car is 15 years old" are not ages. 1, 2 and 4 are text-speak (one/too/for: "im 2 drunk",
# "love u 2") unless an age suffix follows. Weak subjects ("u", "you", "ur" = your) and a bare clause-leading number count
# only when the age ends the clause or "& / and" follows ("u 16?", "16 & horny"), so "send u 3 pics" stays clear.
# Fail-closed: an unlisted unit holds for a human, never sends.
_QUANTITY_UNIT = (r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?|pounds?|lbs?|kgs?|kilos?|"
                  r"grams?|miles?|mi|km|kms|kilomet(?:er|re)s?|met(?:er|re)s?|feet|foot|ft|inch(?:es)?|cm|mm|percent|dollars?|"
                  r"bucks|usd|euros?|quid|cents?|times|blocks?|stops?|exits?|drinks?|beers?|shots?|rounds?|episodes?|games?|"
                  r"points?|levels?|pages?|chapters?|steps?)")
_M_NOT = r"(?!\s+(?:away|out|from|ahead|behind|late))"  # "15m away" is a distance
_SUFFIX = (rf"\s*(?:-\s*)?(?:(?:years?|yrs?)[\s-]*(?:old|of\s+age)|years?(?!\s*\w)|yrs?|yo|y/o|y\.o|f|m{_M_NOT}|(?:fe)?male)\b|"
           r"\s*/\s*[fm]\b")
_BARE_SUFFIX = rf"\s*(?:-\s*)?(?:(?:years|yrs)[\s-]*old|yo|y/o|y\.o|f|m{_M_NOT}|(?:fe)?male)\b|\s*/\s*[fm]\b"
_FREE = rf"(?![\w'\"%$/]|[.,]\d|\s*(?:-\s*)?{_QUANTITY_UNIT}\b|\s*m\s+(?:away|out|from|ahead|behind|late)\b)"
_WEAK_END = r"(?![.,]\d)(?=[ \t]*(?:[?!.,;\n]|\Z)|\s*(?:&|and)\s)"
_TEXT_SPEAK = r"(?![124](?!\d))"
_QUAL = r"(?:(?:only|just|barely|still|almost)\s+)?"
_FIRST = (r"(?:i'?m|i\s+am|im)\s+(?:turning|about\s+to\s+turn|gonna\s+be|going\s+to\s+be)|i\s+just\s+turned|i\s+turn|"
          r"i'?ll\s+be|i\s+will\s+be|i'?m|i\s+am|im|my\s+age\s+is|age\s*:")
_SECOND = r"you'?re|youre|ure|you\s+are|(?:you|u)\s+r|are\s+(?:you|u)|r\s+(?:you|u)"
_THIRD = (r"(?:she|he)'?s|(?:she|he)\s+is|is\s+(?:she|he)|(?:my|your|ur|his|her|our|their)\s+(?:(?:little|baby|younger)\s+)?"
          r"(?:daughter|son|niece|nephew|sister|brother|sis|bro|kid|child|cousin|girl|boy|gf|girlfriend|bf|boyfriend)(?:'s|\s+is)")
_MINOR = (
    re.compile(r"\b(?:under[\s-]?aged?|under[\s-]*(?:18|eighteen)|jailbait|loli|lolita|shota|pedo\w*|school\s?(?:girl|boy)s?|"
               r"(?:i'?m|i\s+am|im)\s+(?:(?:still|only|just)\s+)?a\s+minor|"
               # qualified first/second-person assertions ("i'm just a kid", "ur only a child"; Flash G05-PR2)
               r"(?:i'?m|i\s+am|im|you'?re|you\s+are|ur|are\s+you)\s+(?:still|only|just|barely)\s+(?:an?\s+)?(?:kid|child))\b", re.I),
    # strong subject: "im 16 tbh", "pretend you're 16", "she's 16", "my daughter is 15", "I'm a 16-year-old"
    re.compile(rf"\b(?:{_FIRST}|{_SECOND}|{_THIRD})\s+(?:(?:a\s+)?{_QUAL}{UNDER_18}(?:{_SUFFIX})|{_QUAL}{_TEXT_SPEAK}{UNDER_18}{_FREE})",
               re.I),
    # weak subject: "u 16?", "ur 16", "ur 16 and horny" (not "ur 16 pics", "send u 3 pics", "love u 2")
    re.compile(rf"\b(?:u|you|ur)\s+{_QUAL}(?:{UNDER_18}(?:{_SUFFIX})|{_TEXT_SPEAK}{UNDER_18}{_WEAK_END})", re.I),
    # bare clause-leading age: "16f here", "15m", "16 & horny" (not "16 inches", "16 and 17 pics")
    re.compile(rf"(?:\A|(?<=[\n.!?,;]))\s*(?:{UNDER_18}(?:{_BARE_SUFFIX})|"
               rf"{_TEXT_SPEAK}{UNDER_18}\s*(?:&|and)\s+(?!\d|{_QUANTITY_UNIT}\b|more\b|(?:a\s+)?half\b))", re.I),
    # "16 years old" anywhere, unless a non-person "is/was" precedes it ("my car is 15 years old"; persons: strong rule)
    re.compile(rf"(?<!\bis )(?<!\bwas )(?<!'s )(?<!\bare )(?<!\bwere )(?<!\bbe )(?<!\bbeen )\b{UNDER_18}\s*(?:-\s*)?(?:years|yrs)[\s-]*old\b",
               re.I),
    # An explicit age suffix or label is an age claim on its own, no subject needed: "16yo", "she is 16 y/o", "age 16",
    # "age:16" (Codex G05-PR2). The label form keeps the quantity exclusions ("age 16 days in the cellar").
    re.compile(rf"\b{UNDER_18}\s*(?:-\s*)?(?:yo|y/o|y\.o)\b|"
               rf"\bage\s*:?\s*{UNDER_18}(?![\w'\"%$]|[.,]\d|\s*(?:-\s*)?{_QUANTITY_UNIT}\b)", re.I),
)
# Sibling/child step terms (incl. stepsibling/stepchild/stepkid) + teen words (step-PARENT roleplay is adult; the engine's banned set is broader by design).
# Never exempt: "my stepsister got a job" is held for a human (G05).
_STEP = re.compile(r"\b(?:step[ -]?(?:sis|sister|bro|brother|daughter|son|sibling|kid)s?|step[ -]?child(?:ren)?|teen(?:s|age|aged|ager|agers)?)\b", re.I)
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
