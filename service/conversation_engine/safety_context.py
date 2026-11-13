"""Distinguish ordinary family/age references from sensitive role-play context.

This is a narrow false-positive filter around existing safety patterns; it is
not a policy classifier and does not make a model response authoritative.
"""
import re

# School / teen terms are never exempt (W4CEC-4). A step-family noun is exempt only as someone's real relative
# ("my stepsister got a job"), never when cast as a role ("be my stepsis") or used bare ("come here stepsis").
_CONTEXTUAL_MENTION = re.compile(
    r"(?:minors?|(?:little|young) (?:girl|boy)|kiddo|child(?:ren)?|kids?|(?:my|your) (?:daughter|son|niece|nephew))\Z", re.I)
_STEP_FAMILY = re.compile(r"(?:step[ -]?(?:sis|sister|bro|brother|daughter|son|mom|mum|mother|dad|father)s?|teen(?:s|age|aged|ager|agers)?)\Z", re.I)
# Owner possessive + up to 2 adjectives before the noun; "be/as/like/play my ..." casts the fan in the role.
_POSSESSIVE_BEFORE = re.compile(r"\b(?:(be|as|like|play(?:ing)?)\s+)?(?:my|his|her|our|their)\s+(?:[\w'-]+\s+){0,2}\Z", re.I)
UNDER_18 = r"(?:1[0-7]|[1-9]|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)"
# Addressed-to-fan casting, affection/minor cues or an under-18 age anywhere in the clause defeat the exemption.
_ROLE_FRAME = re.compile(rf"\b(?:you(?:'re| are)|ur|pretend|call (?:me|you|u)|(?:that|it)(?:'s| is) (?:you|u)|love|hot|cute|"
                         rf"good (?:girl|boy)|kiss\w*|little|baby|young|naughty|innocent|teen\w*|{UNDER_18})\b", re.I)
_SENSITIVE_CONTEXT = re.compile(
    r"\b(?:pretend|role[ -]?play|fantas(?:y|ies|ise|ize|ising|izing)|incest|sexual(?:ly)?|sex|sexy|"
    r"horny|nudes?|naked|fuck\w*|cocks?|dicks?|pussy|cum\w*|blowjob|orgasm\w*|masturbat\w*|"
    r"suck(?:ing)?|lick(?:ing)?|slut|whore|daddy|mommy|kink\w*|erotic|seduc\w*|molest\w*|abuse\w*|"
    r"rape|raping|pedo\w*|loli|jailbait)\b", re.I)

_MINOR_ADJECTIVE = re.compile(r'\s+(?:issue|issues|problem|problems|delay|delays|change|changes|mistake|mistakes|detail|details)\b', re.I)
_AGE_ASSERTION = re.compile(
    r"\b(?:i(?:'m| am)|you(?:'re| are)|are you|she(?:'s| is)|he(?:'s| is)|is (?:she|he))\s+"
    r"(?:an? |the )?(?:minor|teen(?:ager)?|school ?girl|school ?boy|child|kid)\b", re.I)


def relative_mention(text, match):
    """True only for a step-family / teen noun naming someone's real relative ("my stepsister got a job"): an owner
    possessive (not "your") before it, not cast ("be my"), and no cue in its clause or the next sentence (160 chars each)."""
    if not _STEP_FAMILY.fullmatch(match.group(0)):
        return False
    norm = lambda part: part.replace("\u2019", "'")
    before = re.split(r"[.!?\n]", norm(text[max(0, match.start() - 160):match.start()]))[-1]
    after = " ".join(re.split(r"[.!?\n]", norm(text[match.end():match.end() + 160]))[:2])
    owner = _POSSESSIVE_BEFORE.search(before)
    return bool(owner) and not owner.group(1) and not _ROLE_FRAME.search(before + " " + after)


def sensitive_match(pattern, text, *related):
    """Return the first safety match, ignoring context-free family/age mentions only.

    Sensitive terms elsewhere in the same message or the current fan message
    keep the original block. Multiple matches are all inspected so a benign
    first mention cannot conceal a later prohibited theme.
    """
    context = ' '.join((text, *related))
    sensitive = bool(_SENSITIVE_CONTEXT.search(context) or _AGE_ASSERTION.search(context))
    for match in pattern.finditer(text):
        word = match.group(0).lower()
        if word in {'minor', 'minors'} and not _MINOR_ADJECTIVE.match(text[match.end():]):
            return match
        if _CONTEXTUAL_MENTION.fullmatch(word) and not sensitive:
            continue
        if not sensitive and relative_mention(text, match):
            continue
        return match
    return None
