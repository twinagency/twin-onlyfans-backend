"""CRM Save -> unpublished media upload -> disabled fixed-price catalogue item (handoff 15.3).

Save never sends a fan message, publishes a feed post or touches outgoing
authority. Media reaches the creator's vault only through a SyncTransport that
is explicitly provided; the existing native uploader gets media into the vault
through a visible scheduled carrier post, so it is deliberately NOT imported
here. Production supplies no transport until an unpublished path is verified:
assets then stay visibly in needs_review (unpublished_upload_unavailable) and
the item can never be created or enabled.

A provider write is always preceded by a durable upload_started_at and token.
Any later attempt first asks the transport to look the token up, so a lost
acknowledgement is reconciled instead of blindly uploaded twice; an
undeterminable outcome stays visibly blocked as outcome_unconfirmed.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import uuid
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from onlyfans_read.agency_scope import actor_allowed
from onlyfans_read.contract import ID_PATTERN
from db.models import CatalogItem, OfCreatorBinding, VaultItem
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_offer_import_models import (OfContentSyncAsset as Asset, OfContentSyncDecision as Decision,
    OfContentSyncDraft as Draft, OfContentSyncOperation as Operation)
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from vault_media_limits import CHUNK_BYTES, MAX_BYTES, MIN_FREE_BYTES
from .content_management import digest, validate_patch
from .native_context import canonical
from .store import Refused, utcnow
from .vault_management import InvalidVault, _connection, _ready, create_catalog_single, request_sync
from .vault_media import SPOOL as MEDIA_SPOOL, TYPES

SPOOL = MEDIA_SPOOL / "content-sync"
MAX_FILES = 20
MAX_ATTEMPTS = 6
LEASE_SECONDS = 600
VERIFY_POLL = 15
AUTH_PAUSE = 300
OBSERVATION_POLL = 20
RESOLVE_WINDOW = timedelta(hours=6)
DRAFT_WINDOW = timedelta(hours=24)
ACTIVE = ("queued", "uploading", "verifying", "retrying")
# Worst first: the draft shows the least-advanced / most-blocked asset.
RANK = ("outcome_unconfirmed", "needs_review", "connection_required", "retrying",
        "queued", "uploading", "verifying", "synced")
RETRYABLE_REASONS = {"unpublished_upload_unavailable", "retries_exhausted", "processing_timeout",
                     "observation_timeout", "scope_unavailable", "upload_failed"}


class InvalidSync(ValueError):
    pass


class UploadUnknown(Exception):
    """The provider outcome cannot be established (lost ack / undeterminable lookup)."""


class TransientUpload(Exception):
    """Classified retryable failure; optional retry_after seconds."""
    retry_after = None


class AuthRequired(Exception):
    """The connection is expired or rejected; no provider work until it is verified again."""


class SyncTransport(Protocol):
    def lookup(self, *, upload_token: str) -> int | None: ...   # None = never landed; UploadUnknown = undeterminable
    def upload(self, *, upload_token: str, path: Path, kind: str) -> int: ...
    def verify(self, *, provider_media_id: int) -> str: ...      # 'ready' | 'processing' | 'unusable'


def no_transport(connection, tenant, account):
    """Production default: no verified unpublished upload path exists yet."""
    return None


def _uuid(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        raise InvalidSync("That sync reference is invalid.")
    return value


def _key(value, actor):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value) or not actor_allowed(actor):
        raise InvalidSync("The sync command identity is invalid.")


def _date(value):
    return value.isoformat() + "Z" if value else None


def _name(name):
    return (type(name) is str and name and len(name.encode()) <= 240 and name not in {".", ".."}
            and not any(c in name for c in "/\\") and not any(ord(c) < 32 or ord(c) == 127 for c in name)
            and Path(name).suffix.lower() in TYPES)


def _validate(value, actor):
    keys = {"commandKey", "label", "description", "priceCents", "files", "previewFileIds"}
    if type(value) is not dict or not keys <= set(value) <= keys | {"actor"} or value.get("actor", actor) != actor:
        raise InvalidSync("Save takes exactly a label, description, price, ordered files and previews.")
    _key(value["commandKey"], actor)
    price = value["priceCents"]
    if type(price) is not int or not 300 <= price <= 20000:
        raise InvalidSync("Use an exact whole-cent price of 300-20000 USD cents.")
    try:
        text = validate_patch({"label": value["label"], "description": value["description"]})
    except ValueError as exc:
        raise InvalidSync(str(exc)) from None
    if not text["description"]:
        raise InvalidSync("Describe exactly what the fan unlocks.")
    files = value["files"]
    if type(files) is not list or not 1 <= len(files) <= MAX_FILES:
        raise InvalidSync(f"Choose between 1 and {MAX_FILES} photos or videos.")
    result, ids, hashes = [], set(), set()
    for f in files:
        if type(f) is not dict or set(f) != {"fileId", "name", "size", "sha256"}:
            raise InvalidSync("That file declaration is invalid.")
        fid, name, size, sha = _uuid(f["fileId"]), f["name"], f["size"], f["sha256"]
        if (fid in ids or not _name(name) or type(size) is not int or not 1 <= size <= MAX_BYTES
                or type(sha) is not str or not re.fullmatch(r"[a-f0-9]{64}", sha) or sha in hashes):
            raise InvalidSync("Use distinct supported photos or videos within the per-file limit.")
        ids.add(fid)
        hashes.add(sha)
        result.append({"fileId": fid, "name": name, "size": size, "sha256": sha,
                       "kind": TYPES[Path(name).suffix.lower()]})
    previews = value["previewFileIds"]
    if type(previews) is not list or len(previews) >= len(result):
        raise InvalidSync("Previews must leave at least one paid file.")
    kinds = {f["fileId"]: f["kind"] for f in result}
    if (len({_uuid(p) for p in previews}) != len(previews) or any(kinds.get(p) != "photo" for p in previews)):
        raise InvalidSync("Only included photos may be free previews.")
    return {"commandKey": value["commandKey"], "label": text["label"], "description": text["description"],
            "priceCents": price, "files": result, "previewFileIds": previews}


# ── Spool ───────────────────────────────────────────────────────────────

def _dir(draft_id):
    path = SPOOL / _uuid(draft_id)
    if path.is_symlink() or SPOOL.is_symlink() or (path.resolve().parent != SPOOL.resolve()):
        raise Refused("upload storage unavailable")
    return path


def _blob(draft_id, file_id):
    root = _dir(draft_id)
    path = root / _uuid(file_id) / "blob"
    if path.parent.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise Refused("upload storage unavailable")
    return path


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _staged(draft_id, file_id, size, sha):
    path = _blob(draft_id, file_id)
    return (path.is_file() and path.with_name("ok").is_file() and path.stat().st_size == size
            and path.with_name("ok").read_text() == sha)


def _discard(ref):
    try:
        draft_id, file_id = ref.split("/")
        shutil.rmtree(_blob(draft_id, file_id).parent, ignore_errors=True)
        root = _dir(draft_id)
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    except (ValueError, OSError, Refused, InvalidSync):
        pass


# ── Views ───────────────────────────────────────────────────────────────

def _assets_of(s, draft):
    ids = [e["assetId"] for e in json.loads(draft.assets_json)]
    return {a.asset_id: a for a in s.scalars(select(Asset).where(Asset.account_id == draft.account_id, Asset.asset_id.in_(ids)))}


def draft_view(s, draft):
    entries, previews = json.loads(draft.assets_json), set(json.loads(draft.preview_json))
    rows = _assets_of(s, draft)
    assets, blockers = [], []
    def block(code):
        if code and code not in blockers:
            blockers.append(code)
    if draft.state == "draft":
        block("upload_not_committed")
    for order, e in enumerate(entries):
        a = rows.get(e["assetId"])
        state = a.state if a else "draft"
        assets.append({"assetId": e["assetId"], "fileId": e["fileId"], "name": e["name"], "kind": e["kind"],
            "order": order, "freePreview": e["assetId"] in previews, "state": state,
            "attempts": a.attempts if a else 0, "lastError": a.last_error if a else None})
        if state != "synced" and a is not None:
            block("media_not_synced" if state in ACTIVE else state)
            block(a.last_error)
    synced = bool(assets) and all(x["state"] == "synced" for x in assets)
    item = s.get(CatalogItem, draft.item_id) if draft.item_id else None
    block(draft.reason if draft.state != "synced" else None)
    if synced and item is None:
        block("item_pending")
    if item is not None and any(r.removed_at is not None for r in s.scalars(select(VaultItem).where(
            VaultItem.account_id == draft.account_id, VaultItem.media_id.in_(json.loads(item.media_ids))))):
        block("media_unavailable")
    return {"draftId": draft.draft_id, "revision": draft.revision, "state": draft.state, "reason": draft.reason,
        "label": draft.label, "description": draft.description, "priceCents": draft.price_cents,
        "pricingMode": "fixed", "itemId": str(item.id) if item else None, "enabled": bool(item and item.enabled),
        "assets": assets, "readiness": {"mediaSynced": synced, "canEnable": synced and item is not None and not blockers,
                                         "blockers": blockers}}


def operation_view(op, asset=None):
    result = json.loads(op.result_json)
    return {"operationId": op.operation_id, "kind": op.kind, "status": op.status, "createdAt": _date(op.created_at),
        "updatedAt": _date(op.updated_at), "completedAt": _date(op.completed_at), "coverage": json.loads(op.coverage_json),
        "result": result, "lastError": op.last_error, "draftId": op.draft_id, "assetId": op.asset_id,
        "retryable": _retryable(asset) if asset is not None else False}


def _retryable(asset):
    return (asset.state in {"retrying", "connection_required", "outcome_unconfirmed"}
            or (asset.state == "needs_review" and asset.last_error in RETRYABLE_REASONS))


def _op(s, asset):
    return s.scalar(select(Operation).where(Operation.account_id == asset.account_id,
        Operation.asset_id == asset.asset_id, Operation.kind == "upload"))


def _mirror(op, asset, now):
    """Keep the operation row a faithful, lease-consistent shadow of its asset."""
    status = ("running" if asset.lease_token else {"synced": "complete", "needs_review": "failed",
        "connection_required": "failed", "outcome_unconfirmed": "outcome_unconfirmed"}.get(asset.state, "queued"))
    op.status, op.last_error, op.attempts = status, asset.last_error, asset.attempts
    op.lease_token, op.lease_until = (asset.lease_token, asset.lease_until) if status == "running" else (None, None)
    op.next_attempt_at, op.updated_at = asset.next_attempt_at, now
    op.completed_at = now if status in {"complete", "failed"} else None
    op.result_json = canonical({"assetState": asset.state, "attempts": asset.attempts,
        "providerMediaId": str(asset.provider_media_id) if asset.provider_media_id else None})


def _set(s, asset, state, *, error=None, delay=None, now=None):
    now = now or utcnow()
    if state == "synced":
        asset.verified_at = now  # the table refuses synced without it, even mid-flush
    asset.state, asset.last_error, asset.updated_at = state, error, now
    asset.next_attempt_at = now + timedelta(seconds=delay) if delay else None
    asset.lease_token = asset.lease_until = None
    op = _op(s, asset)
    if op is not None:
        _mirror(op, asset, now)


def _worst(states):
    return min(states, key=RANK.index)


def _settle(s, runtime, asset_ids, pending):
    """Recompute every committed draft that holds these assets; materialize when whole."""
    for ref in set(asset_ids):
        for draft in s.scalars(select(Draft).where(Draft.account_id == runtime.account_id, Draft.state != "draft",
                Draft.state != "synced", Draft.assets_json.contains(ref)).with_for_update()):
            _settle_draft(s, runtime, draft, pending)


def _settle_draft(s, runtime, draft, pending):
    now = utcnow()
    entries = json.loads(draft.assets_json)
    rows = _assets_of(s, draft)
    states = [rows[e["assetId"]].state for e in entries]
    if all(state == "synced" for state in states):
        if draft.item_id is None:
            if not (draft.reason == "vault_media_unavailable" and now - draft.updated_at < timedelta(seconds=OBSERVATION_POLL)):
                _materialize(s, runtime, draft, rows, entries, pending)
        else:
            draft.state, draft.reason, draft.updated_at = "synced", None, now
        return
    worst = _worst(states)
    culprit = next(rows[e["assetId"]] for e in entries if rows[e["assetId"]].state == worst)
    draft.state = worst
    draft.reason = culprit.last_error if worst in {"outcome_unconfirmed", "needs_review", "connection_required", "retrying"} else None
    draft.updated_at = now


def _materialize(s, runtime, draft, rows, entries, pending):
    """One fixed, disabled CatalogItem in the caller's transaction; enabling stays the content v2 action."""
    now = utcnow()
    def hold(state, reason):
        draft.state, draft.reason, draft.updated_at = state, reason, now
    try:
        connection = _connection(s, runtime)
    except Refused:
        return hold("connection_required", "connection_required")
    media = [rows[e["assetId"]].provider_media_id for e in entries]
    chosen = set(json.loads(draft.preview_json))
    previews = [rows[e["assetId"]].provider_media_id for e in entries if e["assetId"] in chosen]
    try:
        with s.begin_nested():
            item = create_catalog_single(s, runtime, connection, media=media, previews=previews, label=draft.label,
                description=draft.description, price_cents=draft.price_cents, enabled=False, pricing_mode="fixed")
            draft.item_id = item.id
            s.add(Decision(account_id=draft.account_id, command_key=hashlib.sha256(("materialize:" + draft.draft_id).encode()).hexdigest(),
                binding_id=draft.binding_id, action="save", request_hash=digest({"materialize": draft.draft_id}),
                actor=draft.actor, subject_id=draft.draft_id, before_json="{}",
                after_json=canonical({"itemId": item.id, "priceCents": draft.price_cents, "pricingMode": "fixed", "enabled": False}),
                result_json=canonical({"draftId": draft.draft_id, "itemId": str(item.id)}), created_at=now))
    except Refused as exc:
        if str(exc) == "vault media unavailable":
            pending.append(("sync", draft.actor, connection.revision))
            return hold("verifying", "vault_media_unavailable")
        return hold("needs_review", "catalogue_assets_exist" if "already present" in str(exc) else "catalogue_unavailable")
    except (InvalidVault, IntegrityError):
        return hold("needs_review", "catalogue_assets_exist")
    hold("synced", None)
    pending.extend(("discard", rows[e["assetId"]].staged_ref) for e in entries if rows[e["assetId"]].staged_ref)


