"""Conversation ownership, turn claims and the shared send boundary.

Two layers, with different jobs:

* **Durable** rows in `twin_bridge_conversations` / `twin_bridge_turns` hold the
  authority: who may act, which revision is current, and how much pilot send
  budget is left. Every transition is an atomic conditional UPDATE against the
  current owner token / revision / state, so a stale worker's write matches
  nothing. No transaction is ever held across a model or network call.
* **Process-local** registry in this module is what a SYNCHRONOUS transport can
  consult. `OFClient.send_message` runs on a worker thread, so it cannot await a
  database read; the registry is the same ownership fact held where the POST
  happens, and it also counts in-flight attempts so ownership acquisition can
  wait for an older local writer to settle instead of racing it.

What this can and cannot fence: writers inside THIS process that funnel through
the guarded transport. Provider-scheduled messages, other processes/machines and
the official app are outside a local fence and stay explicit live-preflight
exclusions (see the bridge README).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import case, select, update

from db.engine import get_session
from db.models import TwinBridgeConversation, TwinBridgeSendAttempt, TwinBridgeTurn

from . import store
from .store import DB_ERRORS, dialect_insert

log = logging.getLogger("of-relay.twin_bridge.coordination")

CONV_ACTIVE = "active"
CONV_PAUSED = "paused"
CONV_ATTENTION = "attention"
CONV_DISABLED = "disabled"

OWNER_KIND = "twin_bridge"

DEFAULT_OWNER_TTL_S = 120.0

_REGISTRY_LOCK = threading.Lock()
# (account_id, fan_id) -> owner token of the CURRENT per-attempt worker claim.
# This is the expiring claim only; it is cleared on pause/takeover even though
# the durable reservation below survives.
_OWNERS: dict[tuple[str, int], str] = {}
# (account_id, fan_id) currently under the DURABLE conversation reservation.
# Mirrors `TwinBridgeConversation.reserved` for the synchronous transport
# boundary; the durable column is the authority and is consulted when this
# process mirror is empty (a restart).
_RESERVED: set[tuple[str, int]] = set()
# (account_id, fan_id) -> how many local send attempts are between "guarded" and
# "settled". Ownership acquisition refuses while this is non-zero.
_IN_FLIGHT: dict[tuple[str, int], int] = {}
# (account_id, fan_id) currently in the middle of reserving ownership. This is
# the ADMISSION token: acquisition takes it atomically with the in-flight check,
# and every competing transport write refuses while it is held, so a writer can
# neither slip in after the quiescence observation nor slip in before the
# reservation lands. Reading the durable reservation is only VISIBILITY; this is
# what arbitrates admission inside the owning process.
_ACQUIRING: set[tuple[str, int]] = set()
# (account_id, fan_id) -> when the in-process owner claim expires. Mirrors the
# durable `owner_expires_at` so the admission gate can allow a legitimate
# replacement worker after the TTL without a DB round-trip.
_OWNER_EXPIRES: dict[tuple[str, int], datetime] = {}
# (account_id, fan_id) -> the one authorization under which the bridge may POST.
# Bound to the durable account/fan/turn/attempt/claim/revision, so a stale
# worker's token no longer matches and it cannot dispatch or release a newer
# claim. Deliberately NOT a magic `purpose` string.
_DISPATCH_AUTH: dict[tuple[str, int], tuple] = {}
# Monotonic per-call tag for the durable in-flight marker, so two writers in one
# process never collide on the same holder string.
_WRITE_SEQ = itertools.count()


class BridgeOwnedError(RuntimeError):
    """A competing writer tried to send to a conversation the bridge owns."""


class BroadcastRefusedError(RuntimeError):
    """A broadcast whose audience cannot exclude a bridge-reserved account."""


class DispatchNotAuthorized(RuntimeError):
    """The bridge transport boundary was asked to send without a live,
    attempt-bound authorization for THIS conversation."""


def _key(account_id: object, fan_id: object) -> tuple[str, int]:
    return (str(account_id), int(fan_id))


# ── durable reservation read (synchronous, for the transport boundary) ─

def _database_url() -> str:
    from db import engine as _engine
    return os.environ.get("DATABASE_URL") or getattr(_engine, "DATABASE_URL", "")


def _sqlite_path() -> str | None:
    """The on-disk SQLite file for this process, or None when not SQLite."""
    target = store.target_from_url(_database_url())
    return None if store.is_pg(target) else target


def _store_target() -> str | None:
    """The coordination store for this process: a SQLite file path, the PostgreSQL
    URL of the application database, or None when neither can be determined.

    The synchronous send boundary cannot await an async session, so it reads the
    durable reservation directly (SQLite file, or PostgreSQL via psycopg2 with the
    same rules; see ``store``). With no usable store the in-process mirror is the
    only gate (documented in the bridge README).
    """
    path = _sqlite_path()
    if path:
        return path
    target = store.target_from_url(_database_url())
    return target if store.is_pg(target) else None


def _connect(target, *, timeout: float, **kwargs):
    if store.is_pg(target):
        return store.connect_pg(target)
    return sqlite3.connect(target, timeout=timeout, **kwargs)


def _durable_reserved(account_id: object, fan_id: object) -> bool:
    """Is this conversation still under the durable bridge reservation?

    Read directly from SQLite so ownership survives a restart even when the
    process registry was emptied. Any read failure is treated as "not reserved"
    so a database blip can never wedge ordinary sends; a live in-process
    reservation is still enforced first.
    """
    path = _store_target()
    if not path:
        return False
    try:
        con = _connect(path, timeout=2.0)
    except DB_ERRORS:
        return False
    try:
        row = con.execute(
            "SELECT reserved FROM twin_bridge_conversations "
            "WHERE account_id = ? AND fan_id = ? LIMIT 1",
            (str(account_id), int(fan_id)),
        ).fetchone()
    except DB_ERRORS:
        return False
    finally:
        con.close()
    return bool(row and row[0])


def _durable_any_reserved(account_id: object) -> bool:
    path = _store_target()
    if not path:
        return False
    try:
        con = _connect(path, timeout=2.0)
    except DB_ERRORS:
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM twin_bridge_conversations "
            "WHERE account_id = ? AND reserved = ? LIMIT 1",
            (str(account_id), True),
        ).fetchone()
    except DB_ERRORS:
        return False
    finally:
        con.close()
    return row is not None


# ── durable in-flight writer handshake (shared atomic admission) ─────
#
# `_IN_FLIGHT` is process-local, so a legacy writer in ANOTHER process (the
# documented `python -m twin_bridge.cli` runner beside Fastt, or any other local
# OFClient) is invisible to a process-local quiescence wait. The durable marker
# table below is what a SYNCHRONOUS transport writes and what ownership
# acquisition reads, and - crucially - BOTH sides take their read+write inside
# one `BEGIN IMMEDIATE` SQLite write transaction. SQLite serialises those, so the
# writer's "check reservation + register" and the bridge's "check writers +
# reserver" cannot interleave: one side commits first and the other sees it.
# The admission COMMIT is the ordering boundary; no transaction is held across a
# network call.
#
# The bridge's own dispatch uses `send_text_once`, which does not come through
# here, so the worker never blocks on its own POST.
#
# LIVENESS: a marker is not cleared merely because its TTL elapsed. The holder
# RENEWS it on a heartbeat while its POST is genuinely in flight, so a live
# writer keeps its marker fresh indefinitely; only a crashed writer's marker
# lapses (no renewal). A marker also carries a `released` flag so a clean exit
# clears it deterministically.
_MARKER_TABLE = (
    "CREATE TABLE IF NOT EXISTS twin_bridge_writes ("
    "account_id TEXT NOT NULL, fan_id INTEGER NOT NULL, holder TEXT NOT NULL,"
    "expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'held', "
    "PRIMARY KEY (account_id, fan_id, holder))"
)

_WRITER_OK = "ok"
_WRITER_RESERVED = "reserved"
_WRITER_UNAVAILABLE = "unavailable"

# Marker lifecycle: `held` is a live in-flight write (renewed by its holder);
# `unresolved` means the holder's provider outcome is UNKNOWN (a post-send
# exception or a death) and can only be cleared by explicit reconciliation. A
# `held` marker whose holder stopped renewing is NOT proof the chat is clear, so
# admission promotes it to `unresolved` rather than letting it lapse.
MARKER_HELD = "held"
MARKER_UNRESOLVED = "unresolved"


def sqlite_path() -> str | None:
    """Public accessor for the coordination SQLite path (None on other backends)."""
    return _sqlite_path()


def store_target() -> str | None:
    """Public accessor: SQLite file path or PostgreSQL URL (never log the latter)."""
    return _store_target()


def sql_dt(value: datetime) -> str:
    """Public datetime formatter matching SQLAlchemy's SQLite DateTime storage."""
    return _sql_dt(value)


