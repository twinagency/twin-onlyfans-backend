"""Reviewed import of existing paid offers into disabled fixed-price drafts.

Inventory reads only messages already imported into this database; it needs no
provider session and never sends, plans or calls a model. Candidates group by
unordered attachments for navigation only. A draft is created solely from one
exact OfOfferObservation that a manager reviewed, at that observation's price,
order and previews. Nothing here enables an item or changes any account mode.
"""
from datetime import datetime, timedelta
import json
import os
import re
import uuid

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session

from onlyfans_read.agency_scope import actor_allowed
from onlyfans_read.contract import ID_PATTERN
from db.models import CatalogItem, CATALOG_IS_SINGLE, ContentOffer, Message, OfCreatorBinding, VaultItem
from db.onlyfans_offer_import_models import (OfOfferImportCandidate, OfOfferImportCursor,
    OfOfferImportDecision, OfOfferImportJob, OfOfferObservation)
from .content_management import digest, editable, fingerprint as item_fingerprint, validate_patch
from .native_context import canonical
from .offer_terms import MAX_ATTACHMENTS, MAX_PRICE_CENTS, MIN_PRICE_CENTS, media_key
from .store import Refused, _id, utcnow

BATCH_ROWS = 1000
INVENTORY_BATCHES = 1000
REFRESH_PAGES_PER_CHAT = 200
MAX_REFRESH_SOURCES = 50
MAX_DRAFTS = 20
MAX_VARIANTS = 24
MAX_SAMPLES = 10
LEASE_SECONDS = 60


class InvalidImport(ValueError):
    pass


def _identity(command_key, actor):
    if (type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key)
            or not actor_allowed(actor)):
        raise InvalidImport("The import command identity is invalid.")


def _date(value):
    return value.isoformat() + "Z" if value else None


def _ids(raw):
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return None
    if type(value) is not list or any(type(v) is not int or not 0 < v < 2**63 for v in value) or len(set(value)) != len(value):
        return None
    return value


def job_view(row):
    return {"jobId": row.job_id, "commandKey": row.command_key, "mode": row.mode, "status": row.status,
        "revision": row.revision, "rowsScanned": row.rows_scanned, "pagesRead": row.pages_read,
        "pageBudget": row.page_budget, "targetsTotal": row.targets_total, "targetsDone": row.targets_done,
        "lastError": row.last_error, "requestedAt": _date(row.requested_at), "updatedAt": _date(row.updated_at),
        "completedAt": _date(row.completed_at),
        # Honest coverage: this is the imported history in our database, not
        # proof that every OnlyFans conversation has been synchronized.
        "coverage": "imported_history" if row.mode == "inventory" else "selected_sources"}


def _connection(s, runtime):
    from .vault_management import _connection as verified
    return verified(s, runtime)


def request_job(store, tenant, account, *, mode, sources=None, command_key, actor):
    """Durably queue inventory or a bounded refresh. Retrying returns the same job."""
    _identity(command_key, actor)
    if mode not in {"inventory", "refresh"}:
        raise InvalidImport("Use an inventory or refresh job.")
    if mode == "inventory":
        if sources is not None:
            raise InvalidImport("Inventory does not take selected sources.")
        targets = []
    else:
        if type(sources) is not list or not 1 <= len(sources) <= MAX_REFRESH_SOURCES:
            raise InvalidImport(f"Select between 1 and {MAX_REFRESH_SOURCES} source offers to refresh.")
        targets = []
        for source in sources:
            if type(source) is not dict or set(source) != {"fanId", "messageId"}:
                raise InvalidImport("Each source needs an exact fan and message reference.")
            targets.append((_reference(source["fanId"]), _reference(source["messageId"])))
        if len(set(targets)) != len(targets):
            raise InvalidImport("A source may only be selected once.")
        targets.sort()
    request_hash = digest({"mode": mode, "sources": [[f, m] for f, m in targets]})
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.account_id == account,
            OfOfferImportJob.command_key == command_key))
        if prior:
            if (prior.binding_id, prior.request_hash, prior.actor) != (runtime.binding_id, request_hash, actor):
                raise Refused("import idempotency conflict")
            return job_view(prior)
        if s.scalar(select(OfOfferImportJob.job_id).where(OfOfferImportJob.account_id == account,
                OfOfferImportJob.mode == mode, OfOfferImportJob.status.in_(("pending", "running"))).limit(1)):
            raise Refused("import job already active")
        now = utcnow()
        if mode == "inventory":
            row = create_inventory_job(s, runtime, command_key=command_key, request_hash=request_hash, actor=actor, now=now)
            return job_view(row)
        row = OfOfferImportJob(account_id=account, job_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            command_key=command_key, request_hash=request_hash, actor=actor, mode=mode, status="pending",
            revision=1, rows_scanned=0, pages_read=0, targets_total=len(targets), targets_done=0,
            requested_at=now, updated_at=now)
        connection = _connection(s, runtime)
        # Targets must be existing outgoing priced sources of this account.
        found = set(s.execute(select(Message.fan_id, Message.message_id).where(
            Message.account_id == account, Message.direction == "out", Message.price_cents > 0,
            tuple_(Message.fan_id, Message.message_id).in_(targets))).all())
        if found != set(targets):
            raise Refused("import source unavailable")
        row.connection_revision = connection.revision
        chats = sorted({fan for fan, _ in targets})
        row.page_budget = REFRESH_PAGES_PER_CHAT * len(chats)
        s.add(row)
        s.flush()
        for fan in chats:
            wanted = sorted((m for f, m in targets if f == fan), reverse=True)
            s.add(OfOfferImportCursor(account_id=account, job_id=row.job_id, fan_id=fan,
                targets_json=canonical(wanted), found_json="[]", pages_read=0, attempts=0,
                status="pending", updated_at=now))
        return job_view(row)


