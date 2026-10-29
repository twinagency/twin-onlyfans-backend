"""Existing catalogue terms for the common delivery authority.

This is neither a sender nor a current ownership store. The intent freezes the
operator's catalogue and exact vault identities; only a proved acceptance may
project the existing Message, ContentOffer and VaultSend rows.
"""
import json

from sqlalchemy import select

from db.models import CatalogItem, ContentOffer, Message, VaultItem, VaultSend
from .native_context import canonical
from .store import Refused, _id

MAX_MEDIA = 35


def media_ids(raw):
    try:
        ids = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise Refused("invalid media identities") from exc
    if type(ids) is not list or not 1 <= len(ids) <= MAX_MEDIA:
        raise Refused("invalid media count")
    if any(type(v) is not int or not 0 < v < 2**63 for v in ids) or len(set(ids)) != len(ids):
        raise Refused("invalid media identities")
    return ids


def catalog_terms(session, account_id, item_id, *, price_cents=None, lock=True):
    """Bounded terms; send paths lock, readiness uses its read-only snapshot."""
    _id(item_id)
    item_query = select(CatalogItem).where(CatalogItem.account_id == account_id, CatalogItem.id == item_id)
    item = session.scalar(item_query.with_for_update(read=True) if lock else item_query)
    if (item is None or not item.enabled or item.script_id is not None or item.is_free_teaser
            or type(item.description_for_ai) is not str or not item.description_for_ai.strip()
            or len(item.description_for_ai.encode()) > 2000):
        raise Refused("approved catalogue terms unavailable")
    from automations._price_limits import OF_PRICE_FLOOR_CENTS, OF_PRICE_MAX_CENTS
    price = item.price_cents if price_cents is None else price_cents
    if (type(price) is not int or type(item.price_cents) is not int
            or not OF_PRICE_FLOOR_CENTS <= item.price_cents <= price <= OF_PRICE_MAX_CENTS):
        raise Refused("invalid catalogue price")
    fixed = item.pricing_mode == "fixed"
    if item.pricing_mode not in {"dynamic", "fixed"} or (fixed and price != item.price_cents):
        # A fixed item's saved amount is the offer; no override may change it.
        raise Refused("fixed catalogue price differs")
    # An unresolved source change or verified unusable media holds new sales of
    # this item only; the snapshot itself is untouched, so queued intents of
    # other items and every dynamic snapshot stay byte-identical.
    from .content_sync import sale_hold
    hold = sale_hold(session, account_id, item)
    if hold:
        raise Refused(hold)
    ids = media_ids(item.media_ids)
    try:
        previews = json.loads(item.preview_media_ids)
    except (ValueError, TypeError) as exc:
        raise Refused("invalid preview identities") from exc
    if (type(previews) is not list or any(type(v) is not int or v not in ids for v in previews)
            or len(set(previews)) != len(previews) or set(previews) == set(ids)):
        raise Refused("invalid preview identities")
    media_query = select(VaultItem).where(VaultItem.account_id == account_id,
        VaultItem.media_id.in_(ids)).order_by(VaultItem.media_id)
    rows = session.scalars(media_query.with_for_update(read=True) if lock else media_query).all()
    if len(rows) != len(ids) or any(r.removed_at is not None or r.kind not in {"image", "photo", "video"} for r in rows):
        raise Refused("catalogue media unavailable")
    snapshot = {"item": {key: getattr(item, key) for key in (
        "id", "account_id", "script_id", "enabled", "kind", "label", "description_for_ai",
        "price_cents", "is_free_teaser", "updated_at")},
        "media_ids": ids, "preview_ids": previews, "price_cents": price,
        "files": [{key: getattr(r, key) for key in ("media_id", "kind", "content_hash",
            "updated_at", "removed_at")} for r in rows]}
    if fixed:
        # Added only for fixed items so every existing dynamic snapshot, queued
        # intent and campaign stays byte-identical and is never reinterpreted.
        from .offer_import import provenance
        source = provenance(session, account_id, item.id)
        if source is not None and source["withdrawn"]:
            raise Refused("approved catalogue terms unavailable")
        snapshot["item"]["pricing_mode"] = "fixed"
        snapshot["source"] = None if source is None else {"decision": source["decision"], "fingerprint": source["fingerprint"]}
    return json.loads(canonical(snapshot))


def terms_match(session, intent):
    try:
        expected = json.loads(intent.catalog_snapshot_json)
        current = catalog_terms(session, intent.account_id, intent.catalog_item_id, price_cents=intent.price_cents)
        expected = {k: v for k, v in expected.items() if k not in {"native_quote", "source_quote", "source_message_id"}}
        return (expected == current and json.loads(intent.media_json) == current["media_ids"]
                and json.loads(intent.preview_json) == current["preview_ids"])
    except (Refused, ValueError, TypeError):
        return False


def assert_unowned(session, account_id, fan_id, snapshot):
    from .commerce import held
    if held(session, account_id, fan_id):
        raise Refused("commerce evidence needs review")
    from ownership import owned_media_in_session
    try:
        owned = owned_media_in_session(session, account_id, fan_id, limit=10000)
    except ValueError as exc:
        raise Refused("ownership evidence unavailable") from exc
    # The existing native seller blocks on hero overlap: paid video or any
    # non-preview photo. Previously purchased free-preview filler may repeat.
    hero = set(snapshot["media_ids"]) - set(snapshot["preview_ids"])
    if hero & owned:
        raise Refused("catalogue media already owned")


def project(session, intent, message_id, stamp):
    """Called inside the receipt projection transaction; never asserts purchase."""
    if intent.catalog_item_id is None:
        return
    if intent.offer_id is not None:
        raise Refused("media offer already projected without its receipt marker")
    snapshot = json.loads(intent.catalog_snapshot_json)
    if intent.origin == "manual" and snapshot.get("manual_vault") is True and intent.price_cents == 0:
        from .manual_media import project_free
        project_free(session, intent, message_id, stamp)
        return
    # Use saved terms, not a mutable or disabled catalogue's current values.
    if (snapshot["media_ids"] != json.loads(intent.media_json)
            or snapshot["preview_ids"] != json.loads(intent.preview_json)
            or snapshot["price_cents"] != intent.price_cents
            or snapshot["item"]["account_id"] != intent.account_id
            or snapshot["item"]["id"] != intent.catalog_item_id):
        raise Refused("media evidence conflict")
    offer = ContentOffer(account_id=intent.account_id, fan_id=intent.fan_id,
        item_id=intent.catalog_item_id, mode="ppv", price_cents=intent.price_cents,
        tip_unlock_cents=0, offer_message_id=message_id, status="open",
        tips_accum_cents=0, offered_at=stamp, updated_at=stamp)
    session.add(offer)
    session.flush()
    intent.offer_id = offer.id
    from .native_sales import record_quote
    record_quote(session, intent, message_id, stamp)
    prior = session.scalars(select(VaultSend).where(VaultSend.account_id == intent.account_id,
        VaultSend.fan_id == intent.fan_id, VaultSend.message_id == message_id)).all()
    by_media = {r.media_id: r for r in prior}
    if (len(by_media) != len(prior) or any(r.media_id not in snapshot["media_ids"]
            or r.price_cents != intent.price_cents for r in prior)):
        raise Refused("media send evidence conflict")
    for mid in snapshot["media_ids"]:
        if mid not in by_media:
            session.add(VaultSend(account_id=intent.account_id, fan_id=intent.fan_id,
                media_id=mid, message_id=message_id, price_cents=intent.price_cents,
                sent_at=stamp, was_purchased=False))
