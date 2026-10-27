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
* a send that CERTAINLY did not reach OnlyFans (a provider HTTP 429 or other 4xx refusal, or a hold/stop seen
  between admission and the wire) is recorded ``not_sent``, gives its allowance unit back and stops nothing; a
  429 keeps the unsent input pending through a durable account cooldown; a last-instant hold has bounded retries. An input lost
  to a model timeout/error is requeued once. Never once any send attempt for that input stands (sent,
  dispatching, unknown), and a timeout, 408, 5xx or unreadable reply stays an unknown outcome as above;
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

from . import postsale, runner
from .followups import DISCOUNT_KIND, NUDGE_KIND
from .ledger import MAX_REQUEUES_MODEL, MAX_REQUEUES_PROVIDER, Ledger, Refused
from .scope import AccountScope

LEASE_TTL_S = 120.0
RENEW_S = 30.0
POLL_S = 2.0
FAN_IDLE_RELEASE_S = 900.0
CANDIDATES_MAX = 200      # fans served per tick, and the size of one scan page / the controls window
SCAN_PAGES_MAX = 10       # a tick reads at most this many pages of CANDIDATES_MAX rows: bounded work
BUDGET_HOLD_S = 300.0
FOLLOWUPS_PER_TICK = 5
HUMAN_SENDS_PER_TICK = 5
HOLD_S = 15.0
QUIET_S = 7.0
DESCRIBE_WAIT_S = 45.0       # a caption-less fan photo waits this long for its description
CONNECTION_CHECK_S = 300.0
TURN_MAX_S = 600.0
PACE_MAX_S = 180.0
MODEL_RETRY_REASONS = ("model-timeout", "model-http-failed", "model-response-failed")
RATE_LIMIT_BACKOFF_S = 60.0   # a provider 429: wait this long (times the retry number) unless Retry-After says otherwise
RATE_LIMIT_BACKOFF_MAX_S = 300.0

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
            if self.hold_check is not None:
                # The throttle can last minutes: an operator pause landing meanwhile must not cost a turn.
                held = await asyncio.to_thread(self.hold_check)
                if held:
                    raise Refused(held)
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
        self.followups = None      # followups.FollowupQueue when --followups, else None (every job suppressed)
        self.prior_catalog = None
        self.catalog_owner = self
        self.next_conn_check = 0.0
        self.started_at = None
        self.turns_started = 0
        self._outcomes = set()     # (kind, offer, reason) already reported by scan_postsale
        self.backoff_s = 0.0       # set by after_failure when an input was requeued; the all-fans runner turns it into a hold
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
            self.start_intake(conn)
            if old is not None:
                old.http.close()
        self.conn = conn
        self.next_conn_check = time.monotonic() + self.connection_check_s

    def start_intake(self, conn=None):
        from .intake import Intake
        if self.started_at is None:
            # All-fans: the activation instant is persisted once and reused across restarts (a process
            # start is not an activation); single-fan keeps its own per-pilot cutoff.
            self.started_at = (self.ledger.service_watermark(
                pilot_id=f"svc-{self.scope.provider_id}-allfans-watermark", account_id=self.db, now=runner.now())
                if self.all_fans else runner.now())
        factory = self._intake_factory or Intake
        # FASTT's own inbound_describe for fan photos (photo_vision.py); the ledger waits for it, briefly.
        kwargs = {}
        if self._intake_factory is None:
            from .photo_vision import make_describer
            kwargs["describe"] = make_describer(self.scope, self.client, api_key=(conn or self.conn).model_api_key,
                                              emit=runner.emit)
            self.ledger.describe_wait_s = DESCRIBE_WAIT_S
        self.intake = factory(self.scope, None if self.all_fans else self.args.fan, self.path, **kwargs)
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
        if self.content is None or self.prior_catalog is not None or not self.content.enables_catalog:
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
        from onlyfans_runtime.fastt_manual import FasttManualTransport, NotSent, dispatch_next_operator, post_operator_once
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
        def post_once(grant):
            if not self._owned(grant.fan_id) or (not self.all_fans and grant.fan_id != self.args.fan):
                raise NotSent()
            return post_operator_once(self.client, grant, owner_id=int(self.scope.provider_id))

        from onlyfans_runtime.campaigns import CampaignService
        if getattr(self, "_twin_campaigns", None) is None:
            self._twin_campaigns = CampaignService(store)
        try:
            self._twin_campaigns.queue_due(tenant, self.db)
        except TwinRefused:
            # Ineligible campaign preparation must not block the human queue.
            pass
        if not hasattr(self, "_operator_scan"):
            self._operator_scan = {}
        outcome = None
        for _ in range(HUMAN_SENDS_PER_TICK if self.all_fans else 1):
            turn = getattr(self, "_operator_turn", 0)
            self._operator_turn = (turn + 1) % 4
            done = dispatch_next_operator(TextAuthority(store), tenant, self.db,
                                        FasttManualTransport(self.db, post_once, policy_revision=int(rev)),
                                        campaign_first=turn == 3, scan_cursors=self._operator_scan)
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
        from .overlap import older_reply_sql
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
            if store.has_table(con, "native_pilot_receipts"):
                # A confirmed reply to an older input cannot consume a message
                # which arrived during that reply. Every other newer row still blocks.
                base = base[:-1] + " AND NOT (" + older_reply_sql() + "))"
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
                            "AND i.message_id=m.message_id AND i.state NOT LIKE 'requeued:%')")
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
            child.followups = self.followups
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
            if child.backoff_s:
                # A requeued input (provider 429 / model failure): leave this fan alone while it backs off.
                self.fan_holds[fan] = (runner.now() + timedelta(seconds=child.backoff_s), "retry-backoff")
                child.backoff_s = 0.0
            if child.turns_started > before:
                return  # one turn at a time; re-read controls and the queue before the next fan
        await self.run_postsale_scan()
        if await self.run_followups():
            return  # a follow-up turn started: re-read controls and the queue first
        self.note(None)
        await self.idle(self.poll_s)

    async def tick(self):
        await self.ensure_session()
        policy = self.ledger.allowance_status(self.db)
        if policy and policy["next_dispatch_at"]:
            wait = (datetime.fromisoformat(policy["next_dispatch_at"]) - runner.now()).total_seconds()
            if wait > 0:
                # The shared send gate includes a provider cooldown, durably retained after restart.
                # Do not spend model calls or probe another fan through the same limited connection.
                await self.idle(min(wait, self.poll_s))
                return
        if self.all_fans:
            return await self.tick_all()
        try:
            await asyncio.to_thread(self._twin_controls)
        except Exception as exc:
            runner.emit({"event": "twin-controls-error", "reason": (str(exc) or type(exc).__name__)[:120]})
        await self.run_postsale_scan()
        if await self.run_followups():
            return
        await self.serve_fan()

    async def serve_fan(self, job=None):
        """Everything one fan's turn needs, after the account-level session and Twin controls.

        ``job``: a due follow-up (the stock nudge) for this fan instead of fresh fan input. Same
        hold / lease / allowance gates; only the turn itself differs."""
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
        if job is not None:
            return await self.followup_turn(job)
        await self.turn()

    async def turn(self):
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
        await self.execute(key, sources, source_id=snap.message_id)

    async def execute(self, key, sources, *, source_id, job=None):
        """Run FASTT's engine for one claimed input (fan message, or the nudge job ``job``) through the
        single-attempt boundary. Returns the failure reason, or None."""
        from .budget import DailyBudget
        from .engine import NativeEngine
        from .model import ENDPOINT, NativePilotModel
        from .followups import job_payload, offer_of, DISCOUNT_KIND
        pilot, ledger, args = self.pilot, self.ledger, self.args
        self.turns_started += 1
        gateway = ServiceGateway(self, pilot, content=self.content, scope=self.scope)
        # A requeued input already owns bubble numbers (its refused attempts): continue after them.
        prior = ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)
        gateway.sources, gateway.input_key, gateway.bubble = sources, key, max((r["bubble_no"] for r in prior), default=0)
        self.backoff_s = 0.0
        gateway.followup = job is not None
        if job is not None and job["kind"] == DISCOUNT_KIND:
            # The one card this job may remove (re-read here, not trusted from the queue row).
            row = await asyncio.to_thread(self.offer_row, offer_of(job["kind"], job_payload(job)))
            gateway.expected_unsend = int(row["offer_message_id"]) if row and row["offer_message_id"] else None
        model = NativePilotModel(
            api_key=self.conn.model_api_key, account_id=self.db, fan_id=args.fan, deadline=time.monotonic() + TURN_MAX_S,
            should_stop=gateway.stopped, on_event=runner.emit,
            reserve_call=lambda: ledger.count_model_call(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now()),
            budget=DailyBudget(self.scope, self.conn.model_id, endpoint=ENDPOINT))
        runner.emit({"event": "generating", "source_message_id": str(source_id), "engine": "fastt-native"})
        run_id = runner.begin_native_run(self.path, self.db)
        stats = error = None
        try:
            unanswered = ledger.unanswered_reply_ids(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token,
                                                     input_key=key, now=runner.now()) if job is None else ()
            with NativeEngine(self.db, args.fan, chat=model.chat, send=gateway.send, emit=runner.emit,
                              content=self.content, context=self.context,
                              **({"unanswered_reply_ids": unanswered} if unanswered else {}),
                              **({"own_offers_only": True} if self.strict else {}),
                              arm_unsend=gateway.arm_unsend, unsent=lambda: gateway.unsent_ids,
                              card_of=lambda mid: ledger.card_evidence(account_id=self.db, fan_id=args.fan, provider_message_id=str(mid)),
                              **({"followups": self.followups} if self.followups is not None else {})) as native:
                stats = await native.run(run_id=run_id, **({"payload": job_payload(job)} if job is not None else {}))
            if gateway.stopped_reason or model.stopped_reason:
                raise Refused(gateway.stopped_reason or model.stopped_reason)
            if not isinstance(stats, dict) or stats.get("errors") or stats.get("cap_hit"):
                raise Refused("native-turn-failed")
        except Exception as exc:
            error = exc.reason if isinstance(exc, Refused) else (gateway.stopped_reason or model.stopped_reason or "native-turn-failed")
        finally:
            runner.finish_native_run(self.path, run_id, stats, error)
            if isinstance(stats, dict):
                # FASTT's own per-turn counters (sell lane, offers, packs, skips): operator visibility only.
                runner.emit({"event": "turn-stats", "stats": {k: v for k, v in stats.items()
                    if v and isinstance(v, (int, float, str, bool, dict)) and k not in ("model", "mode")}})
        receipts = ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)
        if error:
            if self.stop.is_set() and not receipts:
                return error  # clean stop: the unsent claim is dropped on release and answered after restart
            # Answer the input again later only when no send attempt stands for it (the ledger re-checks):
            # a model timeout/error, or a POST that certainly did not reach OnlyFans (a provider 429, or a
            # last-instant hold). A definite non-429 refusal and every follow-up job are not retried.
            retry = None
            if job is None:
                if gateway.not_sent is not None:
                    retry = ("provider", gateway.not_sent.retry_after) if gateway.not_sent.retry else None
                elif error in MODEL_RETRY_REASONS:
                    retry = ("model", None)
            await self.after_failure(error, key, retry=retry)
            return error
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
        if not any(r["state"] == "sent" for r in receipts):
            runner.emit({"event": "native-no-reply", "skip_counts": {k: v for k, v in (stats or {}).items()
                                                                     if k.startswith("skipped_") and isinstance(v, int) and v}})
        return None

    # ── follow-ups (--followups): FASTT's own delayed nudge, run when due ──────
    def offer_open(self) -> bool:
        """True while the fan still has an open (unbought) offer: FASTT's nudge only exists for that."""
        with runner.readonly(self.path) as con:
            if not store.has_table(con, "content_offers"):
                return False
            return con.execute("SELECT 1 FROM content_offers WHERE account_id=? AND fan_id=? AND status='open' LIMIT 1",
                               (self.db, self.args.fan)).fetchone() is not None

    def offer_row(self, offer_id):
        """The offer row of THIS account (fan, status, how it resolved, its provider message id), or None."""
        if type(offer_id) is not int:
            return None
        with runner.readonly(self.path) as con:
            row = con.execute("SELECT id,fan_id,status,resolved_by,offer_message_id,price_cents FROM content_offers "
                              "WHERE id=? AND account_id=?", (offer_id, self.db)).fetchone()
        return dict(zip(("id", "fan_id", "status", "resolved_by", "offer_message_id", "price_cents"), row)) if row else None

    def postsale_block(self, job):
        """Why a discount / post-buy job must not run now (a fixed reason), or None."""
        from .followups import job_payload, offer_of
        kind = job["kind"]
        if not postsale.settings(self.path, self.db)["discount" if kind == DISCOUNT_KIND else "post_buy"]:
            return "feature-disabled"
        offer = self.offer_row(offer_of(kind, job_payload(job)))
        if offer is None or int(offer["fan_id"]) != int(job["fan_id"]):
            return "offer-mismatch"
        if kind == DISCOUNT_KIND:
            return None if offer["status"] == "open" else "already-purchased" if offer["status"] == "delivered" else "offer-resolved"
        return None if offer["status"] == "delivered" and offer["resolved_by"] == "ppv_ledger" else "not-purchased"

    def _postsale_in_scope(self, fan):
        """The fans the scan may queue for: this runner's own, owned by FASTT and not held (the same fences a turn passes)."""
        if self.all_fans:
            return self._owned(fan) and not fan_hold_reason(self.path, self.db, fan, True)
        return fan == self.args.fan

    def scan_postsale(self):
        """Queue the offer-keyed follow-ups FASTT's discounted resend and post-purchase upsell run from (see postsale.py).

        Both switches default off. Reads only, plus the queue write; the stock functions decide when the job runs."""
        if self.followups is None:
            return
        st = postsale.settings(self.path, self.db)
        if not (st["discount"] or st["post_buy"]):
            return
        from automations import _price_ask, upsell
        now = runner.now()
        with runner.readonly(self.path) as con:
            buys = postsale.purchases(con, self.db, now) if st["post_buy"] else []
            cards = postsale.unpaid_cards(con, self.db, now, upsell.RESEND_AFTER_M) if st["discount"] else []
            ready = []
            for offer_id, fan, offered_at, price in cards:
                if not self._postsale_in_scope(fan):
                    continue
                if price <= upsell.OF_PRICE_FLOOR_CENTS:
                    self.outcome("discount", offer_id, "price-at-minimum", fan)
                    continue
                said = postsale.inbound_since(con, self.db, fan, offered_at)
                objected, blocked = _price_ask.objection_state(said)
                if blocked:
                    self.outcome("discount", offer_id, blocked, fan)
                elif _price_ask.beat_after(said, objected):
                    ready.append((offer_id, fan))
        for offer_id, fan in buys:
            if not self._postsale_in_scope(fan):
                continue
            run_at = postsale.post_buy_run_at(self.db, fan, offer_id, now)
            if self.followups.add(fan, "ai_chatter", {"post_buy": offer_id}, run_at):
                # The purchase settles this card: its nudge and any discount are obsolete.
                self.ledger.followups_retire(account_id=self.db, fan_id=fan, kinds=(NUDGE_KIND, DISCOUNT_KIND),
                                             reason="purchased", now=now)
                runner.emit({"event": "post-buy-queued", "fan_id": fan, "offer_id": offer_id})
        for offer_id, fan in ready:
            if self.followups.add(fan, "ai_chatter", {"discount_offer": offer_id}, now):
                runner.emit({"event": "discount-queued", "fan_id": fan, "offer_id": offer_id})

    async def run_postsale_scan(self):
        """The scan is best effort: a failure here never costs a fan turn."""
        try:
            await asyncio.to_thread(self.scan_postsale)
        except Exception as exc:
            runner.emit({"event": "postsale-scan-error", "reason": type(exc).__name__})

    def outcome(self, kind, offer_id, reason, fan):
        """One concise operational outcome per (kind, offer, reason): ids and a fixed reason, never text or media."""
        key = (kind, offer_id, reason)
        if key not in self._outcomes:
            self._outcomes.add(key)
            runner.emit({"event": kind + "-skipped", "reason": reason, "offer_id": offer_id, "fan_id": fan})

    def settle_job(self, job, state, reason):
        if self.ledger.followup_finish(job_id=job["job_id"], state=state, reason=reason, now=runner.now()):
            runner.emit({"event": "followup-" + state, "kind": job["kind"], "fan_id": job["fan_id"],
                         **({"reason": reason} if reason else {})})

    async def followup_turn(self, job):
        """One attempt, never replayed: the job is settled (done / dropped / failed) whatever happens."""
        pilot, ledger, args = self.pilot, self.ledger, self.args
        if job["kind"] != NUDGE_KIND:
            # Discount / post-buy: the saved switch and the offer's own state, re-read at run time (a switch turned off
            # since the job was queued stops it here, before anything is generated or sent).
            why = await asyncio.to_thread(self.postsale_block, job)
            if why:
                return self.settle_job(job, "dropped", why)
        elif not await asyncio.to_thread(self.offer_open):
            return self.settle_job(job, "dropped", "offer-resolved")        # bought, expired or withdrawn
        wait = self.cooldown_left()
        if wait > 0:
            return await self.wait(min(wait + 0.5, self.poll_s * 10))        # FASTT's short post-send cooldown
        from .ledger import FOLLOWUP_DROPS
        try:
            key, anchor = await asyncio.to_thread(ledger.claim_followup, pilot_id=pilot.pilot_id,
                                                  owner_token=pilot.owner_token, job_id=job["job_id"], now=runner.now())
            sources = runner.read_sources(self.path, self.db, args.fan, [anchor["message_id"]])
        except Refused as exc:
            if exc.reason in FOLLOWUP_DROPS or exc.reason in ("source-missing", "source-ids-invalid"):
                return self.settle_job(job, "dropped", exc.reason)
            raise
        outcome = ("failed", "followup-error")
        try:
            error = await self.execute(key, sources, source_id=anchor["message_id"], job=job)
            sent = any(r["state"] == "sent" for r in ledger.receipts(pilot_id=pilot.pilot_id, input_key=key))
            outcome = ("failed", error) if error else ("done", None) if sent else ("dropped", "native-skipped")
        finally:
            self.settle_job(job, *outcome)

    async def run_followups(self):
        """Serve at most one due follow-up. True when a turn was started (the caller then re-reads everything).

        Runs only when no fan message is waiting. A fan held, paused, taken over, restricted or not owned,
        an unresolved send or a changed chat DROPS the job; account-wide limits (allowance, model budget)
        leave it queued until it expires."""
        if self.followups is None:
            return False
        due = await asyncio.to_thread(self.ledger.followups_due, account_id=self.db, now=runner.now(),
                                      fan_id=None if self.all_fans else self.args.fan, limit=FOLLOWUPS_PER_TICK)
        for job in due:
            if self.stop.is_set():
                return False
            fan = job["fan_id"]
            hold = self.fan_holds.get(fan)
            if hold and runner.now() < hold[0]:
                continue
            if self.all_fans and not self._owned(fan):
                self.settle_job(job, "dropped", "fan-not-owned")
                continue
            target = self.child(fan) if self.all_fans else self
            target.last_active = runner.now()
            before = target.turns_started
            try:
                await target.serve_fan(job)
            except Refused as exc:
                target.note(exc.reason)
                if exc.reason.startswith(("allowance-", "model-budget")):
                    raise
                self.settle_job(job, "dropped", exc.reason)
                continue
            except Exception as exc:
                target.note("service-error:" + type(exc).__name__)
                self._hold_fan(fan, "service-error")
                self.settle_job(job, "dropped", "service-error")
                continue
            if target.turns_started > before:
                return True
        return False

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

    async def after_failure(self, reason, key, retry=None):
        """A failed turn: stop for the fan if the ledger says so, else abandon only this input.

        Model failures retain their one-retry bound. A definite provider 429 keeps an unsent message pending
        with a persistent account cooldown. Unknown, dispatching and partly sent inputs are never replayed."""
        pilot, ledger = self.pilot, self.ledger
        if retry is not None and ledger.status(pilot.pilot_id)["state"] == "active":
            kind, after = retry
            provider_defer = kind == "provider" and reason == "provider-rate-limited"
            prior = ledger._requeues.get(key, 0)
            backoff = (max(5.0, after) if after is not None else min(RATE_LIMIT_BACKOFF_S * (prior + 1), RATE_LIMIT_BACKOFF_MAX_S)) \
                if provider_defer else self.hold_s
            # Provider refusals must not consume the separate one-model-retry budget.
            provider_attempts = sum(r["state"] == "not_sent" for r in ledger.receipts(pilot_id=pilot.pilot_id, input_key=key))
            retry_limit = None if provider_defer else MAX_REQUEUES_PROVIDER if kind == "provider" else MAX_REQUEUES_MODEL + provider_attempts
            count = ledger.requeue_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now(),
                                         max_requeues=retry_limit,
                                         not_before=runner.now() + timedelta(seconds=backoff) if provider_defer else None)
            if count:
                runner.emit({"event": "turn-requeued", "reason": reason, "retry": count, "backoff_s": backoff,
                             **({"fan_id": self.args.fan} if self.strict else {})})
                self.backoff_s = backoff
                await self.wait(backoff)
                return
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
        if getattr(self.args, "followups", False):
            from .followups import FollowupQueue
            runner.require_store_schema(self.path, followups=True)
            self.followups = FollowupQueue(self.ledger, self.db, lambda: runner.now())
        if getattr(self.args, "vault_mode", False):
            if getattr(self.args, "content_manifest", None):
                raise Refused("vault-mode-and-manifest")
            from .content import VaultPlan
            self.content = VaultPlan(self.db, "*" if self.all_fans else self.args.fan, self.path)
        elif getattr(self.args, "content_manifest", None):
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


def build_parser():
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
    parser.add_argument("--vault-mode", action="store_true",
                        help="FASTT's stock vault selling (default off); mutually exclusive with --content-manifest")
    parser.add_argument("--followups", action="store_true",
                        help="default off: run FASTT's own delayed re-engage nudge (queued by ai_chatter after an unbought "
                             "paid offer) when due, through the same send boundary; every other background job stays suppressed")
    parser.add_argument("--stop-after-card", action="store_true",
                        help="TEST/PILOT ONLY, default off: set the fan's runtime stop once the paid card is confirmed. "
                             "Production (compose fastt-runner, supervisor children, --all-fans) keeps chatting after a card.")
    return parser


def main(argv=None):
    parser = build_parser()
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