def _write_ttl_s() -> float:
    raw = str(os.environ.get("TWIN_BRIDGE_WRITE_TTL_S", "")).strip()
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


def _write_renew_s() -> float:
    raw = str(os.environ.get("TWIN_BRIDGE_WRITE_RENEW_S", "")).strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    return value if value > 0 else max(0.5, _write_ttl_s() / 4.0)


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _sql_dt(value: datetime) -> str:
    """Format a datetime the way SQLAlchemy's SQLite DateTime column stores it.

    SQLite has no native datetime: SQLAlchemy writes `YYYY-MM-DD HH:MM:SS.ffffff`
    (space separator, no `T`). A raw comparison MUST use the same shape or the
    string ordering is wrong.
    """
    if value.tzinfo is not None:
        value = value.astimezone(tz=None).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _marker_expiry_iso(ttl_s: float | None = None) -> str:
    return (datetime.utcnow() + timedelta(seconds=_write_ttl_s() if ttl_s is None else ttl_s)).isoformat()


def _ensure_marker_table(con) -> None:
    """Create the marker table, and add the `state` column to an older one.

    PostgreSQL never runs runtime DDL: its table is created by migration 0092.
    """
    if isinstance(con, store.PgConnection):
        return
    con.execute(_MARKER_TABLE)
    try:
        con.execute(
            "ALTER TABLE twin_bridge_writes "
            "ADD COLUMN state TEXT NOT NULL DEFAULT 'held'"
        )
    except DB_ERRORS:
        pass  # already present


# holder -> (account_id, fan_id) for markers THIS process must keep renewing.
_HELD: dict[str, tuple[str, int]] = {}
_MARKERS_LOCK = threading.RLock()
_HEARTBEAT: dict[str, object] = {"thread": None}


def _heartbeat_loop() -> None:
    idle = 0
    while True:
        time.sleep(_write_renew_s())
        with _MARKERS_LOCK:
            held = dict(_HELD)
        if not held:
            idle += 1
            if idle >= 4:
                with _MARKERS_LOCK:
                    _HEARTBEAT["thread"] = None
                return
            continue
        idle = 0
        _renew_held_markers()


def _ensure_heartbeat() -> None:
    thread = _HEARTBEAT.get("thread")
    if thread is not None and getattr(thread, "is_alive", lambda: False)():
        return
    t = threading.Thread(target=_heartbeat_loop, name="twin-bridge-marker-heartbeat",
                         daemon=True)
    _HEARTBEAT["thread"] = t
    t.start()


def _renew_held_markers() -> None:
    """Re-stamp every marker THIS process still holds (keeps a live POST live)."""
    with _MARKERS_LOCK:
        held = dict(_HELD)
    if not held:
        return
    con = _durable_write_conn()
    if con is None:
        return
    try:
        expires = _marker_expiry_iso()
        for holder, (account, fan) in held.items():
            con.execute(
                "UPDATE twin_bridge_writes SET expires_at = ? "
                "WHERE account_id = ? AND fan_id = ? AND holder = ? AND state = ?",
                (expires, str(account), int(fan), holder, MARKER_HELD),
            )
        con.commit()
    except DB_ERRORS:
        pass
    finally:
        con.close()


def _track_holder(holder: str, account_id: object, fan_id: object) -> None:
    with _MARKERS_LOCK:
        _HELD[holder] = (str(account_id), int(fan_id))
    _ensure_heartbeat()


def _untrack_holder(holder: str) -> None:
    with _MARKERS_LOCK:
        _HELD.pop(holder, None)


