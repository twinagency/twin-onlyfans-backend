"""One bounded read step. Transport is explicitly injected and account-bound.

There is deliberately no real provider construction or planner/send import.
Production transport/session integration remains a separately gated adapter.
"""
from dataclasses import dataclass, field
from typing import Protocol
import os
from .store import Event, PAGE_LIMIT, Refused, Store


@dataclass(frozen=True)
class ChatPage:
    fan_ids: list[int]
    next_cursor: str | None
    heads: dict[int, int] = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)
    creator_profile: dict | None = None


@dataclass(frozen=True)
class MessagePage:
    events: list[Event]
    has_more: bool
    # Commercial terms of outgoing paid offers on this page, captured from the
    # original rows. Optional so existing readers stay compatible.
    observations: list = field(default_factory=list)
    # Oldest provider row id read, including rows the reader skipped; the
    # next cursor. None keeps the legacy min(event ids) cursor.
    next_before: int | None = None


@dataclass(frozen=True)
class ReadProgress:
    applied: int
    discovery_pages: int
    recovery_pages: int


class BoundReader(Protocol):
    account_id: str
    def chats(self, *, cursor: str | None, limit: int) -> ChatPage: ...
    def messages(self, *, fan_id: int, before_id: int | None, limit: int) -> MessagePage: ...


def _provider_claim(store, tenant, account, reader, connection_revision, kind):
    from .provider_read_budget import acquire, native_reader
    if not native_reader(reader):return None
    revision=connection_revision if connection_revision is not None else getattr(reader,'connection_revision',None)
    if type(revision) is not int or revision<1:
        raise Refused('provider read connection revision unavailable')
    return acquire(store,tenant,account,connection_revision=revision,kind=kind)


def _release_provider(store, tenant, claim, error=None):
    if claim is not None:
        from .provider_read_budget import release
        from .provider_connector import NativeRateLimitError
        release(store,tenant,claim,retry_after=getattr(error,'retry_after',None),
            rate_limited=isinstance(error,NativeRateLimitError))


def _defer_recent_claim(store,tenant,claim,*,scan):
    """Undo only our unexecuted read lease; retain cursor and attempt order."""
    from sqlalchemy.orm import Session
    from db.onlyfans_models import OfScan, OfRecovery
    with Session(store.engine) as s,s.begin():
        store._runtime(s,tenant,claim.account_id)
        row=s.get(OfScan,claim.account_id) if scan else s.get(OfRecovery,(claim.account_id,claim.fan_id))
        if (row is not None and row.status=='running' and row.generation==claim.generation
                and row.lease_token==claim.token):
            row.status='needed';row.lease_token=row.lease_until=None


def read_step(store: Store, tenant_id: str, account_id: str, reader: BoundReader, *, connection_revision=None,
              recent_cutover=False) -> ReadProgress:
    if reader.account_id != account_id:
        raise Refused("reader scope mismatch")
    scope = {"connection_revision": connection_revision} if connection_revision is not None else {}
    applied = store.drain(tenant_id, account_id, **scope)
    if (os.environ.get("ONLYFANS_RECENT_INBOX_ENABLED") == "1"
            and not getattr(store, "_independent_inbox", False)):
        from .recent_inbox import poll_recent
        applied += poll_recent(store, tenant_id, account_id, reader, connection_revision=connection_revision)
    discovered = recovered = 0
    scan = store.claim_scan(tenant_id, account_id, **scope)
    if scan:
        from .provider_read_budget import ReadDeferred
        try:budget=_provider_claim(store,tenant_id,account_id,reader,connection_revision,'discovery')
        except ReadDeferred:_defer_recent_claim(store,tenant_id,scan,scan=True)
        else:
            error=None
            try:
                # No database transaction remains open across this network boundary.
                page = reader.chats(cursor=scan.cursor, limit=PAGE_LIMIT)
                fenced={'provider_read_claim':budget} if budget is not None else {}
                store.scan_page(tenant_id, scan, page.fan_ids, next_cursor=page.next_cursor, profiles=page.profiles, creator_profile=page.creator_profile, **scope,**fenced)
                discovered = 1
            except Exception as failure:
                error=failure
                raise
            finally:_release_provider(store,tenant_id,budget,error)
    recovery = store.claim_recovery(tenant_id, account_id, **scope)
    if recovery:
        before_id = recovery.before_id
        if recent_cutover:
            # Initial head = newest page even after a legacy deep cursor; the remainder goes to archive recovery.
            from .launch_history_memory.recent import request_before
            before_id = request_before(store, tenant_id, recovery, **scope)
        from .provider_read_budget import ReadDeferred
        try:budget=_provider_claim(store,tenant_id,account_id,reader,connection_revision,'recent')
        except ReadDeferred:_defer_recent_claim(store,tenant_id,recovery,scan=False)
        else:
            error=None
            try:
                page = reader.messages(fan_id=recovery.fan_id, before_id=before_id, limit=PAGE_LIMIT)
                fenced={'provider_read_claim':budget} if budget is not None else {}
                if recent_cutover:
                    from .launch_history_memory.recent import commit_recent_page
                    commit_recent_page(store, tenant_id, recovery, page.events, has_more=page.has_more,
                                       observations=page.observations, next_before=page.next_before, **scope,**fenced)
                else:
                    store.recovery_page(tenant_id, recovery, page.events, has_more=page.has_more,
                                        observations=page.observations, next_before=page.next_before, **scope,**fenced)
                recovered = 1
            except Exception as failure:
                error=failure
                from .history_diagnostics import record_history_failure
                record_history_failure(failure)
                raise
            finally:_release_provider(store,tenant_id,budget,error)
    return ReadProgress(applied, discovered, recovered)


def tick(store: Store, tenant_id: str, account_id: str, reader: BoundReader) -> int:
    progress = read_step(store, tenant_id, account_id, reader)
    return progress.applied + progress.discovery_pages + progress.recovery_pages
