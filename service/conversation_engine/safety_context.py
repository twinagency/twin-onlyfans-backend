"""Distinguish ordinary family/age references from sensitive role-play context.

This is a narrow false-positive filter around existing safety patterns; it is
not a policy classifier and does not make a model response authoritative.
"""
import re

# School, teen and step-family terms are never exempt (W4CEC-4, G05): a cue-list exemption for "my stepsister got a job"
# leaked casting forms in review, so such mentions are held for a human. Revisit only on held-case volume (G05 PR2).
_CONTEXTUAL_MENTION = re.compile(
    r"(?:minors?|(?:little|young) (?:girl|boy)|kiddo|child(?:ren)?|kids?|(?:my|your) (?:daughter|son|niece|nephew))\Z", re.I)
UNDER_18 = r"(?:1[0-7]|[1-9]|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)"
_SENSITIVE_CONTEXT = re.compile(
    r"\b(?:pretend|role[ -]?play|fantas(?:y|ies|ise|ize|ising|izing)|incest|sexual(?:ly)?|sex|sexy|"
    r"horny|nudes?|naked|fuck\w*|cocks?|dicks?|pussy|cum\w*|blowjob|orgasm\w*|masturbat\w*|"
    r"suck(?:ing)?|lick(?:ing)?|slut|whore|daddy|mommy|kink\w*|erotic|seduc\w*|molest\w*|abuse\w*|"
    r"rape|raping|pedo\w*|loli|jailbait)\b", re.I)

_MINOR_ADJECTIVE = re.compile(r'\s+(?:issue|issues|problem|problems|delay|delays|change|changes|mistake|mistakes|detail|details)\b', re.I)
_AGE_ASSERTION = re.compile(
    r"\b(?:i(?:'m| am|m)|you(?:'re| are)|are you|she(?:'s| is)|he(?:'s| is)|is (?:she|he))\s+"
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
        return match
    return None
