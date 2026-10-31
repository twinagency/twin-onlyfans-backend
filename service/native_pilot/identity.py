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
    """True when the fan asks whether she is a bot / AI / real, or asks for a real person. Same detector the prompt ladder uses."""
    return detect_bot_accusation(text) or wants_human(text)


# Fixed public reason (never message text): the send boundary refused a self-disclosing bubble.
IDENTITY_HELD = "identity-disclosure-held"
# What the control audit records when the runner hands the chat to a human.
IDENTITY_REASON = "Fan asked whether he is talking to AI, or a reply mentioned AI; a human must answer."


# ── engine v2: "I want a real person", and replies that falsely deny automation ───────────────────────────────────
_WANT_HUMAN = (
    re.compile(r"\b(?:i\s*(?:want|need|wanna)|let\s+me|can\s+i|could\s+i|put\s+me\s+(?:through\s+)?(?:to|with)|connect\s+me\s+(?:to|with)|get\s+me)\s+"
               r"(?:to\s+)?(?:talk|speak|chat|text|message)?\s*(?:to|with)?\s*(?:a\s+|an\s+|the\s+)?(?:real|actual|live|living)\s+(?:person|human|girl|woman|one)\b", re.I),
    re.compile(r"\b(?:talk|speak|chat)(?:ing)?\s+(?:to|with)\s+(?:a\s+)?(?:real|actual|live)\s+(?:person|human|girl|woman)\b", re.I),
    re.compile(r"\b(?:i\s*(?:want|need|wanna)|can\s+i\s+(?:talk|speak)\s+(?:to|with))\s+(?:to\s+talk\s+to\s+)?(?:a\s+)?human\b", re.I),
    re.compile(r"\b(?:is\s+this|are\s+you|am\s+i\s+talking\s+to)\s+(?:really\s+|actually\s+|truly\s+)?(?:her|the\s+(?:real|actual)\s+\w+)\s*(?:\?|$)", re.I),
    re.compile(r"\bnot\s+(?:a\s+)?(?:bot|ai|robot|machine)\b.*\b(?:real|human|person|her)\b", re.I),
    re.compile(r"\b(?:talk|speak)\s+to\s+(?:the\s+)?(?:creator|owner|manager|admin|support|someone\s+real)\b", re.I),
    re.compile(r"\b(?:talk|speak|chat)(?:ing)?\s+(?:to|with)\s+(?:a\s+)?(?:human|person)\b", re.I),
    re.compile(r"\bget\s+me\s+(?:the\s+)?(?:creator|owner|a\s+human|a\s+real\s+person|someone\s+real)\b", re.I),
)


def wants_human(text: str | None) -> bool:
    """The fan asks for a real person / the creator herself. The AI does not answer; the chat goes to the staff handoff."""
    if not text:
        return False
    return any(rule.search(text) for rule in _WANT_HUMAN)


_END = r"(?=\s*(?:[.!?,;:]|$|\s(?:babe|baby|lol|haha|though|tho|promise|i\s+swear|ok|okay|sweetie|hun|honey)\b))"
_NOTBOT = r"(?:bot|ai|a\.i\.|robot|chat\s*bot|machine|fake|automated|automation)"
_DENY = (
    # "I'm real", "I'm a real person", "I'm human" (but not "I am a real fan of that")
    re.compile(r"\b(?:i\s*'?\s*m|im|i\s+am)\s+(?:a\s+|an\s+)?(?:really\s+|actually\s+|totally\s+|100\s*%\s*)?(?:real|human)(?:\s+(?:person|human|girl|woman|one))?" + _END, re.I),
    # "I'm not a bot", "this is not a bot", "you're not talking to a bot", or a sentence that is just "not a bot lol"
    re.compile(r"\b(?:i\s*'?\s*m|im|i\s+am|this\s+is|(?:you|u)\s*(?:'?re|r|are)|that\s*'?s)\s+not\s+(?:talking\s+to\s+)?(?:a\s+|an\s+)?" + _NOTBOT + r"\b", re.I),
    re.compile(r"^\W*not\s+(?:a\s+|an\s+)?" + _NOTBOT + r"\b", re.I),
    re.compile(r"\b(?:it'?s|this\s+is|its)\s+(?:really|actually|truly)\s+(?:me|her)\b", re.I),
    re.compile(r"\b(?:i\s*'?\s*m|im|i\s+am)\s+(?:typing|texting|writing|replying)\s+(?:this|these|all\s+of\s+this|every\s+(?:one|message))?\s*(?:myself|personally)\b", re.I),
    re.compile(r"\b(?:i|me)\s+(?:personally|myself)\s+(?:am\s+)?(?:typing|texting|writing|replying)\b", re.I),
    re.compile(r"\bno\s+one\s+else\s+(?:is\s+)?(?:typing|texting|replying|chatting|answering)\b", re.I),
)


def denies_automation(text: str | None) -> bool:
    """A reply that claims to be a real person / the creator typing personally, or denies being automated. Never sent."""
    if not text:
        return False
    return any(rule.search(text) for rule in _DENY)


HUMAN_REQUESTED = "human-requested"