def _after(store, tenant, account, pending):
    """Work that opens its own transaction (vault sync) or touches files runs after commit."""
    for kind, *rest in pending:
        try:
            if kind == "discard":
                _discard(rest[0])
            else:
                actor, revision = rest
                key = hashlib.sha256(f"content-sync-vault:{account}:{revision}:{int(utcnow().timestamp()) // 60}".encode()).hexdigest()
                request_sync(store, tenant, account, expected_connection_revision=revision, command_key=key, actor=actor)
        except Exception:
            pass  # an active/refused vault sync is fine: the next pass looks again


# ── Save, stage, commit ─────────────────────────────────────────────────

def save_draft(store, tenant, account, *, value, actor):
    body = _validate(value, actor)
    request_hash = digest({k: body[k] for k in ("label", "description", "priceCents", "files", "previewFileIds")})
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.scalar(select(Draft).where(Draft.account_id == account, Draft.command_key == body["commandKey"]))
        if prior is not None:
            if (prior.binding_id, prior.request_hash, prior.actor) != (runtime.binding_id, request_hash, actor):
                raise Refused("sync idempotency conflict")
            return draft_view(s, prior)
        if s.get(Decision, (account, body["commandKey"])) is not None:
            raise Refused("sync idempotency conflict")
        _connection(s, runtime)
        entries, now = [], utcnow()
        for f in body["files"]:
            # Reuse only a synced asset of this exact account and binding.
            have = s.scalar(select(Asset).where(Asset.account_id == account, Asset.binding_id == runtime.binding_id,
                Asset.content_hash == f["sha256"], Asset.state == "synced"))
            entries.append({**f, "assetId": have.asset_id if have else str(uuid.uuid4())})
        by_file = {e["fileId"]: e["assetId"] for e in entries}
        draft = Draft(account_id=account, draft_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            command_key=body["commandKey"], request_hash=request_hash, actor=actor, revision=1, label=body["label"],
            description=body["description"], price_cents=body["priceCents"], currency="USD",
            assets_json=canonical(entries), preview_json=canonical([by_file[p] for p in body["previewFileIds"]]),
            state="draft", created_at=now, updated_at=now)
        s.add(draft)
        s.add(Decision(account_id=account, command_key=body["commandKey"], binding_id=runtime.binding_id, action="save",
            request_hash=request_hash, actor=actor, subject_id=draft.draft_id, before_json="{}",
            after_json=canonical({"priceCents": body["priceCents"], "files": len(entries)}),
            result_json=canonical({"draftId": draft.draft_id, "revision": 1}), created_at=now))
        s.flush()
        return draft_view(s, draft)


