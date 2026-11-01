"""A reply about a card that is already on the table, built from the offer's own data.

Replaces the hard-coded "That one is already in the card above." (OnlyFans complete-answers scope only).

The facts come through ONE typed interface, ``OfferFacts``, produced by an ``OfferFactsSource``. Today the
source reads the vault_sends rows of the card (price charged, number of files, kinds). R6 (the canonical quote
authority) replaces ``vault_send_facts`` with its own source; nothing here would change. Anything not in
``OfferFacts`` is never mentioned: no description, no price, no count is ever produced from memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy import select

from db.engine import get_session
from db.models import VaultItem, VaultSend


@dataclass(frozen=True)
class OfferFacts:
    offer_message_id: Optional[int]
    price_cents: Optional[int] = None        # the quote the card actually carries; None = unknown
    media_count: Optional[int] = None
    media_kinds: tuple = ()                  # subset of ("photo", "video")
    purchased: Optional[bool] = None


OfferFactsSource = Callable[[str, int, Optional[int]], Awaitable[Optional[OfferFacts]]]


async def vault_send_facts(account_id: str, fan_id: int, message_id: Optional[int]) -> Optional[OfferFacts]:
    if message_id is None:
        return None
    async with get_session() as s:
        rows = (await s.execute(select(VaultSend.media_id, VaultSend.price_cents, VaultSend.was_purchased).where(
            VaultSend.account_id == str(account_id), VaultSend.fan_id == int(fan_id),
            VaultSend.message_id == int(message_id)))).all()
        if not rows:
            return None
        kinds = (await s.execute(select(VaultItem.kind).where(
            VaultItem.account_id == str(account_id),
            VaultItem.media_id.in_([r[0] for r in rows])))).scalars().all()
    prices = {int(r[1]) for r in rows if r[1]}
    return OfferFacts(int(message_id), price_cents=prices.pop() if len(prices) == 1 else None,
                      media_count=len({r[0] for r in rows}),
                      media_kinds=tuple(sorted({k for k in kinds if k in ("photo", "video")})),
                      purchased=rows[0][2])


def compose(facts: Optional[OfferFacts]) -> str:
    """Pure text from the facts. No facts => a statement that needs none."""
    tail = "if u want something different just tell me what"
    if facts is None or facts.purchased is True:
        return (f"that one's already with u, {tail}" if facts is not None
                else f"that one's already in the card i sent, {tail}")
    bits = []
    if facts.media_count:
        noun = ("video" if facts.media_kinds == ("video",) else "pic" if facts.media_kinds == ("photo",) else "file")
        bits.append(f"{facts.media_count} {noun}{'s' if facts.media_count != 1 else ''}")
    if facts.price_cents:
        bits.append(f"${facts.price_cents / 100:.2f}".replace(".00", ""))
    what = (" (" + ", ".join(bits) + ")") if bits else ""
    return f"that one's still waiting in the card above{what}, unlock it there. {tail}"


async def existing_offer_reply(account_id: str, fan_id: int, detail, *, source: OfferFactsSource = None) -> str:
    try:
        mid = int(detail)
    except (TypeError, ValueError):
        mid = None
    try:
        facts = await (source or vault_send_facts)(account_id, fan_id, mid)
    except Exception:
        facts = None
    return compose(facts)
