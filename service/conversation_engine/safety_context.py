"""Distinguish ordinary family/age references from sensitive role-play context.

This is a narrow false-positive filter around existing safety patterns; it is
not a policy classifier and does not make a model response authoritative.
"""
import re

# School / teen terms are never exempt (W4CEC-4). A step-family noun is exempt only as someone's real relative
# ("my stepsister got a job"), never when cast as a role ("be my stepsis") or used bare ("come here stepsis").
_CONTEXTUAL_MENTION = re.compile(
    r"(?:minors?|(?:little|young) (?:girl|boy)|kiddo|child(?:ren)?|kids?|(?:my|your) (?:daughter|son|niece|nephew))\Z", re.I)
_STEP_FAMILY = re.compile(r"step ?(?:sis|sister|bro|brother|daughter|son|mom|mum|mother|dad|father)\Z", re.I)
_POSSESSIVE_BEFORE = re.compile(r"\b(?:my|your|his|her|our|their)\s+\Z", re.I)
_ROLE_FRAME = re.compile(r"\b(?:be|being|play|playing|as|like|call (?:me|you|u)|you(?:'re| are)|ur|i(?:'m| am)|im|i'll be|i will be)"
                         r"\s+(?:\S+\s+){0,2}\Z", re.I)
_SENSITIVE_CONTEXT = re.compile(
    r"\b(?:pretend|role[ -]?play|fantas(?:y|ies|ise|ize|ising|izing)|incest|sexual(?:ly)?|sex|sexy|"
    r"horny|nudes?|naked|fuck\w*|cocks?|dicks?|pussy|cum\w*|blowjob|orgasm\w*|masturbat\w*|"
    r"suck(?:ing)?|lick(?:ing)?|slut|whore|daddy|mommy|kink\w*|erotic|seduc\w*|molest\w*|abuse\w*|"
    r"rape|raping|pedo\w*|loli|jailbait)\b", re.I)

_MINOR_ADJECTIVE = re.compile(r'\s+(?:issue|issues|problem|problems|delay|delays|change|changes|mistake|mistakes|detail|details)\b', re.I)
_AGE_ASSERTION = re.compile(
    r"\b(?:i(?:'m| am)|you(?:'re| are)|are you|she(?:'s| is)|he(?:'s| is)|is (?:she|he))\s+"
    r"(?:an? |the )?(?:minor|teen(?:ager)?|school ?girl|school ?boy|child|kid)\b", re.I)


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
        before = text[:match.start()]
        if (_STEP_FAMILY.fullmatch(word) and not sensitive and _POSSESSIVE_BEFORE.search(before)
                and not _ROLE_FRAME.search(before)):
            continue
        return match
    return None
