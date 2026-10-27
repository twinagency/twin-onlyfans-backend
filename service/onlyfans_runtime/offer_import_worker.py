"""Bounded source-detail refresh for selected paid offers; worker role only.

Uses the existing account-bound chat-history GET and its descending cursor; no
new endpoint, sender or provider connection. Each page is read with no DB
transaction open, then revalidated (lease, binding, connection revision) and
checkpointed atomically with its observations. Finding one source verifies
only that source. A target not reached within budget is unresolved, never
verified or deleted.
"""
from datetime import timedelta, timezone
import json
import os
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from onlyfans_read.contract import ID_PATTERN
from db.models import OfCreatorBinding
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_offer_import_models import OfOfferImportCursor, OfOfferImportJob
from db.onlyfans_content_sync_progress_models import OfContentSyncSource
from .native_context import canonical
from .native_protocol import NativeProtocolError
from .offer_import import LEASE_SECONDS, REFRESH_PAGES_PER_CHAT, _terminal
from .store import PAGE_LIMIT, Refused, utcnow

ACCOUNT_INTERVAL_SECONDS = 5
MAX_ATTEMPTS = 3


def _current(runtime, connection, job):
    return (connection is not None and connection.binding_id == runtime.binding_id == job.binding_id
        and connection.revision == job.connection_revision and connection.status == "verified"
        and connection.last_error is None and connection.expires_at > utcnow())


def _finish(job, s):
    cursors = s.scalars(select(OfOfferImportCursor).where(OfOfferImportCursor.account_id == job.account_id,
        OfOfferImportCursor.job_id == job.job_id)).all()
    job.targets_done = sum(len(json.loads(c.found_json)) for c in cursors)
    if all(c.status != "pending" for c in cursors):
        by_fan = {c.fan_id: c for c in cursors}
        for source in s.scalars(select(OfContentSyncSource).where(
                OfContentSyncSource.account_id == job.account_id,
                OfContentSyncSource.binding_id == job.binding_id,
                OfContentSyncSource.refresh_job_id == job.job_id)):
            cursor = by_fan.get(source.fan_id)
            source.refresh_completed_at = utcnow()
            source.refresh_job_id = None
            source.refresh_error = None if cursor and source.source_id in json.loads(cursor.found_json) else (
                cursor.outcome if cursor else "source_unresolved")
        _terminal(job, "complete" if all(c.status == "complete" for c in cursors) else "partial",
                  None if all(c.status == "complete" for c in cursors) else "sources_unresolved")


def _transport(connection, tenant, account):
    from .connections import _open
    from .provider_connector import NativeProviderConnector, NativeRateLimitError, session_from_capture
    clear = _open(connection, tenant)
    return NativeProviderConnector(session_from_capture(account_id=account, owner_id=connection.owner_id,
        policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
        expires_at=connection.expires_at.replace(tzinfo=timezone.utc)))


