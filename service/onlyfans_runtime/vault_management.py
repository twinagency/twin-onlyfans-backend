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
from sqlalchemy import and_, or_, select, func, case
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

import vault_import_policy as window_policy

PAGE_SIZE = 24
MAX_PAGES = 200
CATALOGUE_BOUND = 2000
# Folder lists are repeated per media item; response bytes remain independently bounded.
MAX_FOLDER_STATES = 1000
# Terminal causes that say nothing about the vault itself; a successor job resumes at the cursor.
RESUMABLE_ERRORS = {"connection_changed", "connection_unavailable", "vault_job_expired", "vault_read_failed"}
RESUME_OVERLAP = 24


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


def _page_size(account):
    """Per-account operational paging, independent of content/sales policy.

    Large accounts repeat substantial folder metadata on every media item.
    Smaller pages retain the wire byte bound and the ordinary import cursor.
    Unconfigured accounts keep the existing page size.
    """
    try:
        sizes = json.loads(os.environ.get('ONLYFANS_VAULT_PAGE_SIZES', '{}'))
        if (type(sizes) is not dict or any(type(k) is not str or not ID_PATTERN.fullmatch(k)
                or type(v) is not int or not 1 <= v <= PAGE_SIZE for k, v in sizes.items())):
            raise ValueError()
        return sizes.get(account, PAGE_SIZE)
    except (ValueError, TypeError):
        raise Refused('invalid vault page size configuration') from None


def parse_vault_page(raw, *, owner_id, page_size=PAGE_SIZE, allow_identical_duplicates=False):
    """Strict supported subset of the existing GET vault/media dialect.

    Missing isReady is not invented as True. Unsupported kinds remain visible
    but cannot become sellable products. A malformed page is applied atomically
    or not at all; no skipped malformed row can silently advance its cursor.
    """
    _id(owner_id)
    if type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE:
        raise NativeProtocolError('invalid vault page size')
    load(raw, max_bytes=MAX_VAULT_BYTES)  # strict JSON with a separate bounded vault-page budget
    try:
        value = json.loads(raw)
        json.dumps(value, allow_nan=False)
        rows, more = value["list"], value["hasMore"]
        if type(rows) is not list or type(more) is not bool or len(rows) > page_size or more and len(rows) != page_size:
            raise ValueError()
        seen = {}
        for row in rows:
            if type(row) is not dict:
                raise ValueError()
            mid = _id(row.get("id"))
            if (mid in seen and (not allow_identical_duplicates or seen[mid]!=row)
                    or row.get("type") not in {"photo","video","audio","gif"}):
                raise ValueError()
            seen[mid]=row
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
            if folders is not None and (type(folders) is not list or len(folders) > MAX_FOLDER_STATES or any(type(f) is not dict for f in folders)):
                raise ValueError()
        return rows, more
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise NativeProtocolError("unsupported vault response") from None


def _connection(session, runtime):
    from .provider_read_budget import require_native_family
    require_native_family(session,runtime.account_id)
    row = session.get(OfProviderConnection, runtime.account_id)
    if (row is None or row.binding_id != runtime.binding_id or row.status != "verified"
        or row.verified_at is None or row.expires_at <= utcnow()):
        raise Refused("verified connection unavailable")
    return row


def _content_connection(session, runtime):
    """Select the configured provider family. No native credential fallback."""
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfFansConnection
    policy=session.get(OfDeliveryPolicy,runtime.account_id)
    if policy is not None and policy.transport_kind=="fansapi":
        row=session.scalar(select(OfFansConnection).where(
            OfFansConnection.account_id==runtime.account_id,
            OfFansConnection.binding_id==runtime.binding_id,OfFansConnection.status=="active"))
        if row is None:raise Refused("verified connection unavailable")
        return row
    return _connection(session,runtime)


def deferred_error(problem):
    """Specific, bounded defer reason: provider_read_deferred:<cause>[:<lease owner lane>]. Never provider text."""
    from .ledger import deferred_reason_code
    code = f"provider_read_deferred:{deferred_reason_code(str(problem))}"
    owner = getattr(problem, "owner", None)
    return (code + ":" + str(owner))[:64] if owner else code


