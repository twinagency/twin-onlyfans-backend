"""Meaning-level repeat and completeness checks for the OnlyFans complete-answers scope (pure, no I/O).

Extends the existing echo family (``welcome_chatter_for_info._looks_like_echo`` compares a bubble with the fan's
last message; ``_dedupe_lead_reaction`` drops a repeated lead word) to the conversational MOVE: a repeated
challenge, pet name, sign-off, closing question, a get-to-know question he already answered, or a reworded loop.
There is no word ban: "trouble" is only flagged once it has become a repeated label in this chat.

``problems(candidate, ...)`` returns tags. Tags in ``TRIM`` can be cut out without losing the answer; the rest
need the one bounded regeneration (see ai_chatter) and, if they persist, surface the turn as unresolved.
"""
from __future__ import annotations

import re

from . import _complete_answers as _ca

CHALLENGE = re.compile(
    r"\b(prove it|prove u|make me|earn (?:it|that|me)|convince me|impress me|ask (?:me )?(?:nicely|nicer|politely|like u mean it)"
    r"|say please|beg(?:ging)?\b|try again|work for it|big talk|talk big|only if u|if u ask like|slow down|not so fast"
    r"|someone'?s eager|straight to it|can u handle|think u can handle)\b", re.I)
SIGNOFF = re.compile(r"\b(good ?night|gn|sweet dreams|talk (?:to u |to you )?(?:later|soon)|ttyl|gotta go|g2g|catch u later|"
                     r"sleep well|text me later)\b", re.I)
CLOSERS = {
    "whats_up": re.compile(r"\bwhat(?:'?s| is| are)?\s+(?:u|you|ya)\s+(?:up to|doing|been up to)\b|\bwhat(?:'s| is) (?:on )?(?:ur|your) mind\b", re.I),
    "tell_more": re.compile(r"\btell me (?:more|everything)\b|\bwhat about (?:u|you)\b", re.I),
    "what_want": re.compile(r"\bwhat (?:do )?(?:u|you) (?:want|wanna)(?: me to| to)?\b|\bwhat (?:should|would) (?:i|u|you)\b", re.I),
}
NICK_LABELS = re.compile(r"\b(trouble|bad boy|bad girl|naughty(?: boy| girl)?|bold|big guy|stud|tiger|champ|cutie|handsome|mr \w+)\b", re.I)
BELITTLE = re.compile(r"\b(poor thing|broke (?:boy|boys|baby)|stop sulking|dont sulk|don'?t sulk|mr sensitive|"
                      r"(?:u|you)(?:'re| r)? (?:so )?cheap)\b", re.I)
DOUBLE = re.compile(r"\b(is|the|a|to|and|of|it|that|u|you|i) \1\b", re.I)
_STOP = frozenset("the a an and or but to of in on at for with is are was were be been it its that this u ur you your i im me my we so "
                  "do does did not no yes just like get got have has had can will would up out all what when how why then than too "
                  "very really lol haha hey".split())
_GENERIC = frozenset({"babe", "baby", "hun", "honey", "love", "sexy"})

# tags that can be cut out of the reply without losing the answer
TRIM = {"signoff", "closing_question", "nickname"}


def _bubbles(recent_out):
    return [b for b in recent_out if b and b.strip()]


def _moves(text: str) -> set[str]:
    out = set()
    if CHALLENGE.search(text):
        out.add("challenge")
    if SIGNOFF.search(text):
        out.add("signoff")
    for name, rule in CLOSERS.items():
        if rule.search(text):
            out.add("closing:" + name)
    for topic in _ca.asked_topics(text):
        out.add("ask:" + topic)
    return out


def _tail_word(text: str) -> str:
    m = re.search(r"([a-z]{3,12})[^a-z]*$", text.lower())
    return m.group(1) if m else ""


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z']{3,}", text.lower())
    return {re.sub(r"(ing|ed|s)$", "", w) for w in words if w not in _STOP}


