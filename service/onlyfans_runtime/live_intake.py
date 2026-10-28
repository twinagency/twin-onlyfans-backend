"""Permanent live intake over the existing OnlyFans WebSocket (off unless flagged).

Scope is deliberately narrow: new messages only, plus a targeted read for a gap.
Nothing here starts an archive, whole-account or discovery pull. Every provider
read still goes through ``provider_read_budget``. The socket never records a
purchase, never proves an ambiguous send, and a reconnect is never proof of
completeness: a disconnect marks only the approved fans as needing their own
bounded recent read before they can be current again.

Layout
* ``normalize_frame``     pure frame -> full message | partial notification.
* ``NativeWsAuthClient``  the small client ``of_ws.OFWebSocket`` needs; it uses
                          only the configured account's sealed session (the
                          worker's ``NativeProviderConnector``) and the read budget.
* ``IngestionOwner``      one Postgres session-level advisory lock on a dedicated
                          connection; losing that connection means ownership lost.
* ``LiveIntake``          a process-wide runner: one asyncio thread for sockets, one
                          writer thread (own ``Store``) so worker model calls and
                          synchronous DB work never block the event loop, plus the
                          bounded targeted polling fallback and background fairness.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_models import OfRecovery
from .native_protocol import API_PATH, NativeProtocolError, load, message
from .store import Event, PAGE_LIMIT, Refused, Store, _id, fan_current_override, utcnow

FLAG = "ONLYFANS_LIVE_INTAKE_ENABLED"
log = logging.getLogger("of-live-intake")
ws_log = logging.getLogger("of-ws")

HINT_KEYS = frozenset({"new_message", "chat_message", "chat_messages", "chat_new_message"})


def enabled():
    return os.environ.get(FLAG) == "1"


# ── Pure frame normalization ──────────────────────────────────────────────

@dataclass(frozen=True)
class Frame:
    kind: str                      # "message" | "partial" | "ignore"
    event: Event | None = None
    fan_id: int | None = None


def _fan_of(value, owner_id):
    """Conversation partner of a raw message dict, or None when not derivable."""
    try:
        sender = _id((value.get("fromUser") or {}).get("id"))
        if sender != owner_id:
            return sender
        return _id((value.get("toUser") or {}).get("id"))
    except (Refused, AttributeError, TypeError):
        return None


def normalize_frame(frame, owner_id):
    """Only a complete, strictly valid message becomes a record. Anything that
    announces a message but cannot be fully validated is a *partial*: the caller
    schedules a targeted read and never guesses a record."""
    if type(frame) is not dict:
        return Frame("ignore")
    value = frame.get("api2_chat_message")
    if value is not None:
        if type(value) is not dict:
            return Frame("partial")
        fan = _fan_of(value, owner_id)
        if fan is None or fan == owner_id:
            return Frame("partial")
        try:
            return Frame("message", message(value, owner_id=owner_id, fan_id=fan), fan)
        except NativeProtocolError:
            return Frame("partial", None, fan)
    for key in HINT_KEYS:
        if key in frame:
            hint = frame[key]
            fan = _fan_of(hint, owner_id) if type(hint) is dict else None
            return Frame("partial", None, fan)
    return Frame("ignore")


# ── Redaction ─────────────────────────────────────────────────────────────

_SECRETS: set[str] = set()
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")


def remember_secrets(*values):
    for value in values:
        if isinstance(value, str) and len(value) >= 6:
            _SECRETS.add(value)


def scrub(text):
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return _JWT.sub("[redacted]", _USERINFO.sub(r"\1[redacted]@", text))


class RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = None
                record.exc_info = None
        except Exception:
            record.msg, record.args = "[unprintable log record]", ()
        return True


# ── Auth client for OFWebSocket ───────────────────────────────────────────

def _production_url_ok(url):
    return (type(url) is str and len(url) <= 2048 and url.isascii()
            and re.fullmatch(r"wss://[A-Za-z0-9.-]*onlyfans\.com(:443)?/[A-Za-z0-9/_.\-]*", url) is not None)


class NativeWsAuthClient:
    """Duck-typed client for ``OFWebSocket``: auth + route from one sealed session.

    ``me()`` is the only network call. It runs under the shared provider read
    budget (kind ``recent``: leases, rate-limit backoff and the 2-token reserve
    all apply) and returns only the WS URL and token. The proxy is the account's
    fixed route; there is never a direct fallback when one is assigned.
    """
    def __init__(self, store, tenant, connector, revision, *, url_ok=_production_url_ok):
        self.store, self.tenant, self.connector, self.revision = store, tenant, connector, revision
        self.url_ok = url_ok
        self.fatal = False
        handle = connector._handle
        self.user_agent = handle.user_agent
        self.proxy_url = handle.proxy_url
        # Never expose the proxy URL (credentials) to of_ws's log line.
        self.proxy_label = "account-fixed-proxy" if handle.proxy_url else None
        remember_secrets(handle.proxy_url, handle.x_bc, handle.x_hash, *(value for _, value in handle.cookies))

    def me(self):
        from .provider_connector import NativeAuthenticationError
        from .provider_read_budget import acquire, current, native_reader, release
        connector = self.connector
        budget = error = None
        try:
            if native_reader(connector):
                budget = acquire(self.store, self.tenant, connector.account_id,
                                 connection_revision=self.revision, kind="recent")
            value = load(connector._call("GET", API_PATH + "/users/me"))
            if type(value.get("id")) is not int or value["id"] != connector._handle.owner_id:
                raise NativeAuthenticationError("native provider owner mismatch")
            url, token = value.get("wsUrl"), value.get("wsAuthToken")
            if (type(token) is not str or not 8 <= len(token) <= 8192 or not self.url_ok(url)):
                raise NativeProtocolError("native websocket route is unavailable")
            remember_secrets(token)
            if budget is not None:
                current(self.store, self.tenant, budget)
            return {"wsUrl": url, "wsAuthToken": token}
        except NativeAuthenticationError as failure:
            error = failure
            self.fatal = True
            raise
        except BaseException as failure:
            error = failure
            raise
        finally:
            if budget is not None:
                release(self.store, self.tenant, budget, retry_after=getattr(error, "retry_after", None))


# ── Single ingestion owner across processes ───────────────────────────────

def lock_key(tenant, account):
    return int.from_bytes(hashlib.sha256(f"of-live-intake:{tenant}:{account}".encode()).digest()[:8], "big", signed=True)


class IngestionOwner:
    """Session-level advisory lock on a dedicated connection (not the Store pool).

    The connection IS the lease: if it drops (crash, kill, network) PostgreSQL
    releases the lock and ``alive()`` turns False, so the holder stops its socket.
    """
    def __init__(self, url, tenant, account):
        from sqlalchemy.engine import make_url
        self._dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
        self.key = lock_key(tenant, account)
        self._conn = None

    def acquire(self):
        import psycopg2
        if self._conn is not None:
            return self.alive()
        conn = psycopg2.connect(self._dsn, connect_timeout=5, keepalives=1, keepalives_idle=10,
                                keepalives_interval=5, keepalives_count=3,
                                options="-c statement_timeout=5000 -c timezone=UTC")
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (self.key,))
                got = cur.fetchone()[0]
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self._conn = conn
        return True

    def alive(self):
        conn = self._conn
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted AND pid=pg_backend_pid()"
                            " AND classid::bigint=%s AND objid::bigint=%s AND objsubid=1",
                            ((self.key >> 32) & 0xFFFFFFFF, self.key & 0xFFFFFFFF))
                return cur.fetchone()[0] == 1
        except Exception:
            self._drop()
            return False

    def _drop(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def release(self):
        conn = self._conn
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
            except Exception:
                pass
        self._drop()


# ── Targeted gap / read requests (no scan, no archive) ────────────────────

def approved_fans(s, account_id):
    """Automated, not-taken-over fans plus the explicit override list."""
    from db.models import OfConversationState
    rows = s.scalars(select(OfConversationState.fan_id).where(
        OfConversationState.account_id == account_id, OfConversationState.mode == "automated",
        OfConversationState.takeover_state == "none").order_by(OfConversationState.fan_id).limit(1000)).all()
    return sorted(set(rows) | fan_current_override(account_id))


def _known_high(s, account_id, fan_id):
    return s.scalar(select(func.max(Message.message_id)).where(
        Message.account_id == account_id, Message.fan_id == fan_id)) or 0


def _need_fan(s, account_id, fan_id):
    """Mark ONE fan as needing its own bounded recent read. A fan without a
    recovery row gets one whose high-water is its newest stored message, so the
    read stops there instead of walking the archive."""
    row = s.get(OfRecovery, (account_id, fan_id))
    if row is None:
        s.add(OfRecovery(account_id=account_id, fan_id=fan_id, status="needed",
                         high_water=_known_high(s, account_id, fan_id)))
    elif row.status == "complete":
        row.status = "needed"
        row.upper_bound = row.before_id = None


def request_fan_reads(store, tenant, account_id, fan_ids=None, *, reason="gap"):
    """Durably record a gap: ``recovery_needed`` plus a per-fan read request for
    the approved fans only (or the given subset). Needs no OfScan cycle."""
    with Session(store.engine) as s, s.begin():
        runtime = Store._runtime(s, tenant, account_id)
        targets = approved_fans(s, account_id)
        if fan_ids is not None:
            targets = [fan for fan in targets if fan in set(fan_ids)]
        for fan in targets:
            _need_fan(s, account_id, fan)
        if targets or fan_ids is None:
            runtime.recovery_needed = True
        return len(targets)


# ── Read health: bounded diagnostics + backoff ────────────────────────────

BACKOFF_BASE = 30
BACKOFF_MAX = 900


class ReadHealth:
    def __init__(self, clock=time.monotonic):
        self.clock, self._rows, self._lock = clock, {}, threading.Lock()

    def fail(self, account, fan, error):
        oversized = "byte budget" in str(error)
        with self._lock:
            count = self._rows.get((account, fan), (0, 0, False))[0] + 1
            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (count - 1))
            self._rows[(account, fan)] = (count, self.clock() + delay, oversized)
            if len(self._rows) > 4096:
                self._rows.pop(next(iter(self._rows)))
        if count & (count - 1) == 0:   # log the 1st, 2nd, 4th, 8th ... failure only
            log.warning("live intake read failed account=%s fan=%s class=%s oversized=%s failures=%d retry_in=%ds",
                        account, fan, type(error).__name__, oversized, count, delay)
        return delay

    def ok(self, account, fan):
        with self._lock:
            self._rows.pop((account, fan), None)

    def blocked(self, account, fan):
        with self._lock:
            row = self._rows.get((account, fan))
            return row is not None and row[1] > self.clock()

    def snapshot(self, limit=50):
        with self._lock:
            return {key: {"failures": v[0], "retry_in": max(0, round(v[1] - self.clock())), "oversized": v[2]}
                    for key, v in list(self._rows.items())[:limit]}


# ── The runner ────────────────────────────────────────────────────────────

class _Pump:
    def __init__(self, account, connector, revision):
        self.account, self.connector, self.revision = account, connector, revision
        self.last_seen = time.monotonic()
        self.stop_flag = False
        self.owner_lost = False
        self.connected = False
        self.gap_open = False
        self.binding_id = None
        self.ws = None
        self.client = None
        self.task = None
        self.done = False


_STOP = object()
_REGISTRY: dict[str, "LiveIntake"] = {}
_REGISTRY_LOCK = threading.Lock()


class LiveIntake:
    def __init__(self, store, tenant, *, queue_size=2000, poll_interval=20.0, polls_per_cycle=3,
                 poll_limit=20, background_every=5, watch_seconds=2.0, acquire_retry=5.0, restart_delay=3.0,
                 idle_seconds=90.0, scope_seconds=5.0, ws_factory=None, owner_factory=None, url_ok=_production_url_ok,
                 clock=time.monotonic):
        if type(store) is not Store or os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("live intake requires an explicit worker Store")
        self.store, self.tenant = store, tenant
        self._url = store.engine.url.render_as_string(hide_password=False)
        self.queue_size, self.poll_interval, self.polls_per_cycle = queue_size, poll_interval, polls_per_cycle
        self.poll_limit, self.background_every = min(poll_limit, PAGE_LIMIT), background_every
        self.watch_seconds, self.acquire_retry, self.restart_delay = watch_seconds, acquire_retry, restart_delay
        self.idle_seconds, self.url_ok, self.clock = idle_seconds, url_ok, clock
        self.scope_seconds = scope_seconds
        self._ws_factory = ws_factory or self._default_ws
        self._owner_factory = owner_factory or (lambda account: IngestionOwner(self._url, tenant, account))
        self.health = ReadHealth(clock)
        self._q = queue.Queue(queue_size)
        self._lock = threading.Lock()
        self._loop = self._thread = self._writer = self._wstore = None
        self._pumps: dict[str, _Pump] = {}
        self._retry_gap: set[str] = set()
        self._forced: dict[str, set] = {}
        self._last_poll: dict[tuple, float] = {}
        self._starve: dict[str, int] = {}
        self._overflow: set[str] = set()
        self.stats = {"events": 0, "partials": 0, "ignored": 0, "dropped": 0, "gaps": 0, "polled": 0, "connects": 0}
        self._closed = False
        ws_log.addFilter(RedactFilter())

    # -- registry ---------------------------------------------------------
    @classmethod
    def shared(cls, store, tenant, **kwargs):
        with _REGISTRY_LOCK:
            found = _REGISTRY.get(tenant)
            if found is None or found._closed or found.store is not store:
                found = _REGISTRY[tenant] = cls(store, tenant, **kwargs)
            return found

    @staticmethod
    def shutdown_all():
        with _REGISTRY_LOCK:
            items = list(_REGISTRY.values())
            _REGISTRY.clear()
        for item in items:
            item.close()

    @staticmethod
    def _default_ws(client, on_state):
        from of_ws import OFWebSocket
        return OFWebSocket(client, on_state=on_state)

    # -- thread bootstrap -------------------------------------------------
    def _ensure_started(self):
        with self._lock:
            if self._closed:
                raise Refused("live intake is closed")
            if self._loop is None:
                self._wstore = Store(self._url)       # own engine: never competes with the worker pool
                self._loop = asyncio.new_event_loop()
                ready = threading.Event()
                def run():
                    asyncio.set_event_loop(self._loop)
                    self._loop.call_soon(ready.set)
                    self._loop.run_forever()
                self._thread = threading.Thread(target=run, name="of-live-intake", daemon=True)
                self._thread.start()
                ready.wait(5)
                self._writer = threading.Thread(target=self._writer_loop, name="of-live-intake-writer", daemon=True)
                self._writer.start()

    def close(self, timeout=10.0):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
        if loop is not None:
            async def stop_all():
                pumps = list(self._pumps.values())
                for pump in pumps:
                    pump.stop_flag = True
                    if pump.ws is not None:
                        pump.ws.stop()
                tasks = [pump.task for pump in pumps if pump.task is not None]
                if tasks:
                    await asyncio.wait(tasks, timeout=timeout)
            try:
                asyncio.run_coroutine_threadsafe(stop_all(), loop).result(timeout + 1)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            self._thread.join(timeout)
            self._q.put(_STOP)
            self._writer.join(timeout)
            if self._wstore is not None:
                self._wstore.close()

    # -- worker-thread API ------------------------------------------------
    @staticmethod
    def eligible(account):
        from .provider_connector import NativeProviderConnector
        revision = getattr(account.transport, "connection_revision", None)
        return (account.model is not None and account.read_connection_revision is None
                and type(account.transport) is NativeProviderConnector
                and type(revision) is int and revision >= 1)

    def sync(self, account):
        """Called every worker step: start / keep / rebind this account's socket."""
        if not self.eligible(account):
            return False
        self._ensure_started()
        transport, revision = account.transport, account.transport.connection_revision
        self._loop.call_soon_threadsafe(self._ensure_pump, account.account_id, transport, revision)
        return True

    def active_accounts(self):
        return sorted(key for key, pump in list(self._pumps.items()) if not pump.done)

    def owned(self, account):
        pump = self._pumps.get(account)
        return bool(pump and pump.ws is not None and not pump.done)

    def submit(self, account, frame, owner_id):
        """Hand one raw frame to the writer (also the test seam). Never blocks."""
        result = normalize_frame(frame, owner_id)
        if result.kind == "ignore":
            self.stats["ignored"] += 1
            return result
        try:
            self._q.put_nowait((result.kind, account, result))
        except queue.Full:
            # Cannot hold it; the next writer pass records a gap for approved fans.
            self.stats["dropped"] += 1
            self._overflow.add(account)
        return result

    def enqueue_gap(self, account, reason):
        try:
            self._q.put_nowait(("gap", account, reason))
        except queue.Full:
            self._overflow.add(account)

    def flush(self, timeout=10.0):
        """Wait until the writer has handled everything queued so far (tests/ops)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._q.unfinished_tasks == 0 and not self._retry_gap and not self._overflow:
                return True
            time.sleep(.02)
        return False

    # -- asyncio side ------------------------------------------------------
    def _ensure_pump(self, account, connector, revision):
        pump = self._pumps.get(account)
        if pump is not None and not pump.done:
            if pump.revision == revision and not pump.stop_flag:
                pump.connector, pump.last_seen = connector, time.monotonic()
                if pump.client is not None:
                    pump.client.connector = connector
                return
            pump.stop_flag = True        # session/binding generation changed: close and rebind
            if pump.ws is not None:
                pump.ws.stop()
        pump = _Pump(account, connector, revision)
        self._pumps[account] = pump
        pump.task = self._loop.create_task(self._pump_main(pump))

    async def _sleep(self, pump, seconds):
        end = time.monotonic() + seconds
        while not pump.stop_flag and time.monotonic() < end:
            await asyncio.sleep(min(.1, max(0, end - time.monotonic())))

    async def _pump_main(self, pump):
        try:
            while not pump.stop_flag:
                owner = self._owner_factory(pump.account)
                try:
                    got = await asyncio.to_thread(owner.acquire)
                except Exception:
                    got = False
                if not got:
                    if time.monotonic() - pump.last_seen > self.idle_seconds:
                        pump.stop_flag = True
                    await self._sleep(pump, self.acquire_retry)
                    continue
                try:
                    await self._run_owned(pump, owner)
                except asyncio.CancelledError:
                    raise
                except Exception as failure:
                    log.warning("live intake pump ended account=%s class=%s", pump.account, type(failure).__name__)
                finally:
                    await asyncio.to_thread(owner.release)
                if not pump.stop_flag:
                    await self._sleep(pump, self.restart_delay)
        finally:
            pump.done = True

    def _scope(self, pump):
        """Current sealed-session generation and verified binding, or Refused."""
        with Session(self._wstore.engine) as s, s.begin():
            runtime = Store._runtime(s, self.tenant, pump.account)
            Store._read_connection(s, runtime, pump.revision)
            return runtime.binding_id

    async def _run_owned(self, pump, owner):
        pump.binding_id = await asyncio.to_thread(self._scope, pump)
        self.stats["connects"] += 1
        pump.gap_open = True      # nothing was observed before this socket: the start is a gap
        self.enqueue_gap(pump.account, "owner_start")
        pump.client = NativeWsAuthClient(self.store, self.tenant, pump.connector, pump.revision, url_ok=self.url_ok)
        ws = self._ws_factory(pump.client, lambda state: self._on_state(pump, state))
        pump.ws = ws
        owner_id = pump.connector._handle.owner_id
        watch = asyncio.create_task(self._watch(pump, owner))
        try:
            async for frame in ws.events():
                self.submit(pump.account, frame, owner_id)
        finally:
            watch.cancel()
            ws.stop()
            pump.ws = None
            pump.connected = False
            if pump.owner_lost or not pump.gap_open:
                pump.gap_open = True
                self.enqueue_gap(pump.account, "owner_lost" if pump.owner_lost else "socket_closed")

    def _on_state(self, pump, state):
        if state == "connected":
            pump.connected = True
            pump.gap_open = False       # frames now flow; the durable gap stays until read
        elif state == "disconnected":
            pump.connected = False
            if not pump.gap_open:
                pump.gap_open = True
                self.enqueue_gap(pump.account, "disconnect")

    async def _watch(self, pump, owner):
        last_scope = time.monotonic()
        while True:
            await asyncio.sleep(self.watch_seconds)
            if pump.stop_flag:
                reason = "stop"
            elif time.monotonic() - pump.last_seen > self.idle_seconds:
                pump.stop_flag, reason = True, "idle"
            elif pump.client is not None and pump.client.fatal:
                pump.stop_flag, reason = True, "authentication"
            elif not await asyncio.to_thread(owner.alive):
                pump.owner_lost, reason = True, "owner_lost"
            else:
                reason = None
                if time.monotonic() - last_scope >= max(self.watch_seconds, self.scope_seconds):
                    last_scope = time.monotonic()
                    try:
                        if await asyncio.to_thread(self._scope, pump) != pump.binding_id:
                            reason = "binding_changed"
                    except Refused:
                        reason = "scope_changed"
                    except SQLAlchemyError:
                        reason = None
            if reason:
                if pump.ws is not None:
                    pump.ws.stop()
                return

    # -- writer thread -----------------------------------------------------
    def _writer_loop(self):
        dirty: set[str] = set()
        handled = 0
        while True:
            try:
                item = self._q.get(timeout=.2)
            except queue.Empty:
                item = None
            if item is _STOP:
                self._q.task_done()
                return
            if item is not None:
                try:
                    if self._handle(item):
                        dirty.add(item[1])
                except Exception as failure:
                    log.warning("live intake write failed class=%s", type(failure).__name__)
                    self._retry_gap.add(item[1])
                finally:
                    self._q.task_done()
                handled += 1
            if dirty and (item is None or handled >= 20 or self._q.empty()):
                handled = 0
                for account in list(dirty):
                    try:
                        while self._wstore.drain(self.tenant, account) >= PAGE_LIMIT:
                            pass
                        dirty.discard(account)
                    except Exception as failure:
                        log.warning("live intake drain failed class=%s", type(failure).__name__)
                        break
            if item is None:
                for account in list(self._overflow | self._retry_gap):
                    try:
                        self._record_gap(account, "retry")
                        self._overflow.discard(account)
                        self._retry_gap.discard(account)
                    except Exception:
                        pass

    def _record_gap(self, account, reason):
        self.stats["gaps"] += 1
        request_fan_reads(self._wstore, self.tenant, account, None, reason=reason)

    def _handle(self, item):
        kind, account, payload = item
        if kind == "gap":
            self._record_gap(account, payload)
            return False
        if kind == "message":
            self.stats["events"] += 1
            if self._wstore.record(self.tenant, account, payload.event, source="ws") == "backpressure":
                # Not held: ask for this fan's own read instead of a silent loss.
                request_fan_reads(self._wstore, self.tenant, account, [payload.event.fan_id], reason="backpressure")
            return True
        if kind == "partial":
            self.stats["partials"] += 1
            if payload.fan_id is None:
                with self._lock:
                    self._forced.setdefault(account, set()).add(None)    # bounded poll of approved fans
                return False
            with Session(self._wstore.engine) as s:
                approved = payload.fan_id in approved_fans(s, account)
            if approved:
                request_fan_reads(self._wstore, self.tenant, account, [payload.fan_id], reason="partial")
                with self._lock:
                    self._forced.setdefault(account, set()).add(payload.fan_id)
            return False
        return False

    # -- guarded recovery read + targeted polling (worker thread) ---------
    def read_step(self, store, tenant, account_id, reader, **kwargs):
        """``worker.read_step`` that cannot let one failing/oversized chat abort the
        whole account cycle. The claimed recovery row keeps its lease (extended to
        an exponential backoff) so other fans proceed and it cannot monopolize."""
        from .worker import ReadProgress, read_step
        try:
            return read_step(store, tenant, account_id, reader, **kwargs)
        except (Refused, SQLAlchemyError):
            raise
        except Exception as failure:
            self._note_recovery_failure(store, account_id, failure)
            return ReadProgress(0, 0, 0)

    def _note_recovery_failure(self, store, account_id, failure):
        from .provider_connector import NativeRateLimitError
        if isinstance(failure, NativeRateLimitError):
            return                                   # the shared budget already holds the backoff
        from datetime import timedelta
        with Session(store.engine) as s, s.begin():
            row = s.scalar(select(OfRecovery).where(OfRecovery.account_id == account_id, OfRecovery.status == "running")
                           .order_by(OfRecovery.lease_until.desc()).limit(1).with_for_update())
            if row is None:
                return
            delay = self.health.fail(account_id, row.fan_id, failure)
            row.lease_until = max(row.lease_until or utcnow(), utcnow() + timedelta(seconds=delay))

    def poll(self, store, tenant, account_id, reader):
        """Bounded targeted fallback: newest page of a few automated fans, new
        messages only. Never scans the account or pages into history; coalesced
        per fan; every read holds the shared provider budget."""
        from .provider_connector import NativeRateLimitError
        from .provider_read_budget import ReadDeferred, current
        from .worker import _provider_claim, _release_provider
        now = self.clock()
        with Session(store.engine) as s:
            fans = approved_fans(s, account_id)
        with self._lock:
            forced = self._forced.pop(account_id, set())
        if None in forced:
            forced.discard(None)
            forced |= set(fans)
        due = [fan for fan in fans if fan in forced
               or self._last_poll.get((account_id, fan), 0) + self.poll_interval <= now]
        due.sort(key=lambda fan: (fan not in forced, self._last_poll.get((account_id, fan), 0), fan))
        recorded = reads = 0
        deferred = []
        for fan in due:
            if reads >= self.polls_per_cycle:
                deferred.append(fan)
                continue
            if self.health.blocked(account_id, fan):
                continue
            try:
                budget = _provider_claim(store, tenant, account_id, reader, None, "recent")
            except ReadDeferred:
                deferred.append(fan)
                break
            except Refused:
                break
            reads += 1
            error = None
            try:
                page = reader.messages(fan_id=fan, before_id=None, limit=self.poll_limit)
                if budget is not None:
                    current(store, tenant, budget)
                recorded += self._record_new(store, tenant, account_id, fan, page)
                self._last_poll[(account_id, fan)] = now
                self.health.ok(account_id, fan)
                self.stats["polled"] += 1
            except Refused as failure:
                error = failure
                deferred.append(fan)
            except NativeRateLimitError as failure:
                error = failure          # the shared budget now holds the backoff; stop reading
                deferred.append(fan)
                break
            except Exception as failure:
                error = failure
                self._last_poll[(account_id, fan)] = now
                self.health.fail(account_id, fan, failure)
            finally:
                _release_provider(store, tenant, budget, error)
        if deferred:
            with self._lock:
                self._forced.setdefault(account_id, set()).update(f for f in deferred if f in forced)
        if recorded:
            store.drain(tenant, account_id)
        return recorded

    def _record_new(self, store, tenant, account_id, fan, page):
        with Session(store.engine) as s:
            known = _known_high(s, account_id, fan)
        fresh = sorted((e for e in page.events if e.message_id > known), key=lambda e: e.message_id)
        if page.has_more and len(fresh) == len(page.events):
            # The newest page is entirely unknown: the gap is wider than one page.
            # Use the fan's own bounded recovery (stops at its high-water), no pull here.
            request_fan_reads(store, tenant, account_id, [fan], reason="wide_gap")
        done = 0
        for event in fresh:
            if store.record(tenant, account_id, event, source="catchup") == "backpressure":
                break
            done += 1
        return done

    # -- scheduling fairness ----------------------------------------------
    def allow_background(self, account_id, demand):
        """Live/reply demand wins, but background work is never starved: after
        ``background_every`` consecutive busy cycles one background turn runs."""
        if not demand:
            self._starve[account_id] = 0
            return True
        count = self._starve.get(account_id, 0) + 1
        if count >= self.background_every:
            self._starve[account_id] = 0
            return True
        self._starve[account_id] = count
        return False
