"""Render a playbook template in a persona's voice. Deterministic for a given seed."""
from __future__ import annotations

import random
import re

EMOJI = {"heart": "❤️", "cry": "😭", "eyes": "👀", "kiss": "😘", "laugh": "😂", "shy": "🥺", "fire": "🔥"}
_ELONGATED = re.compile(r"([a-z])\1{2,}", re.I)
_DASHES = re.compile(r"\s*[—–]\s*")


class MissingFact(KeyError):
    pass


def money(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def render(template: str, persona, *, seed: int = 0, item: str | None = None, price_cents: int | None = None) -> str:
    rng = random.Random(seed)
    v = persona.voice

    def sub(m):
        tok = m.group(1)
        if tok == "pet":
            return rng.choice(v.pet_names) if v.pet_names else ""
        if tok == "item":
            if item is None:
                raise MissingFact("item")
            return item
        if tok == "price":
            if price_cents is None:
                raise MissingFact("price")
            return money(price_cents)
        if tok.startswith("e:"):
            return EMOJI.get(tok[2:], "") if v.emojis else ""
        key = tok[5:]
        if key not in persona.facts:
            raise MissingFact(key)
        return persona.facts[key]

    out = re.sub(r"\{([^}]*)\}", sub, template)
    if not v.elongate:
        out = _ELONGATED.sub(r"\1", out)
    out = _DASHES.sub(", ", out)                       # house rule: no em/en dashes in fan text
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    if v.lowercase:
        out = out.lower() if not price_cents else re.sub(r"[A-Z]", lambda c: c.group(0).lower(), out)
    return out


def violates_never_say(text: str, persona) -> bool:
    low = text.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in persona.voice.never_say)