def create_inventory_job(s, runtime, *, command_key, request_hash, actor, now):
    """Pending inventory job over imported history, with a captured upper boundary.

    The caller holds the runtime lock and has checked there is no active job.
    """
    account = runtime.account_id
    row = OfOfferImportJob(account_id=account, job_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
        command_key=command_key, request_hash=request_hash, actor=actor, mode="inventory", status="pending",
        revision=1, rows_scanned=0, pages_read=0, targets_total=0, targets_done=0, requested_at=now, updated_at=now,
        upper_message_id=s.scalar(select(func.max(Message.message_id)).where(
            Message.account_id == account, Message.direction == "out", Message.price_cents > 0)) or 0,
        page_budget=INVENTORY_BATCHES)
    s.add(row)
    s.flush()
    return row


def _reference(value):
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise InvalidImport("Use exact positive decimal references.")
    return int(value)


def cancel_job(store, tenant, account, job_id, *, expected_revision, actor):
    """Durable cancel; approved catalogue decisions are never undone by it."""
    if not actor_allowed(actor) or type(expected_revision) is not int or expected_revision < 1:
        raise InvalidImport("The cancel request is invalid.")
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
        row = s.get(OfOfferImportJob, (account, _uuid(job_id)), with_for_update=True)
        if row is None:
            raise Refused("import job unavailable")
        if row.status == "cancelled":
            return job_view(row)
        if row.revision != expected_revision:
            raise Refused("import job revision conflict")
        if row.status not in {"pending", "running"}:
            raise Refused("import job already finished")
        _terminal(row, "cancelled")
        return job_view(row)


