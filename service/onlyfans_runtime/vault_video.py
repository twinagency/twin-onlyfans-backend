"""Requested full-video caching and permission-checked range playback.

Only the worker contacts the fixed account CDN. API reads serve bounded-memory
local files after the SAME canonical provenance checks as image previews.
"""
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_media_models import OfVaultMediaJob
from db.onlyfans_connection_models import OfProviderConnection
from db.models import OfCreatorBinding
from db.onlyfans_models import OfRuntime
from of_shapes import media_urls
import vault_still_store as images
from vault_media_limits import MAX_BYTES, MIN_FREE_BYTES
from .native_context import canonical
from .store import Refused, utcnow
from .vault_management import _connection, _identity, protected_source
from .vault_previews import asset_scope, _paths

SCHEMA = "vault-video.v1"


class ProtectedVideoUnavailable(Refused):
    """The owner may use a vault identity without a progressive playback URL."""


def video_type(data):
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    return None


def _files(account, media_id):
    image, _ = _paths(account, media_id)
    path = image.with_suffix(".video")
    return path, path.with_suffix(".video.native.json")


def expire_videos(store, tenant):
    """Retire expired full-video bytes and abandoned partials; stills stay permanent."""
    with Session(store.engine) as session:
        accounts = session.scalars(select(OfRuntime.account_id).join(OfCreatorBinding,
            OfCreatorBinding.id == OfRuntime.binding_id).where(OfCreatorBinding.tenant_id == tenant)).all()
    now = utcnow()
    for account in accounts:
        folder = _files(account, 1)[0].parent
        if folder.is_symlink() or not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            try:
                if path.name.endswith(".video.part"):
                    if path.stat().st_mtime < (now - timedelta(minutes=15)).replace(tzinfo=timezone.utc).timestamp():
                        path.unlink(missing_ok=True)
                elif path.suffix == ".video":
                    receipt = path.with_suffix(".video.native.json")
                    if receipt.exists() and not receipt.is_symlink() and receipt.stat().st_size <= 4096:
                        proof = json.loads(receipt.read_bytes())
                        expired = type(proof) is not dict or not isinstance(proof.get("expiresAt"), str) or proof["expiresAt"] <= now.isoformat() + "Z"
                    else:
                        expired = path.stat().st_mtime < (now - timedelta(minutes=15)).replace(tzinfo=timezone.utc).timestamp()
                    if expired:
                        path.unlink(missing_ok=True)
                        receipt.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                continue


def _cached(account, media_id, binding, owner):
    path, receipt = _files(account, media_id)
    try:
        if path.is_symlink() or path.parent.is_symlink() or receipt.is_symlink() or receipt.stat().st_size > 4096:
            return None
        proof = json.loads(receipt.read_bytes())
        stat = path.stat()
        expected = {"schemaVersion": SCHEMA, "accountId": account, "mediaId": media_id,
                    "bindingId": binding, "ownerId": owner, "bytes": stat.st_size, "mtimeNs": stat.st_mtime_ns}
        if (type(proof) is not dict or any(proof.get(key) != value for key, value in expected.items())
                or proof.get("mime") not in {"video/mp4", "video/webm"}
                or not re.fullmatch(r"[a-f0-9]{64}", proof.get("sha256", ""))
                or not 0 < stat.st_size <= MAX_BYTES or proof.get("expiresAt", "") <= utcnow().isoformat() + "Z"):
            return None
        with path.open("rb") as handle:
            if video_type(handle.read(32)) != proof["mime"]:
                return None
        return path, proof
    except (OSError, ValueError, TypeError):
        return None


def _status(store, tenant, account, media_id):
    with Session(store.engine) as session, session.begin():
        runtime, row, connection, raw = asset_scope(session, store, tenant, account, media_id)
        if row.kind != "video":
            raise Refused("video unavailable")
        cached = _cached(account, row.media_id, runtime.binding_id, connection.owner_id)
        jobs = session.scalars(select(OfVaultMediaJob).where(OfVaultMediaJob.account_id == account,
            OfVaultMediaJob.binding_id == runtime.binding_id, OfVaultMediaJob.kind == "video")
            .order_by(OfVaultMediaJob.requested_at.desc()).limit(20)).all()
        job = next((job for job in jobs if json.loads(job.request_json).get("mediaId") == media_id), None)
        return {"mediaId": media_id, "ready": cached is not None,
            "status": "ready" if cached else job.status if job else "unprepared",
            "bytes": cached[1]["bytes"] if cached else None,
            "lastError": None if cached else "protected_playback_not_supported" if protected_source(raw)
                else job.last_error if job else None}


