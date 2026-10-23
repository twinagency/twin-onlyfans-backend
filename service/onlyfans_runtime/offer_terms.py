"""Exact commercial terms of one creator paid offer; pure, no DB or transport.

A history row's previews and lockedText are captured before message
normalization discards them. Unknown is never filled with a permissive default:
a missing previews key is unknown (an empty list is a fact), and a missing
lockedText is unknown (False is a fact). Event identity and payloads are not
touched, so ingress hashes and replay behaviour stay unchanged.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

from .native_context import canonical
from .store import Refused, _id

PARSER_VERSION = "of-offer.v1"
MAX_ATTACHMENTS = 20
MIN_PRICE_CENTS = 300
MAX_PRICE_CENTS = 20000
SOURCE_KINDS = frozenset({"message", "post"})
CONTEXTS = frozenset({"direct", "queue", "unknown"})
# Intrinsic reasons a source cannot become a draft. Asset availability is
# rechecked at review time against the current vault, never stored as fact.
REASONS = frozenset({"previews_unknown", "locked_text_unknown", "locked_text",
    "too_many_attachments", "price_out_of_range", "no_paid_content", "unsupported_previews"})


def media_key(media_ids):
    """Catalogue duplicate key: the unordered attachment set."""
    return hashlib.sha256(canonical(sorted(media_ids)).encode()).hexdigest()


@dataclass(frozen=True)
class OfferObservation:
    source_kind: str
    source_id: int
    fan_id: int | None
    source_context: str
    source_at: str
    media_ids: tuple[int, ...]
    preview_ids: tuple[int, ...] | None
    locked_text: bool | None
    price_cents: int
    currency: str = "USD"
    reasons: tuple[str, ...] = ()

    def normalized(self):
        if self.source_kind not in SOURCE_KINDS or self.source_context not in CONTEXTS:
            raise Refused("invalid offer source")
        _id(self.source_id)
        if self.fan_id is not None:
            _id(self.fan_id)
        if type(self.media_ids) is not tuple or not 1 <= len(self.media_ids) <= 100:
            raise Refused("invalid offer attachments")
        media = tuple(_id(v) for v in self.media_ids)
        if len(set(media)) != len(media):
            raise Refused("invalid offer attachments")
        previews = self.preview_ids
        if previews is not None:
            if type(previews) is not tuple or len(set(previews)) != len(previews) or any(_id(v) not in media for v in previews):
                raise Refused("invalid offer previews")
        if self.locked_text is not None and type(self.locked_text) is not bool:
            raise Refused("invalid offer lock")
        if type(self.price_cents) is not int or not 0 < self.price_cents <= 100_000_000 or self.currency != "USD":
            raise Refused("invalid offer price")
        try:
            date = datetime.fromisoformat(self.source_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise Refused("invalid offer timestamp") from exc
        if date.tzinfo is None:
            raise Refused("offer timestamp must include timezone")
        if type(self.reasons) is not tuple or not set(self.reasons) <= REASONS:
            raise Refused("invalid offer reasons")
        return OfferObservation(self.source_kind, self.source_id, self.fan_id, self.source_context,
            date.astimezone(timezone.utc).isoformat(), media, previews, self.locked_text,
            self.price_cents, self.currency, tuple(sorted(set(self.reasons) | intrinsic_reasons(
                media, previews, self.locked_text, self.price_cents))))

    @property
    def media_key(self):
        return media_key(self.media_ids)

    def terms(self):
        value = self.normalized()
        return {"sourceKind": value.source_kind, "sourceId": value.source_id,
            "sourceContext": value.source_context, "mediaIds": list(value.media_ids),
            "previewIds": None if value.preview_ids is None else list(value.preview_ids),
            "lockedText": value.locked_text, "priceCents": value.price_cents,
            "currency": value.currency, "parser": PARSER_VERSION}

    def fingerprint(self):
        return hashlib.sha256(canonical(self.terms()).encode()).hexdigest()


def intrinsic_reasons(media, previews, locked_text, price_cents):
    reasons = set()
    if previews is None:
        reasons.add("previews_unknown")
    elif set(previews) == set(media):
        reasons.add("no_paid_content")
    if locked_text is None:
        reasons.add("locked_text_unknown")
    elif locked_text:
        reasons.add("locked_text")
    if len(media) > MAX_ATTACHMENTS:
        reasons.add("too_many_attachments")
    if not MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS:
        reasons.add("price_out_of_range")
    return reasons


def from_history(row, event):
    """Observation for one already-validated outgoing priced history row.

    `event` is the strict Event the existing parser accepted for the same row,
    so identity, sender, media and price are reused, never reparsed loosely.
    Returns None for rows that are not creator paid offers.
    """
    if event.direction != "out" or event.price_cents <= 0 or not event.media_ids:
        return None
    reasons = set()
    previews = None
    if "previews" in row:
        raw = row["previews"]
        if (type(raw) is list and len(raw) <= 100 and all(type(v) is int for v in raw)
                and len(set(raw)) == len(raw) and set(raw) <= set(event.media_ids)):
            previews = tuple(raw)
        else:
            reasons.add("unsupported_previews")
    locked = row.get("lockedText") if type(row.get("lockedText")) is bool else None
    queue = row.get("isFromQueue")
    context = "queue" if queue is True else "direct" if queue is False else "unknown"
    return OfferObservation("message", event.message_id, event.fan_id, context, event.created_at,
        tuple(event.media_ids), previews, locked, event.price_cents, "USD", tuple(sorted(reasons))).normalized()
