"""ONE minor / underage signal for every safety detector on the send paths (G05-PR2).

The engine (journey fan signals, model_output draft judge) and the native detector (safety_detect.assess and
output_violation, not wired to a sender until G14) all call ``minor_match``, so a phrase gets the same minor verdict on
every detector (pinned by tests/test_g05pr2_minor_parity.py). The engine's own banned-theme regexes stay as they are on top.
"""
import re
import unicodedata

from .safety_context import _AGE_ASSERTION, _SENSITIVE_CONTEXT, UNDER_18, sensitive_match

# Stated age (W5NR-5, G05-G): matched on the full text; explicit disclosures are never exempt.
# A stated age needs a person subject (first, second or third person; first/second also turning/will be/just turned: "im 16",
# "you're turning 16", "my daughter is 15"; a third-person "just turned 16" stays a benign family mention) or an age suffix ("16f here", "16 years old", "a 16 year old girl").
# After a strong subject any continuation counts ("im 16 tbh", "she's 16 lol") unless the number carries a quantity unit,
# a height, score or rating mark ("I'm 15 minutes away", "she's 5'6", "ur 10/10", "you're 10 out of 10", "I'm a 10").
# 1, 2 and 4 are text-speak (one/too/for) only when a word follows ("im 2 drunk", "u 4 real"); "im 4", "she's 2." and
# "im 4 and horny" are ages. Weak subjects count at clause start ("u 16?") or as "ur" anywhere ("pretend ur 15 lol"), and
# only when the age ends the clause, filler follows ("lol", "rn") or "& / and" follows, so "send u 3 pics" and
# "i'll tip you 15" stay clear. A bare clause-leading number counts with an age suffix, "& <word>" or "and <self-description>"
# ("16 & horny", "16 and single"), not haggling ("15 and you send the video", "15 and up").
# Fail-closed: an unlisted unit holds for a human, never sends.
_QUANTITY_UNIT = (r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?|pounds?|lbs?|kgs?|kilos?|"
                  r"grams?|miles?|mi|km|kms|kilomet(?:er|re)s?|met(?:er|re)s?|feet|foot|ft|inch(?:es)?|cm|mm|percent|dollars?|"
                  r"bucks|usd|euros?|quid|cents?|times|blocks?|stops?|exits?|drinks?|beers?|shots?|rounds?|episodes?|games?|"
                  r"points?|levels?|pages?|chapters?|steps?|stars?)")
_M_NOT = (r"(?!\s+(?:away|out|from|ahead|behind|late|views|followers|subs|subscribers|likes|fans|plays|streams|downloads|"
          r"members))")  # "15m away" is a distance, "16m views" a count
_PERSON = (r"(?:girl|boy|daughter|son|kid|child|sister|brother|sis|bro|student|virgin|niece|nephew|cousin|teen|"
           r"female|male|f|m|here|me|myself)")
# a singular "year old" with a non-person word after it is a thing ("12 year old whiskey")
_NOT_THING = rf"(?![\s-]+(?!{_PERSON}\b)[a-z])"
_SUFFIX = (rf"\s*(?:-\s*)?(?:(?:years?|yrs?)[\s-]*(?:old|of\s+age)|years?(?!\s*\w)|yrs?|yo|y/o|y\.o|f|m{_M_NOT}|(?:fe)?male)\b|"
           r"\s*/\s*[fm]\b")
_BARE_SUFFIX = (rf"\s*(?:-\s*)?(?:(?:years|yrs)[\s-]*old|(?:year|yr)[\s-]*old{_NOT_THING}|yo|y/o|y\.o|f|m{_M_NOT}|(?:fe)?male)\b|"
                r"\s*/\s*[fm]\b")