def job_view(row):
    def date(value):
        return value.isoformat() + "Z" if value else None
    window = window_policy.load(getattr(row, "window_json", None))
    view = {"jobId": row.job_id, "commandKey": row.command_key, "status": row.status,
        "pagesRead": row.pages_read, "itemsSeen": row.items_seen, "lastError": row.last_error,
        "requestedAt": date(row.requested_at), "updatedAt": date(row.updated_at), "completedAt": date(row.completed_at),
        # A deferred job reuses lease_until as its retry deadline; surface it so a stalled import is diagnosable.
        "retryAt": date(row.lease_until) if str(row.last_error or "").startswith("provider_read_deferred") else None,
        "pageOffset": row.page_offset}
    if window is not None:
        view["scope"] = ({"selection":"newest_combined",**window["policy"]}
            if window["policy"]["version"]==2 else window_policy.scope())
        view["refreshMode"] = window["mode"]
        if window["policy"]["version"]==2:
            view["selectedCount"]=len(window["selected"])
        else:view["kindOffsets"] = {kind: window[kind]["offset"] for kind in window_policy.CAPS}
    return view


def request_sync(store, tenant, account, *, expected_connection_revision, command_key, actor):
    _identity(command_key, actor)
    if type(expected_connection_revision) is not int or not 1 <= expected_connection_revision < 2**31:
        raise InvalidVault("A current verified connection revision is required.")
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    with Session(store.engine) as session,session.begin():
        store._runtime(session,tenant,account)
        policy=session.get(OfDeliveryPolicy,account)
        if policy is not None and policy.transport_kind=="fansapi":
            from fans_api_adapter.vault_sync import request_sync as request_fans_sync
            return request_fans_sync(store,tenant,account,expected_connection_revision=expected_connection_revision,
                command_key=command_key,actor=actor,session=session)
    request = {"expectedConnectionRevision": expected_connection_revision}
    if window_policy.enabled(): request["windowPolicy"] = window_policy.POLICY
    fingerprint = digest(request)
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
        active=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
            OfVaultSyncJob.status.in_(("pending","running"))).with_for_update())
        now=utcnow()
        if active is not None:
            if (active.provider_kind=="native" and active.binding_id==runtime.binding_id
                    and active.connection_revision==connection.revision and active.owner_id==connection.owner_id):
                raise Refused("vault sync already active")
            _terminal(active,"superseded","connection_changed");s.flush()
        # An interrupted initial scan continues from its committed cursor instead of
        # restarting a multi-hour import from zero. A short overlap re-reads the last
        # page so items that shifted while the job was down are not skipped.
        offset = pages = 0
        window = None
        previous = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account,
            OfVaultSyncJob.binding_id == runtime.binding_id,OfVaultSyncJob.provider_kind=="native").order_by(OfVaultSyncJob.requested_at.desc()).limit(1))
        if window_policy.enabled():
            # Full selected windows run initially and daily. Ordinary refreshes
            # scan newest-first only until a page of already known IDs overlaps.
            full = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account,
                OfVaultSyncJob.binding_id == runtime.binding_id, OfVaultSyncJob.status == "complete",
                OfVaultSyncJob.provider_kind=="native",OfVaultSyncJob.window_json.like('%"mode":"window"%'))
                .order_by(OfVaultSyncJob.requested_at.desc()).limit(1))
            mode = "head" if full is not None and full.completed_at is not None and now - full.completed_at < timedelta(seconds=window_policy.RECONCILE_SECONDS) else "window"
            window = window_policy.start(now, mode)
            if (previous is not None and previous.status in {"failed", "superseded"}
                    and previous.last_error in RESUMABLE_ERRORS and previous.owner_id == connection.owner_id
                    and previous.window_json is not None):
                window = window_policy.load(previous.window_json)
                offset, pages = previous.page_offset, previous.pages_read
        elif (previous is not None and previous.status in {"failed", "superseded"}
                and previous.last_error in RESUMABLE_ERRORS and previous.owner_id == connection.owner_id
                and previous.page_offset > 0):
            offset, pages = max(0, previous.page_offset - RESUME_OVERLAP), previous.pages_read
        row = OfVaultSyncJob(account_id=account, job_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            command_key=command_key, request_hash=fingerprint, actor=actor,
            connection_revision=connection.revision, owner_id=connection.owner_id, max_pages=MAX_PAGES,
            status="pending", page_offset=offset, pages_read=pages, items_seen=offset, requested_at=now, updated_at=now,
            window_json=window_policy.dump(window) if window is not None else None)
        s.add(row)
        s.flush()
        return job_view(row)


