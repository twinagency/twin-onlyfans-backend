"""File described media in existing internal folders, without model/provider calls.

Folder filing is versioned once per item. Unsorted membership is temporary. A durable marker preserves subsequent
manual membership changes, folder renames and archives across analysis reruns.

Commercial classification is different: it is a statement about CURRENT evidence and is re-evaluated on every call
(confidence, bound content stamp, metadata presence, evidenced category). The one-time markers only decide which
folders may be filed automatically, never whether an old "proposed" classification survives weaker evidence.
"""
from __future__ import annotations

import json
import re
from sqlalchemy import select, func, delete
from db.models import Account, VaultItem, VaultFolder, VaultFolderItem
from vault_ai_effective import effective_value

MARKER = "_auto_folders_v1"
CURRENT_MARKER = "_auto_folders_v2"
PENDING_MARKER = "_auto_folder_pending_v1"
FILED = "_auto_categories_filed_v1"   # commercial: categories already filed once; never re-added after operator removal
LEGACY_KEYS = {"selfies", "swimwear", "lingerie", "casual", "fitness", "uncategorised"}
FOLDERS = {
    "selfies": ("Selfies", {"selfie", "selfies", "mirror selfie", "handheld selfie", "sfw selfie"}),
    "swimwear": ("Bikini / swimwear", {"bikini", "swimsuit", "swimwear", "bathing suit"}),
    "lingerie": ("Lingerie", {"lingerie", "lingerie on", "bra", "panties", "underwear", "thong"}),
    "casual": ("Casual / everyday", {"casual", "everyday", "casual outfit", "casual clothes", "streetwear"}),
    "fitness": ("Fitness", {"fitness", "workout", "gym", "exercise", "activewear", "sports bra"}),
    "outdoors": ("Outdoors", {"outdoor", "outdoors", "outside", "garden"}),
    "bedroom": ("Bedroom", {"bedroom", "bed"}),
    "bathroom": ("Bathroom / shower", {"bathroom", "shower", "bath", "shower bath"}),
    "studio": ("Studio", {"studio", "photoshoot", "photo shoot"}),
    "beach": ("Beach / pool", {"beach", "pool", "poolside", "swimming pool"}),
    "cosplay": ("Outfits / cosplay", {"cosplay", "costume", "outfit cosplay"}),
    "talking": ("Talking videos", {"talking head", "talking to camera"}),
    "dance": ("Dance / twerk", {"dance", "dancing", "twerk", "ass twerk", "ass shaking"}),
    "solo_tease": ("Solo tease", {"solo tease"}),
    "solo_fingering": ("Solo fingering", {"solo fingering"}),
    "solo_toy": ("Solo toys", {"solo toy"}),
    "nude_stills": ("Nude photos", {"nude stills"}),
    "boobs": ("Boobs", {"boobs"}),
    "blowjob": ("Blowjob", {"blowjob"}),
    "partner": ("With partner", {"sex with partner"}),
    "feet": ("Feet", {"feet"}),
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
    for key in ("camera", "primary_folder", "clothing_state", "setting"):
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


def _withdraw_filings(session, account_id, media_id, filed):
    """Remove live memberships in auto folders the classifier filed; return their keys. Operator additions,
    Miscellaneous/Unsorted and archived folders are untouched. Idempotent: a second call finds nothing."""
    owners = [OWNER_PREFIX + k for k in sorted(filed - {"miscellaneous", "pending"})]
    if not owners:
        return set()
    rows = session.execute(select(VaultFolder.id, VaultFolder.created_by).join(VaultFolderItem,
        (VaultFolderItem.account_id == VaultFolder.account_id) & (VaultFolderItem.folder_id == VaultFolder.id))
        .where(VaultFolder.account_id == account_id, VaultFolderItem.media_id == media_id,
               VaultFolder.deleted_at.is_(None), VaultFolder.created_by.in_(owners))).all()
    if rows:
        session.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id == account_id,
            VaultFolderItem.media_id == media_id, VaultFolderItem.folder_id.in_([r[0] for r in rows])))
    return {r[1][len(OWNER_PREFIX):] for r in rows}


