"""Commercial-claim checks for a model proposal.

A plain reply is conversation only. Prices, purchase/access statements and
pitches must come from an application-rendered offer (verified terms), never
from model prose. The structured fields (`pitch`, `price_claims`,
`purchase_claim`) are the model's declaration; the text patterns are a
defence-in-depth backstop for an undeclared claim. Neither is a perfect
classifier: real-model grounding still has to be measured separately.
"""
import re

from onlyfans_runtime.store import Refused

_PRICE = re.compile(r"(?:[$£€]\s*\d|\b(?:usd|aud|eur|gbp|cad)\b\s*\$?\s*\d|\d\s*(?:usd|aud|eur|gbp|dollars?|bucks|credits?|stars?)\b)", re.I)
_AMOUNT = re.compile(r"(?:(?:[$\u00a3\u20ac]|\b(?:usd|aud|eur|gbp|cad)\b)\s*\$?\s*(\d+(?:[.,]\d{1,2})?)"
                     r"|(\d+(?:[.,]\d{1,2})?)\s*(?:usd|aud|eur|gbp|cad|dollars?|bucks)\b)", re.I)
_PURCHASE = re.compile(r"\b(?:you(?:'ve|\s+have)?\s+(?:already\s+)?(?:purchased|bought|paid|unlocked)"
                       r"|(?:already|just)\s+(?:purchased|bought|paid\s+for|unlocked)"
                       r"|thanks?\s+for\s+(?:buying|purchasing)|your\s+purchase)\b", re.I)
_PITCH = re.compile(r"\b(?:buy|purchase|unlock|order)\b|\bfor\s+sale\b|\bon\s+offer\b|\bspecial\s+offer\b|\bdiscount\b|\bpay\b", re.I)


def _cents(text):
    out = set()
    for match in _AMOUNT.finditer(text):
        raw = (match.group(1) or match.group(2)).replace(',', '.')
        whole, _, frac = raw.partition('.')
        out.add(int(whole) * 100 + int((frac + '00')[:2]))
    return out


def check_plain_text(texts, proposal, sales_mode, verified_prices=frozenset()):
    """Refuse unsupported commercial content in model-written text.

    `sales_mode` is the effective mode after this proposal's own decline/reopen
    signal. `verified_prices` are cents from an already-sent offer that the
    engine matched for this message; only those may be restated in prose.
    Factual answers that carry none of these claims are untouched.
    """
    if any(cents not in verified_prices for cents in proposal.price_claims):
        raise Refused('unsupported price claim in model text')
    if proposal.purchase_claim:
        raise Refused('unsupported purchase claim in model text')
    if proposal.pitch and proposal.action == 'reply':
        raise Refused('a pitch must use a verified offer')
    if proposal.pitch and sales_mode == 'paused':
        raise Refused('sales are paused')
    for text in texts:
        if _PRICE.search(text) and not (_cents(text) and _cents(text) <= set(verified_prices)
                                        and not re.search(r"\b(?:credits?|stars?)\b", text, re.I)):
            raise Refused('unsupported price claim in model text')
        if _PURCHASE.search(text):
            raise Refused('unsupported purchase claim in model text')
        if sales_mode == 'paused' and _PITCH.search(text):
            raise Refused('sales are paused')