def _provenance(row, observation, job, connection, binding, *, current_revision=True):
    provider=getattr(job,"provider_kind","native") if job is not None else None
    if provider=="fansapi":
        from db.onlyfans_engine_models import OfFansConnection
        return (type(connection) is OfFansConnection and observation is not None
            and connection.status=="active" and job.binding_id==connection.binding_id==binding
            and job.connection_revision==connection.epoch and job.provider_account==connection.provider_account_id
            and str(job.owner_id)==connection.provider_creator_id and observation.raw_hash==digest(row.raw_json))
    if provider!="native":return False
    return (observation is not None and job is not None and connection is not None
        and job.binding_id == binding and connection.binding_id == binding
        and job.owner_id == connection.owner_id
        and (not current_revision or job.connection_revision == connection.revision)
        and observation.raw_hash == digest(row.raw_json))


def unusable(raw):
    """Reason the provider's own item metadata forbids offering an asset, else None.

    The one readiness predicate for FansAPI import (vault_sync.classify), curation
    (`_ready`) and every sale (`catalog_terms`). A missing isReady key is not judged
    here (native rows may lack it); `_ready` and classify require isReady true.
    """
    if type(raw) is not dict:
        return "unreadable"
    if raw.get("isDeleted") is True or raw.get("isHidden") is True:
        return "removed"
    if raw.get("isReady") is False or raw.get("isProcessing") is True:
        return "not_ready"
    if raw.get("canView") is False:
        return "cannot_view"
    if raw.get("hasError"):
        return "error"
    return None


def https_url(files, name):
    node = files.get(name) if type(files) is dict else None
    url = node.get("url") if type(node) is dict else None
    return url if type(url) is str and url.startswith("https://") and len(url) <= 8192 else None


def protected_source(raw):
    files = raw.get("files")
    return bool(raw.get("drm") or isinstance(files, dict) and files.get("drm"))


def fansapi_drm(kind, raw):
    """FansAPI video with a DRM marker or no progressive https full URL (files
    missing included). FansAPI never offers it (OQ-3, owner 2026-11-13 13:3xZ: until
    FansAPI is used); native DRM stays offerable. Curation keys "FansAPI" on the
    row's sync job; sale (media_delivery) on the account's current delivery policy."""
    return kind == "video" and (https_url(raw.get("files"), "full") is None or protected_source(raw))


def _ready(row, observation, job, connection, binding):
    """Whether a verified native vault identity may be curated for an offer.

    The native message body contains vault media IDs, never CDN source URLs.
    Native protected videos legitimately have no progressive full-file URL.
    FansAPI imports never offer DRM videos (OQ-3, owner 2026-11-13 13:3xZ: until
    FansAPI is used); native DRM stays offerable. Playback has its own
    source/cache checks in vault_video; it grants no offer rights.
    """
    if row.removed_at is not None:
        return False, "removed"
    if row.kind not in {"photo", "video"}:
        return False, "unsupported_kind"
    # Same binding and owner, unchanged raw item: a credential-only connection
    # revision (model key save, activation) does not invalidate vault identity,
    # matching vault_previews. The connection itself must still be verified.
    fansapi = job is not None and getattr(job,"provider_kind","native")=="fansapi"
    if (not _provenance(row, observation, job, connection, binding, current_revision=False)
        or not fansapi and (connection.status != "verified" or connection.expires_at <= utcnow())):
        return False, "unverified_owner"
    try:
        raw = json.loads(row.raw_json)
    except (ValueError, TypeError):
        return False, "readiness_unknown"
    if type(raw) is not dict:
        return False, "readiness_unknown"
    if raw.get("id") != row.media_id or raw.get("type") != row.kind:
        return False, "unverified_owner"
    reason = unusable(raw)
    if reason == "removed":
        return False, reason
    if "isReady" not in raw:
        return False, "readiness_unknown"
    if reason or raw.get("isReady") is not True:
        return False, "not_ready"
    if fansapi and fansapi_drm(row.kind, raw):
        return False, "drm"
    return True, None


