"""Persistent, supervised FASTT runner for ONE account and ONE fan (``--fan``), or for ALL fans
of ONE account (``--all-fans``; see "All-fans mode" below).

``python -m native_pilot.service --account of-<owner> --fan <id> --handle <name>
--enable-send [--twin-context] [--content-manifest FILE]`` runs until stopped
(SIGTERM/SIGINT = clean stop). It is the bounded pilot (``runner.py``, unchanged)
made long-lived, with every safety rule kept:

* exact account + exact fan; one stable ``svc-`` pilot row, one fenced owner;
* the cap is the account's EXISTING ``of_delivery_policy``: each admission spends
  one unit in the same transaction as the durable attempt (never reset, never
  past ``limit_units``) and waits out ``throttle_seconds``;
* durable admission before each single-attempt POST; an unknown outcome stops
  sending for the fan and is NEVER resent - an operator resolves it and resumes
  the conversation, only then may the service re-acquire;
* no deadline: the lease is renewed every few seconds; a lapsed/lost lease
  stops sending and the process re-acquires only through ``activate_service``;
* a restart adopts its own pilot once the old lease has lapsed, refuses while
  anything is unsettled, and never regenerates a claim a crash left behind;
* per-call model limits (400 tokens, 45 s, no retry) and the daily budget;
* a fan under an operator stop (skip_list manual_restrict, or a conversation
  that is not ``automated``) is not answered - the process keeps running;
* live inbound arrives through ``intake.Intake`` (FASTT's OFWebSocket).

All-fans mode (``--all-fans``): the same rules, per fan. One process per OnlyFans account,
one turn at a time. Each tick (1) lets Twin's controls act for every fan with unanswered
inbound (reconcile the runtime stop, the account's new-conversation policy, queued human sends),
(2) picks the OLDEST fan whose newest message is an unanswered inbound and answers it through a
per-fan child ``Service`` (own ``svc-<owner>-<fan>`` pilot, lease, conversation row, input claim,
unknown-outcome hold). A fan is answered ONLY when its Twin state row exists and reads
``automated`` / takeover ``none`` with no skip_list stop and the fan is FASTT-owned; no row, or any
other mode, is never answered and nobody is ever enabled here. The delivery allowance and the
daily model budget stay account-wide and shared. A new pilot's cutoff is the runner's start, so
nothing older than this run is answered, and an unknown outcome holds THAT fan only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import time
from datetime import datetime, timedelta, timezone

from twin_bridge import store

from . import runner
from .ledger import Ledger, Refused
from .scope import AccountScope

LEASE_TTL_S = 120.0
RENEW_S = 30.0
POLL_S = 2.0
FAN_IDLE_RELEASE_S = 900.0
CANDIDATES_MAX = 200      # fans served per tick, and the size of one scan page / the controls window
SCAN_PAGES_MAX = 10       # a tick reads at most this many pages of CANDIDATES_MAX rows: bounded work
BUDGET_HOLD_S = 300.0
HUMAN_SENDS_PER_TICK = 5
HOLD_S = 15.0
QUIET_S = 7.0
CONNECTION_CHECK_S = 300.0
TURN_MAX_S = 600.0
PACE_MAX_S = 180.0

# skip_list reasons a human (or OnlyFans) wrote to say "stop automating this fan".
COOLDOWN_WAIT_MAX_S = 120.0
STOP_REASONS = ("manual_restrict", "of_restricted", "muted_creator")


def fan_hold_reason(path, account: str, fan: int, strict: bool = False):
    """The ONE predicate for "this fan must not be answered right now".

    Reads the durable operator stop (``skip_list``) and the control projection
    (``of_conversation_state``). Returns a fixed reason string, or None. Kept as a
    single small function so the control wiring (s5) can replace or extend it.
    A missing projection row is not a stop; the skip_list row is the stop. ``strict`` (all-fans
    mode) inverts that for the projection: no row (or no table) means "never enabled" and holds.
    """
    with runner.readonly(path) as con:
        if store.has_table(con, "skip_list"):
            row = con.execute("SELECT reason FROM skip_list WHERE account_id=? AND fan_id=?", (account, fan)).fetchone()
            if row and row[0] in STOP_REASONS:
                return "fan-restricted"
        if store.has_table(con, "of_conversation_state"):
            row = con.execute("SELECT mode,takeover_state FROM of_conversation_state WHERE account_id=? AND fan_id=?",
                              (account, fan)).fetchone()
            if strict and not row:
                return "fan-not-enabled"
            if row and row[0] != "automated":
                return "fan-" + str(row[0]).replace("_", "-")
            if row and row[1] not in (None, "none"):
                return "fan-takeover"
        elif strict:
            return "fan-not-enabled"
    return None


async def ensure_fan_conversation(scope: AccountScope, fan: int):
    """Idempotently create the fan's bridge conversation row (activation needs one)."""
    from db.engine import get_session
    from twin_bridge.coordination import ensure_conversation
    async with get_session() as session:
        await ensure_conversation(session, account_id=scope.db_id, fan_id=fan, creator_slug="fastt",
                                  conversation_id=f"fastt-{scope.db_id}-{fan}",
                                  scope_fingerprint=f"fastt:{scope.db_id}:{fan}",
                                  now=runner.now(), send_budget_limit=0)


