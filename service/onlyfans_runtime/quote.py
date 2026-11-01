"""ONE canonical quote and delivery lifecycle (R6), carried on the existing ``content_offers`` record.

There is no second ledger. ``content_offers`` already is the price snapshot the unlock watcher and the native text
delivery read (``price_cents``, ``item_id``, ``status``); R6 adds the missing parts of the quote to that same row
(``quote_json``, ``currency``, ``obligation_id``, ``lifecycle``, ``quote_version``).

Writers: only :func:`price` computes an amount (and only through the existing policy: ``upsell.may_discount`` and
``upsell.discount_price``, applied per creator and never globally). Readers (chat wording, the locked card, the pack
sender, the upseller) take the amount from a :class:`Quote` or from :func:`format_price`; they do not compute one.

Pure functions come first (no I/O); the persistence helpers take a SQLAlchemy ``Session``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal

PROVIDER_MIN_CENTS = 300          # automations._price_limits.OF_PRICE_FLOOR_CENTS and the media_shape CHECK (>= 300)
PROVIDER_MAX_CENTS = 20000        # media_shape CHECK (<= 20000)
ROUTES = ("tip", "locked_card", "custom_service")
BUDGET_SCOPES = ("per_item", "lifetime")

# lifecycle: selected/quoted -> awaiting payment -> durable dispatch intent -> provider accepted -> local projection
# reconciled -> purchased/unlocked -> fulfilled; escalated is reachable from anywhere a human has to take over.
QUOTED, AWAITING, INTENT, ACCEPTED, RECONCILED, PURCHASED, FULFILLED, ESCALATED, CANCELLED = (
    "quoted", "awaiting_payment", "dispatch_intent", "provider_accepted", "reconciled", "purchased", "fulfilled",
    "escalated", "cancelled")
FORWARD = {QUOTED: (AWAITING, CANCELLED), AWAITING: (INTENT, CANCELLED), INTENT: (ACCEPTED, AWAITING, CANCELLED),
           ACCEPTED: (RECONCILED,), RECONCILED: (PURCHASED, CANCELLED), PURCHASED: (FULFILLED,), FULFILLED: (),
           CANCELLED: (), ESCALATED: ()}
RECEIPT_STATES = (RECONCILED, PURCHASED, FULFILLED)       # the card is known to be on the fan's screen
PAID_STATES = (PURCHASED, FULFILLED)


class QuoteError(ValueError):
    pass


def format_price(cents: int, currency: str = "USD") -> str:
    """The one place a price is rendered: integer units in, '$79.99' out. USD only renders with '$'."""
    cents = int(cents)
    symbol = "$" if currency.upper() == "USD" else currency.upper() + " "
    return f"{symbol}{cents // 100}.{cents % 100:02d}"


_AMOUNT = re.compile(r"(?:\$|usd\s*)\s*(\d{1,5})(?:\.(\d{1,2}))?", re.I)


def amounts_in(text: str) -> list[int]:
    """Every dollar amount named in ``text`` as integer cents."""
    out = []
    for whole, frac in _AMOUNT.findall(text or ""):
        out.append(int(whole) * 100 + int((frac or "0").ljust(2, "0")))
    return out


def text_consistent(text: str, quote: "Quote") -> bool:
    """Chat/card/caption text may only name the quoted amount (or nothing)."""
    return all(a == quote.price_cents for a in amounts_in(text))


def obligation_id(route: str, account_id: str, fan_id: int, subject: str) -> str:
    """Deterministic: every send path computing the same obligation reaches the same id, so a second path finds the
    first one's open request instead of asking again."""
    if route not in ROUTES:
        raise QuoteError("unknown payment route")
    return hashlib.sha256(f"{route}|{account_id}|{int(fan_id)}|{subject}".encode()).hexdigest()[:40]