def _durable_write_conn():
    path = _store_target()
    if not path:
        return None
    try:
        con = _connect(path, timeout=5.0)
    except DB_ERRORS:
        return None
    try:
        _ensure_marker_table(con)
        con.commit()
    except DB_ERRORS:
        con.close()
        return None
    return con


def _bridge_enabled_for(account_id: object) -> bool:
    """Is the bridge ENABLED for this account (so coordination must be enforced)?

    Used to decide fail-closed behaviour when coordination storage is
    unavailable. A disabled / out-of-scope bridge keeps ordinary Fastt send
    behaviour; an ENABLED bridge never silently falls back to process-local.
    """
    flag = str(os.environ.get("TWIN_BRIDGE_ENABLED", "")).strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    scope_account = str(os.environ.get("TWIN_BRIDGE_ACCOUNT_ID", "")).strip()
    return (not scope_account) or (scope_account == str(account_id))


def _writer_register(account_id: object, fan_id: object, holder: str, *,
                     broadcast: bool = False, skip_reservation: bool = False) -> str:
    """ATOMICALLY check the reservation and register a writer, or refuse.

    Both the conflict read and the marker insert happen inside one
    `BEGIN IMMEDIATE` transaction, so a sibling process's bridge reservation
    cannot slip between them. A broadcast registers account-wide (fan 0) and is
    refused while ANY conversation for the account is reserved; a targeted DM is
    refused only for its own conversation.
    """
    path = _store_target()
    if not path:
        return _WRITER_UNAVAILABLE
    try:
        con = _connect(path, timeout=5.0, isolation_level=None)
    except DB_ERRORS:
        return _WRITER_UNAVAILABLE
    try:
        _ensure_marker_table(con)
        con.execute("BEGIN IMMEDIATE")
        if not skip_reservation:
            if broadcast:
                row = con.execute(
                    "SELECT 1 FROM twin_bridge_conversations "
                    "WHERE account_id = ? AND reserved = ? LIMIT 1",
                    (str(account_id), True),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT 1 FROM twin_bridge_conversations "
                    "WHERE account_id = ? AND fan_id = ? AND reserved = ? LIMIT 1",
                    (str(account_id), int(fan_id), True),
                ).fetchone()
            if row is not None:
                con.execute("ROLLBACK")
                return _WRITER_RESERVED
        con.execute(
            "INSERT INTO twin_bridge_writes "
            "(account_id, fan_id, holder, expires_at, state) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (account_id, fan_id, holder) DO UPDATE SET "
            "expires_at = excluded.expires_at, state = excluded.state",
            (str(account_id), 0 if broadcast else int(fan_id), str(holder),
             _marker_expiry_iso(), MARKER_HELD),
        )
        con.execute("COMMIT")
        return _WRITER_OK
    except DB_ERRORS:
        try:
            con.execute("ROLLBACK")
        except DB_ERRORS:
            pass
        return _WRITER_UNAVAILABLE
    finally:
        con.close()


def _durable_write_enter(account_id: object, fan_id: object, holder: str,
                         ttl_s: float | None = None) -> None:
    """Publish (and track for renewal) a durable in-flight marker."""
    con = _durable_write_conn()
    if con is None:
        return
    try:
        con.execute(
            "INSERT INTO twin_bridge_writes "
            "(account_id, fan_id, holder, expires_at, state) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (account_id, fan_id, holder) DO UPDATE SET "
            "expires_at = excluded.expires_at, state = excluded.state",
            (str(account_id), int(fan_id), str(holder), _marker_expiry_iso(ttl_s),
             MARKER_HELD),
        )
        con.commit()
    except DB_ERRORS:
        pass
    finally:
        con.close()
    _track_holder(holder, account_id, fan_id)


def _durable_write_exit(account_id: object, fan_id: object, holder: str, *,
                        resolved: bool = True) -> None:
    """End this holder's marker: DELETE only for a PROVEN settled outcome.

    `resolved=False` keeps durable blocking evidence for a writer whose provider
    outcome is UNKNOWN (a post-send exception or a death): the marker becomes
    `unresolved` and is no longer renewed, so admission keeps refusing the chat
    until an explicit reconciliation. This is what stops an uncertain writer from
    being silently treated as settled.
    """
    _untrack_holder(holder)
    con = _durable_write_conn()
    if con is None:
        return
    try:
        if resolved:
            con.execute(
                "DELETE FROM twin_bridge_writes "
                "WHERE account_id = ? AND fan_id = ? AND holder = ?",
                (str(account_id), int(fan_id), str(holder)),
            )
        else:
            con.execute(
                "UPDATE twin_bridge_writes SET state = ? "
                "WHERE account_id = ? AND fan_id = ? AND holder = ?",
                (MARKER_UNRESOLVED, str(account_id), int(fan_id), str(holder)),
            )
        con.commit()
    except DB_ERRORS:
        pass
    finally:
        con.close()


def _durable_write_fresh(account_id: object, fan_id: object) -> bool:
    """True while any in-flight OR unresolved writer marker exists for the chat.

    A live writer renews its marker, so it is never cleared merely because time
    elapsed. A marker left by a crashed or uncertain writer is NOT proof the chat
    is clear, so it is still reported here and can only be removed by an explicit
    reconciliation (see `reconcile_writes`).
    """
    con = _durable_write_conn()
    if con is None:
        # No usable coordination store: we cannot prove the chat is clear, so a
        # caller must not treat it as clear.
        return True
    try:
        rows = con.execute(
            "SELECT 1 FROM twin_bridge_writes "
            "WHERE account_id = ? AND fan_id = ?",
            (str(account_id), int(fan_id)),
        ).fetchall()
    except DB_ERRORS:
        return True
    finally:
        con.close()
    return bool(rows)


def reconcile_writes(*, account_id: object, fan_id: object | None = None) -> int:
    """EXPLICITLY clear in-flight/unresolved writer markers for a conversation.

    The operator path for the `unresolved` state: an uncertain or crashed local
    writer must never be treated as settled automatically, so this is the only
    thing that releases its durable blocking evidence. Returns the number of
    markers cleared.
    """
    con = _durable_write_conn()
    if con is None:
        return 0
    try:
        if fan_id is None:
            cur = con.execute(
                "DELETE FROM twin_bridge_writes WHERE account_id = ?",
                (str(account_id),),
            )
        else:
            cur = con.execute(
                "DELETE FROM twin_bridge_writes "
                "WHERE account_id = ? AND fan_id = ?",
                (str(account_id), int(fan_id)),
            )
        cleared = int(cur.rowcount or 0)
        con.commit()
        return cleared
    except DB_ERRORS:
        return 0
    finally:
        con.close()


