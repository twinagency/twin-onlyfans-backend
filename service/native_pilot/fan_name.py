"""The name a fan GAVE in this conversation (pure, no I/O).

The account's display name is often not his name (a test fan's account is "ronan", he says "I'm Jesse"). The latest thing the fan
himself said wins, including a correction ("no it's Jesse, not Ronan"). Only first names introduced with an explicit
self-introduction are taken; a mood word after "I'm" (I'm good, I'm tired) never is.
"""
from __future__ import annotations

import re

_NOT_NAMES = frozenset("""good fine great ok okay well bored tired horny here back there just so not very really sorry sure ready
home at in on a an the your you me my to for from with from still also already always never now then too only gonna going looking
trying thinking wondering free busy single married lonely drunk high sober happy sad mad angry hot cold hungry sleepy awake up down
out off over late early new old next last first real serious joking kidding hard wet naked alone bi gay straight male female guy man
boy girl dude bro sir daddy baby babe honey yes no yeah yep nope maybe please thanks thank hello hi hey sup what who why how when
where here""".split())
_NAME = r"([A-Z][a-zA-Z'\-]{1,19})"
_ANY = r"([A-Za-z][a-zA-Z'\-]{1,19})"
# explicit introductions: capital letter required after "I'm / it's / this is"; "my name is / call me" accept any case
_CAP = re.compile(r"\b(?:I'?m|Im|I am|it'?s|It'?s|its|Its|this is|This is)\s+" + _NAME + r"\b")
_NAMED = re.compile(r"\b(?:my name is|my name'?s|name'?s|call me|you can call me|people call me|everyone calls me)\s+" + _ANY + r"\b", re.I)
_CORRECT = re.compile(r"\b(?:no|nope|not [A-Za-z]+|actually|its|it'?s)[,.]?\s*(?:it'?s|its|i'?m|im)?\s*" + _NAME + r"\b(?:\s*,?\s*not\s+[A-Za-z]+)?")
_NOT_X = re.compile(r"\b(?:i'?m|im|it'?s|its|my name is)?\s*" + _ANY + r"\s*,?\s*not\s+[A-Za-z]+\b", re.I)


def _clean(word: str) -> str | None:
    word = word.strip("'-")
    if not 2 <= len(word) <= 20 or word.lower() in _NOT_NAMES:
        return None
    return word[:1].upper() + word[1:].lower() if word.isupper() or word.islower() else word


def given_name(texts) -> str | None:
    """The latest name the fan introduced himself with, from his messages OLDEST FIRST; None when he never did."""
    found = None
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        hits = []
        for rule in (_CAP, _NAMED):
            for m in rule.finditer(text):
                hits.append((m.start(), m.group(1)))
        # "it's Jesse, not Ronan" / "no, it's Jesse": the word BEFORE "not" is the name
        m = re.search(r"\b(?:it'?s|its|i'?m|im|my name is|call me)\s+" + _ANY + r"\s*,?\s*(?:and\s+)?not\s+[A-Za-z]+", text, re.I)
        if m:
            hits.append((m.start(), m.group(1)))
        m = re.search(r"\b(?:no|nope)[,.!]?\s+(?:it'?s|its|i'?m|im)\s+" + _ANY + r"\b", text, re.I)
        if m:
            hits.append((m.start(), m.group(1)))
        for _, word in sorted(hits):
            name = _clean(word)
            if name:
                found = name
    return found