class Lease:
    def __init__(self, deadline: str):
        self.deadline, self.lost = deadline, False


class ServiceGateway(runner.Gateway):
    """The bounded gateway with the allowance as cap, a renewable deadline and pacing."""

    def __init__(self, service, pilot, **kwargs):
        super().__init__(service.ledger, pilot, service.client, asyncio.get_running_loop(), **kwargs)
        self.service = service
        self.hold_check = lambda: fan_hold_reason(service.path, service.db, service.args.fan, *service.strict_arg)

    @property
    def deadline(self):  # type: ignore[override]
        return self.service.lease.deadline

    @deadline.setter
    def deadline(self, value):  # the base __init__ assigns the frozen pilot deadline
        pass

    def stopped(self):
        if self.stopped_reason:
            return True
        svc = self.service
        if svc.stop.is_set():
            self.stopped_reason = "service-stopping"
        elif svc.lease.lost or runner.now() >= datetime.fromisoformat(svc.lease.deadline):
            self.stopped_reason = "authority-lost"
        else:
            state = self.ledger.status(self.pilot.pilot_id)
            if state["state"] != "active":
                self.stopped_reason = state["reason"] or "pilot-stopped"
        return bool(self.stopped_reason)

    def limit_label(self):
        policy = self.ledger.allowance_status(self.pilot.account_id)
        return None if policy is None else policy["limit"] - policy["used"]

    async def pace(self):
        """Wait out the account's throttle, then let begin_attempt spend the unit atomically."""
        waited = 0.0
        while True:
            if self.stopped():
                raise Refused(self.stopped_reason)
            policy = self.ledger.allowance_status(self.pilot.account_id)
            if policy is None:
                raise Refused("allowance-missing")
            if not policy["enabled"]:
                raise Refused("allowance-disabled")
            if policy["used"] >= policy["limit"]:
                raise Refused("allowance-exhausted")
            wait = 1.0 if policy["busy"] else 0.0
            if policy["next_dispatch_at"]:
                wait = max(wait, (datetime.fromisoformat(policy["next_dispatch_at"]) - runner.now()).total_seconds())
            if wait <= 0:
                return
            if waited >= PACE_MAX_S:
                raise Refused("allowance-wait-timeout")
            step = min(wait, 1.0)
            await self.service.idle(step)
            waited += step


