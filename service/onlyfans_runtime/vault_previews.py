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
        # Legacy caption collectors may replace their shared .jpg file. Serve
        # the verified gallery copy independently so analysis cannot blank it.
        owned = path.with_suffix(".owned.jpg")
        if owned.is_symlink():
            return None
        source = owned if owned.exists() else path
        with source.open("rb") as handle:
            data = handle.read(images.MAX_STILL_BYTES + 1)
        if (not data or len(data) > images.MAX_STILL_BYTES or not images.image_type(data)
                or proof["sha256"] != hashlib.sha256(data).hexdigest()):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _request_path(account, media_id):
    path, _ = _paths(account, media_id)
    return path.with_suffix(".preview-request")


def request_preview(account, binding, owner, media_id):
    """A canonical cache miss requests worker priority, without provider I/O."""
    return images.write(_request_path(account, media_id), canonical(_scope(account, binding, owner, media_id)).encode())


def requested_ids(account, binding, owner):
    path, _ = _paths(account, 1)
    result=[]
    for marker in path.parent.glob("*.preview-request"):
        try:
            value=marker.name.split(".",1)[0]
            if not value.isascii() or not value.isdecimal() or value.startswith("0") or len(value)>19:
                continue
            mid=_id(int(value))
            if marker.is_symlink() or marker.stat().st_size>2048:
                continue
            if json.loads(marker.read_bytes()) == _scope(account,binding,owner,mid):
                result.append((marker.stat().st_mtime,mid))
        except (OSError,ValueError,TypeError,Refused):
            continue
    return [mid for _,mid in sorted(result)[:24]]


def clear_request(account, binding, owner, media_id):
    marker=_request_path(account,media_id)
    try:
        if not marker.is_symlink() and marker.stat().st_size<=2048 and json.loads(marker.read_bytes())==_scope(account,binding,owner,media_id):
            marker.unlink(missing_ok=True)
    except (OSError,ValueError,TypeError):
        pass


def promote_cached(account, binding, owner, media_id):
    """Pin an already ownership-verified still before a legacy collector runs."""
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("vault preview warming requires the worker role")
    data = _read(account, binding, owner, media_id)
    if data is None:
        return False
    path, _ = _paths(account, media_id)
    owned = path.with_suffix(".owned.jpg")
    if owned.is_symlink():
        return False
    okay = owned.exists() or images.write(owned, data)
    if okay:
        clear_request(account,binding,owner,media_id)
    return okay


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
            if promote_cached(transport.account_id, binding_id, owner_id, mid): return True
            url = still_url(media)
            if not url: return False
            data = transport.still_bytes(url)
            if not data or len(data) > images.MAX_STILL_BYTES or not images.image_type(data): return False
            path, receipt = _paths(transport.account_id, mid)
            proof = {**_scope(transport.account_id, binding_id, owner_id, mid),
                     "sha256": hashlib.sha256(data).hexdigest()}
            return (images.write(path, data) and images.write(path.with_suffix(".owned.jpg"), data)
                and images.write(receipt, canonical(proof).encode()))
        except Exception:
            # CDN failures may contain authenticated URLs. No raw exception log.
            return False
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="vault-still") as executor:
        return sum(executor.map(one, rows))


def asset_scope(session, store, tenant, account, media_id, *, allow_audio=False):
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
    if (row is None or row.removed_at is not None or row.kind not in ({"photo", "video", "gif", "audio"} if allow_audio else {"photo", "video", "gif"})
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
        if data is None:
            request_preview(account,runtime.binding_id,connection.owner_id,mid)
            raise Refused("vault preview unavailable")
        return data, images.image_type(data)