def content_ready(session,runtime,row,connection):
    observation=session.get(OfVaultObservation,(runtime.account_id,row.media_id))
    job=session.get(OfVaultSyncJob,(runtime.account_id,observation.job_id)) if observation else None
    ready,reason=_ready(row,observation,job,connection,runtime.binding_id)
    if ready and job.provider_kind=="fansapi":
        if not session.scalar(select(VaultItem.media_id).where(
                VaultItem.account_id==runtime.account_id,VaultItem.media_id==row.media_id,
                window_policy.commercial_window())):
            return False,"outside_commercial_selection"
    return ready,reason


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
        try:connection = _content_connection(s, runtime)
        except Refused:connection = None
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
            ready, reason = content_ready(s,runtime,row,connection)
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
            "sync": job_view(latest) if latest else None, "connectionRevision": (getattr(connection,"revision",None) or getattr(connection,"epoch",None)) if connection else None,
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



def create_category_drafts(store, tenant, account, *, category, command_key, actor):
    """Agency-priced, atomic category batch using existing disabled catalogue drafts.

    Folder membership is organisation, not selling permission. Agency confirmation,
    verified media and reviewed descriptions remain required; no model/history price
    is read and this command never enables a product or account.
    """
    from db.models import VaultFolder, VaultFolderItem
    _identity(command_key, actor)
    if actor.startswith('worker-import:'):
        raise Refused('agency pricing permission required')
    keys = {'folderId', 'priceCents', 'classificationConfirmed', 'rightsConfirmed', 'reviewedState', 'categoryPriceRevision', 'products'}
    if type(category) is not dict or set(category) != keys:
        raise InvalidVault('Provide one category and its explicit agency price.')
    folder_id = category['folderId']
    if (type(folder_id) is not int or not 0 < folder_id < 2**31
            or category['classificationConfirmed'] is not True or category['rightsConfirmed'] is not True
            or type(category['products']) is not list or not 1 <= len(category['products']) <= 25):
        raise InvalidVault('Confirm the category and select 1-25 product drafts.')
    products = []
    for value in category['products']:
        if type(value) is not dict or set(value) not in ({'label', 'description', 'mediaIds', 'previewMediaIds'},
                {'label', 'description', 'mediaIds', 'previewMediaIds', 'approvedTotalCents'}):
            raise InvalidVault('Each draft needs reviewed text and exact media references.')
        if type(value.get('mediaIds')) is list and len(value['mediaIds']) > 1 and 'approvedTotalCents' not in value:
            raise InvalidVault('A bundle needs an explicitly approved total, separate from the category single price.')
        fields = {key: val for key, val in value.items() if key != 'approvedTotalCents'}
        products.append(validate_product({**fields, 'priceCents': value.get('approvedTotalCents', category['priceCents']), 'enabled': False}))
    if type(category['priceCents']) is not int or not 300 <= category['priceCents'] <= 20000:
        raise InvalidVault('Set an explicit category single price.')
    hashes = [digest(sorted(int(mid) for mid in product['mediaIds'])) for product in products]
    if len(set(hashes)) != len(hashes):
        raise InvalidVault('A product may occur only once in a category batch.')
    fingerprint = digest({'kind': 'agency-category-drafts.v1', 'category': category})
    commands = [digest({'kind': 'agency-category-drafts.v1', 'command': command_key, 'index': i})
                for i in range(len(products))]
    with Session(store.engine) as session, session.begin():
        runtime = store._runtime(session, tenant, account)
        priors = [session.get(OfVaultProductCreate, (account, key)) for key in commands]
        if any(priors):
            if not all(priors) or any((row.actor, row.request_hash) != (actor, fingerprint) for row in priors):
                raise Refused('vault idempotency conflict')
            return {'items': [json.loads(row.acknowledgement_json) for row in priors]}
        folder = session.scalar(select(VaultFolder).where(VaultFolder.id == folder_id,
            VaultFolder.account_id == account, VaultFolder.deleted_at.is_(None)).with_for_update(read=True))
        if folder is None:
            raise Refused('vault folder unavailable')
        from .vault_editing import category_review_state
        if (type(category['categoryPriceRevision']) is not int or
                folder.price_revision!=category['categoryPriceRevision'] or
                folder.pricing_binding_id!=runtime.binding_id or folder.single_price_cents!=category['priceCents']):
            raise Refused("category price changed or is not agency approved")
        all_media = {int(mid) for product in products for mid in product['mediaIds']}
        reviewed = category_review_state(session, account, folder_id, sorted(all_media))
        if category['reviewedState'] != reviewed:
            raise Refused('reviewed category or asset changed')
        connection = _content_connection(session, runtime)
        members = set(session.scalars(select(VaultFolderItem.media_id).where(
            VaultFolderItem.account_id == account, VaultFolderItem.folder_id == folder_id,
            VaultFolderItem.media_id.in_(all_media)).with_for_update(read=True)))
        if members != all_media:
            raise Refused('category membership changed')
        result = []
        for key, product in zip(commands, products):
            media = [int(mid) for mid in product['mediaIds']]
            row = create_catalog_single(session, runtime, connection, media=media,
                previews=[int(mid) for mid in product['previewMediaIds']], label=product['label'],
                description=product['description'], price_cents=product['priceCents'],
                enabled=False, pricing_mode='fixed')
            acknowledgement = {'itemId': str(row.id), 'commandKey': key, 'categoryId': str(folder.id),
                'categoryName': folder.name, 'item': editable(row, 0)}
            session.add(OfVaultProductCreate(account_id=account, command_key=key, item_id=row.id,
                request_hash=fingerprint, media_hash=digest(sorted(media)), actor=actor,
                product_json=canonical({**product, 'agencyCategoryId': str(folder.id),
                    'classificationConfirmed': True, 'rightsConfirmed': True,
                    'reviewedState': {**reviewed, 'assets': {mid: reviewed['assets'][mid] for mid in product['mediaIds']}}}),
                acknowledgement_json=canonical(acknowledgement), created_at=row.created_at))
            result.append(acknowledgement)
        return {'items': result}