def _own(s, store, tenant, account, draft_id, *, write=False):
    runtime = store._runtime(s, tenant, account)
    draft = s.scalar(select(Draft).where(Draft.account_id == account, Draft.draft_id == _uuid(draft_id)).with_for_update(read=not write))
    if draft is None or draft.binding_id != runtime.binding_id:
        raise Refused("sync draft unavailable")
    return runtime, draft


def get_draft(store, tenant, account, draft_id):
    with Session(store.engine) as s, s.begin():
        _, draft = _own(s, store, tenant, account, draft_id)
        return draft_view(s, draft)


def list_drafts(store, tenant, account, *, after, limit):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise InvalidSync("Use a page size between 1 and 50.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        query = select(Draft).where(Draft.account_id == account, Draft.binding_id == runtime.binding_id)
        if after is not None:
            mark = s.get(Draft, (account, _uuid(after)))
            if mark is None or mark.binding_id != runtime.binding_id:
                raise InvalidSync("That page reference is invalid.")
            query = query.where(or_(Draft.created_at < mark.created_at,
                and_(Draft.created_at == mark.created_at, Draft.draft_id < mark.draft_id)))
        rows = s.scalars(query.order_by(Draft.created_at.desc(), Draft.draft_id.desc()).limit(limit + 1)).all()
        return {"items": [draft_view(s, r) for r in rows[:limit]],
                "nextCursor": rows[limit - 1].draft_id if len(rows) > limit else None}


def put_chunk(store, tenant, account, draft_id, file_id, index, data):
    if (type(index) is not int or index < 0 or type(data) is not bytes or not 1 <= len(data) <= CHUNK_BYTES):
        raise InvalidSync("That upload chunk is invalid.")
    _uuid(file_id)
    with Session(store.engine) as s, s.begin():
        runtime, draft = _own(s, store, tenant, account, draft_id, write=True)
        if draft.state != "draft":
            raise Refused("sync draft already committed")
        if utcnow() - draft.created_at > DRAFT_WINDOW:
            raise Refused("sync draft expired")
        entry = next((e for e in json.loads(draft.assets_json) if e["fileId"] == file_id), None)
        if entry is None:
            raise Refused("sync draft file unavailable")
        if s.get(Asset, (account, entry["assetId"])) is not None:
            raise Refused("sync file already synced")
        size, offset = entry["size"], index * CHUNK_BYTES
        if offset >= size or len(data) != min(CHUNK_BYTES, size - offset):
            raise InvalidSync("That chunk does not fit the declared file size.")
        path = _blob(draft_id, file_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        have = path.stat().st_size if path.exists() else 0
        if offset + len(data) <= have:
            with path.open("rb") as handle:
                handle.seek(offset)
                if handle.read(len(data)) != data:
                    raise Refused("sync chunk conflict")
            return {"received": have, "complete": _staged(draft_id, file_id, size, entry["sha256"])}
        # A crash can leave a torn tail; only the first unconfirmed chunk may repair it.
        if offset != have // CHUNK_BYTES * CHUNK_BYTES:
            raise Refused("sync chunk offset conflict")
        if shutil.disk_usage(path.parent).free - len(data) < MIN_FREE_BYTES:
            raise Refused("insufficient upload storage")
        with path.open("ab" if path.exists() else "wb") as handle:
            handle.truncate(offset)
            handle.seek(offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        received = offset + len(data)
        if received < size:
            return {"received": received, "complete": False}
        if _sha(path) != entry["sha256"]:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise InvalidSync("The staged file does not match its declared checksum. Send it again.")
        path.with_name("ok").write_text(entry["sha256"])
        return {"received": received, "complete": True}


def commit_draft(store, tenant, account, draft_id, *, command_key, actor):
    _key(command_key, actor)
    _uuid(draft_id)
    request_hash = digest({"commit": draft_id})
    pending = []
    try:
        with Session(store.engine) as s, s.begin():
            runtime, draft = _own(s, store, tenant, account, draft_id, write=True)
            prior = s.get(Decision, (account, command_key))
            if prior is not None:
                if (prior.action, prior.request_hash, prior.actor, prior.binding_id, prior.subject_id) != (
                        "save", request_hash, actor, runtime.binding_id, draft_id):
                    raise Refused("sync idempotency conflict")
                return draft_view(s, draft)
            if draft.state != "draft":
                raise Refused("sync draft already committed")
            if utcnow() - draft.created_at > DRAFT_WINDOW:
                raise Refused("sync draft expired")
            connection = _connection(s, runtime)
            now, entries, remap, fresh = utcnow(), json.loads(draft.assets_json), {}, []
            for e in entries:
                have = s.scalar(select(Asset).where(Asset.account_id == account, Asset.binding_id == runtime.binding_id,
                    Asset.content_hash == e["sha256"]).with_for_update())
                if have is not None:
                    if have.asset_id != e["assetId"]:
                        remap[e["assetId"]] = have.asset_id
                    pending.append(("discard", f"{draft_id}/{e['fileId']}"))
                    continue
                if not _staged(draft_id, e["fileId"], e["size"], e["sha256"]):
                    raise InvalidSync("Finish transferring every selected file first.")
                asset = Asset(account_id=account, asset_id=e["assetId"], binding_id=runtime.binding_id, kind=e["kind"],
                    name=e["name"], size_bytes=e["size"], content_hash=e["sha256"], staged_ref=f"{draft_id}/{e['fileId']}",
                    upload_token=str(uuid.uuid4()), state="queued", attempts=0, created_at=now, updated_at=now)
                s.add(asset)
                s.add(Operation(account_id=account, operation_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
                    command_key=hashlib.sha256(f"upload:{runtime.binding_id}:{e['sha256']}".encode()).hexdigest(),
                    request_hash=digest({"upload": e["sha256"]}), actor=actor, kind="upload", status="queued",
                    connection_revision=connection.revision, draft_id=draft_id, asset_id=e["assetId"],
                    coverage_json="{}", result_json=canonical({"assetState": "queued", "attempts": 0, "providerMediaId": None}),
                    attempts=0, created_at=now, updated_at=now))
                fresh.append(e["fileId"])
            if remap:
                draft.assets_json = canonical([{**e, "assetId": remap.get(e["assetId"], e["assetId"])} for e in entries])
                draft.preview_json = canonical([remap.get(a, a) for a in json.loads(draft.preview_json)])
            draft.state, draft.reason, draft.updated_at = "queued", None, now
            s.add(Decision(account_id=account, command_key=command_key, binding_id=runtime.binding_id, action="save",
                request_hash=request_hash, actor=actor, subject_id=draft_id, before_json=canonical({"state": "draft"}),
                after_json=canonical({"state": "queued", "uploads": len(fresh)}),
                result_json=canonical({"draftId": draft_id, "committed": True}), created_at=now))
            s.flush()
            _settle_draft(s, runtime, draft, pending)
            view = draft_view(s, draft)
    except IntegrityError:
        # A concurrent commit or a foreign operation key won; nothing here was kept.
        raise Refused("sync conflict") from None
    _after(store, tenant, account, pending)
    return view


def retry_asset(store, tenant, account, operation_id, *, command_key, actor):
    """Requeue one retryable asset. Its upload token and upload_started_at are kept,
    so the worker looks the token up first and never blindly re-uploads."""
    _key(command_key, actor)
    request_hash = digest({"retry": _uuid(operation_id)})
    pending = []
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(Decision, (account, command_key))
        op = s.get(Operation, (account, operation_id))
        if op is None or op.kind != "upload" or op.binding_id != runtime.binding_id:
            raise Refused("sync operation unavailable")
        asset = s.scalar(select(Asset).where(Asset.account_id == account, Asset.asset_id == op.asset_id).with_for_update())
        if prior is not None:
            if (prior.action, prior.request_hash, prior.actor, prior.binding_id) != ("retry", request_hash, actor, runtime.binding_id):
                raise Refused("sync idempotency conflict")
            return operation_view(op, asset)
        if asset.binding_id != runtime.binding_id or not _retryable(asset) or asset.lease_token:
            raise Refused("sync operation not retryable")
        _connection(s, runtime)
        now, before = utcnow(), asset.state
        if asset.attempts >= MAX_ATTEMPTS:
            asset.attempts = 0
        _set(s, asset, "queued", now=now)
        s.add(Decision(account_id=account, command_key=command_key, binding_id=runtime.binding_id, action="retry",
            request_hash=request_hash, actor=actor, subject_id=op.operation_id, before_json=canonical({"state": before}),
            after_json=canonical({"state": "queued"}), result_json=canonical({"operationId": op.operation_id}), created_at=now))
        s.flush()
        _settle(s, runtime, [asset.asset_id], pending)
        return operation_view(op, asset)


# ── Worker ──────────────────────────────────────────────────────────────

@dataclass
class _Lease:
    account: str
    asset_id: str
    token: str
    binding: str
    owner: int
    revision: int
    connection: object
    actor: str
    upload_token: str
    provider_id: int | None
    started: bool
    kind: str
    ref: str | None
    size: int
    sha: str
    started_at: object


def _usable(connection, binding, now):
    return (connection is not None and connection.binding_id == binding and connection.status == "verified"
            and connection.verified_at is not None and connection.expires_at > now)


def _delay(attempts, hint=None):
    if hint:
        return max(1, min(int(hint), 3600))
    return min(3600, 30 * 2 ** max(attempts - 1, 0)) * (1 + random.random() * 0.25)


def _candidates(store, tenant, now, skip):
    with Session(store.engine) as s:
        due = and_(or_(Asset.lease_until.is_(None), Asset.lease_until <= now),
                   or_(Asset.next_attempt_at.is_(None), Asset.next_attempt_at <= now))
        rows = s.execute(select(Asset.account_id, Asset.asset_id).join(OfCreatorBinding, OfCreatorBinding.id == Asset.binding_id)
            .outerjoin(OfProviderConnection, and_(OfProviderConnection.account_id == Asset.account_id,
                OfProviderConnection.binding_id == Asset.binding_id, OfProviderConnection.status == "verified",
                OfProviderConnection.expires_at > now))
            .where(OfCreatorBinding.tenant_id == tenant, or_(and_(Asset.state.in_(ACTIVE), due),
                and_(Asset.state == "connection_required", OfProviderConnection.account_id.is_not(None),
                     or_(Asset.next_attempt_at.is_(None), Asset.next_attempt_at <= now,
                         OfProviderConnection.updated_at > Asset.updated_at))))
            .order_by(Asset.updated_at, Asset.asset_id).limit(16)).all()
    return [tuple(r) for r in rows if tuple(r) not in skip]


def _park(store, account, asset_id, reason):
    """Binding gone/changed: this asset can never be used by another binding."""
    with Session(store.engine) as s, s.begin():
        asset = s.scalar(select(Asset).where(Asset.account_id == account, Asset.asset_id == asset_id).with_for_update())
        if asset is not None and asset.state in ACTIVE + ("connection_required",):
            _set(s, asset, "needs_review", error=reason)
    return 1


def _lease(store, tenant, account, asset_id, now):
    pending = []
    try:
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            asset = s.scalar(select(Asset).where(Asset.account_id == account, Asset.asset_id == asset_id)
                             .with_for_update(skip_locked=True))
            if asset is None or asset.state not in ACTIVE + ("connection_required",) or (
                    asset.lease_until and asset.lease_until > now):
                return None
            connection = s.get(OfProviderConnection, account)
            # A reconnect (connection touched after the asset paused) overrides the pause delay.
            reconnected = (asset.state == "connection_required" and connection is not None
                           and connection.updated_at > asset.updated_at)
            if asset.next_attempt_at and asset.next_attempt_at > now and not reconnected:
                return None
            op = _op(s, asset)
            if asset.binding_id != runtime.binding_id:
                _set(s, asset, "needs_review", error="binding_changed")
                _settle(s, runtime, [asset.asset_id], pending)
                return 1
            if not _usable(connection, runtime.binding_id, now):
                if asset.state != "connection_required":
                    _set(s, asset, "connection_required", error="connection_required")
                    _settle(s, runtime, [asset.asset_id], pending)
                return 1
            asset.lease_token, asset.lease_until = str(uuid.uuid4()), now + timedelta(seconds=LEASE_SECONDS)
            _mirror(op, asset, now)
            s.expunge(connection)
            return _Lease(account, asset_id, asset.lease_token, asset.binding_id, connection.owner_id, connection.revision,
                connection, op.actor, asset.upload_token, asset.provider_media_id, asset.upload_started_at is not None,
                asset.kind, asset.staged_ref, asset.size_bytes, asset.content_hash, asset.upload_started_at)
    except Refused:
        return _park(store, account, asset_id, "scope_unavailable")


def _commit(store, tenant, lease, change):
    """Revalidate binding, owner and lease after provider I/O; apply change(s, asset, runtime, connection).

    A rebound or re-owned account never inherits this mapping."""
    pending = []
    with Session(store.engine) as s, s.begin():
        try:
            runtime = store._runtime(s, tenant, lease.account)
        except Refused:
            runtime = None
        asset = s.scalar(select(Asset).where(Asset.account_id == lease.account, Asset.asset_id == lease.asset_id).with_for_update())
        if asset is None or asset.lease_token != lease.token:
            return False
        connection = s.get(OfProviderConnection, lease.account)
        if runtime is None or runtime.binding_id != lease.binding or connection is None or connection.binding_id != lease.binding \
                or connection.owner_id != lease.owner:
            _set(s, asset, "needs_review", error="binding_changed" if runtime is None or runtime.binding_id != lease.binding else "connection_changed")
        else:
            change(s, asset, runtime, connection)
        s.flush()
        if runtime is not None:
            _settle(s, runtime, [asset.asset_id], pending)
    _after(store, tenant, lease.account, pending)
    return True


def process_uploads(store, tenant, *, limit=1, stop=None, transport_for=None):
    """At most limit leased asset steps, one provider interaction each, outside transactions."""
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("content sync uploads require the worker role")
    if type(tenant) is not str or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant):
        raise Refused("invalid content sync tenant")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise Refused("invalid content sync work bound")
    provide = transport_for or no_transport
    done, skip = _sweep(store, tenant), set()
    while done < limit and not (stop is not None and stop.is_set()):
        found = _candidates(store, tenant, utcnow(), skip)
        if not found:
            break
        account, asset_id = found[0]
        skip.add((account, asset_id))
        lease = _lease(store, tenant, account, asset_id, utcnow())
        if lease is None:
            continue
        done += 1
        if lease != 1:
            _step(store, tenant, lease, provide, stop)
    return done


def _sweep(store, tenant):
    """Committed drafts whose assets are all synced but which hold no item yet (retry the DB-only step)."""
    count = 0
    with Session(store.engine) as s:
        wanted = s.execute(select(Draft.account_id, Draft.draft_id).join(OfCreatorBinding, OfCreatorBinding.id == Draft.binding_id)
            .where(OfCreatorBinding.tenant_id == tenant, Draft.state.in_(("verifying", "connection_required")),
                   Draft.item_id.is_(None)).order_by(Draft.updated_at).limit(8)).all()
    for account, draft_id in wanted:
        pending = []
        try:
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                draft = s.scalar(select(Draft).where(Draft.account_id == account, Draft.draft_id == draft_id).with_for_update(skip_locked=True))
                if draft is None or draft.binding_id != runtime.binding_id or draft.state not in {"verifying", "connection_required"}:
                    continue
                before = (draft.state, draft.reason, draft.item_id)
                _settle_draft(s, runtime, draft, pending)
                count += (draft.state, draft.reason, draft.item_id) != before
        except Refused:
            continue
        _after(store, tenant, account, pending)
    return count


def lease_authority(store, tenant, lease):
    """Callable for a provider write boundary: raises Refused unless this exact leased asset, binding and
    connection (owner, revision, unexpired) still hold. Evaluated against the database every time."""
    def authorize():
        now = utcnow()
        with Session(store.engine) as s, s.begin():
            try:
                runtime = store._runtime(s, tenant, lease.account)
            except Refused:
                raise Refused("upload scope changed") from None
            asset = s.get(Asset, (lease.account, lease.asset_id))
            connection = s.get(OfProviderConnection, lease.account)
            if (asset is None or asset.lease_token != lease.token or asset.lease_until is None or asset.lease_until <= now
                    or asset.state not in ACTIVE or asset.binding_id != lease.binding or runtime.binding_id != lease.binding
                    or not _usable(connection, lease.binding, now) or connection.owner_id != lease.owner
                    or connection.revision != lease.revision):
                raise Refused("upload authority lost")
    return authorize


def _step(store, tenant, lease, provide, stop):
    try:
        if getattr(provide, "needs_authority", False):
            transport = provide(lease.connection, tenant, lease.account, lease_authority(store, tenant, lease))
        else:
            transport = provide(lease.connection, tenant, lease.account)
    except AuthRequired:
        transport = AuthRequired
    except Exception:
        transport = TransientUpload
    if transport is None:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "needs_review", error="unpublished_upload_unavailable"))
    if transport is AuthRequired:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "connection_required", error="connection_required", delay=AUTH_PAUSE))
    if transport is TransientUpload:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _retry(s, a, None))
    if lease.provider_id is not None:
        return _verify(store, tenant, lease, transport)
    if lease.started:
        # A prior attempt may have landed (lost ack/crash): reconcile before any new write.
        try:
            found = transport.lookup(upload_token=lease.upload_token)
        except UploadUnknown:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "outcome_unconfirmed", error="outcome_unconfirmed"))
        except AuthRequired:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "connection_required", error="connection_required", delay=AUTH_PAUSE))
        except TransientUpload as exc:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _retry(s, a, exc))
        if found is not None:
            return _record(store, tenant, lease, found)
        if stop is not None and stop.is_set():
            return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "retrying", error="upload_stopped", delay=1))
    return _upload(store, tenant, lease, transport)


