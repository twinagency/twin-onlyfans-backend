"""service/automations/_price_ask.py — did the fan voice a price objection, and is anything blocking a cut.

FASTT's own detectors cover a stated cap ("can you do 15", `upsell.detect_stated_cap`) and a poverty plea
(`classify_decline` / `detect_spend_regret`). Neither sees a plain request for a better price, so the earned
discount (`upsell.may_discount`) never had an objection to answer on a gate-off account. These are those requests.
A decline or a money-trouble signal is judged FIRST and wins: "too much for me" is a decline, never an objection.

Pure (no DB, no I/O): the runner's post-sale scan and `ai_chatter._run_discount` both read the same function, so
what queues a job and what the job re-checks cannot disagree.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import upsell

PRICE_ASK = re.compile(
    r"\b(discount|cheaper|lower (?:the )?price|better price|any deal|a deal|price down|reduce (?:the |it )?price"
    r"|too (?:expensive|pricey|much)|bit (?:much|steep)|thats? (?:a lot|steep)|for less)\b", re.I)

BLOCK_DECLINED = "fan-declined"
BLOCK_DISTRESS = "fan-distress"


def objection_state(inbound: list[tuple[datetime, str]]) -> tuple[datetime | None, str | None]:
    """``(objection_at, blocker)`` over the fan's messages since the card, oldest first.

    ``blocker`` is set when ANY of them is a decline or a money-trouble signal (the conservative reading of
    "no decline or financial-distress signal"). ``objection_at`` is the newest message that stated a price cap or
    asked for a better price."""
    objection_at, blocker = None, None
    for created, body in inbound:
        if upsell.classify_decline(body):
            blocker = blocker or BLOCK_DECLINED
        if upsell.detect_spend_regret(body):
            blocker = blocker or BLOCK_DISTRESS
        if upsell.detect_stated_cap(body) or PRICE_ASK.search(body or ""):
            objection_at = created
    return objection_at, blocker


def beat_after(inbound: list[tuple[datetime, str]], objection_at: datetime | None) -> bool:
    """True when the fan spoke again after the objection (FASTT: never cut on the same breath he asked)."""
    return objection_at is not None and any(created > objection_at for created, _ in inbound)
