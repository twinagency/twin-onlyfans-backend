"""ONE authority for 'may this vault item be offered, and what may be said about it' (R4).

Pure and synchronous: callers supply the row (a ``VaultItem`` or any object with the same
attributes) and the account context. Used by contextual availability, the content resolver's
candidate pool and pack selection. It replaces 'search_text is non-empty', which the vault import
satisfies with the bare kind word.

Hard requirements (an item failing any is NOT ready and is never offered):
  owned, not removed, current version known (and equal to the caller's expected version),
  recognised format, a meaningful description, a price source and a currency,
  not already owned/offered to this fan, no creator-boundary hit, not operator-rejected.
Soft attributes (preview policy, material exclusions, custom/live classification) that the
schema cannot ground are reported in ``unknown`` and are never part of ``facts``: a caller must
not advertise them. Nothing here invents a value.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DEFAULT_CURRENCY = "USD"                      # existing precedent: transaction_ingest `currency or "USD"`
FORMATS = ("photo", "video", "audio", "gif")
_PLACEHOLDERS = frozenset({"photo", "photos", "video", "videos", "audio", "gif", "image", "images", "pic",
                           "pics", "vid", "vids", "media", "clip", "none", "null", "n/a", "unknown",
                           "untitled", "undescribed"})
_WORD = re.compile(r"[a-z]{3,}")
_UNREADY_STATUS = frozenset({"failed", "blocked_drm", "pending"})
_REJECTED_REVIEW = frozenset({"rejected", "hidden", "removed", "blocked"})


def meaningful_description(text: str | None) -> bool:
    """False for empty text, a bare type word, or text with no content word."""
    words = [w for w in _WORD.findall(str(text or "").lower()) if w not in _PLACEHOLDERS]
    return len(set(words)) >= 1


def best_description(item, *, prefer_search: bool = False) -> str:
    """The first meaningful of video_description / description / search_text, else ''.
    ``prefer_search`` puts the denormalised search blob first (retrieval ranking reads that)."""
    order = ("search_text", "video_description", "description") if prefer_search else (
        "video_description", "description", "search_text")
    for field_name in order:
        value = " ".join(str(getattr(item, field_name, None) or "").split())
        if meaningful_description(value):
            return value
    return ""


def boundary_terms(limits) -> tuple[str, ...]:
    """Creator hard limits ('a; b, c' or a list) as lower-case phrases of 3+ characters."""
    parts = limits if isinstance(limits, (list, tuple)) else re.split(r"[;,\n]", str(limits or ""))
    return tuple(p.strip().lower() for p in parts if len(p.strip()) >= 3)


@dataclass(frozen=True)
class Readiness:
    media_id: int
    ready: bool
    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()             # attributes that must not be advertised
    facts: dict = field(default_factory=dict)  # the only things a caption/answer may state


def item_readiness(item, *, account_id: str, price_cents: int | None = None, currency: str | None = None,
                   bought=frozenset(), offered=frozenset(), limits=(), expected_version: str | None = None,
                   price_source: str | None = None) -> Readiness:
    mid = int(getattr(item, "media_id", 0) or 0)
    missing: list[str] = []
    if str(getattr(item, "account_id", "")) != str(account_id):
        missing.append("not_account_owned")
    if getattr(item, "removed_at", None) is not None:
        missing.append("removed")
    version = getattr(item, "content_hash", None) or getattr(item, "updated_at_of", None)
    if not version:
        missing.append("version_unknown")
    elif expected_version is not None and str(version) != str(expected_version):
        missing.append("version_changed")
    kind = str(getattr(item, "kind", "") or "").lower()
    if kind not in FORMATS:
        missing.append("format_unknown")
    description = best_description(item)
    if not description:
        missing.append("no_meaningful_description")
    if getattr(item, "describe_status", None) in _UNREADY_STATUS and not description:
        missing.append("description_unavailable")
    # price source: the item's own approved price, or the account's pricing ladder (the single pricing function
    # computes the amount at quote time; no amount is advertised from here in that case).
    if not ((isinstance(price_cents, int) and price_cents > 0) or price_source == "pack_ladder"):
        missing.append("price_missing")
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha()):
        missing.append("currency_missing")
    if mid in set(bought):
        missing.append("already_owned")
    elif mid in set(offered):
        missing.append("already_offered")
    if str(getattr(item, "review_state", "") or "").lower() in _REJECTED_REVIEW:
        missing.append("operator_rejected")
    blob = " ".join([description, str(getattr(item, "tags", "") or ""),
                     str(getattr(item, "ai_fields_json", "") or "")]).lower()
    if any(term in blob for term in boundary_terms(limits)):
        missing.append("creator_boundary")
    unknown = ["material_exclusions", "preview_policy", "custom_or_live_availability"]
    duration = getattr(item, "duration_seconds", None)
    if kind == "video" and not duration:
        unknown.append("duration")
    facts = {}
    if not missing:
        facts = {"media_id": mid, "format": kind, "description": description[:300], "price_cents": price_cents, "price_source": "item" if price_cents else price_source,
                 "currency": currency.upper(), "version": str(version),
                 "classification": "prerecorded"}      # an existing vault asset is, by definition, prerecorded
        if duration:
            facts["duration_seconds"] = int(duration)
    return Readiness(mid, not missing, tuple(missing), tuple(unknown), facts)


def item_price_cents(item, ladder_cents: int | None = None) -> int | None:
    """Price source: the item's own approved price, else the caller's account price ladder."""
    for name in ("default_price_cents", "suggested_price_cents"):
        value = getattr(item, name, None)
        if isinstance(value, int) and value > 0:
            return value
    return ladder_cents if isinstance(ladder_cents, int) and ladder_cents > 0 else None


def ready_ids(items, *, account_id, cfg=None, bought=frozenset(), offered=frozenset(), limits=(),
              price_source=None) -> set[int]:
    cfg = cfg or {}
    currency = str(cfg.get("currency") or DEFAULT_CURRENCY)
    source = price_source or ("pack_ladder" if cfg.get("pack_send_enabled") else None)
    return {int(i.media_id) for i in items
            if item_readiness(i, account_id=account_id, price_cents=item_price_cents(i), currency=currency,
                              bought=bought, offered=offered, limits=limits, price_source=source).ready}
