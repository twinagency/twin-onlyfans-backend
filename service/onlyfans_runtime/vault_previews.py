"""Native ownership adapter over the existing permanent vault image store.

Only the worker fetches provider bytes. API reads require current canonical
vault provenance and an owner/binding/hash receipt beside the existing image.
No signed URL, credential, new media record, classifier or model is returned.
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import VaultItem
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from of_shapes import still_url
import vault_still_store as images
from .native_context import canonical
from .store import Refused, _id
from .vault_management import InvalidVault, _provenance

SCHEMA = "vault-still.v1"


def _paths(account, media_id):
    path = images.image_path(account, media_id)
    if account in {".", ".."} or path.parent.resolve().parent != images.IMAGE_DIR.resolve():
        raise Refused("vault preview unavailable")
    return path, path.with_suffix(".native.json")


def _scope(account, binding, owner, media_id):
    return dict(schemaVersion=SCHEMA, accountId=account, bindingId=binding,
                ownerId=owner, mediaId=media_id)


def _read(account, binding, owner, media_id):
    path, receipt = _paths(account, media_id)
    try:
        if path.is_symlink() or receipt.is_symlink() or receipt.stat().st_size > 2048:
            return None
        proof = json.loads(receipt.read_bytes())
        if type(proof) is not dict or set(proof) != {*_scope(account, binding, owner, media_id), "sha256"}:
            return None
        if any(proof[k] != v for k, v in _scope(account, binding, owner, media_id).items()):
            return None
        with path.open("rb") as handle:
            data = handle.read(images.MAX_STILL_BYTES + 1)
        if (not data or len(data) > images.MAX_STILL_BYTES or not images.image_type(data)
                or proof["sha256"] != hashlib.sha256(data).hexdigest()):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def warm_page(transport, rows, *, binding_id, owner_id, stop=None):
    """At most 24 stills/three simultaneous two-second GETs for one sync page.

    Cache failures do not make metadata disappear or authorize a paid send.
    A crash between image and receipt writes yields a miss, never partial data.
    Re-reading a page after a crash reuses verified permanent bytes.
    """
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("vault preview warming requires the worker role")
    if type(rows) is not list or len(rows) > 24:
        raise Refused("invalid vault preview page")
    def one(media):
        if stop is not None and stop.is_set(): return False
        if (media.get("type") not in {"photo", "video", "gif"} or media.get("isDeleted") is True
                or media.get("isHidden") is True or media.get("canView") is False): return False
        mid = _id(media.get("id"))
        try:
            if _read(transport.account_id, binding_id, owner_id, mid) is not None: return True
            url = still_url(media)
            if not url: return False
            data = transport.still_bytes(url)
            if not data or len(data) > images.MAX_STILL_BYTES or not images.image_type(data): return False
            path, receipt = _paths(transport.account_id, mid)
            proof = {**_scope(transport.account_id, binding_id, owner_id, mid),
                     "sha256": hashlib.sha256(data).hexdigest()}
            return images.write(path, data) and images.write(receipt, canonical(proof).encode())
        except Exception:
            # CDN failures may contain authenticated URLs. No raw exception log.
            return False
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="vault-still") as executor:
        return sum(executor.map(one, rows))


def asset_scope(session, store, tenant, account, media_id):
    """Shared canonical ownership check for cached stills and full video reads."""
    if (type(media_id) is not str or not media_id.isascii() or not media_id.isdecimal()
            or media_id.startswith("0") or len(media_id) > 19):
        raise InvalidVault("Use an exact vault media reference.")
    mid = _id(int(media_id))
    runtime = store._runtime(session, tenant, account)
    row = session.get(VaultItem, (account, mid))
    observation = session.get(OfVaultObservation, (account, mid))
    job = session.get(OfVaultSyncJob, (account, observation.job_id)) if observation else None
    connection = session.execute(select(OfProviderConnection.binding_id, OfProviderConnection.owner_id,
        OfProviderConnection.revision).where(OfProviderConnection.account_id == account)).first()
    if (row is None or row.removed_at is not None or row.kind not in {"photo", "video", "gif"}
            or not _provenance(row, observation, job, connection, runtime.binding_id, current_revision=False)):
        raise Refused("vault preview unavailable")
    raw = json.loads(row.raw_json)
    if (raw.get("id") != mid or raw.get("type") != row.kind or raw.get("isDeleted") is True
            or raw.get("isHidden") is True or raw.get("canView") is False):
        raise Refused("vault preview unavailable")
    return runtime, row, connection, raw


def read_preview(store, tenant, account, media_id):
    with Session(store.engine) as session, session.begin():
        runtime, row, connection, raw = asset_scope(session, store, tenant, account, media_id)
        # Cached bytes remain readable after pause/expiry, never becoming sellable.
        mid = row.media_id
        data = _read(account, runtime.binding_id, connection.owner_id, mid)
        if data is None: raise Refused("vault preview unavailable")
        return data, images.image_type(data)

