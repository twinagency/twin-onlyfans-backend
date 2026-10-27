"""Bounded operator metadata edits; native CatalogItem remains authoritative.

No provider, upload, automation/model client or new catalogue owner is loaded.
The common account lock serializes edits with planning and final delivery checks.
"""
import hashlib
import re

from onlyfans_read.agency_scope import actor_allowed
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import CatalogItem, CATALOG_IS_SINGLE
from db.onlyfans_content_models import OfContentChange
from .native_context import canonical
from .store import Refused, utcnow

FIELDS = {"label": "label", "description": "description_for_ai", "priceCents": "price_cents", "enabled": "enabled"}


class InvalidContent(ValueError):
    pass


class VersionRequired(InvalidContent):
    """A v1 caller tried to change commercial terms of a fixed imported item."""


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def fingerprint(row):
    # All native columns count, including fields this editor deliberately does
    # not display or own. A legacy writer cannot silently be overwritten.
    return digest({column.name: getattr(row, column.name) for column in row.__table__.columns})


def item_reference(value):
    if type(value) is int and 0 < value <= 2147483647:
        return value
    if type(value) is str and re.fullmatch(r"[1-9][0-9]{0,9}", value) and int(value) <= 2147483647:
        return int(value)
    raise InvalidContent("That content reference is invalid.")


def _item(session, account_id, item_id, *, write=False):
    row = session.scalar(select(CatalogItem).where(CatalogItem.account_id == account_id,
        CatalogItem.id == item_id, CATALOG_IS_SINGLE).with_for_update(read=not write))
    if row is None:
        raise Refused("content unavailable")
    return row


def _revision(session, account_id, item_id):
    return session.scalar(select(func.max(OfContentChange.revision)).where(
        OfContentChange.account_id == account_id, OfContentChange.item_id == item_id)) or 0


def _text(value, maximum):
    if value is None:
        return "", False
    if type(value) is not str or len(value.encode("utf-8")) > maximum:
        return None, True
    return value, False


def _valid_price(value, free):
    return type(value) is int and (value == 0 if free else 300 <= value <= 20000)


def editable(row, revision):
    label, label_truncated = _text(row.label, 160)
    description, description_truncated = _text(row.description_for_ai, 2000)
    return {"revision": revision, "fingerprint": fingerprint(row), "label": label,
        "labelTruncated": label_truncated, "description": description,
        "descriptionTruncated": description_truncated,
        "priceCents": row.price_cents if _valid_price(row.price_cents, row.is_free_teaser) else None,
        "enabled": row.enabled, "currency": "USD", "isFreeTeaser": row.is_free_teaser}


def get_content(store, tenant_id, account_id, item_id):
    item_id = item_reference(item_id)
    with Session(store.engine) as session, session.begin():
        store._runtime(session, tenant_id, account_id)
        row = _item(session, account_id, item_id)
        return editable(row, _revision(session, account_id, item_id))


def validate_patch(patch):
    if type(patch) is not dict or not patch or not set(patch) <= set(FIELDS):
        raise InvalidContent("Only the content label, description, price and enabled state may be edited here.")
    result = {}
    for field, value in patch.items():
        if field in {"label", "description"}:
            maximum = 160 if field == "label" else 2000
            try:
                valid = type(value) is str and "\x00" not in value and len(value.encode("utf-8")) <= maximum
            except UnicodeError:
                valid = False
            if not valid or (field == "label" and not value.strip()):
                raise InvalidContent(f"The {field} must be text of at most {maximum} UTF-8 bytes" + (" and cannot be blank." if field == "label" else "."))
            result[field] = value.strip()
        elif field == "priceCents":
            if type(value) is not int or (value != 0 and not 300 <= value <= 20000):
                raise InvalidContent("Use an exact price of 300–20000 USD cents for paid content, or zero for a free teaser.")
            result[field] = value
        else:
            if type(value) is not bool:
                raise InvalidContent("The enabled state must be a boolean.")
            result[field] = value
    return result


def _audit_value(value):
    # Preserve exact evidence hashes for overlong legacy metadata without
    # copying unbounded native strings into the bounded operator audit.
    if type(value) is str and len(value.encode()) > 2000:
        return {"sha256": hashlib.sha256(value.encode()).hexdigest(), "bytes": len(value.encode()), "truncated": True}
    return value


def editable_v2(session, row, revision):
    """v2 adds pricing mode and import provenance; v1's exact shape is untouched."""
    from .offer_import import provenance
    source = provenance(session, row.account_id, row.id)
    value = editable(row, revision)
    value.update({"pricingMode": row.pricing_mode,
        "provenance": None if source is None else {"sourcePriceCents": source["sourcePriceCents"],
            "sourceAt": source["sourceAt"], "sourceContext": source["sourceContext"],
            "previewIds": source["previewIds"], "withdrawn": source["withdrawn"],
            "label": "Price from selected OnlyFans offer"},
        "priceOverridden": source is not None and row.price_cents != source["sourcePriceCents"]})
    return value