# ── the shared send boundary ──────────────────────────────────────────

def register_owner(account_id: object, fan_id: object, owner_token: str,
                   expires_at: datetime | None = None) -> None:
    with _REGISTRY_LOCK:
        key = _key(account_id, fan_id)
        _OWNERS[key] = str(owner_token)
        if expires_at is not None:
            _OWNER_EXPIRES[key] = expires_at
        _RESERVED.add(key)
        _ACQUIRING.discard(key)


def _begin_admission(account_id: object, fan_id: object, owner_token: str,
                     now: datetime) -> bool:
    """Atomically take the admission token, or refuse.

    Succeeds only when no local writer is between `guarded` and `settled`, no
    acquisition is already in progress, and no DIFFERENT LIVE owner holds the
    reservation. The check and the take happen under one lock, so an older writer
    entering `guarded_send` cannot pass the quiescence observation and then race
    the reservation. A DIFFERENT owner whose in-process claim has EXPIRED is not
    live: a legitimate replacement worker is allowed through, exactly as the
    durable `owner_expires_at` allows it.
    """
    key = _key(account_id, fan_id)
    with _REGISTRY_LOCK:
        if key in _ACQUIRING:
            return False
        if _IN_FLIGHT.get(key, 0) != 0:
            return False
        current = _OWNERS.get(key)
        if current is not None and current != str(owner_token):
            expires = _OWNER_EXPIRES.get(key)
            if expires is not None and expires > now:
                return False
        _ACQUIRING.add(key)
        return True


def _end_admission(account_id: object, fan_id: object) -> None:
    with _REGISTRY_LOCK:
        _ACQUIRING.discard(_key(account_id, fan_id))


def clear_owner(account_id: object, fan_id: object, owner_token: str) -> bool:
    """EXPLICIT release of the reservation, only for the current owner token.

    This is the only local path that drops the reservation; pause / attention /
    delivery-unknown keep it (their claim is cleared but the reservation stays).
    """
    with _REGISTRY_LOCK:
        key = _key(account_id, fan_id)
        if _OWNERS.get(key) != str(owner_token):
            return False
        _OWNERS.pop(key, None)
        _RESERVED.discard(key)
        _ACQUIRING.discard(key)
        _OWNER_EXPIRES.pop(key, None)
        _DISPATCH_AUTH.pop(key, None)
        _IN_FLIGHT.pop(key, None)
        return True


def clear_claim(account_id: object, fan_id: object) -> None:
    """Drop the expiring per-attempt claim but KEEP the durable reservation."""
    with _REGISTRY_LOCK:
        key = _key(account_id, fan_id)
        _OWNERS.pop(key, None)
        _OWNER_EXPIRES.pop(key, None)
        _DISPATCH_AUTH.pop(key, None)


def drop_reservation(account_id: object, fan_id: object) -> None:
    """Drop the reservation entirely (a recorded human takeover)."""
    with _REGISTRY_LOCK:
        key = _key(account_id, fan_id)
        _OWNERS.pop(key, None)
        _RESERVED.discard(key)
        _ACQUIRING.discard(key)
        _OWNER_EXPIRES.pop(key, None)
        _DISPATCH_AUTH.pop(key, None)


def is_owned(account_id: object, fan_id: object) -> bool:
    """True while the conversation is under the durable bridge reservation."""
    with _REGISTRY_LOCK:
        return _key(account_id, fan_id) in _RESERVED


def any_owned(account_id: object) -> bool:
    """True when the account has ANY reserved conversation."""
    aid = str(account_id)
    with _REGISTRY_LOCK:
        if any(acct == aid for acct, _fan in _RESERVED):
            return True
    return _durable_any_reserved(aid)


def _reserved(account_id: object, fan_id: object) -> bool:
    key = _key(account_id, fan_id)
    with _REGISTRY_LOCK:
        if key in _RESERVED:
            return True
    return _durable_reserved(*key)


def authorize_dispatch(account_id: object, fan_id: object, *, turn_id: str,
                       attempt_no: int, owner_token: str, revision: int) -> str:
    """Register the ONE authorization the bridge may POST under, and return its
    opaque token.

    Tied to the durable identity (turn / attempt / claim / revision), never to a
    `purpose` string. The transport compares the presented token against this
    registration, so a stale worker whose claim was replaced cannot dispatch.
    """
    import secrets
    token = "twb-" + secrets.token_hex(12)
    with _REGISTRY_LOCK:
        _DISPATCH_AUTH[_key(account_id, fan_id)] = (
            token, str(turn_id), int(attempt_no), str(owner_token), int(revision),
        )
    return token


def revoke_dispatch(account_id: object, fan_id: object, token: str) -> None:
    with _REGISTRY_LOCK:
        key = _key(account_id, fan_id)
        current = _DISPATCH_AUTH.get(key)
        if current is not None and current[0] == token:
            _DISPATCH_AUTH.pop(key, None)


def _authorized(account_id: object, fan_id: object, token: str | None) -> bool:
    if not token:
        return False
    with _REGISTRY_LOCK:
        current = _DISPATCH_AUTH.get(_key(account_id, fan_id))
    return current is not None and current[0] == token


def in_flight_count(account_id: object, fan_id: object) -> int:
    with _REGISTRY_LOCK:
        return _IN_FLIGHT.get(_key(account_id, fan_id), 0)


def _in_flight_drop(key: tuple[str, int]) -> None:
    """Decrement the in-flight count for `key`; caller holds `_REGISTRY_LOCK`."""
    left = _IN_FLIGHT.get(key, 0) - 1
    if left > 0:
        _IN_FLIGHT[key] = left
    else:
        _IN_FLIGHT.pop(key, None)


