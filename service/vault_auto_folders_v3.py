"""Reconcile automatic folders from verified visual evidence, preserving manual edits."""
from __future__ import annotations
import hashlib
import json
from sqlalchemy import select, inspect
from db.models import Account, VaultItem, VaultFolder, VaultFolderItem
from vault_category_evidence import LABELS, KEY, verified_categories

MARKER = "_auto_folders_v1"
CURRENT_MARKER = "_auto_folders_v3"
PENDING_MARKER = "_auto_folder_pending_v1"
OWNER_PREFIX = "auto-category:"
FOLDERS = {key: (label, set()) for key, label in LABELS.items()}
NAMES = {**LABELS, "pending": "Unsorted", "review": "Needs review", "uncategorised": "Uncategorised"}

def _object(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}

def categories(item):
    return verified_categories(item) or ["review"]

def _fingerprint(item, fields):
    return hashlib.sha256(json.dumps({"evidence": fields.get(KEY),
        "description": fields.get("description"), "tags": fields.get("tags"),
        "kind": item.kind, "overrides": getattr(item, "operator_overrides_json", None)},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

def _protected_folders(session, account):
    """Old audit rows omitted edited IDs. Preserve those whole folders."""
    cache = session.info.setdefault("folder_accuracy_protected", {})
    if account not in cache:
        from db.onlyfans_vault_edit_models import OfVaultEdit
        if inspect(session.connection()).has_table(OfVaultEdit.__tablename__):
            values = session.scalars(select(OfVaultEdit.target_id).where(
                OfVaultEdit.account_id == account,
                OfVaultEdit.operation.in_(("folder_add", "folder_remove", "folder_rename"))))
            cache[account] = {int(value) for value in values if str(value).isdigit()}
        else:
            cache[account] = set()
    return cache[account]

def assign_sync(session, account_id, media_id):
    if session.scalar(select(Account.id).where(Account.id == account_id).with_for_update()) is None:
        raise ValueError("Unknown vault account")
    item = session.scalar(select(VaultItem).where(VaultItem.account_id == account_id,
        VaultItem.media_id == media_id).with_for_update())
    if item is None or item.removed_at is not None or item.kind not in {"photo", "video", "gif", "audio"}:
        return {"status": "skipped"}
    try:
        fields = json.loads(item.ai_fields_json or "{}")
    except (ValueError, TypeError):
        return {"status": "invalid_metadata"}
    if not isinstance(fields, dict):
        return {"status": "invalid_metadata"}
    fingerprint = _fingerprint(item, fields)
    previous = fields.get(CURRENT_MARKER)
    if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
        return {"status": "already_categorised", "categories": previous.get("categories", [])}
    has_metadata = bool(fields.get("description") or fields.get("tags") or fields.get(KEY)
        or item.description or item.video_description)
    wanted = categories(item) if has_metadata else ["pending"]
    rows = list(session.scalars(select(VaultFolder).where(VaultFolder.account_id == account_id)))
    by_key = {r.created_by[len(OWNER_PREFIX):]: r for r in rows if r.created_by.startswith(OWNER_PREFIX)}
    protected = _protected_folders(session, account_id)
    protected |= {r.id for key, r in by_key.items() if r.name != NAMES.get(key, r.name)}
    members = {m.folder_id: m for m in session.scalars(select(VaultFolderItem).where(
        VaultFolderItem.account_id == account_id, VaultFolderItem.media_id == media_id))}
    manual = _object(item.operator_overrides_json).get("_folder_membership", {})
    manual = manual if isinstance(manual, dict) else {}
    blocked = set(previous.get("blocked", [])) if isinstance(previous, dict) else set()
    if not isinstance(previous, dict) and fields.get("_auto_folders_v2") is True:
        from vault_legacy_category_migration import categories as old_categories
        blocked |= {key for key in old_categories(item) if key in by_key and by_key[key].id not in members}
    managed = set(previous.get("managed", [])) if isinstance(previous, dict) else set(by_key)
    if isinstance(previous, dict):
        blocked |= {key for key in managed if key in by_key and by_key[key].id not in members}
    added, removed, next_managed = [], [], set()
    for key, folder in by_key.items():
        member = members.get(folder.id)
        manual_value = manual.get(str(folder.id))
        if folder.id in protected or manual_value is True or (member and member.manual_order is not None):
            continue
        if member and key in managed and (key not in wanted or manual_value is False):
            session.delete(member); removed.append(key)
        elif member and key in managed:
            next_managed.add(key)
    for key in wanted:
        folder = by_key.get(key)
        if key in blocked:
            continue
        if folder is not None and (folder.deleted_at is not None or folder.id in protected or manual.get(str(folder.id)) is False):
            continue
        if folder is None:
            if sum(r.deleted_at is None for r in rows) >= 200:
                raise ValueError("Internal folder limit reached")
            folder = VaultFolder(account_id=account_id, created_by=OWNER_PREFIX+key, name=NAMES[key])
            session.add(folder); session.flush(); rows.append(folder); by_key[key] = folder
        if folder.id not in members:
            session.add(VaultFolderItem(account_id=account_id, folder_id=folder.id, media_id=media_id))
            added.append(key); next_managed.add(key)
        elif key in managed:
            next_managed.add(key)
    fields[CURRENT_MARKER] = {"fingerprint": fingerprint, "categories": wanted,
        "managed": sorted(next_managed), "blocked": sorted(blocked), "version": 3}
    fields[MARKER] = True
    fields["_auto_folders_v2"] = True
    if wanted == ["pending"]:
        fields[PENDING_MARKER] = True
    else:
        fields.pop(PENDING_MARKER, None)
    item.ai_fields_json = json.dumps(fields, ensure_ascii=False)
    return {"status": "awaiting_metadata" if wanted == ["pending"] else "categorised",
            "categories": wanted, "added": added, "removed": removed}

async def assign(session, account_id, media_id):
    return await session.run_sync(assign_sync, account_id, media_id)