def _uuid(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        raise InvalidImport("That import reference is invalid.")
    return value


def _terminal(row, status, error=None):
    row.status, row.last_error = status, error
    row.lease_token = row.lease_until = None
    row.updated_at = row.completed_at = utcnow()
    row.revision += 1


def get_job(store, tenant, account, job_id):
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
        row = s.get(OfOfferImportJob, (account, _uuid(job_id)))
        if row is None:
            raise Refused("import job unavailable")
        return job_view(row)


def _merge(candidate, media, price, source):
    """Fold one source into a candidate; order and price variants stay visible."""
    variants = json.loads(candidate.variants_json)
    key = f"{price}:{','.join(map(str, media))}"
    match = next((v for v in variants if v["key"] == key), None)
    if match is None and len(variants) >= MAX_VARIANTS:
        # Bounded storage; the overflow bucket is shown, never silently dropped.
        key = "other"
        match = next((v for v in variants if v["key"] == key), None)
    if match is None:
        exact = key != "other"
        match = {"key": key, "priceCents": price if exact else None,
                 "mediaIds": [str(m) for m in media] if exact else None, "count": 0, "latest": None}
        variants.append(match)
    match["count"] += 1
    if match["latest"] is None or source["messageId"] > match["latest"]["messageId"]:
        match["latest"] = source
    candidate.variants_json = canonical(variants)
    samples = json.loads(candidate.samples_json)
    if len(samples) < MAX_SAMPLES:
        samples.append(source)
        candidate.samples_json = canonical(samples)
    candidate.source_count += 1
    candidate.revision += 1


def inventory_batch(store, tenant, account, job_id, *, token=None):
    """One committed keyset batch over imported outgoing priced messages."""
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        job = s.get(OfOfferImportJob, (account, job_id), with_for_update=True)
        if job is None or job.mode != "inventory" or job.status not in {"pending", "running"}:
            return None
        if job.binding_id != runtime.binding_id:
            _terminal(job, "superseded", "binding_changed")
            return None
        if token is not None and job.lease_token != token:
            return None
        cursor = (job.after_fan_id or 0, job.after_message_id or 0)
        rows = s.execute(select(Message.fan_id, Message.message_id, Message.media_ids, Message.price_cents,
                Message.created_at, Message.is_unsent).where(Message.account_id == account, Message.direction == "out",
                Message.price_cents > 0, Message.message_id <= job.upper_message_id,
                tuple_(Message.fan_id, Message.message_id) > cursor)
            .order_by(Message.fan_id, Message.message_id).limit(BATCH_ROWS)).all()
        now = utcnow()
        from .content_sync import echo_map
        from db.onlyfans_content_sync_progress_models import OfContentSyncSource
        ids = [mid for _, mid, _, _, _, _ in rows]
        echoes = echo_map(s, account, runtime.binding_id, ids)
        receipts = {r.source_id: r for r in s.scalars(select(OfContentSyncSource).where(
            OfContentSyncSource.account_id == account, OfContentSyncSource.binding_id == runtime.binding_id,
            OfContentSyncSource.source_kind == "message", OfContentSyncSource.source_id.in_(ids)).with_for_update())} if ids else {}
        parsed = [(fan, mid, raw, price, created, unsent, _ids(raw)) for fan, mid, raw, price, created, unsent in rows]
        mkeys = {media_key(media) for _, mid, _, _, _, unsent, media in parsed if media and not unsent and mid not in echoes}
        candidates = {r.media_key: r for r in s.scalars(select(OfOfferImportCandidate).where(
            OfOfferImportCandidate.account_id == account, OfOfferImportCandidate.job_id == job_id,
            OfOfferImportCandidate.media_key.in_(mkeys)).with_for_update())} if mkeys else {}
        # One receipt query and one candidate query per bounded batch, rather
        # than one round trip/autoflush per source at historical scale.
        with s.no_autoflush:
            for fan, mid, raw, price, created, unsent, media in parsed:
                receipt = receipts.get(mid)
                if receipt is None:
                    receipt = OfContentSyncSource(account_id=account, binding_id=runtime.binding_id,
                        source_kind="message", source_id=mid, fan_id=fan, occurrence=0)
                    s.add(receipt)
                    receipts[mid] = receipt
                elif receipt.fan_id != fan:
                    raise Refused("source fan changed")
                receipt.folded_message_media_json, receipt.folded_message_price_cents = raw, price
                receipt.folded_message_unsent, receipt.folded_at = unsent, now
                receipt.fold_pending = False
                if not media or mid in echoes or unsent:
                    if (unsent or not media) and receipt.current_observation_id is not None:
                        from .content_sync import mark_source_unavailable
                        mark_source_unavailable(s, runtime, receipt, now,
                            "source_retracted" if unsent else "source_terms_unusable")
                    continue
                mkey = media_key(media)
                if receipt.candidate_job_id == job_id and receipt.candidate_media_key == mkey:
                    continue
                candidate = candidates.get(mkey)
                if candidate is None:
                    candidate = OfOfferImportCandidate(account_id=account, job_id=job_id, media_key=mkey,
                        media_json=canonical(sorted(media)), source_count=0, variants_json="[]", samples_json="[]",
                        revision=0, updated_at=now)
                    s.add(candidate)
                    candidates[mkey] = candidate
                _merge(candidate, media, price, {"fanId": str(fan), "messageId": mid, "sourceKind": "message",
                    "priceCents": price, "sentAt": _date(created)})
                receipt.candidate_job_id, receipt.candidate_media_key = job_id, mkey
                candidate.updated_at = now
        job.rows_scanned += len(rows)
        job.pages_read += 1
        job.updated_at = now
        job.revision += 1
        if rows:
            job.after_fan_id, job.after_message_id = rows[-1][0], rows[-1][1]
        if len(rows) < BATCH_ROWS:
            _terminal(job, "complete")
        elif job.pages_read >= job.page_budget:
            _terminal(job, "partial", "inventory_budget")
        elif token is not None:
            job.status, job.lease_token, job.lease_until = "pending", None, None
        return len(rows)


def process_inventory_jobs(store, tenant, *, limit=1, stop=None):
    """Worker step: at most `limit` batches; a crash simply rereads the batch."""
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("offer import requires the worker role")
    if type(tenant) is not str or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant):
        raise Refused("invalid import worker tenant")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise Refused("invalid import work bound")
    processed = 0
    for _ in range(limit):
        if stop is not None and stop.is_set():
            break
        now = utcnow()
        with Session(store.engine) as s, s.begin():
            job = s.scalar(select(OfOfferImportJob).join(OfCreatorBinding, OfCreatorBinding.id == OfOfferImportJob.binding_id)
                .where(OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
                    OfOfferImportJob.mode == "inventory", or_(OfOfferImportJob.status == "pending",
                        and_(OfOfferImportJob.status == "running", OfOfferImportJob.lease_until <= now)))
                .order_by(OfOfferImportJob.updated_at).limit(1).with_for_update(skip_locked=True, of=OfOfferImportJob))
            if job is None:
                break
            token = str(uuid.uuid4())
            job.status, job.lease_token, job.lease_until = "running", token, now + timedelta(seconds=LEASE_SECONDS)
            account, job_id = job.account_id, job.job_id
        try:
            inventory_batch(store, tenant, account, job_id, token=token)
        except Refused:
            with Session(store.engine) as s, s.begin():
                job = s.get(OfOfferImportJob, (account, job_id), with_for_update=True)
                if job is not None and job.lease_token == token:
                    _terminal(job, "superseded", "scope_unavailable")
        processed += 1
    return processed


# ── Review views ────────────────────────────────────────────────────────

def _asset_state(s, account, runtime, connection, media):
    from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
    from .vault_management import _ready
    kinds, reasons = {}, set()
    for mid in media:
        row = s.get(VaultItem, (account, mid))
        if row is None:
            reasons.add("asset_missing")
            continue
        observation = s.get(OfVaultObservation, (account, mid))
        job = s.get(OfVaultSyncJob, (account, observation.job_id)) if observation else None
        ready, why = _ready(row, observation, job, connection, runtime.binding_id) if connection else (False, "unverified_owner")
        if not ready:
            reasons.add("asset_" + why)
        kinds[mid] = row.kind
    return kinds, reasons