def video_status(store, tenant, account, media_id):
    return _status(store, tenant, account, media_id)


def request_video(store, tenant, account, media_id, *, command_key, actor):
    _identity(command_key, actor)
    with Session(store.engine) as session, session.begin():
        runtime, row, connection, raw = asset_scope(session, store, tenant, account, media_id)
        connection = _connection(session, runtime)
        if row.kind != "video" or raw.get("isReady") is not True:
            raise Refused("video processing is incomplete")
        if _cached(account, row.media_id, runtime.binding_id, connection.owner_id):
            return {"mediaId": media_id, "ready": True, "status": "ready", "bytes": None, "lastError": None}
        if protected_source(raw):
            raise ProtectedVideoUnavailable("protected video playback is unavailable")
        _, source = media_urls(raw)
        if (not source or not urlsplit(source).path.lower().endswith((".mp4", ".m4v", ".webm"))):
            raise Refused("a playable full video is unavailable")
        request = {"mediaId": media_id, "rawHash": hashlib.sha256(row.raw_json.encode()).hexdigest()}
        fingerprint = hashlib.sha256(canonical(request).encode()).hexdigest()
        prior = session.scalar(select(OfVaultMediaJob).where(OfVaultMediaJob.account_id == account,
            OfVaultMediaJob.command_key == command_key))
        if prior and (prior.request_hash != fingerprint or prior.actor != actor or prior.binding_id != runtime.binding_id):
            raise Refused("video command conflict")
        active = session.scalar(select(OfVaultMediaJob).where(OfVaultMediaJob.account_id == account,
            OfVaultMediaJob.kind == "video", OfVaultMediaJob.status.in_(["pending", "running"])))
        if active and json.loads(active.request_json).get("mediaId") != media_id:
            raise Refused("another video is being prepared")
        if not prior and not active:
            now = utcnow()
            session.add(OfVaultMediaJob(account_id=account, job_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
                command_key=command_key, request_hash=fingerprint, actor=actor, kind="video",
                connection_revision=connection.revision, owner_id=connection.owner_id,
                request_json=canonical(request), status="pending", requested_at=now, updated_at=now))
    return _status(store, tenant, account, media_id)


def read_video(store, tenant, account, media_id):
    with Session(store.engine) as session, session.begin():
        runtime, row, connection, raw = asset_scope(session, store, tenant, account, media_id)
        if row.kind != "video":
            raise Refused("video unavailable")
        value = _cached(account, row.media_id, runtime.binding_id, connection.owner_id)
        if value is None:
            raise Refused("video unavailable")
        return value


