"""Rule-based scenario detection for one fan message. Deterministic; no model call.

Order matters: escalation triggers first (the protocol says "escalate immediately, do not reply first"), then
identity/secrecy/boundary scenarios, then sales intents, then the playbook's relationship scenarios, then small talk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── scenario keys ────────────────────────────────────────────────────────────────────────────────────────────────
ESCALATE = "escalate"                  # leak/extortion, threats, stalking, real-world details, crisis, non-consent
CHARGEBACK = "chargeback"              # formal refund/chargeback: one firm reply allowed, then escalate
IDENTITY = "identity"                  # "are you a bot / is this really her"
SECRECY = "secrecy"                    # 20 asks for secrecy from management
OTHER_CREATOR = "other_creator"        # 16
FAMILY_COMMENT = "family_comment"      # 17
PAST_PROMISE = "past_promise"          # 19
BOUNDARY_PUSH = "boundary_push"        # 18
ALREADY_BOUGHT = "already_bought"      # 15
CUSTOM = "custom_request"              # 14
TOO_MANY_SALES = "too_many_sales"      # 13
BORED_CONTENT = "bored_content"        # 12
PRICE_COMPARE = "price_compare"        # 11
REAL_RELATIONSHIP = "real_relationship"  # 10
DELAYED_REPLY = "delayed_reply"        # 9
EXCLUSIVE = "exclusive_attention"      # 8
JEALOUS = "jealous"                    # 7
FEELINGS = "feelings"                  # 6
REMEMBER_PAST = "remember_past"        # 5
IGNORED_QUESTION = "ignored_question"  # 4
CONFLICTING = "conflicting_details"    # 3
ODD_HOURS = "odd_hours"                # 2
REPETITIVE = "repetitive"              # 1
RATING_ASK = "rating_ask"
CONTENT_ASK = "content_ask"
YES = "yes"
NO = "no"
SMALL_TALK = "small_talk"

_I = re.I


def _rx(*patterns):
    return tuple(re.compile(p, _I) for p in patterns)


_RULES: tuple[tuple[str, tuple[re.Pattern, ...]], ...] = (
    (ESCALATE, _rx(
        r"\b(?:leak|post|share|send)\w*\s+(?:your|ur)\s+(?:pics|nudes|content|videos?)\b.*\b(?:unless|if\s+you\s+don'?t|everywhere|online|family|friends)\b",
        r"\b(?:i'?ll|i\s+will|gonna)\s+(?:leak|expose|dox|find)\s+(?:you|u|your|ur)\b",
        r"\b(?:i\s+know|found)\s+where\s+(?:you|u)\s+(?:live|work|stay)\b|\b(?:outside|near)\s+(?:your|ur)\s+(?:house|place|apartment|work|gym)\b",
        r"\bwere\s+(?:you|u)\s+at\b.*\b(?:today|yesterday|last\s+night)\b.*\b(?:i\s+saw|saw\s+you)\b",
        r"\b(?:kill|hurt|rape|beat)\s+(?:you|u|your|ur)\b",
        r"\b(?:kill(?:ing)?|hurt(?:ing)?)\s+myself\b|\bsuicid(?:e|al)\b|\bend\s+it\s+all\b|\btook\s+(?:a\s+lot\s+of|too\s+many)\s+pills\b",
        r"\b(?:underage|under\s*18|minor|year\s*old\s+(?:girl|boy))\b|\b1[0-7]\s*(?:yo|y/o|years?\s+old)\b",
        r"\b(?:stupid|dumb|worthless|ugly)\s+(?:bitch|whore|slut|cunt)\b",
    )),
    (CHARGEBACK, _rx(r"\b(?:charge\s*back|chargeback|dispute\s+(?:the|this|my)\s+(?:charge|payment)|report\s+(?:you|this)\s+to\s+(?:onlyfans|my\s+bank)|refund\s+(?:me|my)\b)")),
    (IDENTITY, _rx(
        r"\b(?:are|r)\s+(?:you|u)\s+(?:a\s+)?(?:bot|ai|robot|real|fake|chat\s*gpt|her\s+manager|a\s+(?:guy|man|team))\b",
        r"\bis\s+(?:this|it)\s+(?:really|actually)\s+(?:you|her)\b|\bwho\s+(?:am\s+i|is)\s+(?:really\s+)?(?:talking|texting)\s+(?:to|with)\b",
        r"\b(?:chatter|ghost\s*writer|management)\s+(?:typing|writing|answering)\b",
    )),
    (SECRECY, _rx(r"\b(?:promise|swear)\b.*\b(?:not\s+(?:to\s+)?tell|keep\s+(?:this|it)\s+(?:secret|between))\b|\bdon'?t\s+tell\s+(?:anyone|your\s+(?:manager|team|agency))\b")),
    (OTHER_CREATOR, _rx(r"\b(?:that|the)\s+other\s+(?:girl|creator|model)\b|\bwhat'?s\s+she\s+(?:really|actually)\s+like\b")),
    (FAMILY_COMMENT, _rx(r"\b(?:your|ur)\s+(?:mom|mum|mother|sister|sis|dad|family)\b.*\b(?:hot|sexy|look\s+like|single|pics?|nudes?)\b")),
    (PAST_PROMISE, _rx(r"\b(?:you|u)\s+promised\b|\byou\s+said\s+i'?d\s+get\b")),
    (BOUNDARY_PUSH, _rx(r"\bjust\s+(?:this\s+)?once\b|\bmake\s+an?\s+exception\b|\bi\s+know\s+(?:you|u)\s+can\b|\bcome\s+on\s+please\b|\bwhy\s+not\s+just\b")),
    (ALREADY_BOUGHT, _rx(r"\b(?:already|alr)\s+(?:bought|have|got|paid\s+for|unlocked)\b|\bselling\s+(?:me\s+)?(?:the\s+same|it)\s+again\b")),
    (CUSTOM, _rx(r"\bcustom\b|\b(?:make|film|record)\s+(?:something|a\s+(?:video|vid|clip))\s+(?:just\s+)?for\s+me\b|\bspecific\s+idea\b|\bused\s+(?:panties|undies|underwear)\b")),
    (TOO_MANY_SALES, _rx(r"\b(?:always|every\s+time)\b.*\b(?:sell|selling|pitch)\b|\bstop\s+(?:selling|sending\s+(?:me\s+)?(?:ppv|offers|locked))\b|\bcan\s+we\s+just\s+talk\b")),
    (BORED_CONTENT, _rx(r"\b(?:content|pics|videos?)\b.*\b(?:repetitive|boring|the\s+same|same\s+old)\b|\bbored\s+of\s+(?:your|ur)\b")),
    (PRICE_COMPARE, _rx(r"\bwhy\s+is\s+(?:this|it)\b.*\$?\d+.*\b(?:when|but)\b|\b(?:so|too)\s+(?:expensive|pricey)\b|\bwhy\s+(?:so|does\s+it\s+cost)\b")),
    (REAL_RELATIONSHIP, _rx(r"\breal\s+relationship\b|\b(?:meet|date)\s+(?:in\s+person|irl|for\s+real)\b|\bbe\s+my\s+(?:girlfriend|gf|wife)\b")),
    (DELAYED_REPLY, _rx(r"\btook\s+(?:you\s+)?\w+\s+(?:hours?|days?)\s+to\s+(?:reply|answer)\b|\bnot\s+important\s+anymore\b|\bignoring\s+me\b")),
    (EXCLUSIVE, _rx(r"\bshould(?:n'?t)?\s+i\s+get\s+more\s+attention\b|\bsupporting\s+(?:you|u)\s+(?:forever|so\s+long)\b|\bmore\s+than\s+(?:these|the)\s+new\s+guys\b")),
    (JEALOUS, _rx(r"\b(?:talking|chatting)\s+to\s+other\s+(?:guys|men|fans)\b|\bjealous\b")),
    (FEELINGS, _rx(r"\bfalling\s+for\s+(?:you|u)\b|\bi\s+(?:think\s+i\s+)?love\s+(?:you|u)\b|\bfeelings\s+for\s+(?:you|u)\b")),
    (REMEMBER_PAST, _rx(r"\bremember\s+(?:that|what|when)\b.*\b(?:i\s+told|i\s+said)\b")),
    (IGNORED_QUESTION, _rx(r"\b(?:you|u)\s+(?:completely\s+)?ignored\s+(?:me|my\s+question)\b|\bnever\s+answered\b")),
    (CONFLICTING, _rx(r"\b(?:you|u)\s+said\b.*\bnow\s+(?:you'?re|ur)\s+saying\b|\bwhich\s+is\s+it\b")),
    (ODD_HOURS, _rx(r"\bhow\s+are\s+(?:you|u)\s+always\s+(?:awake|online|up)\b|\bdo\s+(?:you|u)\s+(?:ever\s+)?sleep\b")),
    (REPETITIVE, _rx(r"\bsame\s+messages?\b|\bcopy(?:ing)?\s+(?:and|n)\s+past(?:e|ing)\b|\bkeep\s+sending\s+(?:me\s+)?the\s+same\b")),
    (RATING_ASK, _rx(r"\brate\s+(?:my|me)\b|\b(?:dick|cock)\s+rat(?:e|ing)\b")),
    (CONTENT_ASK, _rx(
        r"\b(?:send|show|see|got|have|want|wanna)\b[^.?!]{0,25}\b(?:pics?|photos?|videos?|vids?|clips?|content|more|something|boobs?|tits|booty|ass|feet|bikini|lingerie|nudes?)\b",
        r"\bwhat\s+(?:do\s+you|u)\s+have\b|\bppv\b|\bunlock\b")),
    (YES, _rx(r"^\W*(?:yes+|yeah+|yea+|yep|sure|ok(?:ay)?|send\s+it|i'?m\s+in|deal|go\s+on|please|pls|👍|🔥|😍)\W*$")),
    (NO, _rx(r"^\W*(?:no+|nah+|nope|not\s+now|maybe\s+later|no\s+thanks?|can'?t\s+afford)\W*$")),
)


@dataclass(frozen=True)
class Classified:
    scenario: str
    matched: str = ""


def classify(text: str | None) -> Classified:
    t = " ".join(str(text or "").replace("’", "'").split())
    if not t:
        return Classified(SMALL_TALK)
    try:  # reuse the shared OnlyFans safety detector when present (distress/consent are never downgraded)
        from native_pilot import safety_detect
        for v in safety_detect.assess([t]):
            if v.category in ("distress", "consent") and not getattr(v, "ambiguous", False):
                return Classified(ESCALATE, f"safety:{v.category}")
    except Exception:
        pass
    for scenario, rules in _RULES:
        for rule in rules:
            m = rule.search(t)
            if m:
                return Classified(scenario, m.group(0)[:60])
    return Classified(SMALL_TALK)


def requested_items(text: str | None, persona) -> list:
    """Price-table items whose keywords the fan named, in persona order."""
    t = str(text or "").lower()
    return [p for p in persona.prices if any(re.search(rf"\b{re.escape(k)}\b", t) for k in p.keywords)]


def names_hard_no(text: str | None, persona) -> bool:
    t = str(text or "").lower()
    return any(re.search(rf"\b{re.escape(k)}\b", t) for k in persona.hard_no_keywords)