def observation_view(row, kinds=None, asset_reasons=(), echo_item=None):
    media = _ids(row.media_json) or []
    previews = None if row.preview_json is None else _ids(row.preview_json)
    reasons = set(json.loads(row.reasons_json)) | set(asset_reasons)
    if kinds is not None and previews:
        if any(kinds.get(mid) == "video" for mid in previews):
            reasons.add("video_preview")
    return {"observationId": row.observation_id, "fingerprint": row.fingerprint,
        "sourceKind": row.source_kind, "sourceId": str(row.source_id),
        "fanId": None if row.fan_id is None else str(row.fan_id), "sourceContext": row.source_context,
        "sourceAt": _date(row.source_at), "observedAt": _date(row.observed_at),
        "mediaIds": [str(m) for m in media],
        "previewIds": None if previews is None else [str(m) for m in previews],
        "lockedText": row.locked_text, "priceCents": row.price_cents, "currency": row.currency,
        "attachments": [{"mediaId": str(m), "kind": (kinds or {}).get(m), "freePreview": previews is not None and m in previews} for m in media],
        "reasons": sorted(reasons), "ready": not reasons,
        # Only present for an observed copy of a message this CRM sent itself.
        **({} if echo_item is None else {"crmOriginated": True, "itemId": str(echo_item)})}


def _existing(s, account, media):
    """The catalogue single already holding exactly these attachments, if any."""
    for row in s.scalars(select(CatalogItem).where(CatalogItem.account_id == account, CATALOG_IS_SINGLE)
                         .order_by(CatalogItem.id).limit(2001)):
        ids = _ids(row.media_ids)
        if ids is not None and sorted(ids) == sorted(media):
            return row
    return None