def download_video(transport, url, path, authorize):
    """One fixed-proxy CDN GET, streamed to a private file with a byte budget."""
    transport._ready()
    transport._handle.current()
    # Reuse the exact CDN admission policy of the existing native still reader.
    from .provider_connector import validate_cdn_url
    validate_cdn_url(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = min(MAX_BYTES, shutil.disk_usage(path.parent).free - MIN_FREE_BYTES)
    if budget <= 0:
        raise Refused("insufficient video cache storage")
    digest, size = hashlib.sha256(), 0
    from curl_cffi import requests
    with path.open("xb") as handle:
        def receive(chunk):
            nonlocal size
            if size // (8 * 1024 * 1024) != (size + len(chunk)) // (8 * 1024 * 1024) or size == 0:
                authorize()
                if shutil.disk_usage(path.parent).free < MIN_FREE_BYTES + len(chunk):
                    raise Refused("insufficient video cache storage")
            size += len(chunk)
            if size > budget:
                raise Refused("video exceeded its cache budget")
            digest.update(chunk)
            handle.write(chunk)
            return len(chunk)
        with requests.Session(impersonate="chrome", trust_env=False, retry=0) as client:
            response = client.request("GET", url, headers={"Accept": "video/mp4,video/webm", "Accept-Encoding": "identity",
                "User-Agent": transport._handle.user_agent, "Referer": "https://onlyfans.com/"},
                proxy=transport._handle.proxy_url, timeout=300, allow_redirects=False, verify=True,
                discard_cookies=True, accept_encoding="identity", content_callback=receive)
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise Refused("full video read unavailable")
            mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if mime not in {"video/mp4", "video/webm"} or size == 0:
                raise Refused("full video read unavailable")
        handle.flush()
        os.fsync(handle.fileno())
    with path.open("rb") as handle:
        if video_type(handle.read(32)) != mime:
            raise Refused("full video format unavailable")
    return {"bytes": size, "sha256": digest.hexdigest(), "mime": mime}


def process_video(store, tenant, *, stop=None):
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("video preparation requires the worker role")
    with Session(store.engine) as session:
        candidate = session.execute(select(OfVaultMediaJob.account_id, OfVaultMediaJob.job_id).join(
            OfCreatorBinding, OfCreatorBinding.id == OfVaultMediaJob.binding_id).where(
            OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
            OfVaultMediaJob.kind == "video", OfVaultMediaJob.status == "pending")
            .order_by(OfVaultMediaJob.requested_at).limit(1)).first()
    if candidate is None or stop is not None and stop.is_set():
        return 0
    account, identity = candidate
    temporary = None
    try:
        with Session(store.engine) as session, session.begin():
            runtime = store._runtime(session, tenant, account)
            job = session.get(OfVaultMediaJob, (account, identity))
            if job.status != "pending":
                return 0
            connection = _connection(session, runtime)
            payload = json.loads(job.request_json)
            _, row, _, raw = asset_scope(session, store, tenant, account, payload["mediaId"])
            if (connection.revision != job.connection_revision or connection.owner_id != job.owner_id or runtime.binding_id != job.binding_id
                    or hashlib.sha256(row.raw_json.encode()).hexdigest() != payload["rawHash"]):
                raise Refused("video source changed")
            from .connections import _open
            clear = _open(connection, tenant)
            binding, owner, expires = job.binding_id, job.owner_id, connection.expires_at
            token = str(uuid.uuid4())
            job.status, job.lease_token, job.lease_until = "running", token, utcnow() + timedelta(minutes=10)
            mid, source = row.media_id, media_urls(raw)[1]
        def authorize():
            if stop is not None and stop.is_set():
                raise Refused("video worker is stopping")
            with Session(store.engine) as session, session.begin():
                runtime = store._runtime(session, tenant, account)
                connection = _connection(session, runtime)
                job = session.get(OfVaultMediaJob, (account, identity))
                if job.status != "running" or job.lease_token != token or connection.revision != job.connection_revision:
                    raise Refused("video preparation changed")
        from .provider_connector import NativeProviderConnector, session_from_capture
        transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner, policy_revision=1,
            capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"], expires_at=expires.replace(tzinfo=timezone.utc)))
        transport.verify_identity()
        path, receipt = _files(account, mid)
        temporary = path.with_name("." + identity + ".video.part")
        result = download_video(transport, source, temporary, authorize)
        authorize()
        os.replace(temporary, path)
        proof = {**result, "schemaVersion": SCHEMA, "accountId": account, "mediaId": mid,
            "bindingId": binding, "ownerId": owner, "mtimeNs": path.stat().st_mtime_ns,
            "expiresAt": (utcnow() + timedelta(hours=48)).isoformat() + "Z"}
        if not images.write(receipt, canonical(proof).encode()):
            raise Refused("video receipt unavailable")
        error = None
    except Exception:
        error = "video_prepare_failed_sync_vault_and_retry"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    with Session(store.engine) as session, session.begin():
        store._runtime(session, tenant, account)
        job = session.get(OfVaultMediaJob, (account, identity))
        if job.status == "running" or job.status == "pending":
            job.status, job.last_error = ("failed" if error else "complete"), error
            job.lease_until, job.lease_token = None, None
            job.completed_at, job.updated_at = utcnow(), utcnow()
    return 1