@dataclass(frozen=True)
class Quote:
    obligation_id: str
    route: str
    item_id: int
    assets: tuple                          # ((media_id, version), ...)
    list_cents: int
    price_cents: int
    currency: str = "USD"
    discount_pct: int = 0
    discount_reason: str = "none"
    budget_cents: int | None = None
    budget_scope: str | None = None
    accepted: bool = False
    version: int = 1

    def as_json(self) -> str:
        return json.dumps({**asdict(self), "assets": [list(a) for a in self.assets]}, sort_keys=True)

    @staticmethod
    def from_json(raw: str) -> "Quote":
        d = json.loads(raw)
        d["assets"] = tuple(tuple(a) for a in d["assets"])
        return Quote(**d)


@dataclass(frozen=True)
class PriceOutcome:
    kind: str                              # ok | below_floor | over_budget | invalid
    quote: Quote | None = None
    explanation: str = ""
    smaller_items: tuple = ()              # eligible cheaper alternatives, caller-supplied (ids), never invented


def price(*, route: str, account_id: str, fan_id: int, item_id: int, assets, list_cents: int, subject: str | None = None,
          currency: str = "USD", cfg: dict | None = None, discount_ctx=None, discount_count: int = 0,
          original_cents: int | None = None, budget_cents: int | None = None, budget_scope: str | None = None,
          floor_cents: int | None = None, cheaper: tuple = (), agreed_over_budget: bool = False) -> PriceOutcome:
    """The single pricing function. Never raises a price silently; an unreachable budget is explained, not charged."""
    if type(list_cents) is not int or list_cents < PROVIDER_MIN_CENTS or list_cents > PROVIDER_MAX_CENTS:
        return PriceOutcome("invalid", None, "list price outside the provider range")
    if budget_cents is not None and budget_scope not in BUDGET_SCOPES:
        raise QuoteError("a stated budget needs its scope: per_item or lifetime")
    floor = max(PROVIDER_MIN_CENTS, int(floor_cents or 0))
    cfg = cfg or {}
    px, pct, why = list_cents, 0, "none"
    # Discount: only where THIS creator's policy allows it (the saved switch), only on a qualifying request, and the
    # policy's own cap (15% first cut, 20% total, a third refused) -- never on silence or distress.
    if cfg.get("fastt_discount_resend") is True and discount_ctx is not None:
        from automations import upsell
        ok, reason = upsell.may_discount(discount_ctx)
        if ok:
            basis = original_cents or list_cents
            second = discount_count >= 1
            pct = 20 if second else 15
            px = upsell.discount_price(list_cents, None, account_id, discount_pct=pct / 100, min_cents=floor,
                                       original_cents=basis)
            why = "second_cut" if second else "first_cut"
        else:
            why = "refused:" + reason
    if px < floor:
        return PriceOutcome("below_floor", None, f"{format_price(floor, currency)} is the lowest this creator can charge here",
                            tuple(cheaper))
    if budget_cents is not None and px > budget_cents and not agreed_over_budget:
        return PriceOutcome("over_budget", None,
                            f"{format_price(px, currency)} is above the stated {format_price(budget_cents, currency)} "
                            f"({budget_scope.replace('_', ' ')}); this item cannot be offered lower",
                            tuple(cheaper))
    q = Quote(obligation_id(route, account_id, fan_id, subject or f"item:{item_id}"), route, int(item_id),
              tuple((int(m), str(v)) for m, v in assets), int(list_cents), int(px), currency.upper(), pct, why,
              budget_cents, budget_scope)
    return PriceOutcome("ok", q)


def accept(quote: Quote) -> Quote:
    return Quote(**{**asdict(quote), "assets": quote.assets, "accepted": True})


def reprice(quote: Quote, new_cents: int, *, agreement: bool = False) -> Quote:
    """An accepted quote is immutable; a corrected earlier invalid quote needs the fan's agreement before charging."""
    if new_cents == quote.price_cents:
        return quote
    if quote.accepted and not agreement:
        raise QuoteError("an accepted quote is never changed without the fan's agreement")
    if new_cents > quote.price_cents and not agreement:
        raise QuoteError("a quote is never raised silently")
    return Quote(**{**asdict(quote), "assets": quote.assets, "price_cents": int(new_cents),
                    "version": quote.version + 1, "accepted": False})