def list_candidates(store, tenant, account, job_id, *, after=None, limit=25):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise InvalidImport("Use a page size between 1 and 50.")
    if after is not None and (type(after) is not str or not re.fullmatch(r"[a-f0-9]{64}", after)):
        raise InvalidImport("That candidate cursor is invalid.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        job = s.get(OfOfferImportJob, (account, _uuid(job_id)))
        if job is None or job.mode != "inventory":
            raise Refused("import job unavailable")
        query = select(OfOfferImportCandidate).where(OfOfferImportCandidate.account_id == account,
            OfOfferImportCandidate.job_id == job.job_id)
        if after:
            query = query.where(OfOfferImportCandidate.media_key > after)
        rows = s.scalars(query.order_by(OfOfferImportCandidate.media_key).limit(limit + 1)).all()
        return {"job": job_view(job), "items": [_candidate_summary(s, account, runtime, row) for row in rows[:limit]],
            "nextCursor": rows[limit - 1].media_key if len(rows) > limit else None}


def _candidate_summary(s, account, runtime, row):
    variants = json.loads(row.variants_json)
    prices = sorted({v["priceCents"] for v in variants if v["priceCents"] is not None})
    # Edits are immutable observations of an existing source, not another
    # source to count. Their exact prices must still remain visible in review.
    prices = sorted(set(prices) | set(s.scalars(select(OfOfferObservation.price_cents).where(
        OfOfferObservation.account_id == account, OfOfferObservation.binding_id == runtime.binding_id,
        OfOfferObservation.media_key == row.media_key))))
    verified = s.scalar(select(func.count()).select_from(OfOfferObservation).where(
        OfOfferObservation.account_id == account, OfOfferObservation.binding_id == runtime.binding_id,
        OfOfferObservation.media_key == row.media_key))
    decision = s.scalar(select(OfOfferImportDecision).where(OfOfferImportDecision.account_id == account,
        OfOfferImportDecision.binding_id == runtime.binding_id, OfOfferImportDecision.media_key == row.media_key,
        OfOfferImportDecision.action == "create"))
    media = _ids(row.media_json) or []
    if decision is not None:
        status = "draft_created"
    elif len(media) > MAX_ATTACHMENTS:
        status = "blocked"
    elif not verified:
        status = "source_details_needed"
    else:
        status = "ready_for_review"
    return {"candidateId": row.media_key, "revision": row.revision, "attachmentCount": len(media),
        "sourceCount": row.source_count, "observedPrices": prices, "variantCount": len(variants),
        "verifiedSources": verified, "status": status,
        "itemId": str(decision.item_id) if decision else None}


def get_candidate(store, tenant, account, job_id, candidate_id):
    if type(candidate_id) is not str or not re.fullmatch(r"[a-f0-9]{64}", candidate_id):
        raise InvalidImport("That candidate reference is invalid.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row = s.get(OfOfferImportCandidate, (account, _uuid(job_id), candidate_id))
        if row is None:
            raise Refused("import candidate unavailable")
        from db.onlyfans_connection_models import OfProviderConnection
        connection = s.get(OfProviderConnection, account)
        if connection is not None and (connection.binding_id != runtime.binding_id or connection.status != "verified"):
            connection = None
        media = _ids(row.media_json) or []
        kinds, asset_reasons = _asset_state(s, account, runtime, connection, media)
        observations = s.scalars(select(OfOfferObservation).where(OfOfferObservation.account_id == account,
            OfOfferObservation.binding_id == runtime.binding_id, OfOfferObservation.media_key == candidate_id)
            .order_by(OfOfferObservation.source_at.desc(), OfOfferObservation.source_id.desc()).limit(50)).all()
        from db.onlyfans_content_sync_progress_models import OfContentSyncSource
        current = {(source.source_kind, source.source_id): source for source in s.scalars(
            select(OfContentSyncSource).where(OfContentSyncSource.account_id == account,
                OfContentSyncSource.binding_id == runtime.binding_id,
                tuple_(OfContentSyncSource.source_kind, OfContentSyncSource.source_id).in_(
                    [(o.source_kind, o.source_id) for o in observations])))} if observations else {}
        existing = _existing(s, account, media)
        from .content_sync import echo_map
        echoes = echo_map(s, account, runtime.binding_id, [o.source_id for o in observations if o.source_kind == "message"])
        decision = s.scalar(select(OfOfferImportDecision).where(OfOfferImportDecision.account_id == account,
            OfOfferImportDecision.binding_id == runtime.binding_id, OfOfferImportDecision.media_key == candidate_id,
            OfOfferImportDecision.action == "create"))
        return {**_candidate_summary(s, account, runtime, row),
            "variants": json.loads(row.variants_json), "samples": json.loads(row.samples_json),
            "observations": [observation_view(o, kinds, set(asset_reasons) | (
                {"superseded_source"} if (o.source_kind, o.source_id) in current and (
                    current[(o.source_kind, o.source_id)].current_fingerprint != o.fingerprint
                    or current[(o.source_kind, o.source_id)].source_unavailable) else set()),
                echoes.get(o.source_id)) for o in observations],
            "assetReasons": sorted(asset_reasons),
            "existingItem": None if existing is None else {"itemId": str(existing.id), "priceCents": existing.price_cents,
                "enabled": existing.enabled, "pricingMode": existing.pricing_mode,
                "importedBy": decision.command_key if decision and decision.item_id == existing.id else None}}


# ── Draft creation ──────────────────────────────────────────────────────

def validate_draft(value):
    keys = {"commandKey", "observationId", "expectedFingerprint", "label", "description",
            "reuseConfirmed", "descriptionApproved"}
    if type(value) is not dict or set(value) != keys:
        raise InvalidImport("Each draft names one exact source, its fingerprint and the reviewed text.")
    if type(value["commandKey"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["commandKey"]):
        raise InvalidImport("The draft command identity is invalid.")
    _uuid(value["observationId"])
    if type(value["expectedFingerprint"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["expectedFingerprint"]):
        raise InvalidImport("The source fingerprint is invalid.")
    if value["reuseConfirmed"] is not True or value["descriptionApproved"] is not True:
        raise InvalidImport("Confirm the content is reusable and approve its factual description.")
    try:
        text = validate_patch({"label": value["label"], "description": value["description"]})
    except ValueError as exc:
        raise InvalidImport(str(exc)) from None
    if not text["description"]:
        raise InvalidImport("Describe exactly what the fan unlocks.")
    return {**value, **text}


def create_drafts(store, tenant, account, *, drafts, actor):
    """Bounded batch with an independent, idempotent outcome per item."""
    if not actor_allowed(actor):
        raise InvalidImport("The import command identity is invalid.")
    if type(drafts) is not list or not 1 <= len(drafts) <= MAX_DRAFTS:
        raise InvalidImport(f"Create between 1 and {MAX_DRAFTS} drafts at a time.")
    normalized = [validate_draft(d) for d in drafts]
    if len({d["commandKey"] for d in normalized}) != len(normalized):
        raise InvalidImport("Each draft needs its own command key.")
    results = []
    for draft in normalized:
        try:
            results.append({"commandKey": draft["commandKey"], "ok": True, **create_draft(store, tenant, account, draft=draft, actor=actor)})
        except InvalidImport as exc:
            results.append({"commandKey": draft["commandKey"], "ok": False, "code": "invalid_draft", "error": str(exc)})
        except Refused as exc:
            results.append({"commandKey": draft["commandKey"], "ok": False, "code": _code(exc), "error": _message(exc)})
    return results


def _code(exc):
    return {"scope unavailable": "not_available", "import source unavailable": "not_available",
            "catalogue assets already present": "existing_item_conflict",
            "import idempotency conflict": "idempotency_conflict"}.get(str(exc), "conflict")


def _message(exc):
    return {"catalogue assets already present": "A catalogue item already holds these attachments with other terms.",
            "import idempotency conflict": "That command was already used with different details.",
            "source terms changed": "The source changed after review. Refresh and review again.",
            "source not ready": "This source cannot become a draft yet.",
            "crm originated source": "This message was sent from the CRM, so it is already a catalogue item.",
            "verified connection unavailable": "Verify the account connection first.",
            "vault media unavailable": "One or more attachments are not available in the vault."}.get(
            str(exc), "The draft was not created. Refresh and review again.")


def create_draft(store, tenant, account, *, draft, actor):
    from sqlalchemy.exc import IntegrityError
    try:
        return _create_draft(store, tenant, account, draft=draft, actor=actor)
    except IntegrityError:
        # A concurrent command won the PostgreSQL bundle constraint. The same
        # command returns its recorded result; any other is a conflict.
        recorded = get_command(store, tenant, account, draft["commandKey"])
        if recorded["recorded"] and recorded["action"] == "create":
            return _create_draft(store, tenant, account, draft=draft, actor=actor)
        raise Refused("catalogue assets already present") from None


def _create_draft(store, tenant, account, *, draft, actor):
    draft = validate_draft(draft)
    _identity(draft["commandKey"], actor)
    request_hash = digest({key: draft[key] for key in ("observationId", "expectedFingerprint", "label",
        "description", "reuseConfirmed", "descriptionApproved")})
    from .vault_management import create_catalog_single
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(OfOfferImportDecision, (account, draft["commandKey"]))
        if prior:
            if (prior.action, prior.request_hash, prior.actor, prior.binding_id) != ("create", request_hash, actor, runtime.binding_id):
                raise Refused("import idempotency conflict")
            return json.loads(prior.result_json)
        observation = s.get(OfOfferObservation, (account, draft["observationId"]), with_for_update=True)
        if observation is None or observation.binding_id != runtime.binding_id:
            raise Refused("import source unavailable")
        if observation.fingerprint != draft["expectedFingerprint"]:
            raise Refused("source terms changed")
        from db.onlyfans_content_sync_progress_models import OfContentSyncSource
        current = s.get(OfContentSyncSource, (account, runtime.binding_id,
            observation.source_kind, observation.source_id))
        if current is not None and (current.current_fingerprint != observation.fingerprint or current.source_unavailable):
            raise Refused("source terms changed")
        media = _ids(observation.media_json)
        previews = None if observation.preview_json is None else _ids(observation.preview_json)
        if (media is None or previews is None or json.loads(observation.reasons_json)
                or observation.locked_text is not False or observation.currency != "USD"
                or not MIN_PRICE_CENTS <= observation.price_cents <= MAX_PRICE_CENTS
                or not 1 <= len(media) <= MAX_ATTACHMENTS or set(previews) >= set(media)):
            raise Refused("source not ready")
        connection = _connection(s, runtime)
        from .content_sync import echo_item
        if observation.source_kind == "message" and echo_item(s, account, runtime.binding_id, observation.source_id) is not None:
            raise Refused("crm originated source")
        existing = _existing(s, account, media)
        if existing is not None:
            raise Refused("catalogue assets already present")
        from .vault_management import InvalidVault
        try:
            row = create_catalog_single(s, runtime, connection, media=media, previews=previews,
                label=draft["label"], description=draft["description"], price_cents=observation.price_cents,
                enabled=False, pricing_mode="fixed")
        except InvalidVault as exc:
            # Never reduce, reorder or substitute: the shape is reported instead.
            raise InvalidImport(str(exc)) from None
        result = {"itemId": str(row.id), "observationId": observation.observation_id,
            "fingerprint": observation.fingerprint, "priceCents": row.price_cents,
            "sourceAt": _date(observation.source_at), "itemFingerprint": item_fingerprint(row),
            "item": editable(row, 0)}
        s.add(OfOfferImportDecision(account_id=account, command_key=draft["commandKey"],
            binding_id=runtime.binding_id, action="create", request_hash=request_hash, actor=actor,
            observation_id=observation.observation_id, fingerprint=observation.fingerprint,
            media_key=observation.media_key, reuse_confirmed=True, description_approved=True,
            item_id=row.id, result_json=canonical(result), created_at=utcnow()))
        s.flush()
        return result


def get_command(store, tenant, account, command_key):
    """Resolve a lost acknowledgement without creating anything."""
    if type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key):
        raise InvalidImport("That command reference is invalid.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row = s.get(OfOfferImportDecision, (account, command_key))
        if row is None or row.binding_id != runtime.binding_id:
            return {"commandKey": command_key, "recorded": False}
        return {"commandKey": command_key, "recorded": True, "action": row.action, **json.loads(row.result_json)}


def provenance(s, account, item_id):
    """The immutable import evidence behind a catalogue item, if any."""
    create = s.scalar(select(OfOfferImportDecision).where(OfOfferImportDecision.account_id == account,
        OfOfferImportDecision.item_id == item_id, OfOfferImportDecision.action == "create"))
    if create is None:
        return None
    withdrawn = s.scalar(select(OfOfferImportDecision.command_key).where(OfOfferImportDecision.account_id == account,
        OfOfferImportDecision.item_id == item_id, OfOfferImportDecision.action == "withdraw")) is not None
    observation = s.get(OfOfferObservation, (account, create.observation_id))
    result = json.loads(create.result_json)
    return {"decision": create.command_key, "observationId": create.observation_id,
        "fingerprint": create.fingerprint, "sourcePriceCents": result["priceCents"],
        "sourceAt": result["sourceAt"], "sourceContext": observation.source_context if observation else None,
        "previewIds": [str(m) for m in _ids(observation.preview_json)] if observation and observation.preview_json else [],
        "createdFingerprint": result["itemFingerprint"], "withdrawn": withdrawn}


def withdraw_draft(store, tenant, account, item_id, *, command_key, actor):
    """Audited withdrawal of this import's unchanged, disabled, never-used draft."""
    _identity(command_key, actor)
    _id(item_id)
    request_hash = digest({"action": "withdraw", "itemId": item_id})
    from db.onlyfans_delivery_models import OfTextIntent
    from db.onlyfans_campaign_models import OfCampaign
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(OfOfferImportDecision, (account, command_key))
        if prior:
            if (prior.action, prior.request_hash, prior.actor) != ("withdraw", request_hash, actor):
                raise Refused("import idempotency conflict")
            return json.loads(prior.result_json)
        item = s.scalar(select(CatalogItem).where(CatalogItem.account_id == account, CatalogItem.id == item_id)
                        .with_for_update())
        source = provenance(s, account, item_id) if item is not None else None
        if item is None or source is None:
            raise Refused("import draft unavailable")
        if source["withdrawn"]:
            raise Refused("import draft already withdrawn")
        used = (s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.catalog_item_id == item_id).limit(1)) is not None
                or s.scalar(select(ContentOffer.id).where(ContentOffer.account_id == account,
                    ContentOffer.item_id == item_id).limit(1)) is not None
                or s.scalar(select(OfCampaign.campaign_id).where(OfCampaign.account_id == account,
                    OfCampaign.catalog_item_id == item_id).limit(1)) is not None)
        if item.enabled or used or item_fingerprint(item) != source["createdFingerprint"]:
            raise Refused("import draft changed or used")
        create = s.get(OfOfferImportDecision, (account, source["decision"]))
        result = {"itemId": str(item_id), "withdrawn": True}
        s.add(OfOfferImportDecision(account_id=account, command_key=command_key, binding_id=runtime.binding_id,
            action="withdraw", request_hash=request_hash, actor=actor, observation_id=create.observation_id,
            fingerprint=create.fingerprint, media_key=create.media_key, reuse_confirmed=False,
            description_approved=False, item_id=item_id, result_json=canonical(result), created_at=utcnow()))
        return result


# ── Commercial mapping recovery (read-only) ─────────────────────────────
# Recovers what historical priced messages prove and says plainly what they
# do not. It never creates drafts, approvals, prices, bindings or sequences:
# reuse_confirmed/description_approved stay a human decision on an exact
# observation (create_draft), and Moxy settings are changed only by their
# own versioned, approved path. Output is a report plus an unresolved
# register in the same {key, reason} shape as Moxy controls.

# Only an export of Moxy's own scripts or vault sequences can establish step
# membership. Progress/marketing artwork (e.g. "VIP vault" graphics) cannot.
SEQUENCE_SOURCES = ("moxy_script_export", "vault_sequence_export")
GRAPHIC_REASON = ("VIP vault graphics are progress/marketing artwork; they do not establish "
                  "paid-content sequence membership, order or prices")
CATEGORY_REASON = ("product category is not recorded on catalogue items or messages; the creator's "
                   "per-category price floors cannot be applied without verified content descriptions")


def documented_price_floor(creator_key):
    """Lowest minimum_cents among the creator's documented product price rules, or None.

    A conservative bound only: a price below every documented floor is wrong for
    any category. It is not a substitute for the per-category floor.
    """
    from moxy_replacement.configuration import load
    floors = []
    for rule in load(creator_key).public_context()["rules"]:
        value = rule["value"]
        if rule["key"].startswith("product:") and type(value) is dict and type(value.get("minimum_cents")) is int:
            floors.append(value["minimum_cents"])
    return min(floors) if floors else None


def sequence_status(evidence=None):
    """Accept only authoritative sequence exports; report everything else as rejected.

    evidence: [{"kind", "name", "steps": [{"step", "mediaIds", "priceCents"}]}]
    """
    accepted, rejected = {}, []
    for entry in evidence or []:
        kind = entry.get("kind") if type(entry) is dict else None
        if kind not in SEQUENCE_SOURCES:
            rejected.append({"kind": kind, "reason": GRAPHIC_REASON if kind == "vip_graphic"
                             else "not_an_authoritative_sequence_source"})
            continue
        steps = entry.get("steps")
        name = entry.get("name")
        valid = (type(name) is str and name and type(steps) is list and steps and all(
            type(st) is dict and type(st.get("step")) is int and type(st.get("priceCents")) is int
            and st["priceCents"] > 0 and _ids(json.dumps(st.get("mediaIds"))) for st in steps)
            and sorted(st["step"] for st in steps) == list(range(1, len(steps) + 1)))
        if not valid:
            rejected.append({"kind": kind, "reason": "incomplete_sequence_steps"})
            continue
        accepted[name] = sorted(steps, key=lambda st: st["step"])
    unresolved = [] if accepted else [{"key": "sequence:vault", "reason":
        "no authoritative sequence source (Moxy script or sequence export) is available; " + GRAPHIC_REASON}]
    return {"sequences": accepted, "rejected": rejected, "unresolved": unresolved}


def distinct_combinations(s, account, binding_id):
    """Attachment combinations deduplicated across every inventory run.

    Each run rescans the same saved rows, so counts per variant take the highest
    run total rather than the sum; summing is the double count this prevents.
    """
    jobs = {j.job_id: j for j in s.scalars(select(OfOfferImportJob).where(
        OfOfferImportJob.account_id == account, OfOfferImportJob.binding_id == binding_id,
        OfOfferImportJob.mode == "inventory"))}
    merged = {}
    if not jobs:
        return []
    for row in s.scalars(select(OfOfferImportCandidate).where(OfOfferImportCandidate.account_id == account,
            OfOfferImportCandidate.job_id.in_(list(jobs))).order_by(OfOfferImportCandidate.media_key,
            OfOfferImportCandidate.job_id)):
        entry = merged.setdefault(row.media_key, {"mediaKey": row.media_key, "mediaIds": _ids(row.media_json) or [],
            "variants": {}, "samples": {}, "runs": set(), "complete": False})
        entry["runs"].add(row.job_id)
        entry["complete"] = entry["complete"] or jobs[row.job_id].status == "complete"
        for v in json.loads(row.variants_json):
            have = entry["variants"].get(v["key"])
            if have is None:
                entry["variants"][v["key"]] = dict(v)
            else:
                have["count"] = max(have["count"], v["count"])
                if v["latest"] and (have["latest"] is None or v["latest"]["messageId"] > have["latest"]["messageId"]):
                    have["latest"] = v["latest"]
        for sample in json.loads(row.samples_json):
            entry["samples"].setdefault(sample["messageId"], sample)
    out = []
    for entry in merged.values():
        entry["variants"] = sorted(entry["variants"].values(), key=lambda v: v["key"])
        entry["samples"] = sorted(entry["samples"].values(), key=lambda x: x["messageId"])[:MAX_SAMPLES]
        entry["runs"] = sorted(entry["runs"])
        out.append(entry)
    return out


def _classify(entry, *, existing, asset_reasons, observations, floor):
    """(status, reasons, extra). Every non-mapped outcome carries its reason."""
    media = entry["mediaIds"]
    variants = entry["variants"]
    exact = [v for v in variants if v["key"] != "other"]
    if len(media) > MAX_ATTACHMENTS:
        return "blocked", ["too_many_attachments"], {}
    if asset_reasons:
        return "blocked", sorted(asset_reasons), {}
    if any(v["key"] == "other" for v in variants):
        return "unresolved", ["variants_truncated"], {}
    if len(exact) > 1:
        return "unresolved", ["variant_conflict"], {}
    variant = exact[0] if exact else None
    if variant is None:
        return "unresolved", ["no_exact_variant"], {}
    price = variant["priceCents"]
    order = [int(m) for m in variant["mediaIds"]]
    if floor is not None and price < floor:
        return "unresolved", ["historical_price_below_creator_floor"], {"floorCents": floor}
    if existing is not None:
        same = _ids(existing.media_ids) == order and existing.price_cents == price
        if not same:
            return "unresolved", ["existing_item_terms_differ"], {"itemId": existing.id}
        return "mapped_existing", [], {"proposedBinding": {"itemId": existing.id, "priceCents": price,
            "enabled": bool(existing.enabled)}}
    verified = [o for o in observations if o.preview_json is not None and o.locked_text is False
                and not json.loads(o.reasons_json) and o.price_cents == price and _ids(o.media_json) == order]
    if not verified:
        return "unresolved", ["source_details_needed"], {}
    return "awaiting_approval", ["approval_required"], {"observationId": verified[0].observation_id}


def recover_mappings(store, tenant, account, *, creator_key=None, sequence_evidence=None):
    """Read-only recovery report for one account (see section comment above)."""
    floor = documented_price_floor(creator_key) if creator_key else None
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from db.onlyfans_connection_models import OfProviderConnection
        connection = s.get(OfProviderConnection, account)
        if connection is not None and (connection.binding_id != runtime.binding_id or connection.status != "verified"):
            connection = None
        combos = distinct_combinations(s, account, runtime.binding_id)
        items, unresolved, counts = [], [], {}
        for entry in combos:
            media = entry["mediaIds"]
            _, asset_reasons = _asset_state(s, account, runtime, connection, media)
            observations = s.scalars(select(OfOfferObservation).where(OfOfferObservation.account_id == account,
                OfOfferObservation.binding_id == runtime.binding_id,
                OfOfferObservation.media_key == entry["mediaKey"]).limit(50)).all()
            status, reasons, extra = _classify(entry, existing=_existing(s, account, media),
                asset_reasons=asset_reasons, observations=observations, floor=floor)
            counts[status] = counts.get(status, 0) + 1
            items.append({"candidateId": entry["mediaKey"], "attachmentCount": len(media), "status": status,
                "reasons": reasons, "variants": [{"priceCents": v["priceCents"], "mediaIds": v["mediaIds"],
                    "count": v["count"], "latest": v["latest"]} for v in entry["variants"]],
                "runs": entry["runs"], **extra})
            if status != "mapped_existing":
                unresolved.append({"key": "catalogue:" + entry["mediaKey"][:16], "reason": ",".join(reasons)})
        sequences = sequence_status(sequence_evidence)
        account_level = [{"key": "catalogue:category", "reason": CATEGORY_REASON}] + sequences["unresolved"]
        return {"distinctCombinations": len(combos), "byStatus": counts, "items": items,
            "sequences": sequences["sequences"], "rejectedSequenceEvidence": sequences["rejected"],
            "priceFloorCents": floor, "approvalsCreated": 0, "pricesInvented": 0,
            "unresolved": account_level + unresolved}