def assign_sync(session, account_id, media_id, *, commercial=False):
    """Assign once in the caller's transaction. Account lock serializes folders.

    Existing operator folders are never adopted by matching their display name.
    Archived auto folders are not recreated. Only the internally owned Unsorted
    membership is removed after classification; operator memberships are retained.
    A commercially uncertain item also leaves its internal category folders.
    """
    owner = session.scalar(select(Account.id).where(Account.id == account_id).with_for_update())
    if owner is None:
        raise ValueError("Unknown vault account")
    item = session.scalar(select(VaultItem).where(
        VaultItem.account_id == account_id, VaultItem.media_id == media_id).with_for_update())
    if item is None or item.removed_at is not None or item.kind not in {"photo", "video", "gif", "audio"}:
        return {"status": "skipped"}
    try:
        fields = json.loads(item.ai_fields_json or "{}")
    except (ValueError, TypeError):
        return {"status": "invalid_metadata"}
    if not isinstance(fields, dict):
        return {"status": "invalid_metadata"}
    previous = fields.get("_commercial_classification_v1")
    commercial = commercial or isinstance(previous, dict)
    if fields.get(CURRENT_MARKER) is True and not commercial:
        return {"status": "already_categorised"}
    has_metadata = bool(effective_value(item, "description") or effective_value(item, "video_description")
        or _tags(effective_value(item, "tags", default=[]))
        or effective_value(item, "primary_folder") or effective_value(item, "setting"))
    pending = not has_metadata
    if pending and fields.get(PENDING_MARKER) is True and not (commercial and fields.get(CURRENT_MARKER) is True):
        return {"status": "awaiting_metadata"}
    wanted = ["pending"] if pending else categories(item)
    evidenced = list(wanted)
    filed = None
    # V1 already classified these categories. A migration must not re-add a
    # membership an operator removed, or recreate their archived folders.
    # (Only a genuine V1 item: this module sets both markers itself on every v2 pass.)
    legacy = not pending and fields.get(MARKER) is True and fields.get(CURRENT_MARKER) is not True
    if commercial:
        from vault_describe_state import stamp_of
        confidence=effective_value(item,"confidence")
        bound=fields.get("_content_stamp")==stamp_of(item) and stamp_of(item) is not None
        reason=("analysis_pending" if pending else
                "classification_confidence_unknown" if type(confidence) is not int or not 0<=confidence<=100 else
                "low_confidence" if confidence<70 else "analysis_version_unbound" if not bound else
                "no_evidenced_category" if wanted==["uncategorised"] else "metadata_evidence")
        uncertain=reason!="metadata_evidence"
        classification={
            "status":"uncertain" if uncertain else "proposed","reason":reason,
            "content_stamp":stamp_of(item),"categories":["miscellaneous"] if uncertain else wanted,
            "sale_approved":False}
        saved = fields.get(FILED)
        if isinstance(saved, list):
            filed = {k for k in saved if isinstance(k, str)}
        elif fields.get(CURRENT_MARKER) is not True:
            filed = set()
        elif isinstance(previous, dict) and previous.get("status") == "proposed" and isinstance(previous.get("categories"), list):
            filed = {k for k in previous["categories"] if isinstance(k, str)}
        elif isinstance(previous, dict):
            filed = set()           # filed as uncertain only; nothing automatic to preserve
        else:
            filed = set(evidenced)  # categorised before commercial mode: respect the current memberships
        if legacy:
            filed |= LEGACY_KEYS
        withdrawn = set()
        if uncertain:
            # Uncertain is Miscellaneous only. Withdrawn filings are not operator removals: un-file them so
            # confident evidence re-files them. Runs before the fast return so already-uncertain rows heal.
            wanted = ["miscellaneous"]
            withdrawn = _withdraw_filings(session, account_id, media_id, filed)
            filed -= withdrawn
        if fields.get(CURRENT_MARKER) is True and previous == classification and not withdrawn:
            return {"status": "already_categorised"}   # unchanged evidence: no write, no membership churn
        fields["_commercial_classification_v1"]=classification
    if legacy:
        wanted = [key for key in wanted if key not in LEGACY_KEYS]
    if filed is not None and not pending and wanted != ["miscellaneous"]:
        wanted = [key for key in wanted if key not in filed]
    for key in wanted:
        actor = OWNER_PREFIX + key
        folder = session.scalar(select(VaultFolder).where(
            VaultFolder.account_id == account_id, VaultFolder.created_by == actor).order_by(VaultFolder.id))
        if folder is None:
            count = session.scalar(select(func.count()).select_from(VaultFolder).where(
                VaultFolder.account_id == account_id, VaultFolder.deleted_at.is_(None)))
            if count >= 200:
                raise ValueError("Internal folder limit reached")
            folder = VaultFolder(account_id=account_id, created_by=actor,
                name="Miscellaneous" if key=="miscellaneous" else "Unsorted" if key == "pending" else FOLDERS[key][0] if key in FOLDERS else "Uncategorised")
            session.add(folder)
            session.flush()
        if folder.deleted_at is not None:
            continue
        identity = (account_id, folder.id, media_id)
        if session.get(VaultFolderItem, identity) is None:
            count = session.scalar(select(func.count()).select_from(VaultFolderItem).where(
                VaultFolderItem.account_id == account_id, VaultFolderItem.folder_id == folder.id))
            if count >= 5000:
                raise ValueError("Internal folder membership limit reached")
            session.add(VaultFolderItem(account_id=account_id, folder_id=folder.id, media_id=media_id))
    if pending:
        fields[PENDING_MARKER] = True
    else:
        if fields.pop(PENDING_MARKER, False):
            pending_folders = select(VaultFolder.id).where(VaultFolder.account_id == account_id,
                VaultFolder.created_by.in_([OWNER_PREFIX+"pending",OWNER_PREFIX+"miscellaneous"] if commercial and "miscellaneous" not in wanted else [OWNER_PREFIX+"pending"]))
            session.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id == account_id,
                VaultFolderItem.media_id == media_id, VaultFolderItem.folder_id.in_(pending_folders)))
        if commercial and not uncertain:
            # Only the internally owned Miscellaneous filing is withdrawn; operator folders are never touched.
            misc = select(VaultFolder.id).where(VaultFolder.account_id == account_id,
                VaultFolder.created_by == OWNER_PREFIX + "miscellaneous")
            session.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id == account_id,
                VaultFolderItem.media_id == media_id, VaultFolderItem.folder_id.in_(misc)))
        fields.update({MARKER: True, CURRENT_MARKER: True})
    if commercial:
        fields[FILED] = sorted(filed if uncertain else filed | set(wanted))
    item.ai_fields_json = json.dumps(fields, ensure_ascii=False)
    return {"status": "awaiting_metadata" if pending else "categorised", "categories": wanted}


async def assign(session, account_id, media_id):
    """Keep the existing async caption-writer API over one shared classifier."""
    return await session.run_sync(assign_sync, account_id, media_id)

