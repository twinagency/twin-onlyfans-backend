"""File described media in existing internal folders, without model/provider calls.

Classification happens once per item. A durable marker preserves subsequent
manual membership changes, folder renames and archives across analysis reruns.
"""
from __future__ import annotations

import json
import re
from sqlalchemy import select, func
from db.models import Account, VaultItem, VaultFolder, VaultFolderItem
from vault_ai_effective import effective_value

MARKER = "_auto_folders_v1"
FOLDERS = {
    "selfies": ("Selfies", {"selfie", "selfies", "mirror selfie", "handheld selfie", "sfw selfie"}),
    "swimwear": ("Bikini / swimwear", {"bikini", "swimsuit", "swimwear", "bathing suit"}),
    "lingerie": ("Lingerie", {"lingerie", "lingerie on", "bra", "panties", "underwear", "thong"}),
    "casual": ("Casual / everyday", {"casual", "everyday", "casual outfit", "casual clothes", "streetwear"}),
    "fitness": ("Fitness", {"fitness", "workout", "gym", "exercise", "activewear", "sports bra"}),
}
OWNER_PREFIX = "auto-category:"


def _normal(value):
    return re.sub(r"\s+", " ", str(value).lower().replace("_", " ").replace("-", " ")).strip()


def _tags(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = re.split(r"[,\n]", value)
    return {_normal(t) for t in value if isinstance(t, str)} if isinstance(value, (list, tuple)) else set()


def categories(item):
    """Conservative, overlapping categories from operator-effective metadata."""
    tags = _tags(effective_value(item, "tags", default=[]))
    tags |= _tags(effective_value(item, "clothing_items", default=[]))
    for key in ("camera", "primary_folder", "clothing_state"):
        value = effective_value(item, key)
        if isinstance(value, str):
            tags.add(_normal(value))
    result = {key for key, (_, terms) in FOLDERS.items() if tags & terms}
    # Add positive description evidence, allowing a bikini selfie in both folders.
    # Avoid substring matches ("selfiesque") and negated clothing descriptions.
    desc = effective_value(item, "description") or effective_value(item, "video_description") or ""
    if isinstance(desc, str):
        for sentence in re.split(r"[.!?;\n]", _normal(desc)):
            if re.search(r"\b(no|not|without|neither|removed|removing|off)\b", sentence):
                continue
            for key, (_, terms) in FOLDERS.items():
                if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", sentence) for term in terms):
                    result.add(key)
    if _normal(effective_value(item, "clothing_state") or "") == "fully nude":
        result -= {"swimwear", "lingerie", "casual"}
    return sorted(result) or ["uncategorised"]


async def assign(session, account_id, media_id):
    """Assign once in the caller's transaction. Account lock serializes folders.

    Existing operator folders are never adopted by matching their display name.
    Archived auto folders are not recreated. No membership is ever deleted.
    """
    owner = await session.scalar(select(Account.id).where(Account.id == account_id).with_for_update())
    if owner is None:
        raise ValueError("Unknown vault account")
    item = await session.scalar(select(VaultItem).where(
        VaultItem.account_id == account_id, VaultItem.media_id == media_id).with_for_update())
    if item is None or item.removed_at is not None or item.kind not in {"photo", "video", "gif"}:
        return {"status": "skipped"}
    try:
        fields = json.loads(item.ai_fields_json or "{}")
    except (ValueError, TypeError):
        return {"status": "invalid_metadata"}
    if not isinstance(fields, dict):
        return {"status": "invalid_metadata"}
    if fields.get(MARKER) is True:
        return {"status": "already_categorised"}
    if not (effective_value(item, "description") or effective_value(item, "video_description")
            or _tags(effective_value(item, "tags", default=[]))):
        return {"status": "awaiting_metadata"}
    wanted = categories(item)
    for key in wanted:
        actor = OWNER_PREFIX + key
        folder = await session.scalar(select(VaultFolder).where(
            VaultFolder.account_id == account_id, VaultFolder.created_by == actor).order_by(VaultFolder.id))
        if folder is None:
            count = await session.scalar(select(func.count()).select_from(VaultFolder).where(
                VaultFolder.account_id == account_id, VaultFolder.deleted_at.is_(None)))
            if count >= 200:
                raise ValueError("Internal folder limit reached")
            folder = VaultFolder(account_id=account_id, created_by=actor,
                name=FOLDERS[key][0] if key in FOLDERS else "Uncategorised")
            session.add(folder)
            await session.flush()
        if folder.deleted_at is not None:
            continue
        identity = (account_id, folder.id, media_id)
        if await session.get(VaultFolderItem, identity) is None:
            count = await session.scalar(select(func.count()).select_from(VaultFolderItem).where(
                VaultFolderItem.account_id == account_id, VaultFolderItem.folder_id == folder.id))
            if count >= 5000:
                raise ValueError("Internal folder membership limit reached")
            session.add(VaultFolderItem(account_id=account_id, folder_id=folder.id, media_id=media_id))
    item.ai_fields_json = json.dumps({**fields, MARKER: True}, ensure_ascii=False)
    return {"status": "categorised", "categories": wanted}