def process_refresh_jobs(store, tenant, *, limit=1, stop=None, transport_factory=_transport):
    """At most `limit` history pages across accounts, one per account per 5s."""
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("offer refresh requires the worker role")
    if type(tenant) is not str or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant):
        raise Refused("invalid offer refresh tenant")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise Refused("invalid offer refresh bound")
    processed, touched = 0, {}
    for _ in range(limit):
        if stop is not None and stop.is_set():
            break
        now = utcnow()
        with Session(store.engine) as s, s.begin():
            job = s.scalar(select(OfOfferImportJob).join(OfCreatorBinding, OfCreatorBinding.id == OfOfferImportJob.binding_id)
                .where(OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
                    OfOfferImportJob.mode == "refresh",
                    or_(OfOfferImportJob.next_attempt_at.is_(None), OfOfferImportJob.next_attempt_at <= now),
                    or_(OfOfferImportJob.status == "pending",
                        and_(OfOfferImportJob.status == "running", OfOfferImportJob.lease_until <= now)))
                .order_by(OfOfferImportJob.updated_at).limit(1).with_for_update(skip_locked=True, of=OfOfferImportJob))
            if job is None:
                break
            account, identity = job.account_id, job.job_id
            touched[account] = job.actor
            try:
                runtime = store._runtime(s, tenant, account)
            except Refused:
                _terminal(job, "superseded", "scope_unavailable")
                continue
            connection = s.get(OfProviderConnection, account)
            if not _current(runtime, connection, job):
                # Rebinding or a new connection never inherits old work.
                _terminal(job, "superseded", "connection_changed")
                continue
            cursor = s.scalar(select(OfOfferImportCursor).where(OfOfferImportCursor.account_id == account,
                OfOfferImportCursor.job_id == identity, OfOfferImportCursor.status == "pending")
                .order_by(OfOfferImportCursor.updated_at, OfOfferImportCursor.fan_id).limit(1))
            if cursor is None:
                _finish(job, s)
                continue
            try:
                transport = transport_factory(connection, tenant, account)
            except Refused:
                _terminal(job, "failed", "connection_unavailable")
                continue
            token = str(uuid.uuid4())
            job.status, job.lease_token = "running", token
            job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            job.next_attempt_at = now + timedelta(seconds=ACCOUNT_INTERVAL_SECONDS)
            job.updated_at = now
            fan, before, revision = cursor.fan_id, cursor.before_id, job.connection_revision
        if stop is not None and stop.is_set():
            break
        from .provider_read_budget import acquire, current, release, ReadDeferred, current_in_session, native_reader
        from .provider_connector import NativeProviderConnector, NativeRateLimitError
        try:
            read_claim = acquire(store, tenant, account, connection_revision=revision, kind="offer_refresh") if native_reader(transport) else None
        except ReadDeferred as deferred:
            with Session(store.engine) as s, s.begin():
                row = s.get(OfOfferImportJob, (account, identity), with_for_update=True)
                if row is not None and row.lease_token == token:
                    row.status, row.lease_token, row.lease_until = "pending", None, None
                    row.next_attempt_at = deferred.retry_at
            continue
        except Refused:
            with Session(store.engine) as s, s.begin():
                row = s.get(OfOfferImportJob, (account, identity), with_for_update=True)
                if row is not None and row.lease_token == token:
                    _terminal(row, "superseded", "connection_changed")
            continue
        page, error, retry_after = None, None, None
        try:
            page = transport.messages(fan_id=fan, before_id=before, limit=PAGE_LIMIT)
        except NativeRateLimitError as exc:
            error, retry_after = "provider_rate_limit", exc.retry_after
        except NativeProtocolError as exc:
            from .provider_connector import NativeAuthenticationError
            error = "authentication_rejected" if isinstance(exc, NativeAuthenticationError) else "unsupported_history"
        except Exception:
            error = "history_read_failed"
        try:
            if read_claim is not None:
                current(store, tenant, read_claim)
        except Refused:
            if read_claim is not None:
                release(store, tenant, read_claim, retry_after=retry_after)
            continue
        try:
            with Session(store.engine) as s, s.begin():
                try:
                    runtime = store._runtime(s, tenant, account)
                except Refused:
                    continue
                job = s.get(OfOfferImportJob, (account, identity), with_for_update=True)
                if (job is None or job.status != "running" or job.lease_token != token
                        or job.lease_until is None or job.lease_until <= utcnow()):
                    continue
                if not _current(runtime, s.get(OfProviderConnection, account), job):
                    _terminal(job, "superseded", "connection_changed")
                    continue
                if read_claim is not None:
                    current_in_session(store, s, tenant, read_claim)
                cursor = s.get(OfOfferImportCursor, (account, identity, fan), with_for_update=True)
                cursor.updated_at = utcnow()
                job.lease_token = job.lease_until = None
                job.status = "pending"
                job.updated_at = utcnow()
                processed += 1
                if error == "authentication_rejected":
                    _terminal(job, "failed", error)
                    continue
                if error:
                    cursor.attempts += 1
                    cursor.outcome = error
                    if cursor.attempts >= MAX_ATTEMPTS:
                        cursor.status = "unresolved"
                    _finish(job, s)
                    continue
                store._observe_offers(s, runtime, [o.normalized() for o in page.observations
                    if o.fan_id == fan and o.source_kind == "message"])
                targets = json.loads(cursor.targets_json)
                found = set(json.loads(cursor.found_json))
                seen = {o.source_id for o in page.observations}
                ids = [event.message_id for event in page.events]
                found |= seen & set(targets)
                cursor.found_json = canonical(sorted(found, reverse=True))
                cursor.pages_read += 1
                job.pages_read += 1
                cursor.before_id = min(ids) if ids else before
                remaining = [t for t in targets if t not in found]
                if not remaining:
                    cursor.status, cursor.outcome = "complete", "found"
                elif not page.has_more or (ids and min(ids) < min(remaining)):
                    # Passed below every remaining target without seeing it.
                    cursor.status, cursor.outcome = "unresolved", "source_not_found"
                elif cursor.pages_read >= REFRESH_PAGES_PER_CHAT or job.pages_read >= job.page_budget:
                    cursor.status, cursor.outcome = "unresolved", "page_budget"
                if job.pages_read >= job.page_budget:
                    for other in s.scalars(select(OfOfferImportCursor).where(OfOfferImportCursor.account_id == account,
                            OfOfferImportCursor.job_id == identity, OfOfferImportCursor.status == "pending")):
                        other.status, other.outcome, other.updated_at = "unresolved", "page_budget", utcnow()
                _finish(job, s)
        finally:
            if read_claim is not None:
                release(store, tenant, read_claim, retry_after=retry_after)
    from .content_sync import dispatch_refresh_queue
    for account, actor in touched.items():
        dispatch_refresh_queue(store, tenant, account, actor)
    return processed