class Service:
    def __init__(self, args, *, stop=None, ttl_s=LEASE_TTL_S, renew_s=RENEW_S, poll_s=POLL_S, hold_s=HOLD_S,
                 quiet_s=QUIET_S, connection_check_s=CONNECTION_CHECK_S, intake_factory=None):
        self.all_fans = bool(getattr(args, "all_fans", False))
        if self.all_fans == (getattr(args, "fan", None) is not None):
            raise Refused("fan-scope-invalid")  # exactly one of --fan / --all-fans
        self.strict = False        # True on a per-fan child of an all-fans runner
        self.cutoff = None         # a child's new-pilot cutoff (the runner's start)
        self.children, self.fan_holds, self.owned_check = {}, {}, None
        self.args = args
        self.scope = AccountScope.parse(args.account)
        self.db = self.scope.db_id
        self.stop = stop or asyncio.Event()
        self.ttl_s, self.renew_s, self.poll_s, self.hold_s = ttl_s, renew_s, poll_s, hold_s
        self.quiet_s, self.connection_check_s = quiet_s, connection_check_s
        self.pilot_id = getattr(args, "service_id", None) or f"svc-{self.scope.provider_id}-{getattr(args, 'fan', None)}"
        self.path = None
        self.ledger = None
        self.client = self.conn = self.sentinels = self.intake = None
        self.pilot = self.lease = None
        self.content = self.context = None
        self.prior_catalog = None
        self.catalog_owner = self
        self.next_conn_check = 0.0
        self.started_at = None
        self.turns_started = 0
        self.last_active = runner.now()
        self.last_held = None
        self.ensured = False
        self._renew_task = None
        self._intake_factory = intake_factory

    async def idle(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    @property
    def strict_arg(self):
        return (True,) if self.strict else ()

    async def wait(self, seconds):
        """A turn's own pause. A per-fan child of the all-fans runner never sleeps: the account's
        serial loop moves on to the next fan instead."""
        if not self.strict:
            await self.idle(seconds)

    def note(self, reason):
        """Emit a hold/clear event only when the reason changes."""
        if reason != self.last_held:
            self.last_held = reason
            runner.emit({"event": "held" if reason else "resumed", **({"reason": reason} if reason else {}),
                         **({"fan_id": self.args.fan} if self.strict else {})})

    # ── session ───────────────────────────────────────────────────────────
    async def ensure_session(self):
        from . import attribution, connection
        soon = self.conn is not None and self.conn.expires_at.replace(tzinfo=None) <= runner.now() + timedelta(seconds=60)
        if self.client is not None and not soon and time.monotonic() < self.next_conn_check:
            return
        conn = await connection.load_verified_connection(self.scope)
        if self.client is None or conn.revision != self.conn.revision or conn.expires_at != self.conn.expires_at:
            client = await asyncio.to_thread(connection.build_client, conn)
            me = await asyncio.to_thread(client.me)
            if str(me.get("id")) != self.scope.provider_id or str(me.get("username", "")).lstrip("@").casefold() != self.args.handle.casefold():
                client.http.close()
                raise Refused("live-account-mismatch")
            sentinels = await attribution.require_sentinels()
            old, self.client, self.sentinels = self.client, client, sentinels
            if self.intake is not None:
                await self.intake.stop()
            self.start_intake()
            if old is not None:
                old.http.close()
        self.conn = conn
        self.next_conn_check = time.monotonic() + self.connection_check_s

    def start_intake(self):
        from .intake import Intake
        if self.started_at is None:
            # All-fans: the activation instant is persisted once and reused across restarts (a process
            # start is not an activation); single-fan keeps its own per-pilot cutoff.
            self.started_at = (self.ledger.service_watermark(
                pilot_id=f"svc-{self.scope.provider_id}-allfans-watermark", account_id=self.db, now=runner.now())
                if self.all_fans else runner.now())
        factory = self._intake_factory or Intake
        self.intake = factory(self.scope, None if self.all_fans else self.args.fan, self.path)
        self.intake.start(self.client)

    # ── ownership ─────────────────────────────────────────────────────────
    async def acquire(self):
        from twin_bridge.coordination import reserve_ownership
        profile = runner.check_local(self.path, self.db, self.args.fan,
                                         approved_item=self.approved_catalog_ids())
        if not self.ensured:
            await ensure_fan_conversation(self.scope, self.args.fan)
            self.ensured = True
        token = "native-" + secrets.token_hex(24)
        pilot = self.ledger.activate_service(pilot_id=self.pilot_id, account_id=self.db, fan_id=self.args.fan,
                                             owner_token=token, now=runner.now(), ttl_s=self.ttl_s, cutoff=self.cutoff)
        if not await reserve_ownership(account_id=self.db, fan_id=self.args.fan, owner_token=token,
                                       now=runner.now(), ttl_s=self.ttl_s):
            self.ledger.release_service(pilot_id=self.pilot_id, owner_token=token, now=runner.now())
            raise Refused("ownership-unavailable")
        self.pilot, self.lease = pilot, Lease(pilot.deadline)
        if self.content:
            self.catalog_owner.enable_catalog()
        self._renew_task = asyncio.get_running_loop().create_task(self._renew_loop(pilot))
        runner.emit({"event": "ready", "pilot_id": pilot.pilot_id, "account_id": self.db, "fan_id": self.args.fan,
                     "engine": "fastt-native", "persistent": True, "lease_utc": pilot.deadline,
                     "creator_profile": profile["creator_profile"]})

    def approved_catalog_ids(self):
        if self.content is None:
            return None
        ids = getattr(self.content, "catalog_ids", None)
        return tuple(sorted(ids if ids is not None else (self.content.item["catalog_id"],)))

    def enable_catalog(self):
        """Enable every reviewed item once per account process, retaining its exact prior state."""
        if self.content is None or self.prior_catalog is not None:
            return
        prior = {}
        with store.transaction(self.path) as con:
            for item_id in self.approved_catalog_ids():
                row = con.execute("SELECT enabled FROM catalog_items WHERE account_id=? AND id=?",
                                  (self.db, item_id)).fetchone()
                if row is None:
                    raise Refused("content-catalog-missing")
                prior[item_id] = row[0]
            for item_id in prior:
                con.execute("UPDATE catalog_items SET enabled=? WHERE account_id=? AND id=?",
                            (True, self.db, item_id))
        self.prior_catalog = prior

    def restore_catalog(self):
        if self.prior_catalog is None:
            return
        with store.transaction(self.path) as con:
            for item_id, enabled in self.prior_catalog.items():
                con.execute("UPDATE catalog_items SET enabled=? WHERE account_id=? AND id=?",
                            (enabled, self.db, item_id))
        self.prior_catalog = None

    async def _renew_loop(self, pilot):
        from twin_bridge.coordination import register_owner
        while not self.stop.is_set() and not self.lease.lost:
            await self.idle(self.renew_s)
            if self.stop.is_set():
                return
            try:
                deadline = self.ledger.renew(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token,
                                             now=runner.now(), ttl_s=self.ttl_s)
                self.lease.deadline = deadline
                register_owner(pilot.account_id, pilot.fan_id, pilot.owner_token, datetime.fromisoformat(deadline))
            except Refused as exc:
                self.lease.lost = True
                runner.emit({"event": "lease-lost", "reason": exc.reason})
                return
            except Exception:
                runner.emit({"event": "lease-renew-failed"})
                if runner.now() >= datetime.fromisoformat(self.lease.deadline):
                    self.lease.lost = True
                    return

    async def drop_pilot(self):
        """Forget our ownership locally; the durable rows keep their evidence."""
        from twin_bridge.coordination import clear_owner
        pilot, self.pilot = self.pilot, None
        task, self._renew_task = self._renew_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pilot is not None:
            clear_owner(pilot.account_id, pilot.fan_id, pilot.owner_token)

    # ── main loop ─────────────────────────────────────────────────────────
    def _owned(self, fan):
        from onlyfans_runtime.fastt_manual import fastt_serves
        return fastt_serves(self.db, fan)

    def _twin_controls(self, fans=None):
        """Twin's dashboard side, run every tick (also while the fan is paused or taken over):
        repair stop/display drift fail-closed, then send queued human messages through
        this runner's single-attempt boundary. No-op without Twin's PostgreSQL control tables.

        Single-fan mode: that one fan. All-fans mode: ``fans`` (every owned fan with activity) get
        the account's new-conversation policy and the reconcile; a queued human message for ANY
        owned fan of the account is sent (never a non-owned one)."""
        import os
        url, tenant = os.environ.get("DATABASE_URL", ""), os.environ.get("ONLYFANS_WORKER_TENANT", "")
        if not url.startswith("postgresql") or not tenant:
            return None
        from onlyfans_runtime.fastt_manual import FasttManualTransport, NotSent, dispatch_next_manual
        from onlyfans_runtime.store import Refused as TwinRefused, Store
        from onlyfans_runtime.text_delivery import TextAuthority
        from sqlalchemy import text as sql
        if not self.all_fans and not self._owned(self.args.fan):
            return None
        if getattr(self, "_twin_store", None) is None:
            self._twin_store = Store(url)
        store = self._twin_store
        for fan in ([self.args.fan] if not self.all_fans else (fans or [])):
            if not self._owned(fan):
                continue
            try:
                if self.all_fans:
                    store.admit_new_conversation(tenant, self.db, fan)
                store.reconcile_runtime_stop(tenant, self.db, fan)
            except TwinRefused as exc:
                if not self.all_fans:
                    raise
                runner.emit({"event": "twin-controls-skipped", "fan_id": fan, "reason": str(exc)[:80]})
        with store.engine.connect() as c:
            rev = c.execute(sql("SELECT revision FROM of_delivery_policy WHERE account_id=:a"), {"a": self.db}).scalar()
        if rev is None or self.client is None:
            return None
        from twin_bridge.platform import escape_plain_text

        def post_once(grant):
            if (not self._owned(grant.fan_id)) if self.all_fans else grant.fan_id != self.args.fan:
                raise NotSent()
            return self.client.send_text_once_body(grant.fan_id, escape_plain_text(grant.body))

        outcome = None
        for _ in range(HUMAN_SENDS_PER_TICK if self.all_fans else 1):
            done = dispatch_next_manual(TextAuthority(store), tenant, self.db,
                                        FasttManualTransport(self.db, post_once, policy_revision=int(rev)))
            if done is None:
                break
            outcome = done
            runner.emit({"event": "human-send", "outcome": done})
        return outcome

    # ── all-fans orchestration ────────────────────────────────────────────
    def pending_fans(self):
        """``(serve, controlled)`` for one tick of the all-fans runner.

        ``serve``: ``(fan, newest message id)`` oldest first, for owned fans whose newest message is an
        unanswered inbound that arrived after the persisted activation watermark. The ineligible are removed
        BEFORE the bound is applied, so they cannot occupy it: operator-paused / restricted / taken-over /
        never-enabled fans and fans whose newest message is already claimed are filtered in SQL, non-owned
        fans and fans in a live hold in Python while paging oldest first. At most ``CANDIDATES_MAX`` fans
        and ``SCAN_PAGES_MAX`` pages per tick.

        ``controlled``: every owned fan with an unanswered inbound among the newest ``CANDIDATES_MAX``
        (paused or not: Twin's controls still run for them), plus ``serve``.
        """
        from .ledger import _dt
        since, now, owned = _dt(self.started_at), runner.now(), {}

        def is_owned(fan):
            if fan not in owned:
                owned[fan] = bool(self._owned(fan))
            return owned[fan]

        base = ("SELECT m.fan_id,m.message_id,m.created_at FROM messages m WHERE m.account_id=? AND m.direction='in' "
                "AND m.created_at>? AND NOT EXISTS (SELECT 1 FROM messages n WHERE n.account_id=m.account_id "
                "AND n.fan_id=m.fan_id AND NOT (n.direction='out' AND n.is_unsent) "
                "AND (n.created_at>m.created_at OR (n.created_at=m.created_at AND n.message_id>m.message_id)))")
        serve, controlled = [], []
        with runner.readonly(self.path) as con:
            for fan, _, created in con.execute(base + " ORDER BY m.created_at DESC,m.message_id DESC LIMIT ?",
                                               (self.db, since, CANDIDATES_MAX)).fetchall():
                if _dt(created) > since and is_owned(fan) and fan not in controlled:
                    controlled.append(fan)
            if store.has_table(con, "of_conversation_state"):  # strict: no projection table = nobody enabled
                sql, params = base, [self.db, since]
                sql += (" AND EXISTS (SELECT 1 FROM of_conversation_state s WHERE s.account_id=m.account_id "
                        "AND s.fan_id=m.fan_id AND s.mode='automated' AND (s.takeover_state IS NULL OR s.takeover_state='none'))")
                if store.has_table(con, "skip_list"):
                    sql += (" AND NOT EXISTS (SELECT 1 FROM skip_list k WHERE k.account_id=m.account_id "
                            "AND k.fan_id=m.fan_id AND k.reason IN (" + ",".join("?" for _ in STOP_REASONS) + "))")
                    params += list(STOP_REASONS)
                if store.has_table(con, "native_pilot_inputs"):  # claimed = worked: a reply, or a no-reply/abandoned turn
                    sql += (" AND NOT EXISTS (SELECT 1 FROM native_pilot_inputs i WHERE i.pilot_id=? || CAST(m.fan_id AS TEXT) "
                            "AND i.message_id=m.message_id)")
                    params.append(f"svc-{self.scope.provider_id}-")
                cursor = ()
                for _ in range(SCAN_PAGES_MAX):
                    rows = con.execute(
                        sql + (" AND (m.created_at,m.message_id)>(?,?)" if cursor else "")
                        + " ORDER BY m.created_at,m.message_id LIMIT ?",
                        tuple(params) + cursor + (CANDIDATES_MAX,)).fetchall()
                    for fan, mid, created in rows:
                        hold = self.fan_holds.get(fan)
                        if _dt(created) > since and is_owned(fan) and not (hold and now < hold[0]):
                            serve.append((fan, mid))
                            if len(serve) >= CANDIDATES_MAX:
                                break
                    if len(serve) >= CANDIDATES_MAX or len(rows) < CANDIDATES_MAX:
                        break
                    cursor = (rows[-1][2], rows[-1][1])
        controlled += [fan for fan, _ in serve if fan not in controlled]
        return serve, controlled

    def child(self, fan):
        child = self.children.get(fan)
        if child is None:
            args = argparse.Namespace(**{**vars(self.args), "fan": fan, "all_fans": False, "service_id": None})
            child = Service(args, stop=self.stop, ttl_s=self.ttl_s, renew_s=self.renew_s, poll_s=self.poll_s,
                            hold_s=self.hold_s, quiet_s=self.quiet_s, connection_check_s=self.connection_check_s)
            child.strict, child.cutoff = True, self.started_at
            child.path, child.ledger, child.context = self.path, self.ledger, self.context
            child.content, child.catalog_owner = self.content, self
            self.children[fan] = child
        child.client, child.conn, child.sentinels = self.client, self.conn, self.sentinels
        return child

    async def release_child(self, fan):
        child = self.children.pop(fan, None)
        if child is not None:
            await child.release_pilot()

    async def release_pilot(self):
        """Give the lease back; the durable rows keep their evidence (clean-shutdown semantics)."""
        from twin_bridge.coordination import clear_owner
        pilot, self.pilot = self.pilot, None
        task, self._renew_task = self._renew_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pilot is not None:
            try:
                if self.lease is not None and not self.lease.lost:
                    self.ledger.release_service(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now())
            except Refused:
                pass
            clear_owner(pilot.account_id, pilot.fan_id, pilot.owner_token)

    def already_claimed(self, fan, message_id):
        with runner.readonly(self.path) as con:
            if not store.has_table(con, "native_pilot_inputs"):
                return False  # nothing has ever been claimed on this store
        return self.ledger.input_claimed(pilot_id=f"svc-{self.scope.provider_id}-{fan}", message_id=message_id)

    def _hold_fan(self, fan, reason):
        wait = self.poll_s if reason.startswith("fan-") else self.hold_s
        self.fan_holds[fan] = (runner.now() + timedelta(seconds=wait), reason)

    async def tick_all(self):
        pending, controlled = await asyncio.to_thread(self.pending_fans)
        try:
            await asyncio.to_thread(self._twin_controls, controlled)
        except Exception as exc:
            runner.emit({"event": "twin-controls-error", "reason": (str(exc) or type(exc).__name__)[:120]})
        # Give back the lease of any fan quiet for a while (bounds renewal tasks and ownership).
        for fan, child in list(self.children.items()):
            if child.pilot is not None and fan not in controlled and \
                    (runner.now() - child.last_active).total_seconds() > FAN_IDLE_RELEASE_S:
                await self.release_child(fan)
        for fan, mid in pending:
            if self.stop.is_set():
                return
            hold = self.fan_holds.get(fan)
            if hold and runner.now() < hold[0]:
                continue
            if self.already_claimed(fan, mid):
                continue  # already worked (a reply, or a no-reply/abandoned turn): never regenerated
            child = self.child(fan)
            child.last_active = runner.now()
            before = child.turns_started
            try:
                await child.serve_fan()
            except Refused as exc:
                child.note(exc.reason)
                if exc.reason.startswith(("allowance-", "model-budget")):
                    raise  # account-wide: stop selecting fans until it clears
                self._hold_fan(fan, exc.reason)
                continue
            except Exception as exc:
                child.note("service-error:" + type(exc).__name__)
                self._hold_fan(fan, "service-error")
                continue
            if child.turns_started > before:
                return  # one turn at a time; re-read controls and the queue before the next fan
        self.note(None)
        await self.idle(self.poll_s)

    async def tick(self):
        await self.ensure_session()
        if self.all_fans:
            return await self.tick_all()
        try:
            await asyncio.to_thread(self._twin_controls)
        except Exception as exc:
            runner.emit({"event": "twin-controls-error", "reason": (str(exc) or type(exc).__name__)[:120]})
        await self.serve_fan()

    async def serve_fan(self):
        """Everything one fan's turn needs, after the account-level session and Twin controls."""
        if self.strict:
            # All-fans: a fan that is not enabled/paused/stopped never even gets a pilot or lease.
            held = await asyncio.to_thread(fan_hold_reason, self.path, self.db, self.args.fan, True)
            if held:
                raise Refused(held)
        if self.pilot is None:
            await self.acquire()
        if self.lease.lost:
            await self.drop_pilot()
            raise Refused("authority-lost")
        state = self.ledger.status(self.pilot_id)
        if state["state"] != "active":
            await self.drop_pilot()
            raise Refused(state["reason"] or "pilot-stopped")
        held = await asyncio.to_thread(fan_hold_reason, self.path, self.db, self.args.fan, *self.strict_arg)
        if held:
            raise Refused(held)
        policy = self.ledger.allowance_status(self.db)
        if policy is None or not policy["enabled"] or policy["used"] >= policy["limit"]:
            raise Refused("allowance-disabled" if policy is not None and not policy["enabled"]
                          else "allowance-exhausted" if policy else "allowance-missing")
        self.note(None)
        await self.turn()

    async def turn(self):
        from .budget import DailyBudget
        from .engine import NativeEngine
        from .model import ENDPOINT, NativePilotModel
        pilot, ledger, args = self.pilot, self.ledger, self.args
        snap = ledger.snapshot(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now())
        if snap is None or ledger.input_claimed(pilot_id=pilot.pilot_id, message_id=snap.message_id) \
                or (runner.now() - datetime.fromisoformat(snap.ingested_at)).total_seconds() < self.quiet_s:
            return await self.wait(self.poll_s)
        wait = self.cooldown_left()
        if wait > 0:
            # FASTT's own short post-send cooldown (fans.automation_paused_until) would make
            # ai_chatter skip the fan; wait it out instead of claiming and abandoning the input.
            return await self.wait(min(wait + 0.5, self.poll_s * 10))
        try:
            sources = runner.read_sources(self.path, self.db, args.fan, getattr(snap, "member_ids", (snap.message_id,)))
            key = ledger.claim_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, snapshot=snap, now=runner.now())
        except Refused as exc:
            if exc.reason in ("input-already-claimed", "source-changed"):
                return await self.wait(self.poll_s)
            raise
        self.turns_started += 1
        gateway = ServiceGateway(self, pilot, content=self.content, scope=self.scope)
        gateway.sources, gateway.input_key, gateway.bubble = sources, key, 0
        model = NativePilotModel(
            api_key=self.conn.model_api_key, account_id=self.db, fan_id=args.fan, deadline=time.monotonic() + TURN_MAX_S,
            should_stop=gateway.stopped, on_event=runner.emit,
            reserve_call=lambda: ledger.count_model_call(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now()),
            budget=DailyBudget(self.scope, self.conn.model_id, endpoint=ENDPOINT))
        runner.emit({"event": "generating", "source_message_id": str(snap.message_id), "engine": "fastt-native"})
        run_id = runner.begin_native_run(self.path, self.db)
        stats = error = None
        try:
            with NativeEngine(self.db, args.fan, chat=model.chat, send=gateway.send, emit=runner.emit,
                              content=self.content, context=self.context,
                              **({"own_offers_only": True} if self.strict else {})) as native:
                stats = await native.run(run_id=run_id)
            if gateway.stopped_reason or model.stopped_reason:
                raise Refused(gateway.stopped_reason or model.stopped_reason)
            if not isinstance(stats, dict) or stats.get("errors") or stats.get("cap_hit"):
                raise Refused("native-turn-failed")
        except Exception as exc:
            error = exc.reason if isinstance(exc, Refused) else (gateway.stopped_reason or model.stopped_reason or "native-turn-failed")
        finally:
            runner.finish_native_run(self.path, run_id, stats, error)
        receipts = ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)
        if error:
            if self.stop.is_set() and not receipts:
                return  # clean stop: the unsent claim is dropped on release and answered after restart
            await self.after_failure(error, key)
            return
        try:
            runner.verify_attribution(self.path, self.db, args.fan, receipts, content=self.content,
                                      automation_employee_id=self.sentinels.automation)
        except Refused as exc:
            ledger.stop(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, reason=exc.reason, now=runner.now())
            await runner.report_to_twin(ledger, pilot, exc.reason)
            await self.drop_pilot()
            raise
        await runner.report_to_twin(ledger, pilot)
        ledger.finish_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now())
        if getattr(args, "stop_after_card", False) and await self.card_delivered(receipts):
            await self.stop_after_card()
        if not receipts:
            runner.emit({"event": "native-no-reply", "skip_counts": {k: v for k, v in (stats or {}).items()
                                                                     if k.startswith("skipped_") and isinstance(v, int) and v}})

    def cooldown_left(self) -> float:
        """Seconds left on FASTT's per-fan cooldown (fans.automation_paused_until), 0 when none."""
        with runner.readonly(self.path) as con:
            if not store.has_table(con, "fans"):
                return 0.0
            row = con.execute("SELECT automation_paused_until FROM fans WHERE account_id=? AND fan_id=?",
                              (self.db, self.args.fan)).fetchone()
        if not row or not row[0]:
            return 0.0
        until = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
        if until.tzinfo is not None:
            until = until.astimezone(timezone.utc).replace(tzinfo=None)
        # ai_chatter compares this column with the real UTC clock, so this does too.
        left = (until - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
        # Only FASTT's short post-send cooldown is waited out; a longer pause is a real hold
        # and is left to ai_chatter's own skip, exactly as before.
        return left if 0 < left <= COOLDOWN_WAIT_MAX_S else 0.0

    async def card_delivered(self, receipts):
        """True when a confirmed bubble of this turn is the priced card (its canonical row carries a price)."""
        ids = [int(r["provider_message_id"]) for r in receipts if r.get("state") == "sent" and r.get("provider_message_id")]
        if not ids:
            return False
        with runner.readonly(self.path) as con:
            marks = ",".join("?" * len(ids))
            row = con.execute(f"SELECT 1 FROM messages WHERE account_id=? AND fan_id=? AND direction='out' "
                              f"AND price_cents>0 AND message_id IN ({marks}) LIMIT 1", (self.db, self.args.fan, *ids)).fetchone()
        return row is not None

    async def stop_after_card(self):
        """The approved test ends after the paid card.

        Goes through Twin's own control path (``Store.control``, action pause_automation) so the
        runtime stop, the dashboard's conversation state and the control audit change together.
        If that path is unavailable (no Twin state row, SQLite fixture), the runtime stop alone is
        still set: fail closed, never keep sending.
        """
        import asyncio, hashlib, os
        from db.engine import get_session
        from onlyfans_runtime import runtime_stop
        try:
            from onlyfans_runtime.store import Store
            from db.onlyfans_models import OfConversationState
            from sqlalchemy.orm import Session
            url, tenant = os.environ.get("DATABASE_URL", ""), os.environ.get("ONLYFANS_WORKER_TENANT", "")
            if not url or not tenant:
                raise RuntimeError("twin-control-unavailable")

            def pause():
                store = Store(url)
                with Session(store.engine) as s:
                    row = s.get(OfConversationState, (self.db, self.args.fan))
                    if row is None or row.mode != "automated":
                        raise RuntimeError("twin-state-not-automated")
                    rev = row.revision
                key = hashlib.sha256(f"fastt-stop-after-card:{self.db}:{self.args.fan}:{rev}".encode()).hexdigest()[:64]
                return store.control(tenant, self.db, self.args.fan, mode="paused", expected_revision=rev,
                                     command_key=key, actor="fastt-runner", reason="Stopped after the approved paid card",
                                     action="pause_automation")
            await asyncio.to_thread(pause)
            via = "twin-control"
        except Exception as exc:  # fail closed: the runtime stop is still written
            async with get_session() as session:
                await session.run_sync(lambda s: runtime_stop.hold(s, self.db, self.args.fan, runner.now()))
                await session.commit()
            via = "runtime-stop-only:" + (str(exc)[:60] or type(exc).__name__)
        runner.emit({"event": "stopped-after-card", "account_id": self.db, "fan_id": self.args.fan, "via": via})

    async def after_failure(self, reason, key):
        """A failed turn: stop for the fan if the ledger says so, else abandon only this input."""
        pilot, ledger = self.pilot, self.ledger
        await runner.report_to_twin(ledger, pilot, reason)
        state = ledger.status(pilot.pilot_id)
        if state["state"] != "active":
            # delivery-unknown (or any ledger stop): nothing is resent; wait for an operator.
            runner.emit({"event": "stopped", "reason": state["reason"] or reason})
            await self.drop_pilot()
            raise Refused(state["reason"] or reason)
        # Abandon this input only: it is never regenerated (no automatic retry).
        ledger.finish_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now())
        runner.emit({"event": "turn-abandoned", "reason": reason, **({"fan_id": self.args.fan} if self.strict else {})})
        await self.wait(self.hold_s)
        if self.strict and reason.startswith("model-budget"):
            raise Refused(reason)  # the daily model budget is the ACCOUNT's: the runner backs off for everyone

    async def run(self):
        from twin_bridge.coordination import store_target
        self.path = store_target()
        if not self.path:
            raise Refused("pilot-store-required")
        runner.require_store_schema(self.path)
        self.ledger = Ledger(self.path, allowance=True)
        if getattr(self.args, "content_manifest", None):
            from .content import ContentPlan
            self.content = ContentPlan(self.args.content_manifest, self.db, None if self.all_fans else self.args.fan)
            with runner.readonly(self.path) as con:
                self.content.check_catalog(con, self.db)
        if getattr(self.args, "twin_context", False):
            from .twin_context import TwinPromptContext
            self.context = TwinPromptContext()
        try:
            while not self.stop.is_set():
                try:
                    await self.tick()
                except Refused as exc:
                    self.note(exc.reason)
                    await self.idle(self.poll_s if exc.reason.startswith("fan-") else
                                    BUDGET_HOLD_S if exc.reason.startswith("model-budget") else self.hold_s)
                except Exception as exc:
                    self.note("service-error:" + type(exc).__name__)
                    await self.idle(self.hold_s)
        finally:
            await self.shutdown()

    async def shutdown(self):
        from db.engine import engine
        from twin_bridge.coordination import clear_owner
        pilot = self.pilot
        try:
            for fan in list(self.children):
                await self.release_child(fan)
            task, self._renew_task = self._renew_task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if pilot is not None:
                try:
                    if not self.lease.lost:
                        self.ledger.release_service(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now())
                except Refused:
                    pass
                clear_owner(pilot.account_id, pilot.fan_id, pilot.owner_token)
            self.restore_catalog()
            runner.emit({"event": "finished", "persistent": True})
        finally:
            try:
                if self.intake is not None:
                    await self.intake.stop()
            finally:
                try:
                    if self.client is not None:
                        self.client.http.close()
                finally:
                    await engine.dispose()