def _exception_proves_no_send(exc: BaseException) -> bool:
    """True only when an exception PROVES the provider created no message.

    The only transport failures that PROVE nothing was sent are a CONNECT
    timeout (the connection was never established), and a provider 4xx other
    than 408/429 (the provider answered and refused). Everything else - a read
    timeout, a general connection error, a 5xx, or an unknown error - leaves the
    outcome UNKNOWN, so the caller must NOT release its durable evidence.

    A general `ConnectionError` is deliberately NOT treated as proof: the
    standard Requests library (and curl_cffi's independent hierarchy) can raise
    it AFTER bytes were transmitted, so accepting it would clear the marker for
    a send that may have landed. Both libraries expose `ConnectTimeout` for the
    never-connected case, so that is the precise signal used here.
    """
    proven: list[type] = []
    for module_name in ("requests.exceptions", "curl_cffi.requests.exceptions"):
        try:
            module = __import__(module_name, fromlist=["exceptions"])
        except Exception:  # noqa: BLE001 - library may be absent
            continue
        connect_timeout = getattr(module, "ConnectTimeout", None)
        if isinstance(connect_timeout, type):
            proven.append(connect_timeout)
    if proven and isinstance(exc, tuple(proven)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status not in (408, 429) and 400 <= status < 500:
        return True
    return False


@contextmanager
def guarded_send(account_id: object, fan_id: object, *, purpose: str,
                 dispatch_token: str | None = None):
    """The boundary every local DM write passes through.

    * `purpose="twin_bridge"` is the bridge's own dispatch. It is allowed ONLY
      with the live attempt-bound `dispatch_token` from `authorize_dispatch`;
      there is no string-based bypass. It still counts as in-flight.
    * `purpose="broadcast"` is refused for an account that has ANY reserved
      conversation - process-local or durable - because the recipient coverage
      cannot be shown to exclude the reserved fan.
    * every other purpose is refused while the conversation is reserved, whether
      the reservation is in this process or only in the database (a restart).
    """
    key = _key(account_id, fan_id)
    with _REGISTRY_LOCK:
        process_reserved = key in _RESERVED
        acquiring = key in _ACQUIRING
        authorized = (
            purpose == OWNER_KIND
            and dispatch_token is not None
            and _DISPATCH_AUTH.get(key, (None,))[0] == dispatch_token
        )
        if purpose == OWNER_KIND and not authorized:
            raise DispatchNotAuthorized(
                "bridge dispatch needs a live attempt authorization"
            )
        if purpose == "broadcast" and not process_reserved:
            if any(acct == key[0] for acct, _fan in _RESERVED):
                raise BroadcastRefusedError(
                    "broadcast to a bridge-reserved account is refused"
                )
        if purpose == "broadcast" and process_reserved:
            raise BroadcastRefusedError(
                "broadcast to a bridge-reserved account is refused"
            )
        # The reservation is being taken RIGHT NOW: a competing write must lose
        # the quiescence->reservation race, so it is refused even before the
        # durable row flips.
        if purpose not in (OWNER_KIND,) and acquiring:
            raise BridgeOwnedError("conversation is being reserved by the Twin bridge")
        if purpose not in (OWNER_KIND,) and process_reserved:
            raise BridgeOwnedError("conversation is reserved for the Twin bridge")
        _IN_FLIGHT[key] = _IN_FLIGHT.get(key, 0) + 1
    # Register the in-flight write durably, ATOMICALLY with the reservation
    # check, so an OLDER writer in another process and this bridge's reservation
    # cannot both win. `purpose="broadcast"` registers account-wide (fan 0); the
    # bridge's own authorized dispatch skips the reservation check (it holds the
    # reservation by construction) but still publishes-and-clears a marker.
    holder = f"{os.getpid()}:{next(_WRITE_SEQ)}"
    verdict = _writer_register(
        account_id, fan_id, holder, broadcast=(purpose == "broadcast"),
        skip_reservation=(purpose == OWNER_KIND),
    )
    if verdict == _WRITER_RESERVED:
        with _REGISTRY_LOCK:
            _in_flight_drop(key)
        if purpose == "broadcast":
            raise BroadcastRefusedError("broadcast to a bridge-reserved account is refused")
        raise BridgeOwnedError("conversation is reserved for the Twin bridge")
    if verdict == _WRITER_UNAVAILABLE and _bridge_enabled_for(account_id):
        # Coordination storage is required for an ENABLED bridge. Its absence is
        # not proof the chat is clear, so refuse rather than silently sending.
        with _REGISTRY_LOCK:
            _in_flight_drop(key)
        raise BridgeOwnedError("Twin bridge coordination storage is unavailable")
    if verdict == _WRITER_OK:
        _track_holder(holder, account_id, 0 if purpose == "broadcast" else fan_id)
    registered = verdict == _WRITER_OK
    if purpose == "broadcast" and not process_reserved and any_owned(key[0]):
        # Durable account-wide reservation (a restart cleared the mirror).
        with _REGISTRY_LOCK:
            _in_flight_drop(key)
        _durable_write_exit(account_id, fan_id, holder)
        raise BroadcastRefusedError("broadcast to a bridge-reserved account is refused")
    if purpose != OWNER_KIND and not process_reserved and _durable_reserved(*key):
        # Restart: the durable reservation outlived the process registry.
        with _REGISTRY_LOCK:
            _in_flight_drop(key)
        _durable_write_exit(account_id, fan_id, holder)
        raise BridgeOwnedError("conversation is reserved for the Twin bridge")
    try:
        yield
    except BaseException as exc:
        # An exception during the POST does NOT prove the message was not
        # created, so only a PROVEN no-send releases the durable marker. The
        # bridge's own dispatch (OWNER_KIND) is excluded: its uncertainty is
        # already carried by the attempt ledger and the paused conversation, and
        # leaving a second, permanent marker would deadlock its own recovery.
        resolved = purpose == OWNER_KIND or _exception_proves_no_send(exc)
        if registered:
            _durable_write_exit(account_id, fan_id, holder, resolved=resolved)
        with _REGISTRY_LOCK:
            _in_flight_drop(key)
        raise
    else:
        if registered:
            _durable_write_exit(account_id, fan_id, holder, resolved=True)
        with _REGISTRY_LOCK:
            _in_flight_drop(key)


async def wait_for_quiescence(account_id: object, fan_id: object, *, timeout_s: float,
                              poll_s: float = 0.02) -> bool:
    """Wait until no local writer is between "guarded" and "settled".

    Returns False on timeout: an older local writer that already entered dispatch
    must settle (or be given up on) before the bridge may take over.

    This is process-local ONLY. Ownership admission uses
    `wait_for_quiescence_all` so an older writer in the CLI/Fastt runner process
    is also made to settle (or admission refuses).
    """
    deadline = _monotonic() + max(0.0, timeout_s)
    while True:
        if in_flight_count(account_id, fan_id) == 0:
            return True
        if _monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)


