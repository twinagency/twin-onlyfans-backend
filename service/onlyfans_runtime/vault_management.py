"""Bounded vault reads and curated native products; no uploads, sends or models.

The existing vault_mirror mapping owns asset fields. Its refresh allowlist
preserves human/AI descriptions, tags, locks, notes and catalogue metadata.
Sync evidence records only references and hashes. Absence from an offset scan
never deletes an asset or proves a full snapshot of a changing provider vault.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import re
import uuid
from urllib.parse import urlsplit

from onlyfans_read.agency_scope import actor_allowed
from sqlalchemy import and_, or_, select, func
from sqlalchemy.orm import Session

from onlyfans_read.contract import ID_PATTERN
from db.models import CatalogItem, CATALOG_IS_SINGLE, OfCreatorBinding, VaultItem
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultObservation, OfVaultProductCreate, OfVaultSyncJob
from vault_mirror import MIRROR_REFRESH_FIELDS, row_values
from vault_ai_effective import effective_value
from .content_management import digest, editable, validate_patch
from .native_context import canonical
from .native_protocol import MAX_VAULT_BYTES, NativeProtocolError, load
from .store import Refused, _id, utcnow
from . import provider_read_gate as read_gate

PAGE_SIZE = 24
MAX_PAGES = 200
CATALOGUE_BOUND = 2000


class InvalidVault(ValueError):
    pass


def _identity(command_key, actor):
    if (type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key)
        or not actor_allowed(actor)):
        raise InvalidVault("The vault command identity is invalid.")


def _stamp(value):
    if type(value) is not str or len(value) > 64:
        raise NativeProtocolError("unsupported vault timestamp")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError()
        return date.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise NativeProtocolError("unsupported vault timestamp") from None


def parse_vault_page(raw, *, owner_id):
    """Strict supported subset of the existing GET vault/media dialect.

    Missing isReady is not invented as True. Unsupported kinds remain visible
    but cannot become sellable products. A malformed page is applied atomically
    or not at all; no skipped malformed row can silently advance its cursor.
    """
    _id(owner_id)
    load(raw, max_bytes=MAX_VAULT_BYTES)  # strict JSON with a separate bounded vault-page budget
    try:
        value = json.loads(raw)
        json.dumps(value, allow_nan=False)
        rows, more = value["list"], value["hasMore"]
        if type(rows) is not list or type(more) is not bool or len(rows) > PAGE_SIZE or more and len(rows) != PAGE_SIZE:
            raise ValueError()
        seen = set()
        for row in rows:
            if type(row) is not dict:
                raise ValueError()
            mid = _id(row.get("id"))
            if mid in seen or row.get("type") not in {"photo", "video", "audio", "gif"}:
                raise ValueError()
            seen.add(mid)
            _stamp(row.get("createdAt"))
            if row.get("updatedAt") is not None:
                _stamp(row["updatedAt"])
            for flag in ["isReady", "isProcessing", "isDeleted", "isHidden", "canView"]:
                if flag in row and type(row[flag]) is not bool:
                    raise ValueError()
            if "userId" in row and _id(row["userId"]) != owner_id:
                raise ValueError()
            if "user" in row and (type(row["user"]) is not dict or _id(row["user"].get("id")) != owner_id):
                raise ValueError()
            for dimension in ["width", "height"]:
                if row.get(dimension) is not None and (type(row[dimension]) is not int or not 1 <= row[dimension] <= 32768):
                    raise ValueError()
            duration = row.get("duration")
            if duration is not None and (type(duration) not in {int, float} or not math.isfinite(duration) or not 0 <= duration <= 86400):
                raise ValueError()
            files = row.get("files")
            if files is not None:
                if type(files) is not dict:
                    raise ValueError()
                for variant in files.values():
                    if variant is None:
                        continue
                    if type(variant) is not dict:
                        raise ValueError()
                    if variant.get("url") is not None:
                        url = variant["url"]
                        if type(url) is not str or len(url) > 8192 or any(ord(c) < 32 for c in url):
                            raise ValueError()
                        parsed = urlsplit(url)
                        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                            raise ValueError()
            folders = row.get("listStates")
            if folders is not None and (type(folders) is not list or len(folders) > 100 or any(type(f) is not dict for f in folders)):
                raise ValueError()
        return rows, more
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise NativeProtocolError("unsupported vault response") from None


def _connection(session, runtime):
    row = session.get(OfProviderConnection, runtime.account_id)
    if (row is None or row.binding_id != runtime.binding_id or row.status != "verified"
        or row.verified_at is None or row.expires_at <= utcnow()):
        raise Refused("verified connection unavailable")
    return row


def job_view(row):
    def date(value):
        return value.isoformat() + "Z" if value else None
    return {"jobId": row.job_id, "commandKey": row.command_key, "status": row.status,
        "pagesRead": row.pages_read, "itemsSeen": row.items_seen, "lastError": row.last_error,
        "requestedAt": date(row.requested_at), "updatedAt": date(row.updated_at), "completedAt": date(row.completed_at)}


def request_sync(store, tenant, account, *, expected_connection_revision, command_key, actor):
    _identity(command_key, actor)
    if type(expected_connection_revision) is not int or not 1 <= expected_connection_revision < 2**31:
        raise InvalidVault("A current verified connection revision is required.")
    fingerprint = digest({"expectedConnectionRevision": expected_connection_revision})
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account, OfVaultSyncJob.command_key == command_key))
        if prior:
            if (prior.binding_id, prior.request_hash, prior.actor) != (runtime.binding_id, fingerprint, actor):
                raise Refused("vault idempotency conflict")
            return job_view(prior)
        connection = _connection(s, runtime)
        if connection.revision != expected_connection_revision:
            raise Refused("vault connection revision conflict")
        if s.scalar(select(OfVaultSyncJob.job_id).where(OfVaultSyncJob.account_id == account,
            OfVaultSyncJob.status.in_(("pending", "running"))).limit(1)):
            raise Refused("vault sync already active")
        now = utcnow()
        row = OfVaultSyncJob(account_id=account, job_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            command_key=command_key, request_hash=fingerprint, actor=actor,
            connection_revision=connection.revision, owner_id=connection.owner_id, max_pages=MAX_PAGES,
            status="pending", page_offset=0, pages_read=0, items_seen=0, requested_at=now, updated_at=now)
        s.add(row)
        s.flush()
        return job_view(row)


def _provenance(row, observation, job, connection, binding, *, current_revision=True):
    return (observation is not None and job is not None and connection is not None
        and job.binding_id == binding and connection.binding_id == binding
        and job.owner_id == connection.owner_id
        and (not current_revision or job.connection_revision == connection.revision)
        and observation.raw_hash == digest(row.raw_json))


def _ready(row, observation, job, connection, binding):
    """Whether a verified native vault identity may be curated for an offer.

    The native message body contains vault media IDs, never CDN source URLs.
    Protected videos legitimately have no progressive full-file URL. Playback
    has its own source/cache checks in vault_video; it grants no offer rights.
    """
    if row.removed_at is not None:
        return False, "removed"
    if row.kind not in {"photo", "video"}:
        return False, "unsupported_kind"
    if (not _provenance(row, observation, job, connection, binding)
        or connection.status != "verified" or connection.expires_at <= utcnow()):
        return False, "unverified_owner"
    try:
        raw = json.loads(row.raw_json)
    except (ValueError, TypeError):
        return False, "readiness_unknown"
    if raw.get("id") != row.media_id or raw.get("type") != row.kind:
        return False, "unverified_owner"
    if raw.get("isDeleted") is True or raw.get("isHidden") is True:
        return False, "removed"
    if "isReady" not in raw:
        return False, "readiness_unknown"
    if raw.get("isReady") is not True or raw.get("isProcessing") is True or raw.get("canView") is False:
        return False, "not_ready"
    return True, None


def _library_summary(session, account, all_assets, filtered_assets):
    from db.models import VaultFolder
    def count(statement):
        return select(func.count()).select_from(
            statement.with_only_columns(VaultItem.media_id).subquery()).scalar_subquery()
    # One SQL snapshot. Neither page cursor nor page size affects library totals.
    total, photos, videos, matching, folders = session.execute(select(
        count(all_assets), count(all_assets.where(VaultItem.kind == "photo")),
        count(all_assets.where(VaultItem.kind == "video")), count(filtered_assets),
        select(func.count()).select_from(VaultFolder).where(
            VaultFolder.account_id == account, VaultFolder.deleted_at.is_(None)).scalar_subquery()
    )).one()
    return {"total": total, "photos": photos, "videos": videos,
            "other": total - photos - videos, "matching": matching, "folders": folders}


def list_vault(store, tenant, account, *, after_media_id=None, limit=24, query="", kind="all", folder_id=None, include_summary=False):
    if type(include_summary) is not bool:
        raise InvalidVault("Use an explicit summary option.")
    if type(limit) is not int or not 1 <= limit <= 48:
        raise InvalidVault("Use a vault page size between 1 and 48.")
    if after_media_id is not None:
        after_media_id = media_reference(after_media_id)
    if (type(query) is not str or len(query) > 120 or any(ord(c) < 32 for c in query)
            or type(kind) is not str or kind not in {"all", "photo", "video"}):
        raise InvalidVault("Use a supported media type and a search of at most 120 characters.")
    search = query.strip().lower()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        connection = s.get(OfProviderConnection, account)
        if connection is not None and connection.binding_id != runtime.binding_id:
            raise Refused("scope unavailable")
        query = select(VaultItem).where(VaultItem.account_id == account, VaultItem.media_id > 0)
        all_assets = query
        if folder_id is not None:
            from db.models import VaultFolderItem
            from .vault_editing import _folder,folder_reference
            fid=folder_reference(folder_id)
            _folder(s,account,fid)
            query=query.where(VaultItem.media_id.in_(select(VaultFolderItem.media_id).where(
                VaultFolderItem.account_id==account,VaultFolderItem.folder_id==fid)))
        if kind != "all":
            query = query.where(VaultItem.kind == kind)
        if search:
            # Search existing metadata owners. Treat LIKE metacharacters as
            # literal user text; no provider request or additional analyzer.
            query = query.where(or_(*(func.lower(func.coalesce(field, "")).contains(search, autoescape=True)
                for field in [VaultItem.search_text, VaultItem.description, VaultItem.video_description,
                              VaultItem.tags, VaultItem.notes, VaultItem.operator_overrides_json])))
        summary = None
        if include_summary:
            summary = _library_summary(s, account, all_assets, query)
        if after_media_id is not None:
            query = query.where(VaultItem.media_id > after_media_id)
        rows = s.scalars(query.order_by(VaultItem.media_id).limit(limit + 1)).all()
        result = []
        for row in rows[:limit]:
            observation = s.get(OfVaultObservation, (account, row.media_id))
            job = s.get(OfVaultSyncJob, (account, observation.job_id)) if observation else None
            ready, reason = _ready(row, observation, job, connection, runtime.binding_id)
            description = effective_value(row, "description")
            if type(description) is not str or len(description.encode()) > 2000:
                description = None
            def bounded(value, low, high):
                return value if type(value) is int and low <= value <= high else None
            kind = row.kind if type(row.kind) is str and 1 <= len(row.kind) <= 32 else "unsupported"
            result.append({"mediaId": str(row.media_id), "kind": kind,
                "durationSeconds": bounded(row.duration_seconds, 0, 86400),
                "width": bounded(row.width, 1, 32768), "height": bounded(row.height, 1, 32768),
                "ready": ready, "reason": reason, "description": description})
        latest = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account,
            OfVaultSyncJob.binding_id == runtime.binding_id).order_by(OfVaultSyncJob.requested_at.desc()).limit(1))
        return {"items": result, "nextCursor": str(rows[limit - 1].media_id) if len(rows) > limit else None,
            "sync": job_view(latest) if latest else None, "connectionRevision": connection.revision if connection else None,
            **({"summary": summary} if include_summary else {})}


def media_reference(value):
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise InvalidVault("Use exact positive decimal vault media references.")
    return int(value)


def validate_product(product):
    if type(product) is not dict or set(product) != {"label", "description", "priceCents", "enabled", "mediaIds", "previewMediaIds"}:
        raise InvalidVault("Only explicit catalogue metadata and vault references are accepted.")
    try:
        normalized = validate_patch({key: product[key] for key in ["label", "description", "priceCents", "enabled"]})
    except ValueError as exc:
        raise InvalidVault(str(exc)) from None
    if not normalized["description"] or not 300 <= normalized["priceCents"] <= 20000:
        raise InvalidVault("Describe the exact content and set a paid price of 300–20000 USD cents.")
    for key, lower, upper in [("mediaIds", 1, 20), ("previewMediaIds", 0, 19)]:
        values = product[key]
        if type(values) is not list or not lower <= len(values) <= upper:
            raise InvalidVault("Select at most 20 vault assets and leave at least one paid asset.")
        identities = [media_reference(value) for value in values]
        if len(set(identities)) != len(identities):
            raise InvalidVault("A vault asset may only appear once in a product.")
        normalized[key] = [str(value) for value in identities]
    media, previews = set(normalized["mediaIds"]), set(normalized["previewMediaIds"])
    if not previews < media:
        raise InvalidVault("Previews must be included media and leave at least one paid asset.")
    return normalized


def create_product(store, tenant, account, *, product, command_key, actor):
    _identity(command_key, actor)
    product = validate_product(product)
    fingerprint = digest(product)
    media = [int(value) for value in product["mediaIds"]]
    previews = [int(value) for value in product["previewMediaIds"]]
    media_hash = digest(sorted(media))
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(OfVaultProductCreate, (account, command_key))
        if prior:
            if (prior.actor, prior.request_hash) != (actor, fingerprint):
                raise Refused("vault idempotency conflict")
            return json.loads(prior.acknowledgement_json)
        connection = _connection(s, runtime)
        row = create_catalog_single(s, runtime, connection, media=media, previews=previews,
            label=product["label"], description=product["description"], price_cents=product["priceCents"],
            enabled=product["enabled"], pricing_mode="dynamic")
        now = row.created_at
        acknowledgement = {"itemId": str(row.id), "commandKey": command_key, "item": editable(row, 0)}
        s.add(OfVaultProductCreate(account_id=account, command_key=command_key, item_id=row.id,
            request_hash=fingerprint, media_hash=media_hash, actor=actor, product_json=canonical(product),
            acknowledgement_json=canonical(acknowledgement), created_at=now))
        return acknowledgement


def create_catalog_single(s, runtime, connection, *, media, previews, label, description, price_cents,
                          enabled, pricing_mode):
    """Transaction-scoped validated creator shared by manual and import paths.

    The caller owns the runtime lock, command evidence and acknowledgement.
    Bundles may hold several paid photos and/or videos sold for one total;
    free previews must be included photos and at least one asset stays paid.
    """
    account = runtime.account_id
    if (pricing_mode not in {"dynamic", "fixed"} or type(price_cents) is not int
            or not 300 <= price_cents <= 20000 or not 1 <= len(media) <= 20
            or len(set(media)) != len(media) or not set(previews) < set(media)):
        raise InvalidVault("Select 1-20 vault assets, leave one paid asset and set a paid price.")
    assets = []
    for mid in media:
        row = s.scalar(select(VaultItem).where(VaultItem.account_id == account, VaultItem.media_id == mid).with_for_update(read=True))
        observation = s.get(OfVaultObservation, (account, mid))
        job = s.get(OfVaultSyncJob, (account, observation.job_id)) if observation else None
        if row is None or not _ready(row, observation, job, connection, runtime.binding_id)[0]:
            raise Refused("vault media unavailable")
        if mid in previews and row.kind != "photo":
            raise InvalidVault("Only included photos may be free preview assets.")
        assets.append(row)
    paid = [row for row in assets if row.media_id not in previews]
    videos = [row for row in paid if row.kind == "video"]
    # Existing legacy/native products remain canonical. A new command must
    # not create a duplicate shelf entry for the same assets, in any order.
    existing = s.scalars(select(CatalogItem.media_ids).where(CatalogItem.account_id == account,
        CATALOG_IS_SINGLE).limit(CATALOGUE_BOUND + 1)).all()
    if len(existing) > CATALOGUE_BOUND:
        raise Refused("catalogue creation bound exceeded")
    for raw in existing:
        try:
            ids = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if type(ids) is list and all(type(mid) is int for mid in ids) and sorted(ids) == sorted(media):
            raise Refused("catalogue assets already present")
    now = utcnow()
    # One video keeps its duration; a multi-video bundle has no single duration.
    row = CatalogItem(account_id=account, kind="video" if videos else "image" if len(media) == 1 else "image_set",
        label=label, description_for_ai=description, media_ids=canonical(media),
        preview_media_ids=canonical(previews), duration_sec=videos[0].duration_seconds if len(videos) == 1 else None,
        price_cents=price_cents, enabled=enabled, is_free_teaser=False, pricing_mode=pricing_mode,
        created_at=now, updated_at=now)
    s.add(row)
    s.flush()
    return row


def _terminal(row, status, error=None):
    row.status, row.last_error = status, error
    row.updated_at = row.completed_at = utcnow()
    row.lease_token = row.lease_until = None


def _current(runtime, connection, job):
    return (connection is not None and connection.binding_id == runtime.binding_id == job.binding_id
        and connection.revision == job.connection_revision and connection.owner_id == job.owner_id
        and connection.status == "verified" and connection.expires_at > utcnow())


def process_vault_jobs(store, tenant, *, limit=1, stop=None):
    """At most limit GET pages, including identity checks, outside transactions.

    One page is checkpointed atomically. A crash after HTTP simply rereads the
    same safe GET after its lease; competing processes cannot advance its cursor
    twice. New/expired/replaced credentials never inherit an old job's input.
    """
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("vault sync requires the worker role")
    if type(tenant) is not str or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant):
        raise Refused("invalid vault worker tenant")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise Refused("invalid vault work bound")
    if stop is not None and stop.is_set():
        return 0
    processed = 0
    for _ in range(limit):
        if stop is not None and stop.is_set():
            break
        now = utcnow()
        with Session(store.engine) as s:
            candidate = s.execute(select(OfVaultSyncJob.account_id, OfVaultSyncJob.job_id).join(
                OfCreatorBinding, OfCreatorBinding.id == OfVaultSyncJob.binding_id).where(
                OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
                OfCreatorBinding.channel == "onlyfans", or_(OfVaultSyncJob.status == "pending",
                    and_(OfVaultSyncJob.status == "running", OfVaultSyncJob.lease_until <= now)),
                ~read_gate.blocked(OfVaultSyncJob.account_id, now))
                .order_by(OfVaultSyncJob.updated_at, OfVaultSyncJob.account_id).limit(1)).first()
        if candidate is None:
            break
        account, identity = candidate
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            job = s.get(OfVaultSyncJob, (account, identity))
            if job.status not in {"pending", "running"} or job.status == "running" and job.lease_until > utcnow():
                continue
            connection = s.get(OfProviderConnection, account)
            if not _current(runtime, connection, job):
                _terminal(job, "superseded", "connection_changed")
                continue
            if utcnow() - job.requested_at > timedelta(hours=1):
                _terminal(job, "failed", "vault_job_expired")
                continue
            from .connections import _open
            try:
                clear = _open(connection, tenant)
            except Refused:
                _terminal(job, "failed", "connection_unavailable")
                continue
            reading = read_gate.acquire_in(s, account, "vault", now=now)
            if reading is None:
                continue
            token = str(uuid.uuid4())
            job.status, job.lease_token = "running", token
            job.lease_until, job.updated_at = utcnow() + timedelta(seconds=60), utcnow()
            offset, owner, expires, binding = job.page_offset, job.owner_id, connection.expires_at, runtime.binding_id
        if stop is not None and stop.is_set():
            read_gate.release(store, account, reading)
            break
        error = None
        try:
            from .provider_connector import NativeProviderConnector, session_from_capture
            transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner,
                policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
                expires_at=expires.replace(tzinfo=timezone.utc)))
            raw = transport.vault_page(offset=offset, limit=PAGE_SIZE)
            rows, more = parse_vault_page(raw, owner_id=owner)
            if os.environ.get("ONLYFANS_VAULT_PREVIEWS_ENABLED") == "1":
                from .vault_previews import warm_page
                warm_page(transport, rows, binding_id=binding, owner_id=owner, stop=stop)
        except NativeProtocolError:
            error = "unsupported_vault_response"
        except Exception:
            error = "vault_read_failed"
        finally:
            read_gate.release(store, account, reading)
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            job = s.get(OfVaultSyncJob, (account, identity))
            if job.status != "running" or job.lease_token != token:
                continue
            if not _current(runtime, s.get(OfProviderConnection, account), job):
                _terminal(job, "superseded", "connection_changed")
                continue
            if error:
                _terminal(job, "failed", error)
                continue
            page_hash = hashlib.sha256(raw).hexdigest()
            if more and page_hash == job.last_page_hash:
                _terminal(job, "failed", "unsupported_vault_pagination")
                continue
            for media in rows:
                values = row_values(account, media)
                values["created_at"] = _stamp(media["createdAt"]).replace(tzinfo=None)
                row = s.get(VaultItem, (account, media["id"]))
                if row is None:
                    row = VaultItem(**values)
                    s.add(row)
                else:
                    for field in MIRROR_REFRESH_FIELDS:
                        setattr(row, field, values[field])
                s.flush()
                observation = s.get(OfVaultObservation, (account, media["id"]))
                if observation is None:
                    observation = OfVaultObservation(account_id=account, media_id=media["id"])
                    s.add(observation)
                observation.job_id, observation.raw_hash, observation.observed_at = identity, digest(row.raw_json), utcnow()
            job.page_offset += len(rows)
            job.items_seen += len(rows)
            job.pages_read += 1
            job.last_page_hash, job.updated_at = page_hash, utcnow()
            job.lease_token = job.lease_until = None
            if not more:
                _terminal(job, "complete")
            elif job.pages_read >= job.max_pages:
                _terminal(job, "failed", "vault_page_limit")
            else:
                job.status = "pending"
            processed += 1
    return processed