def create_catalog_single(s, runtime, connection, *, media, previews, label, description, price_cents,
                          enabled, pricing_mode):
    """Transaction-scoped validated creator shared by manual and import paths.

    The caller owns the runtime lock, command evidence and acknowledgement.
    Bundles may hold several paid photos and/or videos sold for one total;
    free previews (photo or video, as OnlyFans allows) are included assets and at least one stays paid.
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
        if row is None or not content_ready(s,runtime,row,connection)[0]:
            raise Refused("vault media unavailable")
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


def _current(session,runtime,connection,job):
    from .provider_read_budget import native_family_clause
    return (session.scalar(select(native_family_clause(runtime.account_id)))
        and getattr(job,"provider_kind","native")=="native" and connection is not None and connection.binding_id == runtime.binding_id == job.binding_id
        and connection.revision == job.connection_revision and connection.owner_id == job.owner_id
        and connection.status == "verified" and connection.expires_at > utcnow())


def _vault_job_expired(requested_at, last_progress_at, now):
    """Expire a stalled import, while preserving a long import that is advancing."""
    last_activity = max(requested_at, last_progress_at or requested_at)
    return now - last_activity > timedelta(hours=1)


def vault_priority_accounts():
    """ONLYFANS_VAULT_PRIORITY_ACCOUNTS: comma-separated account ids whose pending vault jobs are
    picked before other accounts' (an initial import for a launch creator). Unset = unchanged order.
    Ordering only: every page still passes the same provider read budget and fences."""
    raw = os.environ.get("ONLYFANS_VAULT_PRIORITY_ACCOUNTS", "")
    return tuple(a for a in (x.strip() for x in raw.split(",")) if a and ID_PATTERN.fullmatch(a))[:16]


def _priority_order():
    accounts = vault_priority_accounts()
    if not accounts:
        return case((OfVaultSyncJob.account_id.is_(None), 0), else_=1)  # constant: unchanged order
    return case((OfVaultSyncJob.account_id.in_(accounts), 0), else_=1)


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
    from .provider_read_budget import native_family_clause
    processed = 0
    for _ in range(limit):
        if stop is not None and stop.is_set():
            break
        now = utcnow()
        with Session(store.engine) as s:
            candidate = s.execute(select(OfVaultSyncJob.account_id, OfVaultSyncJob.job_id).join(
                OfCreatorBinding, OfCreatorBinding.id == OfVaultSyncJob.binding_id).where(
                OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
                OfCreatorBinding.channel == "onlyfans", OfVaultSyncJob.provider_kind == "native", native_family_clause(OfVaultSyncJob.account_id), or_(OfVaultSyncJob.status == "pending",
                    and_(OfVaultSyncJob.status == "running", OfVaultSyncJob.lease_until <= now)))
                .order_by(_priority_order(), OfVaultSyncJob.updated_at, OfVaultSyncJob.account_id).limit(1)).first()
        if candidate is None:
            if processed < limit and not (stop is not None and stop.is_set()):
                processed += confirm_removals(store, tenant, stop=stop)
            break
        account, identity = candidate
        page_size = _page_size(account)
        window = window_read = None
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            job = s.get(OfVaultSyncJob, (account, identity))
            if job.status not in {"pending", "running"} or job.status == "running" and job.lease_until > utcnow():
                continue
            connection = s.get(OfProviderConnection, account)
            if not _current(s,runtime,connection,job):
                _terminal(job, "superseded", "connection_changed")
                continue
            if window_policy.enabled() and job.window_json is None:
                # Fence an uncapped active scan; the existing scheduler queues
                # its capped successor. Retain its observations and history.
                _terminal(job, "superseded", "vault_policy_changed")
                continue
            if job.window_json is not None:
                try:
                    window, window_read = window_policy.next_read(window_policy.load(job.window_json), utcnow(), page_size)
                    job.window_json = window_policy.dump(window)
                except (ValueError, TypeError, KeyError):
                    _terminal(job, "failed", "invalid_vault_window")
                    continue
            if utcnow() - job.requested_at > timedelta(hours=1):
                last_progress = s.scalar(select(func.max(OfVaultObservation.observed_at)).where(OfVaultObservation.account_id == account, OfVaultObservation.job_id == identity))
                if _vault_job_expired(job.requested_at, last_progress, utcnow()):
                    _terminal(job, 'failed', 'vault_job_expired')
                    continue
            from .connections import _open
            try:
                clear = _open(connection, tenant)
            except Refused:
                _terminal(job, "failed", "connection_unavailable")
                continue
            token = str(uuid.uuid4())
            job.status, job.lease_token = "running", token
            job.lease_until, job.updated_at = utcnow() + timedelta(seconds=60), utcnow()
            offset, owner, expires, binding = job.page_offset, job.owner_id, connection.expires_at, runtime.binding_id
            connection_revision=connection.revision
        if stop is not None and stop.is_set():
            break
        error = budget = failure = deferred = None
        try:
            from .provider_connector import NativeProviderConnector, NativeReadTransientError, NativeAuthenticationError, session_from_capture
            transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner,
                policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
                expires_at=expires.replace(tzinfo=timezone.utc)))
            from .worker import _provider_claim
            budget=_provider_claim(store,tenant,account,transport,connection_revision,'vault')
            if window_read is None:
                raw = transport.vault_page(offset=offset, limit=page_size)
                rows, more = parse_vault_page(raw, owner_id=owner, page_size=page_size)
            else:
                raw = transport.vault_page(offset=window_read["offset"], limit=window_read["limit"], kind=window_read["kind"])
                rows, more = parse_vault_page(raw, owner_id=owner, page_size=window_read["limit"])
                if any(media["type"] != window_read["kind"] for media in rows):
                    raise NativeProtocolError("vault provider did not honour type filter")
            if os.environ.get("ONLYFANS_VAULT_PREVIEWS_ENABLED") == "1":
                from .vault_previews import warm_page
                warm_page(transport, rows, binding_id=binding, owner_id=owner, stop=stop)
        except Exception as problem:
            from .provider_read_budget import ReadDeferred
            failure=problem
            if isinstance(problem,ReadDeferred):deferred=problem.retry_at;error=deferred_error(problem)
            elif isinstance(problem,NativeAuthenticationError):error='connection_unavailable'
            elif isinstance(problem,NativeReadTransientError):error='vault_read_failed'
            elif isinstance(problem,NativeProtocolError):
                if hasattr(problem,'retry_after'):error='provider_rate_limited'
                elif getattr(problem,'read_failure',{}).get('category')=='response_limit':error='vault_response_too_large'
                else:error='unsupported_vault_response'
            else:error='vault_read_failed'
        try:
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                job = s.get(OfVaultSyncJob, (account, identity))
                if job.status != "running" or job.lease_token != token:
                    continue
                if not _current(s,runtime,s.get(OfProviderConnection,account),job):
                    _terminal(job, "superseded", "connection_changed")
                    continue
                if budget is not None:
                    from .provider_read_budget import current
                    try:current(store,tenant,budget,session=s)
                    except Refused:
                        job.status='pending';job.last_error='provider_read_stale'
                        job.lease_token=job.lease_until=None
                        continue
                if deferred:
                    # Reuse the durable job lease as a bounded retry deadline. No
                    # provider result or cursor advancement is acknowledged.
                    job.status='running';job.last_error=error
                    job.lease_until=deferred;job.updated_at=utcnow()
                    continue
                if failure is not None and hasattr(failure,'retry_after'):
                    job.status='running';job.last_error=error
                    job.lease_until=utcnow()+timedelta(seconds=min(3600,max(1,int(failure.retry_after))))
                    job.updated_at=utcnow()
                    continue
                if error:
                    _terminal(job, "failed", error)
                    continue
                page_hash = hashlib.sha256(raw).hexdigest()
                if window is None and more and page_hash == job.last_page_hash:
                    _terminal(job, "failed", "unsupported_vault_pagination")
                    continue
                if window is not None:
                    known = []
                    for media in rows:
                        observation = s.get(OfVaultObservation, (account, media["id"]))
                        source_job = s.get(OfVaultSyncJob, (account, observation.job_id)) if observation else None
                        known.append(bool(observation and source_job and source_job.binding_id == binding
                            and source_job.owner_id == owner
                            and observation.observed_at < datetime.fromisoformat(window_read["started"])))
                    try:
                        window = window_policy.advance(window, window_read, rows, more, known,
                            digest([media["id"] for media in rows]), utcnow(), page_size)
                    except ValueError:
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
                    if media.get("isDeleted") is True or media.get("isHidden") is True:
                        # The provider itself says so: stop offering it (history is kept; a later
                        # clean sighting lifts the stamp through MIRROR_REFRESH_FIELDS).
                        row.removed_at = utcnow()
                    s.flush()
                    observation = s.get(OfVaultObservation, (account, media["id"]))
                    if observation is None:
                        observation = OfVaultObservation(account_id=account, media_id=media["id"])
                        s.add(observation)
                    observation.job_id, observation.raw_hash, observation.observed_at = identity, digest(row.raw_json), utcnow()
                    # Category assignment belongs to import as well as captioning.
                    # Undescribed assets enter Unsorted until analysis finishes.
                    if os.environ.get("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED") == "1":
                        from vault_auto_folders import assign_sync
                        try:
                            with s.begin_nested():
                                assign_sync(s, account, row.media_id)
                        except ValueError:
                            # A curator's folder limit must not discard imported data.
                            pass
                job.page_offset += len(rows)
                job.items_seen += len(rows)
                job.pages_read += 1
                job.last_page_hash, job.updated_at = page_hash, utcnow()
                job.lease_token = job.lease_until = None
                if window is not None:
                    job.window_json = window_policy.dump(window)
                if (window is None and not more) or (window is not None and window_policy.complete(window)):
                    _terminal(job, "complete")
                else:
                    # The cursor is committed after each bounded page. Crossing
                    # the original 200-page window must not restart at offset 0
                    # or report an incomplete large vault as a terminal failure.
                    # Each worker pass still admits one page per account; stale
                    # connection, inactivity and repeated-page fences remain.
                    job.status = "pending"
                    job.last_error = None
                processed += 1
        finally:
            from .worker import _release_provider
            _release_provider(store,tenant,budget,failure)
    return processed





_RECHECK_AFTER = {}   # (account, media_id) -> monotonic time before which a failed check is not retried
RECHECK_BACKOFF_SECONDS = 300


def _removal_candidate(session, tenant):
    """One asset missing from the latest complete scan of an otherwise idle account.

    Absence from a scan is only a reason to ask the provider about that exact
    asset, never proof of deletion: an offset scan of a changing vault can skip
    items. Nothing is returned while any scan of the account is active.
    """
    import time
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    accounts = session.scalars(select(OfVaultSyncJob.account_id).join(
        OfCreatorBinding, OfCreatorBinding.id == OfVaultSyncJob.binding_id).where(
        OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
        OfCreatorBinding.channel == "onlyfans", OfVaultSyncJob.status == "complete",
        OfVaultSyncJob.provider_kind=="native",
        OfVaultSyncJob.account_id.not_in(select(OfDeliveryPolicy.account_id).where(
            OfDeliveryPolicy.transport_kind=="fansapi"))).distinct()
        .order_by(OfVaultSyncJob.account_id)).all()
    for account in accounts:
        latest = session.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account)
            .order_by(OfVaultSyncJob.requested_at.desc()).limit(1))
        if latest is None or latest.status in {"pending", "running"}:
            continue
        done = session.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account,
            OfVaultSyncJob.status == "complete",OfVaultSyncJob.provider_kind=="native",
            or_(OfVaultSyncJob.window_json.is_(None), OfVaultSyncJob.window_json.like('%"mode":"window"%')))
            .order_by(OfVaultSyncJob.requested_at.desc()).limit(1))
        if done is None: continue
        rows = session.execute(select(VaultItem.media_id).join(OfVaultObservation, and_(
            OfVaultObservation.account_id == VaultItem.account_id, OfVaultObservation.media_id == VaultItem.media_id))
            .where(VaultItem.account_id == account, VaultItem.media_id > 0, VaultItem.removed_at.is_(None), window_policy.automatic_window(),
                OfVaultObservation.observed_at < done.requested_at)
            .order_by(OfVaultObservation.observed_at).limit(20)).all()
        for (media_id,) in rows:
            if _RECHECK_AFTER.get((account, media_id), 0) <= time.monotonic():
                return account, media_id, done.job_id
    return None


def confirm_removals(store, tenant, *, stop=None):
    """Read one asset the latest complete scan did not return, by id. Returns 1 if one was resolved.

    Provider 404 (identity verified first) confirms removal: the row is
    soft-removed and keeps its descriptions, locks, notes and purchase/delivery
    references. A found asset is refreshed and re-observed; a provider
    deleted/hidden flag removes it from sale. Any other outcome changes nothing.
    Uses the shared provider read budget like every vault page.
    """
    import time
    from decimal import Decimal
    with Session(store.engine) as s:
        found = _removal_candidate(s, tenant)
    if found is None:
        return 0
    account, media_id, done_id = found
    from .provider_read_budget import native_family_clause
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        if not s.scalar(select(native_family_clause(account))):return 0
        connection = s.get(OfProviderConnection, account)
        done = s.get(OfVaultSyncJob, (account, done_id))
        if (connection is None or connection.binding_id != runtime.binding_id or done.binding_id != runtime.binding_id
                or connection.owner_id != done.owner_id or connection.status != "verified"
                or connection.expires_at <= utcnow()):
            return 0
        from .connections import _open
        try:
            clear = _open(connection, tenant)
        except Refused:
            return 0
        owner, expires, revision = connection.owner_id, connection.expires_at, connection.revision
    budget = failure = media = None
    resolved = False
    try:
        from .provider_connector import NativeProviderConnector, session_from_capture
        from .worker import _provider_claim
        transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner,
            policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
            expires_at=expires.replace(tzinfo=timezone.utc)))
        budget = _provider_claim(store, tenant, account, transport, revision, "vault")
        media = transport.vault_media_by_id(media_id)
        if media is not None:
            if type(media) is not dict or media.get("id") != media_id:
                raise NativeProtocolError("unsupported vault response")
            parse_vault_page(json.dumps({"list": [media], "hasMore": False},
                default=lambda v: float(v) if isinstance(v, Decimal) else str(v)).encode(), owner_id=owner)
            media = json.loads(json.dumps(media, default=lambda v: float(v) if isinstance(v, Decimal) else str(v)))
        resolved = True
    except Exception as problem:
        failure = problem
        _RECHECK_AFTER[(account, media_id)] = time.monotonic() + RECHECK_BACKOFF_SECONDS
    try:
        if not resolved:
            return 0
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            if not s.scalar(select(native_family_clause(account))):return 0
            connection = s.get(OfProviderConnection, account)
            if (connection is None or connection.binding_id != runtime.binding_id or connection.owner_id != owner
                    or connection.status != "verified"):
                return 0
            from .provider_read_budget import current
            try:
                current(store, tenant, budget, session=s) if budget is not None else None
            except Refused:
                return 0
            row = s.get(VaultItem, (account, media_id))
            if row is None or row.removed_at is not None:
                return 0
            if media is None:
                row.removed_at = utcnow()
                return 1
            values = row_values(account, media)
            for field in MIRROR_REFRESH_FIELDS:
                setattr(row, field, values[field])
            if media.get("isDeleted") is True or media.get("isHidden") is True:
                row.removed_at = utcnow()
            s.flush()
            observation = s.get(OfVaultObservation, (account, media_id))
            observation.job_id, observation.raw_hash, observation.observed_at = done_id, digest(row.raw_json), utcnow()
            return 1
    finally:
        from .worker import _release_provider
        _release_provider(store, tenant, budget, failure)