async def wait_for_quiescence_all(account_id: object, fan_id: object, *, timeout_s: float,
                                  poll_s: float = 0.02) -> bool:
    """Wait until no writer is in flight in THIS process OR a sibling process.

    A durable in-flight marker (see `_durable_write_*`) is written by whichever
    process runs the transport, so the documented `python -m twin_bridge.cli`
    runner and the Fastt process arbitrate against each other, not just against
    themselves.
    """
    deadline = _monotonic() + max(0.0, timeout_s)
    while True:
        if in_flight_count(account_id, fan_id) == 0 \
                and not _durable_write_fresh(account_id, fan_id):
            return True
        if _monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)


def _monotonic() -> float:
    import time
    return time.monotonic()


# ── durable conversation control ──────────────────────────────────────

async def ensure_conversation(
    session, *, account_id: str, fan_id: int, creator_slug: str, conversation_id: str,
    scope_fingerprint: str, now: datetime, send_budget_limit: int | None = None,
) -> TwinBridgeConversation:
    """Insert the conversation row if it is missing; never overwrite state."""
    await session.execute(
        dialect_insert(TwinBridgeConversation)
        .values(
            account_id=account_id,
            fan_id=int(fan_id),
            creator_slug=creator_slug,
            conversation_id=conversation_id,
            scope_fingerprint=scope_fingerprint,
            state=CONV_ACTIVE,
            revision=1,
            reserved=True,
            send_budget_limit=int(send_budget_limit or 0),
            send_budget_used=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
    )
    row = await session.get(TwinBridgeConversation, (account_id, int(fan_id)))
    assert row is not None  # just inserted or already present
    return row


def _reserve_atomic(account_id: object, fan_id: object, owner_token: str,
                    now: datetime, expires: datetime) -> bool | None:
    """ATOMICALLY check writers and take the durable reservation, or refuse.

    The writer-conflict read and the reservation UPDATE happen inside ONE
    `BEGIN IMMEDIATE` transaction, so a sibling process's writer registration
    cannot interleave: either the writer's txn committed first (this refuses) or
    this commits first (the writer's registration then sees the reservation).
    Returns None when there is no SQLite coordination store (the caller treats
    that as unavailable and refuses rather than silently downgrading).
    """
    path = _store_target()
    if not path:
        return None
    try:
        con = _connect(path, timeout=5.0, isolation_level=None)
    except DB_ERRORS:
        return False
    try:
        _ensure_marker_table(con)
        con.execute("BEGIN IMMEDIATE")
        writers = con.execute(
            "SELECT holder, fan_id, state, expires_at FROM twin_bridge_writes "
            "WHERE account_id = ? AND (fan_id = ? OR fan_id = 0)",
            (str(account_id), int(fan_id)),
        ).fetchall()
        if writers:
            # ANY marker blocks, not just a fresh one: a writer that stopped
            # renewing (crashed mid-POST) has an UNKNOWN outcome, so its marker is
            # promoted to `unresolved` and kept as evidence rather than being
            # allowed to lapse into "the chat is clear".
            for holder, writer_fan, state, expires_at in writers:
                if state == MARKER_HELD:
                    try:
                        stale = datetime.fromisoformat(str(expires_at)) <= datetime.utcnow()
                    except (TypeError, ValueError):
                        stale = True
                    if stale:
                        con.execute(
                            "UPDATE twin_bridge_writes SET state = ? "
                            "WHERE account_id = ? AND fan_id = ? AND holder = ?",
                            (MARKER_UNRESOLVED, str(account_id), int(writer_fan), holder),
                        )
            con.execute("COMMIT")
            return False
        cur = con.execute(
            "UPDATE twin_bridge_conversations SET "
            "reserved = ?, owner_token = ?, owner_expires_at = ?, "
            "revision = revision + (CASE WHEN reserved THEN 0 ELSE 1 END), "
            "updated_at = ? "
            "WHERE account_id = ? AND fan_id = ? AND state = ? "
            "AND (owner_token IS NULL OR owner_token = ? "
            "     OR owner_expires_at IS NULL OR owner_expires_at <= ?)",
            (True, str(owner_token), _sql_dt(expires), _sql_dt(now),
             str(account_id), int(fan_id), CONV_ACTIVE,
             str(owner_token), _sql_dt(now)),
        )
        got = (cur.rowcount or 0) > 0
        con.execute("COMMIT" if got else "ROLLBACK")
        return got
    except DB_ERRORS:
        try:
            con.execute("ROLLBACK")
        except DB_ERRORS:
            pass
        return False
    finally:
        con.close()


async def reserve_ownership(
    *, account_id: str, fan_id: int, owner_token: str, now: datetime,
    ttl_s: float = DEFAULT_OWNER_TTL_S, quiesce_timeout_s: float = 5.0,
) -> bool:
    """Take the durable bridge reservation for one conversation.

    Refuses while a local writer is still in flight, and refuses a paused /
    attention / disabled conversation: a paused conversation must never fall back
    to the older automation writers, and it must not quietly resume.

    The durable `reserved` flag is set on EVERY acquisition, so a paused /
    unknown / restarted conversation stays reserved until an EXPLICIT release.
    The revision fence advances only when the conversation is being REOPENED
    (it had been released), never on a plain claim renewal or a legitimate
    process replacement - so an unchanged, already-accepted draft survives a
    restart instead of being parked as obsolete.
    """
    if not await wait_for_quiescence_all(account_id, fan_id, timeout_s=quiesce_timeout_s):
        return False
    # ADMISSION: take the token atomically with the in-flight re-check. An older
    # writer that entered transport after the wait above, or that enters now,
    # sees `_ACQUIRING` (or a non-zero in-flight count) and is refused, so the
    # observation and the reservation cannot be split by a racing write.
    if not _begin_admission(account_id, fan_id, owner_token, now):
        return False
    expires = now + timedelta(seconds=ttl_s)
    # The writer-conflict check and the reservation write are ONE atomic SQLite
    # transaction (the admission COMMIT is the ordering boundary), so a sibling
    # process's writer registration serialises against this instead of racing
    # between a separate read and write. A missing coordination store returns
    # None and is refused: an ENABLED bridge never silently degrades.
    got = _reserve_atomic(account_id, fan_id, owner_token, now, expires)
    if not got:
        _end_admission(account_id, fan_id)
        return False
    register_owner(account_id, fan_id, owner_token, expires)
    return True


async def release_ownership(
    *, account_id: str, fan_id: int, owner_token: str, now: datetime,
) -> bool:
    """EXPLICITLY release the local + durable reservation for the current owner.

    A stale worker's release matches nothing (its token is no longer current), so
    it cannot tear down a replacement worker's reservation. The row - and any
    unresolved delivery evidence on it - is preserved; only the reservation flag
    is cleared.
    """
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
                TwinBridgeConversation.owner_token == str(owner_token),
            )
            .values(owner_token=None, owner_expires_at=None, reserved=False, updated_at=now)
        )
        released = (res.rowcount or 0) > 0
    clear_owner(account_id, fan_id, owner_token)
    return released


