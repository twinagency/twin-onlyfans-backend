"""Durable native upload admission over the existing vault batch owner.

The API only stages bounded chunks. A claimed worker uses vault_upload for
provider uploads, carrier evidence, pacing, cleanup and per-file outcomes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_media_models import OfVaultMediaJob
from db.onlyfans_connection_models import OfProviderConnection
from db.models import OfCreatorBinding
from vault_media_limits import MAX_BYTES, MAX_FILES, MIN_FREE_BYTES, CHUNK_BYTES
from vault_upload_state import intent_unresolved
from .native_context import canonical
from .store import Refused, utcnow
from .vault_management import _connection, _identity, InvalidVault

SCHEMA = "of-vault-media.v1"
SPOOL = Path(os.environ.get("VAULT_SPOOL_DIR") or Path(__file__).resolve().parents[1] / "vault_spool")
ACTIVE = {"receiving", "pending", "running"}
TYPES = {".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo", ".gif": "photo",
         ".mp4": "video", ".m4v": "video", ".mov": "video", ".webm": "video", ".mkv": "video"}


def identifier(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, AttributeError):
        raise InvalidVault("Use the recorded upload reference.") from None


def manifest(value):
    if type(value) is not list or not 1 <= len(value) <= min(MAX_FILES, 200):
        raise InvalidVault("Choose a supported number of photos or videos.")
    result, seen = [], set()
    for file in value:
        if type(file) is not dict or set(file) != {"fileId", "name", "size"}:
            raise InvalidVault("That file declaration is invalid.")
        fid, name, size = identifier(file["fileId"]), file["name"], file["size"]
        if (fid in seen or type(name) is not str or not name or len(name.encode()) > 240
                or name in {".", ".."} or any(c in name for c in "/\\")
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or Path(name).suffix.lower() not in TYPES
                or type(size) is not int or not 1 <= size <= MAX_BYTES):
            raise InvalidVault("Use a supported photo or video within the per-file limit.")
        seen.add(fid)
        result.append({"fileId": fid, "name": name, "size": size})
    return result


def _dir(job_id):
    path = SPOOL / "incoming" / ("native-" + identifier(job_id))
    if path.is_symlink() or path.resolve().parent != (SPOOL / "incoming").resolve():
        raise Refused("upload storage unavailable")
    return path


def _file(job, file_id):
    entry = next((f for f in json.loads(job.request_json)["files"] if f["fileId"] == identifier(file_id)), None)
    if entry is None:
        raise Refused("upload file unavailable")
    root = _dir(job.job_id)
    path = root / entry["fileId"] / entry["name"]
    if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise Refused("upload storage unavailable")
    return entry, path


def _run(job):
    if not job.run_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", job.run_id):
        return None
    path = SPOOL / job.run_id / "run.json"
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 512 * 1024:
            return None
        value = json.loads(path.read_bytes())
        return value if value.get("account_id") == job.account_id and value.get("run_id") == job.run_id else None
    except (OSError, ValueError, AttributeError):
        return None


def _progress(path, declared_size):
    receipt = path.with_name(path.name + ".chunks.json")
    digests = []
    if receipt.exists():
        if receipt.is_symlink() or receipt.stat().st_size > 1024 * 1024:
            raise Refused("upload progress unavailable")
        digests = json.loads(receipt.read_bytes())
        if (type(digests) is not list or len(digests) > (declared_size + CHUNK_BYTES - 1) // CHUNK_BYTES
                or not all(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) for value in digests)):
            raise Refused("upload progress unavailable")
    received = min(len(digests) * CHUNK_BYTES, declared_size)
    if received and (not path.exists() or path.stat().st_size < received):
        raise Refused("upload progress unavailable")
    return received, digests, hashlib.sha256("".join(digests).encode()).hexdigest()


def _view(job):
    state = _run(job)
    items = (state or {}).get("items", [])
    files = []
    for entry in json.loads(job.request_json).get("files", []):
        _, path = _file(job, entry["fileId"])
        item = next((item for item in items if item.get("path") == str(path)), None)
        received, _, prefix_digest = _progress(path, entry["size"]) if job.status == "receiving" else (0, [], None)
        if job.status not in {"receiving", "expired"}:
            received = entry["size"]
        status = item.get("status") if item else ("failed" if job.status in {"failed", "interrupted"}
            else "staged" if received == entry["size"] else "receiving")
        files.append({**entry, "receivedBytes": received, "prefixDigest": prefix_digest, "status": status,
            "mediaId": str(item["vault_id"]) if item and item.get("vault_id") else None,
            "processing": bool(item and item.get("still_transcoding")),
            "error": ("This file needs review. Retry it after checking the account and upload status."
                      if item and item.get("error") else None)})
    outstanding = bool(state and any((item.get("carrier_post_id") and not item.get("carrier_deleted"))
                                    or intent_unresolved(item) for item in items))
    return {"jobId": job.job_id, "kind": job.kind, "status": job.status,
        "files": files, "lastError": job.last_error, "cleanupRequired": outstanding,
        "requestedAt": job.requested_at.isoformat() + "Z", "updatedAt": job.updated_at.isoformat() + "Z"}


def _job(session, store, tenant, account, job_id, *, current=False):
    runtime = store._runtime(session, tenant, account)
    job = session.get(OfVaultMediaJob, (account, identifier(job_id)))
    if job is None or job.binding_id != runtime.binding_id or job.kind != "upload":
        raise Refused("upload unavailable")
    if current:
        connection = _connection(session, runtime)
        if connection.revision != job.connection_revision or connection.owner_id != job.owner_id:
            raise Refused("upload connection changed")
    return job


def request_upload(store, tenant, account, *, files, expected_connection_revision, command_key, actor):
    _identity(command_key, actor)
    files = manifest(files)
    request = {"files": files, "expectedConnectionRevision": expected_connection_revision}
    digest = hashlib.sha256(canonical(request).encode()).hexdigest()
    with Session(store.engine) as session, session.begin():
        runtime = store._runtime(session, tenant, account)
        prior = session.scalar(select(OfVaultMediaJob).where(OfVaultMediaJob.account_id == account,
            OfVaultMediaJob.command_key == command_key))
        if prior:
            if prior.request_hash != digest or prior.actor != actor or prior.binding_id != runtime.binding_id:
                raise Refused("upload idempotency conflict")
            return _view(prior)
        connection = _connection(session, runtime)
        if type(expected_connection_revision) is not int or connection.revision != expected_connection_revision:
            raise Refused("upload connection changed")
        existing = session.scalar(select(OfVaultMediaJob.job_id).where(OfVaultMediaJob.account_id == account,
            OfVaultMediaJob.kind == "upload", (OfVaultMediaJob.status.in_(ACTIVE)
            | OfVaultMediaJob.cleanup_required.is_(True))).limit(1))
        if existing:
            raise Refused("an upload or its cleanup is outstanding")
        SPOOL.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(SPOOL).free - sum(file["size"] for file in files) < MIN_FREE_BYTES:
            raise Refused("insufficient upload storage")
        now = utcnow()
        job = OfVaultMediaJob(account_id=account, job_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            command_key=command_key, request_hash=digest, actor=actor, kind="upload",
            connection_revision=connection.revision, owner_id=connection.owner_id,
            request_json=canonical(request), status="receiving", requested_at=now, updated_at=now)
        session.add(job)
        session.flush()
        return _view(job)


def list_uploads(store, tenant, account):
    with Session(store.engine) as session, session.begin():
        runtime = store._runtime(session, tenant, account)
        jobs = session.scalars(select(OfVaultMediaJob).where(OfVaultMediaJob.account_id == account,
            OfVaultMediaJob.binding_id == runtime.binding_id, OfVaultMediaJob.kind == "upload")
            .order_by(OfVaultMediaJob.requested_at.desc()).limit(20)).all()
        return {"schemaVersion": SCHEMA, "accountId": account, "maxFiles": min(MAX_FILES, 200),
            "maxFileBytes": MAX_BYTES, "chunkBytes": CHUNK_BYTES,
            "enabled": os.environ.get("ONLYFANS_VAULT_MEDIA_ENABLED") == "1", "jobs": [_view(job) for job in jobs]}


def write_chunk(store, tenant, account, job_id, file_id, *, offset, digest, data, actor):
    if (type(offset) is not int or offset < 0 or type(data) is not bytes or not 1 <= len(data) <= CHUNK_BYTES
            or type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or hashlib.sha256(data).hexdigest() != digest):
        raise InvalidVault("That upload chunk was not verified.")
    with Session(store.engine) as session, session.begin():
        job = _job(session, store, tenant, account, job_id, current=True)
        if actor != job.actor:
            raise Refused("upload actor changed")
        if job.status != "receiving" or utcnow() - job.requested_at > timedelta(hours=24):
            raise Refused("upload is not receiving files")
        entry, path = _file(job, file_id)
        if offset % CHUNK_BYTES or len(data) != min(CHUNK_BYTES, entry["size"] - offset):
            raise InvalidVault("That chunk exceeds the declared file size.")
        path.parent.mkdir(parents=True, exist_ok=True)
        received, digests, _ = _progress(path, entry["size"])
        size = path.stat().st_size if path.exists() else 0
        if offset > received:
            raise Refused("upload offset conflict")
        if size >= offset + len(data):
            with path.open("rb") as handle:
                handle.seek(offset)
                if handle.read(len(data)) != data:
                    raise Refused("upload chunk conflict")
        elif size == offset:
            if shutil.disk_usage(path.parent).free - len(data) < MIN_FREE_BYTES:
                raise Refused("insufficient upload storage")
            with path.open("ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            # A crash partway through the unacknowledged final append can be
            # repaired only at its recorded offset, never over confirmed bytes.
            if offset != received:
                raise Refused("upload offset conflict")
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.seek(offset)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        if offset == received:
            digests.append(digest)
            import vault_still_store
            if not vault_still_store.write(path.with_name(path.name + ".chunks.json"), canonical(digests).encode()):
                raise Refused("upload progress could not be committed")
        job.updated_at = utcnow()
        return {"receivedBytes": min(len(digests) * CHUNK_BYTES, entry["size"])}


def _validate_file(entry, path):
    if not path.is_file() or path.stat().st_size != entry["size"]:
        raise InvalidVault("Finish transferring every selected file first.")
    kind = TYPES[Path(entry["name"]).suffix.lower()]
    if kind == "photo":
        from PIL import Image
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception:
            raise InvalidVault("One selected photo is not a supported image.") from None
    else:
        try:
            result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True)
            if json.loads(result.stdout).get("streams", [{}])[0].get("codec_type") != "video":
                raise ValueError()
        except Exception:
            raise InvalidVault("One selected video could not be validated.") from None


def finish_upload(store, tenant, account, job_id, *, actor):
    # Admission checks only durable transfer completion. Expensive decoding
    # belongs to the worker, where one invalid file need not fail the batch.
    with Session(store.engine) as session, session.begin():
        job = _job(session, store, tenant, account, job_id, current=True)
        if actor != job.actor:
            raise Refused("upload actor changed")
        if job.status in {"pending", "running", "complete", "failed", "interrupted"}:
            return _view(job)
        if job.status != "receiving":
            raise Refused("upload is not receiving files")
        files = [_file(job, file["fileId"]) for file in json.loads(job.request_json)["files"]]
    for entry, path in files:
        received, _, _ = _progress(path, entry["size"])
        if not path.is_file() or path.stat().st_size != entry["size"] or received != entry["size"]:
            raise InvalidVault("Finish transferring every selected file first.")
    with Session(store.engine) as session, session.begin():
        job = _job(session, store, tenant, account, job_id, current=True)
        if job.status == "receiving":
            job.status, job.updated_at = "pending", utcnow()
        return _view(job)


def cancel_upload(store, tenant, account, job_id, *, actor):
    with Session(store.engine) as session, session.begin():
        job = _job(session, store, tenant, account, job_id)
        if actor != job.actor:
            raise Refused("upload actor changed")
        if job.status == "expired":
            return _view(job)
        if job.status not in {"receiving", "pending"}:
            raise Refused("a running upload cannot be discarded")
        job.status, job.updated_at, job.completed_at = "expired", utcnow(), utcnow()
        shutil.rmtree(_dir(job.job_id), ignore_errors=True)
        return _view(job)


def process_upload(store, tenant, *, stop=None):
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("vault uploads require the worker role")
    now = utcnow()
    with Session(store.engine) as session:
        candidate = session.execute(select(OfVaultMediaJob.account_id, OfVaultMediaJob.job_id).join(
            OfCreatorBinding, OfCreatorBinding.id == OfVaultMediaJob.binding_id).where(
            OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
            OfVaultMediaJob.kind == "upload", OfVaultMediaJob.status == "pending")
            .order_by(OfVaultMediaJob.requested_at).limit(1)).first()
    if candidate is None or stop is not None and stop.is_set():
        return 0
    account, identity = candidate
    with Session(store.engine) as session, session.begin():
        job = _job(session, store, tenant, account, identity, current=True)
        if job.status != "pending":
            return 0
        connection = session.get(OfProviderConnection, account)
        from .connections import _open
        clear = _open(connection, tenant)
        token = str(uuid.uuid4())
        job.status, job.lease_token, job.lease_until = "running", token, now + timedelta(minutes=20)
        job.updated_at = now
        job.run_id = account + "-native-" + identity
        run_id, expires, owner, revision = job.run_id, connection.expires_at, job.owner_id, connection.revision
        actor = job.actor
        paths = [str(_file(job, file["fileId"])[1]) for file in json.loads(job.request_json)["files"]]

    def authorize():
        if stop is not None and stop.is_set():
            raise Refused("upload worker is stopping")
        with Session(store.engine) as session, session.begin():
            job = _job(session, store, tenant, account, identity, current=True)
            if job.status != "running" or job.lease_token != token:
                raise Refused("upload lease changed")
            job.lease_until, job.updated_at = utcnow() + timedelta(minutes=20), utcnow()

    error = None
    try:
        from .provider_connector import session_from_capture
        from .vault_upload_transport import NativeVaultUpload
        import vault_upload
        session = session_from_capture(account_id=account, owner_id=owner, policy_revision=1,
            capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
            expires_at=expires.replace(tzinfo=timezone.utc))
        client = NativeVaultUpload(session, authorize=authorize, job_id=identity)
        client.transport.verify_identity()
        vault_upload.begin(account, run_id=run_id)
        result = vault_upload.start(account, client=client, local_paths=paths, run_id=run_id,
            validate_item=lambda item: _validate_file({"name": item["name"], "size": item["size"]}, Path(item["path"])))
        if result["status"] != "done" or any(item.get("status") != "done" for item in result.get("items", [])):
            error = "upload_items_need_review"
        if any(item.get("carrier_deleted") is False for item in result.get("items", [])):
            error = "carrier_cleanup_required"
    except Exception:
        error = "upload_outcome_requires_review"
    finally:
        with Session(store.engine) as session, session.begin():
            job = _job(session, store, tenant, account, identity)
            if job.lease_token == token:
                job.cleanup_required = _view(job)["cleanupRequired"]
                job.status, job.last_error = ("failed" if error else "complete"), error
                job.lease_until, job.lease_token = None, None
                job.completed_at, job.updated_at = utcnow(), utcnow()
        shutil.rmtree(_dir(identity), ignore_errors=True)
    # The normal authoritative sync establishes readiness and preview provenance.
    try:
        from .vault_management import request_sync
        request_sync(store, tenant, account, expected_connection_revision=revision,
            command_key=hashlib.sha256(("upload-sync:" + identity).encode()).hexdigest(), actor=actor)
    except Exception:
        pass
    return 1