def _retry(s, asset, exc):
    if asset.provider_media_id is None and asset.attempts >= MAX_ATTEMPTS:
        return _set(s, asset, "needs_review", error="retries_exhausted")
    _set(s, asset, "retrying", error="provider_retry", delay=_delay(asset.attempts, getattr(exc, "retry_after", None)))


def _record(store, tenant, lease, found):
    if type(found) is not int or not 0 < found < 2**63:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "needs_review", error="invalid_provider_id"))
    def apply(s, asset, runtime, connection):
        try:
            with s.begin_nested():
                asset.provider_media_id = found
                s.flush()
        except IntegrityError:
            asset.provider_media_id = None
            return _set(s, asset, "needs_review", error="provider_id_conflict")
        _set(s, asset, "verifying", now=utcnow())
        asset.upload_started_at = asset.upload_started_at or utcnow()
    return _commit(store, tenant, lease, apply)


def _upload(store, tenant, lease, transport):
    path = None
    try:
        draft_id, file_id = lease.ref.split("/")
        if _staged(draft_id, file_id, lease.size, lease.sha) and _sha(_blob(draft_id, file_id)) == lease.sha:
            path = _blob(draft_id, file_id)
    except (AttributeError, ValueError, OSError, Refused, InvalidSync):
        path = None
    if path is None:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "needs_review", error="staged_file_missing"))
    def begin(s, asset, runtime, connection):
        # Durable before the provider write: the token and moment we may have landed it.
        now = utcnow()
        asset.state, asset.attempts, asset.upload_started_at, asset.last_error, asset.next_attempt_at = "uploading", asset.attempts + 1, now, None, None
        asset.lease_until, asset.updated_at = now + timedelta(seconds=LEASE_SECONDS), now
        _mirror(_op(s, asset), asset, now)
    if not _commit_keep(store, tenant, lease, begin):
        return False
    try:
        found = transport.upload(upload_token=lease.upload_token, path=path, kind=lease.kind)
    except UploadUnknown:
        try:
            found = transport.lookup(upload_token=lease.upload_token)
        except UploadUnknown:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "outcome_unconfirmed", error="outcome_unconfirmed"))
        except AuthRequired:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "connection_required", error="connection_required", delay=AUTH_PAUSE))
        except TransientUpload as exc:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _retry(s, a, exc))
        if found is None:
            return _commit(store, tenant, lease, lambda s, a, rt, c: _retry(s, a, None))
    except AuthRequired:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "connection_required", error="connection_required", delay=AUTH_PAUSE))
    except TransientUpload as exc:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _retry(s, a, exc))
    return _record(store, tenant, lease, found)