async def set_conversation_state(
    *, account_id: str, fan_id: int, state: str, reason: str | None, now: datetime,
    reason_expected: str | None = None,
) -> bool:
    """Pause / resume / flag a conversation, advancing the revision fence.

    Clearing the owner token here is what makes a takeover stick: the previous
    worker's conditional writes all carry the old revision and stop matching.
    """
    async with get_session() as session:
        stmt = (
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
            )
            .values(
                state=state,
                reason=reason,
                revision=TwinBridgeConversation.revision + 1,
                owner_token=None,
                owner_expires_at=None,
                updated_at=now,
            )
        )
        if reason_expected is not None:
            stmt = stmt.where(TwinBridgeConversation.state == reason_expected)
        res = await session.execute(stmt)
        changed = (res.rowcount or 0) > 0
    if changed:
        # The claim is gone, but the RESERVATION survives: a paused / attention
        # conversation is still bridge-owned and must keep excluding competitors.
        clear_claim(account_id, fan_id)
    return changed


async def pause_conversation(*, account_id: str, fan_id: int, reason: str, now: datetime) -> bool:
    return await set_conversation_state(
        account_id=account_id, fan_id=fan_id, state=CONV_PAUSED, reason=reason, now=now,
    )


async def resume_conversation(*, account_id: str, fan_id: int, now: datetime) -> bool:
    """Resume a paused conversation, opening a FRESH-INPUT boundary.

    `reset_at` is stamped so the worker tombstones any unresolved work that was
    frozen BEFORE the resume: a resumed conversation admits only inputs that
    arrive after it, and never sends the old backlog. The reservation is set so
    the conversation stays bridge-owned and still excludes competitors.
    """
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
                TwinBridgeConversation.state == CONV_PAUSED,
            )
            .values(
                state=CONV_ACTIVE, reason=None, reserved=True, reset_at=now,
                owner_token=None, owner_expires_at=None,
                revision=TwinBridgeConversation.revision + 1, updated_at=now,
            )
        )
        changed = (res.rowcount or 0) > 0
    if changed:
        clear_claim(account_id, fan_id)
    return changed


async def mark_cancelled(*, account_id: str, fan_id: int, now: datetime) -> bool:
    """Record a durable cancellation intent for a conversation's unsent work.

    Sets `cancelled_at` (and pauses) so the worker treats every pre-existing
    unresolved turn as cancelled and tombstoned, and any late Twin completion
    cannot re-open it. The reservation is kept.
    """
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
            )
            .values(
                state=CONV_PAUSED, reason="operator-cancel", reserved=True,
                cancelled_at=now, owner_token=None, owner_expires_at=None,
                revision=TwinBridgeConversation.revision + 1, updated_at=now,
            )
        )
        changed = (res.rowcount or 0) > 0
    if changed:
        clear_claim(account_id, fan_id)
    return changed


async def take_takeover(*, account_id: str, fan_id: int, now: datetime) -> bool:
    """A human send in a bridge-owned chat requests takeover BEFORE the transport.

    Bumps the revision and pauses the conversation, which invalidates any unsent
    draft, and RELEASES the reservation so the human's own send is not refused
    at the transport boundary. An attempt that already dispatched keeps its
    evidence (the caller must not pretend pause recalls it).

    This is the ARBITRATION, not a check-then-act: the single conditional UPDATE
    refuses when any bridge provider attempt is in flight or its outcome is
    unknown FOR THIS FAN, checked in the same statement. So a worker that has
    already committed a `dispatching` attempt wins the race: takeover returns
    False and the caller must refuse the manual send rather than clear the
    reservation underneath a live POST.
    """
    no_live_attempt = ~(
        select(TwinBridgeSendAttempt.id)
        .where(
            TwinBridgeSendAttempt.account_id == str(account_id),
            TwinBridgeSendAttempt.fan_id == int(fan_id),
            TwinBridgeSendAttempt.state.in_(["dispatching", "delivery_unknown"]),
        )
        .exists()
    )
    no_live_turn = ~(
        select(TwinBridgeTurn.turn_id)
        .where(
            TwinBridgeTurn.account_id == str(account_id),
            TwinBridgeTurn.fan_id == int(fan_id),
            TwinBridgeTurn.state.in_(["dispatching", "delivery_unknown"]),
        )
        .exists()
    )
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
                no_live_attempt,
                no_live_turn,
            )
            .values(
                state=CONV_PAUSED,
                reason="human-takeover",
                revision=TwinBridgeConversation.revision + 1,
                owner_token=None,
                owner_expires_at=None,
                takeover_at=now,
                reserved=False,
                updated_at=now,
            )
        )
        changed = (res.rowcount or 0) > 0
    if changed:
        drop_reservation(account_id, fan_id)
    return changed