# ── wording gate: delivery language must match the verified order state ───────────────────────────────────────────
_SENT_CLAIM = re.compile(r"\b(?:i(?:'ve| have)?\s+(?:just\s+)?sent|just\s+sent|sent\s+(?:it|you|u)|"
                         r"(?:it(?:'s| is)|its)\s+(?:on\s+its\s+way|sent|delivered|in\s+your\s+(?:inbox|dms?))|"
                         r"check\s+(?:your|ur)\s+(?:inbox|dms?|messages)|it\s+was\s+delivered)\b", re.I)
_DEADLINE = re.compile(r"\b(?:tonight|tomorrow|today by|by\s+(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)\w*|"
                       r"within\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|days?)|in\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|days?)|"
                       r"next\s+(?:week|day)|by\s+(?:morning|noon|evening))\b", re.I)
_REFUND_DONE = re.compile(r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:already\s+)?refund(?:ed)?\b|"
                          r"\brefund\s+(?:is|has\s+been|was)\s+(?:done|processed|issued|sent|complete)|"
                          r"\byou(?:'ve| have)\s+been\s+refunded|\bi\s+refunded\b", re.I)


def wording_gate(text: str, lifecycle: str | None, *, fulfilment_basis: bool = False,
                 refund_evidence: bool = False) -> tuple[bool, str]:
    """(ok, reason). ``lifecycle`` is the state of the ONE order this text is about (None = no such order); a queued
    job for some other order never justifies a promise. A future delivery promise needs a paid order; a statement
    that it was sent needs a reconciled receipt; a deadline needs a fulfilment owner and date; a completed refund
    needs evidence a human decided and performed it."""
    from automations.pack_claim import _DELIVERY_PROMISE
    t = text or ""
    if _REFUND_DONE.search(t) and not refund_evidence:
        return False, "refund_without_evidence"
    if _SENT_CLAIM.search(t) and lifecycle not in RECEIPT_STATES:
        return False, "sent_claim_without_receipt"
    if _DELIVERY_PROMISE.search(t) and lifecycle not in PAID_STATES:
        return False, "promise_without_paid_order"
    if not fulfilment_basis and _deadline_promise(t):
        return False, "deadline_without_fulfilment_basis"
    return True, "ok"


_FULFIL_OBJ = re.compile(r"\b(?:pics?|photos?|videos?|vids?|clips?|custom|voice\s*(?:notes?|messages?)|content|it|yours)\b", re.I)
_FULFIL_VERB = re.compile(r"\b(?:send|sending|deliver|film|record|shoot|make|have|get|ready|finish(?:ed)?|done|post|drop)\b", re.I)


def _deadline_promise(text: str) -> bool:
    """A time commitment about fulfilment: a deadline word AND a fulfilment verb AND a content object. Ordinary talk
    about tomorrow ('see you tomorrow', 'dinner tonight') is not a fulfilment promise."""
    from automations.pack_claim import _DELIVERY_PROMISE
    return bool(_DEADLINE.search(text) and (_DELIVERY_PROMISE.search(text) or _SENT_CLAIM.search(text)
                or (_FULFIL_VERB.search(text) and _FULFIL_OBJ.search(text))))


def boundary_hold(text: str) -> str | None:
    """The state-independent half of the gate, for the last text boundary (which already holds unbacked
    sent/promise claims): a fulfilment deadline or a completed refund can never be backed by this context, because
    the fulfilment owner and the refund decision live with a human case, not in a bubble."""
    if _REFUND_DONE.search(text or ""):
        return "refund-claim-unbacked"
    if _deadline_promise(text or ""):
        return "deadline-unbacked"
    return None


