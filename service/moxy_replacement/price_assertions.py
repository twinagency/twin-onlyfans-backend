"""Bound explicit monetary assertions to their own item or evidenced source.

This is a deterministic screen for written currency amounts, not a general
language/discount classifier. Unmarked amounts and paraphrases still require
model evaluation. Historical quotes report what was said, never current terms.
"""
from decimal import Decimal, InvalidOperation
import re

from .conversation import pending_messages, quotes
from .parts import plan


_MONEY = re.compile(
    r"(?P<prefix>\b(?:USD|AUD|CAD|EUR|GBP)\s*\$?|US\$|A\$|C\$|\$|€|£)\s*"
    r"(?P<p_amount>[+-]?\d[\d,.]*)"
    r"|(?P<s_amount>[+-]?\d[\d,.]*)\s*(?P<suffix>USD|AUD|CAD|EUR|GBP|dollars?|euros?|pounds?\s+sterling)\b",
    re.I,
)
_NUMBER = re.compile(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?")
_BREAK = re.compile(r"(?<!\d)[.!?;,\n]+|[.!?;,\n]+(?!\d)|\b(?:but|and)\b", re.I)
_CARD_ID = re.compile(r"\b(?:card|message)\s*(?:number\s*|#\s*)?(\d+)\b", re.I)
_CHEAPEST_CARD = re.compile(
    r"\b(?:the one in (?:the )?chat|this(?:\s+(?:one|card))?|that\s+(?:one|card)|(?:the\s+)?card|it)"
    r"(?:\s+is|'s)\s+(?:(?:already|actually|currently)\s+)?(?:my|the)\s+cheapest\b", re.I)
_BUDGET_BEFORE = re.compile(
    r"\b(?:your budget(?:\s+(?:is|was|of))?|you\s+(?:only\s+)?(?:have|had|can spend|could spend))"
    r"(?:\s+(?:just|only|up to|exactly))?\s*$", re.I,
)
_SOURCE_BUDGET = re.compile(
    r"^\s*(?:(?:actually|now|well)[,:]?\s+)?"
    r"(?:my budget(?:\s+(?:is|of))?|i\s+(?:only\s+)?(?:have|can spend))"
    r"(?:\s+(?:just|only|up to|exactly))?\s*$", re.I,
)


def _normal(text):
    return " ".join(str(text).replace("’", "'").split()).casefold()


def _money(match):
    value = (match.group("p_amount") or match.group("s_amount")).rstrip(".,")
    marker = re.sub(r"\s+", "", (match.group("prefix") or match.group("suffix")).upper())
    currency = ({"$": "USD", "US$": "USD", "DOLLAR": "USD", "DOLLARS": "USD",
                 "A$": "AUD", "C$": "CAD", "€": "EUR", "EURO": "EUR", "EUROS": "EUR",
                 "£": "GBP", "POUNDSTERLING": "GBP", "POUNDSSTERLING": "GBP",
                 "USD$": "USD", "AUD$": "AUD", "CAD$": "CAD", "EUR$": "EUR", "GBP$": "GBP"}).get(marker, marker)
    try:
        cents = int(Decimal(value.replace(",", "")) * 100) if _NUMBER.fullmatch(value) else None
    except (InvalidOperation, ValueError, OverflowError):
        cents = None
    return cents, currency


def _clauses(text):
    spans = quotes(text)
    start = 0
    for boundary in _BREAK.finditer(text):
        if any(a <= boundary.start() < b for a, b, _ in spans):
            continue
        yield text[start:boundary.start()]
        start = boundary.end()
    yield text[start:]


def _quoted_source(snapshot, clause, match):
    for begin, end, quote in quotes(clause):
        if not begin <= match.start() < end:
            continue
        prefix = clause[:begin]
        if re.search(r"\b(?:you (?:said|wrote|mentioned)|your (?:words|message))\s*[:,-]?\s*$", prefix, re.I):
            direction = "in"
        elif re.search(r"\b(?:i (?:said|wrote|quoted)|my (?:earlier )?(?:words|message))\s*[:,-]?\s*$", prefix, re.I):
            direction = "out"
        else:
            return False
        sources = snapshot.get("history", []) + pending_messages(snapshot)
        return any(m.get("direction") == direction and _normal(quote) in _normal(m.get("text", ""))
                   for m in sources)
    return False


def _budget_claim(snapshot, clause, match):
    before, after = clause[:match.start()], clause[match.end():]
    attributed = bool(_BUDGET_BEFORE.search(before)
                      or re.match(r"\s+(?:is|was)\s+your budget\b", after, re.I)
                      or (re.search(r"\byour\s*$", before, re.I)
                          and re.match(r"\s+budget\b", after, re.I)))
    if not attributed:
        return None
    amount, currency = _money(match)
    known = snapshot.get("fan_state", {}).get("budget_cents")
    if type(known) is int:
        return currency == "USD" and amount == known
    # A currently quoted budget may precede persistence. Only current incoming
    # evidence can supply it here; old history cannot resurrect a superseded limit.
    latest_budget = None
    for message in pending_messages(snapshot):
        if message.get("direction") != "in":
            continue
        for part in _clauses(message.get("text", "")):
            for candidate in _MONEY.finditer(part):
                if _SOURCE_BUDGET.search(part[:candidate.start()]):
                    latest_budget = _money(candidate)
    return amount is not None and latest_budget == (amount, currency)


def _card_target(snapshot, clause, *, allow_current):
    cards = {str(c.get("id")): c for c in snapshot.get("card_state", [])
             if type(c.get("id")) is int and type(c.get("price_cents")) is int}
    explicit = _CARD_ID.findall(clause)
    if explicit:
        return cards.get(explicit[0]) if len(set(explicit)) == 1 else None
    if not allow_current:
        return None
    incoming = pending_messages(snapshot)
    latest = incoming[-1].get("text", "") if incoming else ""
    referenced = _CARD_ID.findall(latest)
    if referenced:
        return cards.get(referenced[0]) if len(set(referenced)) == 1 else None
    if re.search(r"\b(?:last|latest)\s+card\b", latest, re.I):
        last = (snapshot.get("commercial_state") or {}).get("last_offer") or {}
        return cards.get(str(last.get("message_id")))
    if len(cards) == 1 and re.search(r"\b(?:card|that|it|this)\b", latest, re.I):
        return next(iter(cards.values()))
    return None


def _selected_target(snapshot, decision, terms):
    key = decision.get("product_id")
    if key is None:
        return None
    if type(terms) is dict and type(terms.get("price_cents")) is int:
        return terms
    return snapshot.get("inventory", {}).get(key) or snapshot.get("products", {}).get(key)


def _cheapest_card_problem(snapshot, decision, selected, clause):
    """Reject a definite cheapest claim only when a cheaper same-category item is offerable."""
    match = _CHEAPEST_CARD.search(clause)
    if not match or any(start <= match.start() < end for start, end, _ in quotes(clause)):
        return False
    if re.search(r"\b(?:not|never|wrong|incorrect|didn't|did not)\b", clause[:match.start()], re.I):
        return False
    key = decision.get('product_id')
    target = selected
    if key is None:
        cards = snapshot.get('card_state') or []
        if len(cards) != 1:
            return False  # The claim has no unique card target.
        target = cards[0]
        key = target.get('product_id')
    product = snapshot.get('products', {}).get(key, {})
    category = product.get('category')
    price = target.get('price_cents') if isinstance(target, dict) else None
    lowest = (snapshot.get('commercial_state', {}).get('cheapest_offerable_cents_by_category') or {}).get(category)
    return type(price) is int and type(lowest) is int and lowest < price


def price_problem(snapshot, decision, terms=None):
    """Return a scoped correction reason, or None for supported/no money claims.

    Use the exact inventory/dispatch terms for a selected item. An explicit card
    identifier can bind a separate historical assertion. With no selected item,
    current unambiguous card references support discussion without a new sale.
    Unsupported/ambiguous numeric assertions fail instead of selecting an item.
    """
    selected = _selected_target(snapshot, decision, terms)
    for part in plan(decision):
        for clause in _clauses(part["text"]):
            if _cheapest_card_problem(snapshot, decision, selected, clause):
                return 'price_claim_mismatch'
            for match in _MONEY.finditer(clause):
                if _quoted_source(snapshot, clause, match):
                    continue
                budget = _budget_claim(snapshot, clause, match)
                if budget is not None:
                    if not budget:
                        return "price_claim_unbound"
                    continue
                card = _card_target(snapshot, clause, allow_current=selected is None)
                # An explicit unknown card must not fall through to the selected
                # product simply because its price happens to match.
                if _CARD_ID.search(clause) and card is None:
                    return "price_claim_unbound"
                target = card or selected
                if not target or type(target.get("price_cents")) is not int:
                    return "price_claim_unbound"
                amount, currency = _money(match)
                expected_currency = target.get("currency") or "USD"
                if amount != target["price_cents"] or currency != expected_currency:
                    return "price_claim_mismatch"
    return None