def problems(candidate: str, recent_out, *, known: set[str] = frozenset(), fan_name: str = "",
             window: int = 4) -> list[str]:
    """Tags for what is wrong with ``candidate`` given the AI's last ``window`` bubbles."""
    text = " ".join((candidate or "").split())
    prior = _bubbles(recent_out)[-window:]
    tags: list[str] = []
    if not text:
        return tags
    moves = _moves(text)
    prior_moves = [_moves(b) for b in prior]
    if "challenge" in moves and sum("challenge" in m for m in prior_moves[-2:]) >= 1:
        tags.append("challenge")
    if "signoff" in moves and any("signoff" in m for m in prior_moves):
        tags.append("signoff")
    for mv in sorted(moves):
        if mv.startswith("closing:") and sum(mv in m for m in prior_moves[-3:]) >= 2:
            tags.append("closing_question")
            break
    for topic in sorted(_ca.asked_topics(text)):
        if topic in known or sum(("ask:" + topic) in m for m in prior_moves) >= 1:
            tags.append("reask:" + topic)
    # pet names / labels
    labels = {m.group(1).lower() for m in NICK_LABELS.finditer(text)}
    tail = _tail_word(text)
    if tail and tail not in _GENERIC and tail not in _STOP and tail != (fan_name or "").lower():
        if sum(_tail_word(b) == tail for b in prior) >= 2:
            labels.add(tail)
    for label in labels:
        if sum(label in b.lower() for b in prior) >= 2:
            tags.append("nickname")
            break
    # same move pattern in different words (paraphrased loop)
    if moves and len(prior_moves) >= 2 and sum(bool(moves & m) for m in prior_moves[-2:]) == 2 and not tags:
        tags.append("move_loop")
    # reworded repetition of an earlier bubble
    mine = _tokens(text)
    if len(mine) >= 4:
        for b in prior:
            theirs = _tokens(b)
            if len(theirs) >= 4 and len(mine & theirs) / len(mine | theirs) >= 0.6:
                tags.append("paraphrase")
                break
    if BELITTLE.search(text):
        tags.append("belittling")
    return list(dict.fromkeys(tags))


_QUESTIONISH = re.compile(r"\?|^\s*(?:do|does|did|can|could|will|would|is|are|how|what|when|where|why|who|which)\b", re.I | re.M)


def incomplete(reply: str, unanswered, *, kind: str = "") -> str:
    """"" when the reply can stand, else a reason. A tiny reply to a real question/request, or a stuttered stopword."""
    words = re.findall(r"[a-zA-Z0-9']+", reply or "")
    if DOUBLE.search(reply or ""):
        return "stutter"
    asked = any(_QUESTIONISH.search(m or "") for m in unanswered)
    if asked and len(words) <= 2 and "?" not in (reply or "") and kind not in ("rate_pic", "react_pic"):
        return "fragment"
    return ""


_ES = re.compile(r"[¿¡ñáéíóú]|\b(?:que|quieres|quiero|mami|papi|mi |te |tu |estás|estoy|eres|bello|rico|cómo|como estas|nos quedamos|más|mas|"
                 r"hola|muy|con|para|pero|porque|esta|dime|ahora|todita)\b", re.I)
_EN_FUNC = re.compile(r"\b(?:the|you|your|u|ur|and|what|how|are|is|with|that|this|for|but|its|i'?m|im|im)\b", re.I)


def language_mismatch(fan_text: str, reply: str) -> bool:
    """He wrote Spanish and the reply is English (the only mismatch we have evidence for). Conservative:
    needs at least two Spanish markers in his text and no Spanish marker in the reply."""
    if not fan_text or not reply:
        return False
    return len(_ES.findall(fan_text)) >= 2 and not _ES.search(reply) and len(_EN_FUNC.findall(reply)) >= 1


def trim(text: str, tags) -> str:
    """Cut the repeated tail pieces (sign-off / closing question / pet name) when an answer remains. Returns the
    original text when cutting would leave nothing."""
    if not set(tags) & TRIM:
        return text
    parts = [p for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    keep = [p for p in parts if not (("signoff" in tags and SIGNOFF.search(p))
                                     or ("closing_question" in tags and p.rstrip().endswith("?")
                                         and any(r.search(p) for r in CLOSERS.values())))]
    out = " ".join(keep).strip()
    if "nickname" in tags:
        out = NICK_LABELS.sub("", out)
        out = re.sub(r"\s+([,.!?])", r"\1", out)
        out = re.sub(r",\s*([.!?]|$)", r"\1", out)
        out = " ".join(out.split())
    return out or text