def get_content_v2(store, tenant_id, account_id, item_id):
    item_id = item_reference(item_id)
    with Session(store.engine) as session, session.begin():
        store._runtime(session, tenant_id, account_id)
        row = _item(session, account_id, item_id)
        return editable_v2(session, row, _revision(session, account_id, item_id))


def _v1_guard(session, row, patch):
    if row.pricing_mode == "fixed" and (("priceCents" in patch and patch["priceCents"] != row.price_cents)
                                        or patch.get("enabled") is True):
        raise VersionRequired("This imported item's price and enabling need the current content editor.")


def _v2_guard(acknowledge_override):
    def guard(session, row, patch):
        if row.pricing_mode != "fixed":
            return
        from .offer_import import provenance
        source = provenance(session, row.account_id, row.id)
        if ("priceCents" in patch and patch["priceCents"] != row.price_cents and source is not None
                and patch["priceCents"] != source["sourcePriceCents"] and acknowledge_override is not True):
            raise InvalidContent("Confirm that you are overriding the price from the selected OnlyFans offer.")
        if patch.get("enabled") is True and not row.enabled:
            if source is not None and source["withdrawn"]:
                raise InvalidContent("A withdrawn imported draft cannot be enabled.")
            description = patch.get("description", row.description_for_ai)
            if type(description) is not str or not description.strip():
                raise InvalidContent("Approve a factual description before enabling this item.")
            _media_ready(session, row)
    return guard


def _media_ready(session, row):
    """Current vault validation; enabling never relies on stale attachment state."""
    import json
    from db.models import VaultItem
    try:
        ids, previews = json.loads(row.media_ids), json.loads(row.preview_media_ids)
    except (ValueError, TypeError):
        raise InvalidContent("This item's attachments are invalid.") from None
    rows = {r.media_id: r for r in session.scalars(select(VaultItem).where(VaultItem.account_id == row.account_id,
        VaultItem.media_id.in_(ids)).with_for_update(read=True))}
    if (type(ids) is not list or not ids or len(rows) != len(ids)
            or any(r.removed_at is not None or r.kind not in {"photo", "video"} for r in rows.values())
            or any(rows[m].kind != "photo" for m in previews if m in rows) or set(previews) >= set(ids)):
        raise InvalidContent("One or more attachments are no longer available in the vault.")


def update_content(store, tenant_id, account_id, item_id, *, patch, expected_revision,
                   expected_fingerprint, command_key, actor, version=1, acknowledge_override=False):
    item_id = item_reference(item_id)
    patch = validate_patch(patch)
    if version not in {1, 2} or type(acknowledge_override) is not bool or (version == 1 and acknowledge_override):
        raise InvalidContent("The content contract version is invalid.")
    guard = _v1_guard if version == 1 else _v2_guard(acknowledge_override)
    if (type(expected_revision) is not int or not 0 <= expected_revision < 2**31 - 1 or
        type(expected_fingerprint) is not str or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint) or
        type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key) or
        not actor_allowed(actor)):
        raise InvalidContent("The content version or command identity is invalid.")
    request_hash = digest(patch) if version == 1 else digest({"patch": patch, "acknowledgeOverride": acknowledge_override})
    with Session(store.engine) as session, session.begin():
        runtime=store._runtime(session, tenant_id, account_id)
        # Scope is rechecked even for a historical replay, while the recorded
        # acknowledgement continues to describe its original event, not the
        # latest catalogue state. A retry never reapplies the old patch.
        prior = session.get(OfContentChange, (account_id, item_id, command_key))
        if prior:
            if (prior.previous_revision, prior.fingerprint_before, prior.request_hash, prior.actor) != (
                expected_revision, expected_fingerprint, request_hash, actor):
                raise Refused("content idempotency conflict")
            return prior.revision, prior.fingerprint_after
        row = _item(session, account_id, item_id, write=True)
        revision = _revision(session, account_id, item_id)
        if revision != expected_revision or fingerprint(row) != expected_fingerprint:
            raise Refused("content revision conflict")
        if "priceCents" in patch and not _valid_price(patch["priceCents"], row.is_free_teaser):
            raise InvalidContent("A free teaser must keep price zero; paid content must keep a price of 300–20000 USD cents.")
        guard(session, row, patch)
        before = {}; after = {}; changed = False
        for field, value in patch.items():
            attribute = FIELDS[field]
            previous = getattr(row, attribute)
            before[field] = _audit_value(previous)
            after[field] = value
            changed = changed or previous != value
            setattr(row, attribute, value)
        if not changed:
            raise InvalidContent("No content change was needed.")
        row.updated_at = utcnow()
        session.flush()
        after_fingerprint = fingerprint(row)
        session.add(OfContentChange(account_id=account_id, item_id=item_id, command_key=command_key,
            previous_revision=revision, revision=revision + 1, fingerprint_before=expected_fingerprint,
            fingerprint_after=after_fingerprint, request_hash=request_hash, actor=actor,
            before_json=canonical(before), after_json=canonical(after), created_at=utcnow()))
        session.flush()
        from .launch_engagement.events import cancel_for_item
        cancel_for_item(session, account_id, item_id, "catalogue_changed", now=utcnow())
        return revision + 1, after_fingerprint
