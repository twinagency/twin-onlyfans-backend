"""OnlyFans identity rule: the bot never mentions being AI, a bot or a clone, and never claims to be human.

Two pieces, both pure (no DB, no I/O):

* ``sincere_identity_question(text)``: the fan asked whether he is talking to a bot / AI / a real
  person. The AI does not answer that turn at all; the runner pauses the chat and flags it for a
  human (``Service.identity_handoff``), who answers and hands back with the normal controls.
* ``discloses_ai(text)``: the final outbound guard. A reply that says it is an AI, a bot, a clone
  or automated is held (never sent, never resent) and the chat is handed to a human the same way.
"""
from __future__ import annotations

import re

from automations._common import detect_bot_accusation

# First-person self-description as AI / bot / clone / automated, in the casual register the model uses.
_SELF = r"(?:i\s*'?\s*a?m|im|i\s+am|this\s+is|it\s*'?\s*s|its|ur\s+talking\s+to|you\s*'?\s*re\s+talking\s+to|you\s+are\s+talking\s+to|u\s*r\s+talking\s+to)"
_WHAT = (r"(?:an?\s+)?(?:\w+\s+){0,3}?(?:a\.?i\.?|ai|bot|chat\s*bot|chatbot|clone|virtual\s+(?:assistant|girl(?:friend)?)"
         r"|language\s+model|llm|automated|automation|program|assistant)")
_RULES = (
    re.compile(rf"\b{_SELF}\s+{_WHAT}\b", re.I),
    re.compile(r"\b(?:ai|a\.i\.)\s+(?:chat\s+)?(?:clone|version|twin|copy|model)\b", re.I),
    re.compile(r"\b(?:her|the\s+creator'?s?|nicky'?s?|my)\s+(?:ai|a\.i\.|digital|virtual)\s+(?:chat\s+)?(?:clone|twin|copy|version)\b", re.I),
    re.compile(r"\bas\s+an\s+(?:ai|a\.i\.|language\s+model|assistant)\b", re.I),
    re.compile(r"\b(?:not|isn'?t|aren'?t)\s+(?:really\s+)?(?:her|the\s+creator)\s+(?:personally\s+)?(?:typing|texting|replying)\b", re.I),
    re.compile(r"\b(?:i\s*'?\s*m|im|i\s+am)\s+not\s+(?:a\s+)?(?:real|human|actually\s+her)\b", re.I),
)


def discloses_ai(text: str | None) -> bool:
    """True when ``text`` (one outbound bubble) tells the fan it is an AI, bot, clone or automated."""
    if not text:
        return False
    return any(rule.search(text) for rule in _RULES)


def sincere_identity_question(text: str | None) -> bool:
    """True when the fan asks whether she is a bot / AI / real. Same detector the prompt ladder uses."""
    return detect_bot_accusation(text)


# Fixed public reason (never message text): the send boundary refused a self-disclosing bubble.
IDENTITY_HELD = "identity-disclosure-held"
# What the control audit records when the runner hands the chat to a human.
IDENTITY_REASON = "Fan asked whether he is talking to AI, or a reply mentioned AI; a human must answer."