# not an age after a strong subject: a unit, a height ("5 4"), a range ("9 to 5"), a rating ("10 out of 10")
_FREE = (rf"(?![\w'\"%$/]|[.,]\d|\s*(?:-\s*)?{_QUANTITY_UNIT}\b|\s*m\s+(?:away|out|from|ahead|behind|late)\b|"
         rf"\s+(?:out\s+of|outta|of\s+10|to\s+\d|and\s+a\s+half\s+{_QUANTITY_UNIT})\b|(?<=[4-7])\s+(?:1[01]|\d)\b)")
_FILLER = r"(?:lol|lmao|lmfao|haha\w*|hehe\w*|tbh|tho|though|rn|fr|ngl|irl|right|ya|yeah|yes|too|now|already|lmk|ok|okay|xx+|x)"
_EMOJI = "[\U0001F300-\U0001FAFF\u2600-\u27BF]"
_STOP = rf"[ \t]*(?:[?!.,;\n]|\Z|{_EMOJI})"
_END = rf"(?={_STOP}|\s+(?:&|and)\s|\s+{_FILLER}\b(?:{_STOP}|\s+{_FILLER}\b))"
_WEAK_END = rf"(?![.,]\d){_END}"
_SPEAK = r"[124](?!\d)"  # one/too/for unless the age ends the clause, a split digit or a date follows ("i'm 1 6", "i am 2 next month")
_QUAL = r"(?:(?:only|just|barely|still|almost|like|nearly|about|around|literally|basically|technically|not\s+even)\s+)?"
_TURN = r"(?:\s+(?:just\s+)?(?:turning|turned|turns|turn|about\s+to\s+turn|gonna\s+be|going\s+to\s+be|will\s+be))?"
_FIRST = (r"(?:i'?m|i\s+am|im)\s+(?:turning|about\s+to\s+turn|gonna\s+be|going\s+to\s+be)|i\s+just\s+turned|i\s+turn|"
          r"i'?ll\s+be|i\s+will\s+be|i'?m|i\s+am|im|my\s+age\s+is|age\s*:")
_SECOND = (r"(?:you'?re|youre|ure|you\s+are|(?:you|u)\s+r|are\s+(?:you|u)|r\s+(?:you|u)|you'?ll\s+be|you\s+will\s+be|"
           r"(?:you|u)\s+(?:just\s+)?turned|(?:you|u)\s+were|were\s+(?:you|u)|will\s+(?:you|u)\s+be)") + _TURN
_THIRD = (r"(?:(?:she|he)|(?:my|your|ur|his|her|our|their)\s+(?:(?:little|baby|younger|best)\s+)?"
          r"(?:daughter|son|niece|nephew|sister|brother|sis|bro|kid|child|cousin|girl|boy|gf|girlfriend|bf|boyfriend|"
          r"friend|bestie|bff|classmate))(?:'s|(?<=he)s|\s+is)"
          r"|is\s+(?:she|he|(?:my|your|ur|his|her)\s+\w+)") + _TURN
