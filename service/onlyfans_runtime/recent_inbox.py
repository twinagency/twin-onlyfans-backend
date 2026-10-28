"""Poll the inbox head independently of archive discovery and recovery.

The process cache is only a scheduling optimization. On restart the newest
page is read again; canonical ingress provides durable deduplication. Archive
cursors and high-water marks are never advanced by this lane.
"""
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import time

from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_models import OfIngress, OfRecovery
from .provider_read_budget import acquire, current, release, ReadDeferred
from .store import PAGE_LIMIT, MAX_PENDING, Refused, utcnow

INTERVAL_SECONDS = 15
PAGES_PER_TURN = 2


@dataclass
class HeadState:
    due: float = 0
    discovery_cursor: str | None = None
    pending: deque = field(default_factory=deque)


def _read(store, tenant, account, revision, callback, consume):
    claim = acquire(store, tenant, account, connection_revision=revision, kind="recent")
    error = None
    try:
        page = callback()
        return consume(page, claim)
    except Exception as exc:
        error = exc
        raise
    finally:
        release(store, tenant, claim, retry_after=getattr(error, "retry_after", None))


def _missing(store, tenant, account, revision, page, claim):
    if len(page.fan_ids) > PAGE_LIMIT or len(set(page.fan_ids)) != len(page.fan_ids):
        raise Refused("invalid inbox head page")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        store._read_connection(s, runtime, revision)
        current(store, tenant, claim, session=s)
        from .profiles import apply_profiles
        apply_profiles(s,runtime,page.fan_ids,getattr(page,"profiles",{}),getattr(page,"creator_profile",None))
        # Last-message identities are hints only. Missing hints cause a bounded
        # message read, never a completeness assertion or an archive reset.
        return [fan for fan in page.fan_ids
                if page.heads.get(fan) is None
                or s.get(Message, (account, fan, page.heads[fan])) is None]


def _commit(store, tenant, account, revision, fan, page, claim, *, history_only):
    events = [event.normalized() for event in page.events]
    ids = [event.message_id for event in events]
    if (len(events) > PAGE_LIMIT or type(page.has_more) is not bool
            or page.has_more and not events or ids != sorted(set(ids), reverse=True)
            or any(event.fan_id != fan for event in events)):
        raise Refused("invalid inbox message page")
    from .offer_terms import OfferObservation
    if (type(page.observations) not in {list, tuple}
            or len(page.observations) > len(events)
            or any(type(item) is not OfferObservation for item in page.observations)):
        raise Refused("invalid inbox offer observations")
    offers = [item.normalized() for item in page.observations]
    if any(item.source_kind != "message" or item.fan_id != fan or item.source_id not in ids for item in offers):
        raise Refused("inbox offer outside message page")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        store._read_connection(s, runtime, revision)
        current(store, tenant, claim, session=s)
        if runtime.pending_events + len(events) > MAX_PENDING:
            raise ReadDeferred(utcnow(), "inbox ingress backpressure")
        store._observe_offers(s, runtime, offers)
        applied = 0
        for event in reversed(events):
            payload = event.payload()
            store._record(s, runtime, event, payload, "import" if history_only else "catchup", utcnow())
            row = s.get(OfIngress, (account, hashlib.sha256(payload.encode()).hexdigest()))
            if row is not None and row.status == "pending":
                store._apply(s, runtime, row, history_only=history_only)
                applied += 1
        # Ensure a newly discovered fan still gets archive recovery. An
        # existing unfinished archive retains its exact cursor and generation.
        recovery = s.get(OfRecovery, (account, fan))
        if recovery is None:
            s.add(OfRecovery(account_id=account, fan_id=fan))
        elif page.has_more and recovery.status == "complete":
            recovery.status = "needed"
            recovery.upper_bound = recovery.before_id = None
        return applied


def poll_recent(store, tenant, account, reader, *, connection_revision=None, clock=time.monotonic):
    """At most one head GET and two message pages, honoring the shared budget.

The verified read-only branch always imports historically. Active accounts
use the existing catch-up semantics and every existing send gate remains in
the native worker. This module has no planner, model or send dependency.
"""
    if reader.account_id != account:
        raise Refused("inbox reader scope mismatch")
    revision = connection_revision if connection_revision is not None else getattr(reader, "connection_revision", None)
    if type(revision) is not int or revision < 1:
        raise Refused("inbox connection revision unavailable")
    states = getattr(store, "_recent_inbox_states", None)
    if states is None:
        states = store._recent_inbox_states = {}
    key = (tenant, account, revision)
    # Bound memory to the current generation of each account.
    for old in list(states):
        if old[:2] == key[:2] and old != key:
            del states[old]
    state = states.setdefault(key, HeadState())
    applied = 0
    try:
        if clock() >= state.due:
            fans, next_cursor = _read(store, tenant, account, revision,
                lambda: reader.chats(cursor=None, limit=PAGE_LIMIT),
                lambda page, claim: (_missing(store, tenant, account, revision, page, claim), page.next_cursor))
            # Give the newest arrival one priority slot, retaining older queued
            # fans ahead of the rest. Optional missing head hints must not make
            # repeated top-of-inbox reads starve the tail of the queue.
            waiting = set(state.pending)
            fresh = [fan for fan in fans if fan not in waiting]
            state.pending = deque(fresh[:1] + list(state.pending) + fresh[1:])
            state.due = clock() + INTERVAL_SECONDS
            if state.discovery_cursor is None:state.discovery_cursor = next_cursor
        if state.discovery_cursor is not None:
            fans, next_cursor = _read(store, tenant, account, revision,
                lambda: reader.chats(cursor=state.discovery_cursor, limit=PAGE_LIMIT),
                lambda page, claim: (_missing(store, tenant, account, revision, page, claim), page.next_cursor))
            if next_cursor == state.discovery_cursor:raise Refused("inbox discovery cursor did not advance")
            waiting = set(state.pending)
            state.pending.extend(fan for fan in fans if fan not in waiting)
            state.discovery_cursor = next_cursor
        for _ in range(PAGES_PER_TURN):
            if not state.pending:
                break
            fan = state.pending[0]
            try:
                applied += _read(store, tenant, account, revision,
                    lambda: reader.messages(fan_id=fan, before_id=None, limit=PAGE_LIMIT),
                    lambda page, claim: _commit(store, tenant, account, revision, fan, page, claim,
                        history_only=connection_revision is not None))
            except ReadDeferred:
                raise
            except Exception:
                # One malformed conversation must not pin every other fan
                # behind it. Retain it for retry and report the failed turn.
                state.pending.rotate(-1)
                raise
            state.pending.popleft()
    except ReadDeferred:
        # Preserve pending work and let the archive/other accounts take a turn.
        pass
    if applied:
        print(json.dumps({"schemaVersion": "of-recent-inbox.v1", "account": account,
                          "imported": applied, "pendingConversations": len(state.pending)}), flush=True)
    return applied