# ── persistence on the existing record ────────────────────────────────────────────────────────────────────────────
def _offer_model():
    from db.models import ContentOffer
    return ContentOffer


def open_request(s, *, account_id: str, fan_id: int, quote: Quote, mode: str = "ppv", now: datetime | None = None):
    """Create the open offer carrying the quote, or return the existing unresolved request for the same obligation.
    Returns (offer, created). A second send path for one obligation gets created=False and must not ask again."""
    from sqlalchemy import select
    O = _offer_model()
    now = now or datetime.utcnow()
    existing = s.scalar(select(O).where(O.account_id == account_id, O.obligation_id == quote.obligation_id,
                                        O.status.in_(("open", "unlocked"))).limit(1))
    if existing is not None:
        if existing.price_cents != quote.price_cents and Quote.from_json(existing.quote_json).accepted:
            raise QuoteError("an accepted quote is never changed without the fan's agreement")
        return existing, False
    row = O(account_id=account_id, fan_id=int(fan_id), item_id=quote.item_id, mode=mode, price_cents=quote.price_cents,
            status="open", quote_json=quote.as_json(), currency=quote.currency, obligation_id=quote.obligation_id,
            lifecycle=QUOTED, quote_version=quote.version, offered_at=now, updated_at=now)
    s.add(row)
    s.flush()
    return row, True


def advance(s, offer, to: str, *, now: datetime | None = None) -> str:
    """Move the offer's lifecycle forward. Invalid hops are refused; escalation is allowed from any live state."""
    cur = offer.lifecycle or QUOTED
    if to == ESCALATED:
        if cur in (FULFILLED, CANCELLED):
            raise QuoteError("a finished order cannot be escalated")
    elif to not in FORWARD.get(cur, ()):
        raise QuoteError(f"lifecycle {cur} -> {to} is not allowed")
    offer.lifecycle = to
    offer.updated_at = now or datetime.utcnow()
    s.flush()
    return to


def idempotency_key(offer) -> str:
    """Stable per obligation+quote version; the provider call and any retry carry the same key."""
    return hashlib.sha256(f"{offer.account_id}|{offer.obligation_id}|{offer.quote_version}".encode()).hexdigest()[:48]


def may_dispatch(offer) -> tuple[bool, str]:
    """Only an order that has NOT reached dispatch intent may start a send. After intent the outcome must be
    reconciled first, so a timeout after provider acceptance can never produce a second charge request."""
    state = offer.lifecycle or QUOTED
    if state in (QUOTED, AWAITING):
        return True, "ok"
    if state == INTENT:
        return False, "outcome_unreconciled"
    return False, "already_" + state


def reconcile_unknown(s, offer, provider_has_it: bool, *, now: datetime | None = None) -> str:
    """Resolve an intent whose outcome was unknown: the provider has it -> accepted (never resend); it does not ->
    back to awaiting so a retry may reuse the SAME idempotency key."""
    if (offer.lifecycle or QUOTED) != INTENT:
        raise QuoteError("only a dispatch intent is reconciled this way")
    return advance(s, offer, ACCEPTED if provider_has_it else AWAITING, now=now)


def dispute_evidence(offer) -> dict:
    """What a human inspects when the fan disputes the order: the assets and versions actually ordered."""
    q = Quote.from_json(offer.quote_json)
    return {"obligation_id": q.obligation_id, "route": q.route, "price": format_price(q.price_cents, q.currency),
            "assets": [list(a) for a in q.assets], "lifecycle": offer.lifecycle}


# ── customs: a real owner, a deadline and an agreed scope, or no handover ─────────────────────────────────────────
CUSTOM_CATEGORY, CUSTOM_SCOPE = "custom_request", "offer"