_YEAR_OLD = rf"{UNDER_18}\s*(?:-\s*)?(?:year|yr)s?[\s-]*old"
_CLAUSE = r"(?:\A|(?<=[\n.!?,;]))[ \t]*(?:(?:hey|hi|hello|so|wait|and|but|omg|lol|haha|wow|ok|okay)[ \t,]+)?"
_MINOR = (
    re.compile(r"\b(?:under[\s-]?aged?|under[\s-]*(?:18|eighteen)|jailbait|loli|lolita|shota|pedo\w*|school\s?(?:girl|boy)s?|"
               r"(?:i'?m|i\s+am|im)\s+(?:(?:still|only|just)\s+)?a\s+minor|"
               # qualified first/second-person assertions ("i'm just a kid", "ur only a child"; Flash G05-PR2)
               r"(?:i'?m|i\s+am|im|you'?re|you\s+are|ur|are\s+you)\s+(?:still|only|just|barely)\s+(?:an?\s+)?(?:kid|child))\b", re.I),
    # strong subject: "im 16 tbh", "pretend you're 16", "she's 16", "my daughter just turned 15", "I'm a 16", "im 4"
    re.compile(rf"\b(?:{_FIRST}|{_SECOND}|{_THIRD})\s+(?:(?:a\s+)?{_QUAL}{UNDER_18}(?:{_SUFFIX})|"
               rf"(?:a\s+(?!10\b))?{_QUAL}(?:(?!{_SPEAK}){UNDER_18}{_FREE}|{_SPEAK}(?:{_END}|(?=[\s-]+\d|\s+(?:next|this|soon|tomorrow|today|tmrw|in\s+(?:a\s+)?(?:few|couple|\d+|month|week|year|day)|on\s+my)\b))))", re.I),
    # weak subject: "u 16?" at clause start, "ur 16 lol" anywhere (not "ur 16 pics", "send u 3 pics", "i'll tip you 15")
    re.compile(rf"(?:{_CLAUSE}(?:u|you)|\bur)\s+{_QUAL}(?:{UNDER_18}(?:{_SUFFIX})|(?!{_SPEAK}){UNDER_18}{_WEAK_END})", re.I),
    # bare clause-leading age: "16f here", "15m", "16 & horny", "16 and single" (not "16 inches", "15 and you send it")
    re.compile(rf"(?:\A|(?<=[\n.!?,;]))[ \t]*(?:{UNDER_18}(?:{_BARE_SUFFIX})|"
               rf"{UNDER_18}\s*(?:&\s*(?!\d|{_QUANTITY_UNIT}\b|more\b|up\b|(?:a\s+)?half\b)\w|"
               r"\s+and\s+(?:horny|single|bored|lonely|wet|hard|curious|alone|shy|innocent|tight|naughty|cute|new|still|"
               r"looking|legal|ready|a\s+virgin|virgin|in\s+(?:high\s+|middle\s+)?school|a\s+(?:girl|boy|student))\b))", re.I),
    # "16 years old" anywhere, unless a non-person "is/was" precedes it ("my car is 15 years old"; persons: strong rule)
    re.compile(rf"(?<!\bis )(?<!\bwas )(?<!'s )(?<!\bare )(?<!\bwere )(?<!\bbe )(?<!\bbeen )\b{UNDER_18}\s*(?:-\s*)?(?:years|yrs)[\s-]*old\b",
               re.I),
    # "16 year old girl", "roleplay as a 16-year-old" (singular: needs a person after it or an article before it with no
    # non-person word after it; "12 year old whiskey" stays clear). A family mention ("my 16 year old son") is contextual.
    re.compile(rf"(?<!\bmy )(?<!\bour )(?<!\bhis )(?<!\bher )(?<!\byour )\b{_YEAR_OLD}[\s-]+{_PERSON}\b|\b(?:an?|as|like|be)\s+{_YEAR_OLD}\b{_NOT_THING}", re.I),
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
# "my 16 year old son" is a family mention; with sensitive context ("my 16 year old daughter is so horny") it is a signal.
_FAMILY_AGE = re.compile(rf"\b(?:my|your|our|his|her)\s+{_YEAR_OLD}[\s-]+{_PERSON}\b", re.I)
_HARD = (*_MINOR, _STEP, _AGE_ASSERTION)


def _normal(text):
    """NFKC (fullwidth digits), drop format chars (zero-width), any Unicode decimal digit -> ASCII, curly apostrophe."""
    t = unicodedata.normalize("NFKC", text or "").replace("\u2019", "'")
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c
                   for c in t if unicodedata.category(c) != "Cf")


def minor_match(text, *related):
    """The first minor signal in ``text`` (``related`` texts only add sensitive context), else None."""
    t = _normal(text)
    for rule in _HARD:
        match = rule.search(t)
        if match:
            return match
    related = tuple(_normal(r) for r in related if r)
    family = _FAMILY_AGE.search(t)
    if family and _SENSITIVE_CONTEXT.search(" ".join((t, *related))):
        return family
    return sensitive_match(_CONTEXTUAL, t, *related)


def is_minor(text, *related):
    return minor_match(text, *related) is not None
