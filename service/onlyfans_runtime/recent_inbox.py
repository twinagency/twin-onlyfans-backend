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
import threading

from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_models import OfIngress, OfRecovery
from .provider_read_budget import acquire, current, release, ReadDeferred
from .store import PAGE_LIMIT, MAX_PENDING, Refused, utcnow

INTERVAL_SECONDS = 10
PAGES_PER_TURN = 4
TURN_SECONDS = 15
MAX_QUEUED_FANS = 1000
FAILED_FAN_RETRY_SECONDS = 60
_STATES_LOCK = threading.Lock()


@dataclass
class HeadState:
    due: float = 0
    discovery_cursor: str | None = None
    pending: deque = field(default_factory=deque)
    heads: dict = field(default_factory=dict)
    retry_at: dict = field(default_factory=dict)
    last_head_at: object = None
    failures: int = 0


def _enqueue(state, fans, heads, *, priority, now):
    """A new message on an already queued fan must move ahead of old history."""
    waiting = set(state.pending)
    promoted, added = [], []
    for fan in fans:
        head = heads.get(fan)
        changed = head is not None and state.heads.get(fan) != head
        if changed:
            state.retry_at.pop(fan, None)
        if state.retry_at.get(fan, 0) > now:
            continue
        if priority and (changed or fan not in waiting):
            promoted.append(fan)
        elif fan not in waiting:
            added.append(fan)
        if head is not None:
            state.heads[fan] = head
    promoted_set = set(promoted)
    items = promoted + [fan for fan in state.pending if fan not in promoted_set] + added
    state.pending = deque(dict.fromkeys(items))
    if len(state.pending) > MAX_QUEUED_FANS:
        # The provider/archive remains the source of truth. Restart this sweep
        # after draining; never declare the discarded tail synchronized.
        state.pending = deque(list(state.pending)[:MAX_QUEUED_FANS])
        state.discovery_cursor = None
    keep = set(state.pending) | set(fans)
    state.heads = {fan: head for fan, head in state.heads.items() if fan in keep}
    state.retry_at = {fan: due for fan, due in state.retry_at.items() if due > now}


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
    """Fetch new message bodies before optional directory work.

    A verified paused account imports without creating outgoing work. Existing
    active-account gates and canonical deduplication are retained.
    """
    from .provider_connector import NativeAuthenticationError, NativeRateLimitError
    if reader.account_id != account:
        raise Refused("inbox reader scope mismatch")
    revision = connection_revision if connection_revision is not None else getattr(reader, "connection_revision", None)
    if type(revision) is not int or revision < 1:
        raise Refused("inbox connection revision unavailable")
    key = (tenant, account, revision)
    with _STATES_LOCK:
        states = getattr(store, "_recent_inbox_states", None)
        if states is None:
            states = store._recent_inbox_states = {}
        for old in list(states):
            if old[:2] == key[:2] and old != key:
                del states[old]
        state = states.setdefault(key, HeadState())
    applied = 0
    started = clock()
    try:
        if clock() >= state.due:
            fans, heads, next_cursor = _read(store, tenant, account, revision,
                lambda: reader.chats(cursor=None, limit=PAGE_LIMIT),
                lambda page, claim: (_missing(store, tenant, account, revision, page, claim),
                                     page.heads, page.next_cursor))
            _enqueue(state, fans, heads, priority=True, now=clock())
            state.due = clock() + INTERVAL_SECONDS
            state.last_head_at = utcnow()
            if state.discovery_cursor is None:
                state.discovery_cursor = next_cursor
        # Directory pagination never sits between the inbox head and its bodies.
        for attempt in range(PAGES_PER_TURN):
            if not state.pending or attempt and clock() - started >= TURN_SECONDS:
                break
            fan = state.pending[0]
            if state.retry_at.get(fan, 0) > clock():
                state.pending.popleft()
                continue
            try:
                applied += _read(store, tenant, account, revision,
                    lambda: reader.messages(fan_id=fan, before_id=None, limit=PAGE_LIMIT),
                    lambda page, claim: _commit(store, tenant, account, revision, fan, page, claim,
                        history_only=connection_revision is not None))
            except (ReadDeferred, NativeRateLimitError, NativeAuthenticationError, Refused):
                raise
            except Exception as exc:
                # A deleted/malformed fan cannot pin other fans behind it.
                # Retry later, or immediately when a newer head ID appears.
                state.retry_at[fan] = clock() + FAILED_FAN_RETRY_SECONDS
                state.failures += 1
                print(json.dumps({"schemaVersion": "of-recent-inbox.v1", "account": account,
                    "state": "conversation_deferred", "errorType": type(exc).__name__,
                    "retrySeconds": FAILED_FAN_RETRY_SECONDS}), flush=True)
            state.pending.popleft()
        # Keep catching arrivals below the first page, but bound this queue and
        # do not let a bad archive cursor prevent the next live-head poll.
        if (state.discovery_cursor is not None and len(state.pending) < PAGES_PER_TURN
                and clock() - started < TURN_SECONDS):
            try:
                fans, heads, next_cursor = _read(store, tenant, account, revision,
                    lambda: reader.chats(cursor=state.discovery_cursor, limit=PAGE_LIMIT),
                    lambda page, claim: (_missing(store, tenant, account, revision, page, claim),
                                         page.heads, page.next_cursor))
                if next_cursor == state.discovery_cursor:
                    raise Refused("inbox discovery cursor did not advance")
                _enqueue(state, fans, heads, priority=False, now=clock())
                state.discovery_cursor = next_cursor
            except (ReadDeferred, NativeRateLimitError, NativeAuthenticationError):
                raise
            except Exception as exc:
                state.discovery_cursor = None
                print(json.dumps({"schemaVersion": "of-recent-inbox.v1", "account": account,
                    "state": "directory_deferred", "errorType": type(exc).__name__}), flush=True)
    except ReadDeferred:
        pass
    if applied:
        print(json.dumps({"schemaVersion": "of-recent-inbox.v1", "account": account,
                          "imported": applied, "pendingConversations": len(state.pending)}), flush=True)
    return applied