def custom_requirements(*, available_verified: bool, scope: str | None, price_cents: int | None, route: str | None,
                        owner: str | None, deadline: datetime | None) -> list[str]:
    missing = []
    if not available_verified:
        missing.append("availability_not_verified")
    if not (scope or "").strip():
        missing.append("scope_not_agreed")
    if type(price_cents) is not int or price_cents < PROVIDER_MIN_CENTS:
        missing.append("price_not_agreed")
    if route != "custom_service":
        missing.append("route_not_permitted")
    if not (owner or "").strip():
        missing.append("no_fulfilment_owner")
    if deadline is None:
        missing.append("no_deadline")
    return missing


def open_custom_case(s, *, account_id: str, fan_id: int, available_verified: bool, scope: str | None, price_cents,
                     route: str | None, owner: str | None, deadline: datetime | None, source_message_id=None,
                     now: datetime | None = None):
    """Create the R1 operator case for a custom. Refused (with every missing requirement) unless a real fulfilment
    owner and deadline exist: mentioning a team is not a handover. Returns (case, []) or (None, missing)."""
    missing = custom_requirements(available_verified=available_verified, scope=scope, price_cents=price_cents,
                                  route=route, owner=owner, deadline=deadline)
    if missing:
        return None, missing
    from .operator_cases import open_case
    case, _ = open_case(s, account_id, fan_id, CUSTOM_CATEGORY, reason="custom_agreed",
                        evidence={"scope": scope[:200], "price_cents": price_cents, "route": route},
                        source_message_id=source_message_id, origin="operator", scope=CUSTOM_SCOPE, owner=owner,
                        due_at=deadline, now=now)
    return case, []


# ── adapters for the existing native catalogue delivery (no new sender) ───────────────────────────────────────────
def from_terms(terms: dict, *, account_id: str, fan_id: int, currency: str = "USD") -> "Quote":
    """The canonical Quote for a frozen catalogue snapshot (``media_delivery.catalog_terms`` output). The obligation
    is the locked card for this item's exact hero media, so any send path reaches the same id."""
    hero = sorted(set(terms["media_ids"]) - set(terms["preview_ids"]))
    versions = {f["media_id"]: str(f.get("content_hash") or "") for f in terms.get("files", [])}
    nq = terms.get("native_quote") or {}
    return Quote(obligation_id(("locked_card"), account_id, fan_id, "item:%s:%s" % (terms["item"]["id"], ",".join(map(str, hero)))),
                 "locked_card", int(terms["item"]["id"]), tuple((m, versions.get(m, "")) for m in terms["media_ids"]),
                 int(terms["item"]["price_cents"]), int(terms["price_cents"]), currency.upper(),
                 budget_cents=None, discount_reason=("native_quote" if nq else "catalogue_price"))


def request_exists(s, account_id: str, fan_id: int, terms: dict) -> bool:
    """True while a payment request for this exact obligation is still unresolved (open/unlocked)."""
    from sqlalchemy import select
    O = _offer_model()
    ob = from_terms(terms, account_id=account_id, fan_id=fan_id).obligation_id
    return s.scalar(select(O.id).where(O.account_id == account_id, O.obligation_id == ob,
                                       O.status.in_(("open", "unlocked"))).limit(1)) is not None


def attach_to_offer(s, offer, terms: dict, *, account_id: str, fan_id: int, lifecycle: str = RECONCILED) -> None:
    """Fill the quote columns on the offer a receipt projection just created. The receipt IS the local projection
    reconciliation, hence the default lifecycle. Never raises: a duplicate open obligation leaves it unset rather
    than failing a receipt that already happened."""
    from sqlalchemy import select
    try:
        quote = from_terms(terms, account_id=account_id, fan_id=fan_id)
        O = _offer_model()
        dup = s.scalar(select(O.id).where(O.account_id == account_id, O.obligation_id == quote.obligation_id,
                                          O.status.in_(("open", "unlocked")), O.id != offer.id).limit(1))
        offer.quote_json, offer.currency, offer.lifecycle, offer.quote_version = quote.as_json(), quote.currency, lifecycle, 1
        offer.obligation_id = None if dup is not None else quote.obligation_id
    except Exception:
        return