async def preflight(args):
    """Read-only: connection, identity, attribution rows and local gates. No ledger write, no socket."""
    from db.engine import engine
    from twin_bridge.coordination import store_target
    from . import attribution, connection
    from .model import MODEL
    scope = AccountScope.parse(args.account)
    path = store_target()
    if not path:
        raise Refused("pilot-store-required")
    runner.require_store_schema(path)
    profile = runner.check_local(path, scope.db_id, args.fan or 0)
    client = None
    try:
        conn = await connection.load_verified_connection(scope)
        client = await asyncio.to_thread(connection.build_client, conn)
        me = await asyncio.to_thread(client.me)
        if str(me.get("id")) != scope.provider_id or str(me.get("username", "")).lstrip("@").casefold() != args.handle.casefold():
            raise Refused("live-account-mismatch")
        found = await attribution.read_sentinels()
        runner.emit({"event": "preflight-ok", **profile, "model": MODEL, "model_id": conn.model_id,
                     "daily_cost_cap_cents": conn.daily_cost_cap_cents, "creator_handle": args.handle,
                     "attribution": {"automation": found.automation is not None, "human": found.human is not None}})
    finally:
        if client is not None:
            client.http.close()
        await engine.dispose()


async def serve(args, **kwargs):
    if not args.enable_send:
        return await preflight(args)
    service = Service(args, **kwargs)
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, service.stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await service.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, help="Twin account id (of-<owner>) or the bare owner id")
    scope_group = parser.add_mutually_exclusive_group(required=True)
    scope_group.add_argument("--fan", type=int)
    scope_group.add_argument("--all-fans", action="store_true",
                             help="answer every fan of the account whose Twin state is automated (never enables anyone)")
    parser.add_argument("--handle", required=True)
    parser.add_argument("--service-id", help="stable pilot id, svc-<letters, digits, - _> (default svc-<owner>-<fan>)")
    parser.add_argument("--enable-send", action="store_true", help="without it: read-only preflight, then exit")
    parser.add_argument("--twin-context", action="store_true")
    parser.add_argument("--content-manifest")
    parser.add_argument("--stop-after-card", action="store_true",
                        help="Set the fan's runtime stop once the paid card is confirmed (approved test scope)")
    args = parser.parse_args(argv)
    from .ledger import SERVICE_ID
    if not AccountScope.valid(args.account) or (args.fan is not None and args.fan <= 0) or (args.service_id and not SERVICE_ID.fullmatch(args.service_id)):
        parser.error("invalid service scope")
    os.environ.setdefault("W7_WEBHOOK_DISPATCH_DISABLED", "1")
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve(args))
    except Exception as exc:
        runner.emit({"event": "not-running", "reason": exc.reason if isinstance(exc, Refused) else "service-error"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
