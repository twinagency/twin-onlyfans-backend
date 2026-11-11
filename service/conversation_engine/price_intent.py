"""Which approved amount a new card carries: decided by application rules, never by the writer (Step 3).

The discount variant exists only when ALL hold: the fan explicitly asked for something cheaper, the live approved terms carry a
discount, and no card, queued send or dispatch trace already exists for this fan and product (an already-sent card keeps its
price; nothing is withdrawn or resent to manufacture a discount). "Too expensive", "not now" and "no" are declines, not asks.
"""
import re

from sqlalchemy import select

from db.onlyfans_delivery_models import OfTextIntent

_ASK = re.compile(
    r"\b(?:cheaper|cheapest|less\s+expensive|discounts?|discounted|(?:lower|better|reduced|smaller)\s+(?:the\s+)?(?:price|rate|cost)s?|"
    r"(?:any|a|some)\s+(?:deals?|offers?\s+on\s+(?:the\s+)?price)|(?:got|have|do\s+you\s+have|do\s+you\s+do)\s+(?:any\s+)?deals?|"
    r"(?:can|could|would)\s+you\s+(?:do|go)\s+(?:it\s+|that\s+)?(?:for\s+)?(?:less|lower|a\s+bit\s+less|a\s+little\s+less)|for\s+less)\b", re.I)
_NEGATED = re.compile(r"(?:(?:\bnot|n't|\bno\s+need\s+for|\bwithout|\bnever|\bdon'?t\s+need|\bdon'?t\s+want)\W+(?:\w+\W+){0,3}|\bno\W+(?:\w+\W+){0,1})$", re.I)


def asks_cheaper(text):
    """True only for an explicit request for a cheaper option or discount (not for a complaint, a decline or a percentage)."""
    if type(text) is not str:
        return False
    for match in _ASK.finditer(text):
        if not _NEGATED.search(text[:match.start()]):
            return True
    return False


_LIVE_CARD = ('queued', 'dispatching', 'accepted', 'unknown')


def discount_blocked(s, account, fan_id, item_id):
    """A reason string when this fan already has (or may have) a card for the product, else None."""
    rows = s.execute(select(OfTextIntent.status, OfTextIntent.wire_started_at, OfTextIntent.provider_message_id).where(
        OfTextIntent.account_id == account, OfTextIntent.fan_id == fan_id,
        OfTextIntent.catalog_item_id == item_id).limit(50)).all()
    for status, wire_started, provider_message in rows:
        if status in _LIVE_CARD or (status == 'cancelled' and (wire_started is not None or provider_message is not None)):
            return 'card_exists'
    return None


def variant_for(s, runtime, fan_id, item_id, fan_text):
    """'discount' only on an explicit ask, an approved discount and no existing card; otherwise 'standard'."""
    from onlyfans_runtime.catalogue_terms import live_terms
    if not asks_cheaper(fan_text):
        return 'standard'
    live = live_terms(s, runtime.account_id, item_id)
    if live is None or live.discount_price_cents is None:
        return 'standard'
    if discount_blocked(s, runtime.account_id, fan_id, item_id):
        return 'standard'
    return 'discount'