def _commit_keep(store, tenant, lease, change):
    """Commit while keeping our lease (same fencing as _commit)."""
    kept = {}
    def wrapped(s, asset, runtime, connection):
        change(s, asset, runtime, connection)
        asset.lease_token, asset.lease_until = lease.token, utcnow() + timedelta(seconds=LEASE_SECONDS)
        _mirror(_op(s, asset), asset, utcnow())
        kept["ok"] = True
    _commit(store, tenant, lease, wrapped)
    return kept.get("ok", False)


def _verify(store, tenant, lease, transport):
    try:
        verdict = transport.verify(provider_media_id=lease.provider_id)
    except AuthRequired:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _set(s, a, "connection_required", error="connection_required", delay=AUTH_PAUSE))
    except (TransientUpload, UploadUnknown) as exc:
        return _commit(store, tenant, lease, lambda s, a, rt, c: _retry(s, a, exc))
    def apply(s, asset, runtime, connection):
        now = utcnow()
        expired = asset.upload_started_at is not None and now - asset.upload_started_at > RESOLVE_WINDOW
        if verdict == "unusable":
            return _set(s, asset, "needs_review", error="media_unusable")
        if verdict != "ready":
            if expired:
                return _set(s, asset, "needs_review", error="processing_timeout")
            return _set(s, asset, "verifying", error="processing", delay=VERIFY_POLL)
        # Provider-ready is not enough: the media must be in this account's own vault observation
        # at the current connection, which also proves ownership by the verified creator.
        row = s.get(VaultItem, (asset.account_id, asset.provider_media_id))
        observation = s.get(OfVaultObservation, (asset.account_id, asset.provider_media_id)) if row else None
        job = s.get(OfVaultSyncJob, (asset.account_id, observation.job_id)) if observation else None
        ok, _ = _ready(row, observation, job, connection, runtime.binding_id) if row else (False, "missing")
        if not ok:
            if expired:
                return _set(s, asset, "needs_review", error="observation_timeout")
            _after_sync.append((connection.revision, lease.actor))
            return _set(s, asset, "verifying", error="awaiting_vault", delay=OBSERVATION_POLL)
        if row.kind != asset.kind:
            return _set(s, asset, "needs_review", error="media_kind_mismatch")
        _set(s, asset, "synced", now=now)
        _discarded.append(asset.staged_ref)
    _after_sync, _discarded = [], []
    done = _commit(store, tenant, lease, apply)
    for revision, actor in _after_sync:
        _after(store, tenant, lease.account, [("sync", actor, revision)])
    for ref in _discarded:
        if ref and not _still_needed(store, lease.account, lease.asset_id):
            _discard(ref)
    return done


def _still_needed(store, account, asset_id):
    with Session(store.engine) as s:
        state = s.scalar(select(Asset.state).where(Asset.account_id == account, Asset.asset_id == asset_id))
    return state != "synced"