# ── turn claims ───────────────────────────────────────────────────────

TAKEOVER_CLEAR = "clear"
TAKEOVER_RECORDED = "takeover"
TAKEOVER_BLOCKED = "blocked"


async def human_takeover(*, account_id: str, fan_id: int, now: datetime,
                         wait_timeout_s: float = 5.0) -> str:
    """Record a human takeover of a bridge-owned chat BEFORE a manual send.

    Returns one of:

    * ``"clear"``    - the bridge holds no reservation; the human may send.
    * ``"takeover"`` - the reservation was released and the revision fenced, so
      unsent bridge work is invalidated and the human may send.
    * ``"blocked"``  - a bridge provider attempt is in flight or its outcome is
      UNKNOWN. The human must not race it: the caller refuses the send rather
      than clearing the reservation underneath a live dispatch.

    A confirmed/unknown delivery is never erased here; only the reservation and
    the claim are cleared, and only once no attempt is mid-flight.
    """
    from .delivery import TwinBridgeSendAttempt as _Attempt  # local: avoid a cycle

    async with get_session() as session:
        row = await session.get(TwinBridgeConversation, (str(account_id), int(fan_id)))
        if row is None:
            return TAKEOVER_CLEAR
        reserved = bool(row.reserved)
        unresolved = await unresolved_turn(session, account_id, int(fan_id))
    if not reserved and unresolved is None:
        return TAKEOVER_CLEAR

    # A published in-flight/unknown attempt is a hard block: the human is refused
    # rather than clearing the reservation underneath a live dispatch.
    async with get_session() as session:
        attempt = (
            await session.execute(
                select(_Attempt.id).where(
                    _Attempt.account_id == str(account_id),
                    _Attempt.fan_id == int(fan_id),
                    _Attempt.state.in_(["dispatching", "delivery_unknown"]),
                ).limit(1)
            )
        ).scalars().first()
    if attempt is not None:
        return TAKEOVER_BLOCKED
    if unresolved is not None and unresolved.state in ("dispatching", "delivery_unknown"):
        return TAKEOVER_BLOCKED

    # Wait for an ordinary in-flight writer to settle first - in THIS process or
    # a sibling process (the CLI/Fastt runner), whichever ran the transport.
    if not await wait_for_quiescence_all(account_id, fan_id, timeout_s=wait_timeout_s):
        return TAKEOVER_BLOCKED

    # The takeover UPDATE itself is conditional on there being no live attempt,
    # so if a worker slipped a `dispatching` attempt in during the wait above,
    # this loses the arbitration and the caller must refuse the manual send.
    changed = await take_takeover(account_id=str(account_id), fan_id=int(fan_id), now=now)
    return TAKEOVER_RECORDED if changed else TAKEOVER_BLOCKED


async def claim_turn(
    *, account_id: str, fan_id: int, turn_id: str, owner_token: str, now: datetime,
    expected_state: str, ttl_s: float = DEFAULT_OWNER_TTL_S,
) -> bool:
    """Take (or renew) ownership of one turn with an atomic conditional UPDATE."""
    expires = now + timedelta(seconds=ttl_s)
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeTurn)
            .where(
                TwinBridgeTurn.account_id == str(account_id),
                TwinBridgeTurn.fan_id == int(fan_id),
                TwinBridgeTurn.turn_id == str(turn_id),
                TwinBridgeTurn.state == expected_state,
            )
            .where(
                (TwinBridgeTurn.owner_token.is_(None))
                | (TwinBridgeTurn.owner_token == str(owner_token))
                | (TwinBridgeTurn.owner_expires_at <= now)
            )
            .values(
                owner_token=str(owner_token),
                owner_expires_at=expires,
                revision=TwinBridgeTurn.revision + 1,
                updated_at=now,
            )
        )
        return (res.rowcount or 0) > 0


async def release_turn(*, account_id: str, fan_id: int, turn_id: str, owner_token: str,
                       now: datetime) -> bool:
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeTurn)
            .where(
                TwinBridgeTurn.account_id == str(account_id),
                TwinBridgeTurn.fan_id == int(fan_id),
                TwinBridgeTurn.turn_id == str(turn_id),
                TwinBridgeTurn.owner_token == str(owner_token),
            )
            .values(owner_token=None, owner_expires_at=None, updated_at=now)
        )
        return (res.rowcount or 0) > 0


async def load_conversation(session, account_id: str, fan_id: int) -> TwinBridgeConversation | None:
    return await session.get(TwinBridgeConversation, (account_id, int(fan_id)))


async def set_budget_limit(*, account_id: str, fan_id: int, limit: int, now: datetime) -> None:
    """Apply the configured pilot send limit to a conversation.

    The persisted cap is the durable safety bound and is never silently RAISED:
    a restart (or a different CLI invocation) with a larger configured number
    takes the MINIMUM of what is already persisted and the new value, so it can
    only tighten - never widen - what the pilot may spend. It is never lowered
    below what has already been consumed, so an unknown/in-flight attempt cannot
    be made to look unspent.
    """
    if int(limit) <= 0:
        return
    async with get_session() as session:
        current = await session.get(TwinBridgeConversation, (str(account_id), int(fan_id)))
        if current is None:
            return
        existing = int(current.send_budget_limit or 0)
        effective = int(limit) if existing <= 0 else min(existing, int(limit))
        effective = max(effective, int(current.send_budget_used or 0))
        if effective == existing:
            return
        await session.execute(
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
            )
            .values(send_budget_limit=effective, updated_at=now)
        )


async def unresolved_turn(session, account_id: str, fan_id: int) -> TwinBridgeTurn | None:
    """The one unresolved turn blocking this conversation, if any.

    Only one turn per conversation may proceed to generation/delivery, so while
    this returns a row every newer turn waits.
    """
    stmt = (
        select(TwinBridgeTurn)
        .where(
            TwinBridgeTurn.account_id == str(account_id),
            TwinBridgeTurn.fan_id == int(fan_id),
            TwinBridgeTurn.state.notin_(["delivered", "cancelled", "parked", "attention"]),
        )
        .order_by(TwinBridgeTurn.created_at, TwinBridgeTurn.turn_id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()
