"""One bounded read step. Transport is explicitly injected and account-bound.

There is deliberately no real provider construction or planner/send import.
Production transport/session integration remains a separately gated adapter.
"""
from dataclasses import dataclass, field
from typing import Protocol
from .store import Event, PAGE_LIMIT, Refused, Store


@dataclass(frozen=True)
class ChatPage:
    fan_ids: list[int]
    next_cursor: str | None


@dataclass(frozen=True)
class MessagePage:
    events: list[Event]
    has_more: bool
    # Commercial terms of outgoing paid offers on this page, captured from the
    # original rows. Optional so existing readers stay compatible.
    observations: list = field(default_factory=list)


@dataclass(frozen=True)
class ReadProgress:
    applied: int
    discovery_pages: int
    recovery_pages: int


class BoundReader(Protocol):
    account_id: str
    def chats(self, *, cursor: str | None, limit: int) -> ChatPage: ...
    def messages(self, *, fan_id: int, before_id: int | None, limit: int) -> MessagePage: ...


def read_step(store: Store, tenant_id: str, account_id: str, reader: BoundReader, *, connection_revision=None,
              recent_cutover=False) -> ReadProgress:
    if reader.account_id != account_id:
        raise Refused("reader scope mismatch")
    scope = {"connection_revision": connection_revision} if connection_revision is not None else {}
    applied = store.drain(tenant_id, account_id, **scope)
    discovered = recovered = 0
    scan = store.claim_scan(tenant_id, account_id, **scope)
    if scan:
        # No database transaction remains open across this network boundary.
        page = reader.chats(cursor=scan.cursor, limit=PAGE_LIMIT)
        store.scan_page(tenant_id, scan, page.fan_ids, next_cursor=page.next_cursor, **scope)
        discovered = 1
    recovery = store.claim_recovery(tenant_id, account_id, **scope)
    if recovery:
        before_id = recovery.before_id
        if recent_cutover:
            # Initial head = newest page even after a legacy deep cursor; the remainder goes to archive recovery.
            from .launch_history_memory.recent import request_before
            before_id = request_before(store, tenant_id, recovery, **scope)
        try:
            page = reader.messages(fan_id=recovery.fan_id, before_id=before_id, limit=PAGE_LIMIT)
        except Exception as error:
            from .history_diagnostics import record_history_failure
            record_history_failure(error)
            raise
        if recent_cutover:
            from .launch_history_memory.recent import commit_recent_page
            commit_recent_page(store, tenant_id, recovery, page.events, has_more=page.has_more,
                               observations=page.observations, **scope)
        else:
            store.recovery_page(tenant_id, recovery, page.events, has_more=page.has_more,
                                observations=page.observations, **scope)
        recovered = 1
    return ReadProgress(applied, discovered, recovered)


def tick(store: Store, tenant_id: str, account_id: str, reader: BoundReader) -> int:
    progress = read_step(store, tenant_id, account_id, reader)
    return progress.applied + progress.discovery_pages + progress.recovery_pages
