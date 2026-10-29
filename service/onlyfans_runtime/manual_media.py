"""Explicit operator-selected media, separate from automated catalogue sales."""
import json
from sqlalchemy import select
from db.models import CatalogItem, VaultItem, VaultSend, Message
from db.onlyfans_models import OfIngress
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from .store import Refused, utcnow
from .native_context import canonical

MAX_MEDIA = 20

def normalize(value):
    if type(value) is not dict or set(value) != {"mediaIds", "previewIds", "priceCents"}:
        raise Refused("invalid manual media selection")
    media, previews, price = value["mediaIds"], value["previewIds"], value["priceCents"]
    def ids(values):
        return type(values) is list and all(type(v) is str and v.isascii() and v.isdecimal()
            and not v.startswith("0") and 0 < int(v) < 2**63 for v in values) and len(set(values)) == len(values)
    if (not ids(media) or not 1 <= len(media) <= MAX_MEDIA or not ids(previews)
            or any(v not in media for v in previews) or type(price) is not int
            or not (price == 0 or 300 <= price <= 20000)
            or (price > 0 and len(previews) >= len(media)) or (price == 0 and previews)):
        raise Refused("invalid manual media selection")
    return {"mediaIds": list(media), "previewIds": list(previews), "priceCents": price}

def _assets(s, runtime, spec):
    from .vault_management import _connection, _ready
    connection = _connection(s, runtime)
    assets = []
    for value in spec["mediaIds"]:
        mid = int(value)
        row = s.scalar(select(VaultItem).where(VaultItem.account_id == runtime.account_id,
            VaultItem.media_id == mid).with_for_update(read=True))
        observation = s.get(OfVaultObservation, (runtime.account_id, mid))
        job = s.get(OfVaultSyncJob, (runtime.account_id, observation.job_id)) if observation else None
        if row is None or not _ready(row, observation, job, connection, runtime.binding_id)[0]:
            raise Refused("vault media unavailable")
        assets.append(row)
    return assets

def _files(assets):
    return json.loads(canonical([{k: getattr(row, k) for k in
        ("media_id", "kind", "content_hash", "updated_at", "removed_at")} for row in assets]))

def freeze(s, runtime, spec):
    """An intent-only disabled item retains receipt links; it never enters the AI shelf."""
    assets = _assets(s, runtime, spec)
    now = utcnow()
    item = CatalogItem(account_id=runtime.account_id, kind="video" if any(r.kind == "video" for r in assets) else "image_set",
        label="Manual vault attachment", description_for_ai="Operator-selected message attachments.",
        media_ids=canonical([int(v) for v in spec["mediaIds"]]),
        preview_media_ids=canonical([int(v) for v in spec["previewIds"]]),
        price_cents=spec["priceCents"], enabled=False, is_free_teaser=spec["priceCents"] == 0,
        pricing_mode="fixed", created_at=now, updated_at=now)
    s.add(item); s.flush()
    return item.id, {"manual_vault": True, "item": {"id": item.id, "account_id": runtime.account_id},
        "media_ids": [int(v) for v in spec["mediaIds"]], "preview_ids": [int(v) for v in spec["previewIds"]],
        "price_cents": spec["priceCents"], "files": _files(assets)}

def matches(s, runtime, intent):
    try:
        frozen = json.loads(intent.catalog_snapshot_json)
        if intent.origin != "manual" or frozen.get("manual_vault") is not True:
            return False
        if frozen["item"] != {"id": intent.catalog_item_id, "account_id": intent.account_id}:
            return False
        spec = normalize({"mediaIds": [str(v) for v in json.loads(intent.media_json)],
            "previewIds": [str(v) for v in json.loads(intent.preview_json)], "priceCents": intent.price_cents})
        assets = _assets(s, runtime, spec)
        item = s.get(CatalogItem, intent.catalog_item_id)
        return (item is not None and item.account_id == runtime.account_id and not item.enabled
            and frozen["files"] == _files(assets) and frozen["media_ids"] == json.loads(intent.media_json)
            and frozen["preview_ids"] == json.loads(intent.preview_json) and frozen["price_cents"] == intent.price_cents
            and item.media_ids == intent.media_json and item.preview_media_ids == intent.preview_json
            and item.price_cents == intent.price_cents)
    except (Refused, ValueError, TypeError, KeyError):
        return False

def project_free(s, intent, message_id, stamp):
    """Free media has delivery history but no paid offer or purchase claim."""
    for mid in json.loads(intent.media_json):
        prior = s.scalar(select(VaultSend).where(VaultSend.account_id == intent.account_id,
            VaultSend.fan_id == intent.fan_id, VaultSend.message_id == message_id, VaultSend.media_id == mid))
        if prior is None:
            s.add(VaultSend(account_id=intent.account_id, fan_id=intent.fan_id, media_id=mid,
                message_id=message_id, price_cents=0, sent_at=stamp, was_purchased=False))
