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
import copy
import logging
import os
import secrets
import threading
import signal
import time
from datetime import datetime, timedelta, timezone

from twin_bridge import store

from . import postsale, runner
from .engine_state import RunnerLease, WorkBoard
from .followups import DISCOUNT_KIND, NUDGE_KIND
from .ledger import MAX_REQUEUES_MODEL, MAX_REQUEUES_PROVIDER, Ledger, Refused
from .scope import AccountScope
from .send_gate import SendGate
from .tuning import Tuning

LEASE_TTL_S = 120.0
RENEW_S = 30.0
POLL_S = 2.0
FAN_IDLE_RELEASE_S = 900.0
CANDIDATES_MAX = Tuning.from_env().queue_max   # (FASTT_QUEUE_MAX, default 200) fans per tick = size of one scan page / the controls window = the bounded queue
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
MODEL_RETRY_REASONS = (
    "model-timeout", "model-http-failed", "model-response-failed",
    "model-http-transient", "model-transport-failed", "media-claim-unbacked",
    "instruction-placeholder-held",
    # R2: a reply cut off at the output cap is regenerated once (bounded by MODEL_RETRY_LIMIT) instead of being abandoned.
    "model-output-incomplete",
)
# Reasons a reply ends WITHOUT any send standing and that are not the fan message's fault. The claim is given
# back (Ledger.release_input) so the fan's latest state is answered from scratch once the cause clears; a draft
# is never kept. value = seconds this fan is left alone before it is looked at again.
RELEASE_PREFIXES = ("fan-", "account-")
# The turn made no send because a human (an external Infloww/OnlyFans-app message) chatted inside the cautious-resume
# window. Still an outcome of "no_reply" (startswith native-no-reply), but now it names the cause.
EXTERNAL_HOLD = "native-no-reply-external-message"
RELEASE_BACKOFF_S = {"new-message-during-generation": 1.0, "source-changed": 1.0, "source-text-changed": 1.0,
                     "source-time-changed": 1.0, "source-invalidated": 1.0, "source-missing": 1.0,
                     "send-queue-timeout": 15.0, "allowance-wait-timeout": 15.0, "credential-change": 5.0,
                     "runner-lease-lost": 30.0, "authority-lost-before-post": 5.0,
                     # genuine holds, surfaced with their reason; the message is answered once they clear
                     "creator-persona-not-approved": 60.0, "creator-facts-not-current": 60.0,
                     "twin-context-unavailable": 30.0, "service-stopping": 1.0,
                     # account-wide limits: the message stays pending with the reason, the whole account waits
                     "allowance-disabled": 30.0, "allowance-exhausted": 30.0, "allowance-missing": 30.0, "allowance-daily-cap": 60.0,
                     "account-credential-changed": 2.0, "account-auth-failed": 300.0}
ACCOUNT_BLOCK_PREFIXES = ("allowance-", "model-budget", "account-")
# A reply the model could not produce (timeout, HTTP error such as a 402, budget exhausted) is kept PENDING with its
# reason and retried with backoff; it is never dropped. Only an input older than the horizon is let go (surfaced).
MODEL_RETRY_LIMIT = 1
MODEL_BACKOFF_MIN_S = 15.0
MODEL_BACKOFF_MAX_S = 900.0
RATE_LIMIT_BACKOFF_S = 60.0   # a provider 429: wait this long (times the retry number) unless Retry-After says otherwise
RATE_LIMIT_BACKOFF_MAX_S = 300.0

# skip_list reasons a human (or OnlyFans) wrote to say "stop automating this fan".
COOLDOWN_WAIT_MAX_S = 120.0
STOP_REASONS = ("manual_restrict", "of_restricted", "muted_creator", "ladder_stop")


def _hold_decision(path, account: str, fan: int, strict: bool, action: str, *, own_attempt=None):
    """Read the durable facts through the FASTT runner's connection and ask the ONE shared policy (R1)."""
    from onlyfans_runtime import conversation_policy as policy, operator_cases
    with runner.readonly(path) as con:
        skip, critical, cases = operator_cases.policy_facts_sql(con, account, fan, store.has_table)
        mode, takeover, unresolved = None, "none", False
        if store.has_table(con, "of_conversation_state"):
            try:
                row = con.execute("SELECT mode,takeover_state,unresolved_attempt_id FROM of_conversation_state "
                                  "WHERE account_id=? AND fan_id=?", (account, fan)).fetchone()
            except Exception:                    # an older fixture without the column keeps its old meaning
                row = con.execute("SELECT mode,takeover_state,NULL FROM of_conversation_state "
                                  "WHERE account_id=? AND fan_id=?", (account, fan)).fetchone()
            if row:
                mode, takeover, unresolved = row[0], row[1], row[2] is not None
                # The final wire check supplies this only after the ledger has
                # validated its owner and receipt. Admission sets the pointer,
                # so this attempt must not hold itself. Other pointers still hold.
                if own_attempt is not None:
                    from .reporting import attempt_ref
                    if row[2] == attempt_ref(own_attempt):
                        unresolved = False
                if unresolved and store.has_table(con, "native_pilot_receipts") and store.has_table(con, "twin_bridge_send_attempts"):
                    from .ledger import settled_pointer
                    unresolved = not settled_pointer(con, account, fan, row[2])
        elif strict:
            return policy.Decision(False, "not_enabled")
    # A missing control row is not a stop (single-fan mode); the policy turns it into "never enabled" when strict.
    return policy.decide(policy.Facts(mode=mode, takeover_state=takeover or "none", skip_reason=skip,
                                      unresolved=unresolved, critical_flag=critical, cases=cases),
                         "automated", action, strict=strict)


def fan_hold_reason(path, account: str, fan: int, strict: bool = False, *, own_attempt=None):
    """The ONE predicate for "this fan must not be answered right now".

    A caller of the shared conversation policy (``onlyfans_runtime.conversation_policy``), the same function the worker
    plane's ``TextAuthority._eligibility`` calls: it reads the durable operator stop (``skip_list``), the control
    projection (``of_conversation_state``), unresolved delivery, a critical safety flag and the open operator cases,
    and returns the historic fixed reason string, or None. ``strict`` (all-fans mode) treats no row as "never enabled".
    Plain chat only; ``fan_commercial_hold_reason`` adds the commercial-scope holds (hardship, complaint).
    """
    from onlyfans_runtime import conversation_policy as policy
    return policy.fan_hold_code(_hold_decision(path, account, fan, strict, policy.CHAT, own_attempt=own_attempt))


def fan_commercial_hold_reason(path, account: str, fan: int, strict: bool = False, *, own_attempt=None):
    """Hold check for a priced card, an offer or a proactive follow-up: everything above plus commercial-scope cases."""
    held = fan_hold_reason(path, account, fan, strict, own_attempt=own_attempt)
    if held:
        return held
    from onlyfans_runtime import conversation_policy as policy
    return policy.fan_hold_code(_hold_decision(path, account, fan, strict, policy.COMMERCIAL, own_attempt=own_attempt))


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

    def _dispatch_hold(self, terms, attempt):
        predicate = (fan_commercial_hold_reason if self._hold_for(terms) is self.commercial_hold_check
                     else fan_hold_reason)
        service = self.service
        return predicate(service.path, service.db, service.args.fan, *service.strict_arg, own_attempt=attempt.id)

    def __init__(self, service, pilot, **kwargs):
        super().__init__(service.ledger, pilot, service.client, asyncio.get_running_loop(), **kwargs)
        self.service = service
        self.hold_check = lambda: fan_hold_reason(service.path, service.db, service.args.fan, *service.strict_arg)
        # The same policy for a priced card / offer / follow-up: hardship and complaint stop selling, not chat.
        self.commercial_hold_check = lambda: fan_commercial_hold_reason(service.path, service.db, service.args.fan, *service.strict_arg)
        self.dashboard_handover = bool(service.strict_arg)
        # Engine v2: the account's FIFO send gate, the account-level hold re-read at the wire, tunable waits.
        tuning = getattr(service, "tuning", None) or Tuning.from_env()
        self.send_gate = getattr(service, "send_gate", None)
        self.send_priority = getattr(service, "priority", None)
        self.tolerate_newer = bool(getattr(service, "tolerate_newer", False))
        if getattr(service, "send_gate", None) is not None:
            self.open_offer_blocks = False         # persistent service: an open offer never stops a text reply
        self.eligibility_check = getattr(service, "account_hold", None)
        self.send_wait_max_s = tuning.pace_wait_max_s
        self.throttled_retries = tuning.throttled_retries

    @property
    def deadline(self):  # type: ignore[override]
        return self.service.lease.deadline

    @deadline.setter
    def deadline(self, value):  # the base __init__ assigns the frozen pilot deadline
        pass

    def abort_reason(self):
        """What a reply waiting in the send queue checks about once a second (no database)."""
        svc = self.service
        if self.stopped_reason:
            return self.stopped_reason
        if svc.stop.is_set():
            return "service-stopping"
        if getattr(svc, "cancel_reason", None):
            return svc.cancel_reason
        if getattr(svc.catalog_owner, "runner_lost", False):
            return "runner-lease-lost"
        return None

    def stopped(self):
        if self.stopped_reason:
            return True
        svc = self.service
        if svc.stop.is_set():
            self.stopped_reason = "service-stopping"
        elif getattr(svc, "cancel_reason", None):
            self.stopped_reason = svc.cancel_reason
        elif getattr(svc.catalog_owner, "runner_lost", False):
            self.stopped_reason = "runner-lease-lost"
        elif svc.lease.lost or runner.now() >= datetime.fromisoformat(svc.lease.deadline):
            self.stopped_reason = "authority-lost"
        else:
            state = self.ledger.status(self.pilot.pilot_id)
            if state["state"] != "active":
                self.stopped_reason = state["reason"] or "pilot-stopped"
        return bool(self.stopped_reason)

    def limit_label(self):
        policy = self.ledger.allowance_status(self.pilot.account_id, self.pilot.fan_id, now=runner.now())
        return None if policy is None else policy["limit"] - policy["used"]

    async def _send(self, text, terms=None):
        self.service.phase = "sending"
        try:
            return await super()._send(text, terms)
        finally:
            self.service.phase = "preparing"
            if self.turn_sends:
                # The reply is out: only the remaining bubbles wait, so the fan no longer holds a preparation slot.
                self.service.first_bubble_sent = True

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
            policy = self.ledger.allowance_status(self.pilot.account_id, self.pilot.fan_id, now=runner.now())
            if policy is None:
                raise Refused("allowance-missing")
            if not policy["enabled"]:
                raise Refused("allowance-disabled")
            if policy["used"] >= policy["limit"]:
                raise Refused("allowance-exhausted")
            if policy.get("daily_limit") is not None and policy["daily_used"] >= policy["daily_limit"]:
                raise Refused("allowance-daily-cap")
            wait = 0.0
            if policy.get("delivery_mode") == "concurrent":
                for stamp in (policy.get("cooldown_until"), policy.get("conversation_next_send_at")):
                    if stamp:
                        wait = max(wait, (datetime.fromisoformat(stamp) - runner.now()).total_seconds())
                wait = max(wait, float(policy.get("provider_wait_s") or 0.0))
            else:
                wait = 1.0 if policy["busy"] else 0.0
                if policy["next_dispatch_at"]:
                    wait = max(wait, (datetime.fromisoformat(policy["next_dispatch_at"]) - runner.now()).total_seconds())
            if wait <= 0:
                return
            if waited >= self.send_wait_max_s:
                raise Refused("allowance-wait-timeout")
            step = min(wait, 1.0)
            await self.service.idle(step)
            waited += step


# Thread pool sizing factor (unchanged from before the send-order work): preparation slots x this + 12 threads.
LIVE_TURNS_PER_SLOT = 3
# Hard cap on live turns (preparing + parked at the send gate) per preparation slot. Parked turns hold no worker thread,
# but each keeps engine state and a claimed input, so their number is bounded. Only a turn that has not sent its first
# bubble is refused at the cap; a parked turn finishing its later bubbles is never starved by it.
LIVE_TURNS_CAP_PER_SLOT = 8


class Service:
    tuning = Tuning()          # class default (objects built without __init__, e.g. query-only tests); __init__ sets the real one

    def __init__(self, args, *, stop=None, ttl_s=LEASE_TTL_S, renew_s=RENEW_S, poll_s=POLL_S, hold_s=HOLD_S,
                 quiet_s=None, connection_check_s=CONNECTION_CHECK_S, intake_factory=None, tuning=None):
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
        self.tuning = tuning or Tuning.from_env(account=self.db)
        self.quiet_s = self.tuning.quiet_s if quiet_s is None else quiet_s
        self.connection_check_s = connection_check_s
        # Engine v2 (all-fans runner): bounded concurrent turns, one send gate per account, durable work board.
        # The limit starts at one (legacy) and is raised in run() only when the account policy says concurrent.
        self.send_gate = SendGate(max_concurrency=1)
        self.cancel_reason = None      # set on a per-fan child: its in-flight reply must stop (takeover, pause, credential change)
        self.runner_lost = False       # the account runner lease was lost: nothing may be sent by this process
        self.tasks: dict[int, asyncio.Task] = {}
        self.board = None
        self.runner_lease = None
        self.account_block = None      # (reason, until): an account-wide limit (allowance / model budget) holds new turns
        self.next_lease_renew = 0.0
        self.next_queue_event = 0.0
        self.next_stall_check = 0.0
        self._stall_reported = 0.0
        self.stalled = False           # an alert is open: owed replies have waited past tuning.stall_alert_s
        self.phase = "preparing"
        self.first_bubble_sent = False   # this turn's first bubble is on the wire: the slot is free for another fan
        self.tolerate_newer = False    # this turn answers a burst past the hard maximum wait: later fan messages do not stale it
        self.priority = None           # ISO text: when this fan's first unanswered message was ingested (send-queue order)
        self.release_reason = None
        self.account_block_reason = None   # a released reply was stopped by an account-wide condition (allowance, account hold)
        self.unresolved: dict[int, str] = {}   # fan -> why an admitted send is unsettled (surfaced on the work board)
        self.budget_block = False       # a reply failed on the account's model budget: no fan of the account starts a model call
        self.queue_stats = {}
        self.pilot_id = getattr(args, "service_id", None) or f"svc-{self.scope.provider_id}-{getattr(args, 'fan', None)}"
        self.path = None
        self.ledger = None
        self.client = self.conn = self.sentinels = self.intake = None
        self.pilot = self.lease = None
        self.content = self.context = None
        self.followups = None      # followups.FollowupQueue when --followups, else None (every job suppressed)
        self.prior_catalog = None
        self._control_store_lock = threading.Lock()
        self.catalog_owner = self
        self.next_conn_check = 0.0
        self._session_lock = None
        self.started_at = None
        self.turns_started = 0
        self._outcomes = set()     # (kind, offer, reason) already reported by scan_postsale
        self.backoff_s = 0.0       # set by after_failure when an input was requeued; the all-fans runner turns it into a hold
        self.last_active = runner.now()
        self.last_held = None
        self.reply_decision = None
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
        if self.catalog_owner is not self:
            # All-fans children share the parent's one account session. Refreshing
            # from each child would build and close several clients at once.
            owner = self.catalog_owner
            await owner.ensure_session()
            self.client, self.conn, self.sentinels = owner.client, owner.conn, owner.sentinels
            self.next_conn_check = owner.next_conn_check
            return
        soon = self.conn is not None and self.conn.expires_at.replace(tzinfo=None) <= runner.now() + timedelta(seconds=60)
        if self.client is not None and not soon and time.monotonic() < self.next_conn_check:
            return
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            # Another child may have refreshed while this child waited for the lock.
            soon = self.conn is not None and self.conn.expires_at.replace(tzinfo=None) <= runner.now() + timedelta(seconds=60)
            if self.client is not None and not soon and time.monotonic() < self.next_conn_check:
                return
            conn = await connection.load_verified_connection(self.scope)
            if self.client is None or conn.revision != self.conn.revision or conn.expires_at != self.conn.expires_at:
                if self.client is not None and self.tasks:
                    # Credential change: nothing prepared with the old session may go out with the new one, and the old
                    # client is only closed after every send on it has settled.
                    await self.quiesce("credential-change")
                client = await asyncio.to_thread(connection.build_client, conn)
                me = await asyncio.to_thread(client.me)
                if str(me.get("id")) != self.scope.provider_id or str(me.get("username", "")).lstrip("@").casefold() != self.args.handle.casefold():
                    from of_client import close_client_http
                    close_client_http(client)
                    raise Refused("live-account-mismatch")
                sentinels = await attribution.require_sentinels()
                old, self.client, self.sentinels = self.client, client, sentinels
                if self.intake is not None:
                    await self.intake.stop()
                self.start_intake(conn)
                if old is not None:
                    from of_client import close_client_http
                    close_client_http(old)
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
                                         approved_item=self.approved_catalog_ids(), open_offer_blocks=False)
        if not self.ensured:
            await ensure_fan_conversation(self.scope, self.args.fan)
            self.ensured = True
        token = "native-" + secrets.token_hex(24)
        pilot = await runner.database_call(self.ledger, self.ledger.activate_service, pilot_id=self.pilot_id, account_id=self.db, fan_id=self.args.fan,
                                             owner_token=token, now=runner.now(), ttl_s=self.ttl_s, cutoff=self.cutoff)
        if not await reserve_ownership(account_id=self.db, fan_id=self.args.fan, owner_token=token,
                                       now=runner.now(), ttl_s=self.ttl_s, io_offload=self.ledger.pg):
            await runner.database_call(self.ledger, self.ledger.release_service, pilot_id=self.pilot_id, owner_token=token, now=runner.now())
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
                renew = dict(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token,
                             now=runner.now(), ttl_s=self.ttl_s)
                deadline = await runner.database_call(self.ledger, self.ledger.renew, **renew)
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
        store = self.control_store(url)
        controlled = [self.args.fan] if not self.all_fans else (fans or [])
        candidates = (store.runtime_stop_candidates(self.db, controlled)
                      if self.all_fans and hasattr(store, "runtime_stop_candidates") else None)
        for fan in controlled:
            if not self._owned(fan):
                continue
            if candidates is not None and fan not in candidates:
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

        ledger = getattr(self, "ledger", None)
        unl_sql, unl_args = ledger.unlabelled_clause("n", now) if ledger is not None else ("", ())
        base = ("SELECT m.fan_id,m.message_id,m.created_at FROM messages m WHERE m.account_id=? AND m.direction='in' "
                "AND m.created_at>? AND NOT EXISTS (SELECT 1 FROM messages n WHERE n.account_id=m.account_id "
                "AND n.fan_id=m.fan_id AND NOT (n.direction='out' AND n.is_unsent) "
                + (f"AND NOT {unl_sql} " if unl_sql else "") +
                "AND (n.created_at>m.created_at OR (n.created_at=m.created_at AND n.message_id>m.message_id)))")
        serve, controlled = [], []
        with runner.readonly(self.path) as con:
            if store.has_table(con, "native_pilot_receipts"):
                # A confirmed reply to an older input cannot consume a message
                # which arrived during that reply. Every other newer row still blocks.
                base = base[:-1] + " AND NOT (" + older_reply_sql() + "))"
            for fan, _, created in con.execute(base + " ORDER BY m.created_at DESC,m.message_id DESC LIMIT ?",
                                               (self.db, since, *unl_args, CANDIDATES_MAX)).fetchall():
                if _dt(created) > since and is_owned(fan) and fan not in controlled:
                    controlled.append(fan)
            if store.has_table(con, "of_conversation_state"):  # strict: no projection table = nobody enabled
                sql, params = base, [self.db, since, *unl_args]
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
            child.send_gate, child.tuning = self.send_gate, self.tuning
            child.path, child.ledger, child.context = self.path, self.ledger, self.context
            # NativeEngine/ContextualAuthority bind a turn's quote, inventory
            # revision and commercial hold to this object. Sharing the account
            # plan lets another fan reset those fields while this one sends.
            child.content, child.catalog_owner = copy.deepcopy(self.content), self
            if child.content is not None and getattr(child.content, "vault_mode", False):
                child.content.fan = fan
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
                    await runner.database_call(self.ledger, self.ledger.release_service, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now())
            except Refused:
                pass
            clear_owner(pilot.account_id, pilot.fan_id, pilot.owner_token)

    def already_claimed(self, fan, message_id):
        with runner.readonly(self.path) as con:
            if not store.has_table(con, "native_pilot_inputs"):
                return False  # nothing has ever been claimed on this store
        return self.ledger.input_claimed(pilot_id=f"svc-{self.scope.provider_id}-{fan}", message_id=message_id)

    SLOW_HOLDS = ("delivery-unresolved", "delivery-unknown", "writer-unresolved", "twin-work-open", "ownership-unavailable",
                  "owner-live", "canonical-control-held", "canonical-delivery-unresolved")

    def _hold_fan(self, fan, reason):
        # A fan with an unsettled send (or a competing owner) is looked at rarely: every look can wait on the writer-marker
        # quiesce, which would otherwise occupy one of the concurrent preparation slots on every retry.
        wait = self.poll_s if reason.startswith("fan-") else 4 * self.hold_s if reason in self.SLOW_HOLDS else self.hold_s
        self.fan_holds[fan] = (runner.now() + timedelta(seconds=wait), reason)

    # ── engine v2: bounded concurrent preparation, strictly ordered per fan ───────
    def burst_state(self, fans):
        """``{fan: (newest inbound ingested_at, first ingested_at of the unanswered burst)}`` in ONE read.

        The burst is the fan's inbound messages after our last outbound and after the activation watermark,
        which is exactly what the ledger would answer together. Drives the quiet window and its hard maximum."""
        from .ledger import _dt
        if not fans:
            return {}
        since = _dt(self.started_at)
        marks = ",".join("?" for _ in fans)
        with runner.readonly(self.path) as con:
            rows = con.execute(
                "SELECT m.fan_id,MAX(m.ingested_at),MIN(m.ingested_at) FROM messages m WHERE m.account_id=? AND m.fan_id IN (" + marks + ") "
                "AND m.direction='in' AND m.created_at>? AND m.created_at>COALESCE((SELECT MAX(n.created_at) FROM messages n "
                "WHERE n.account_id=m.account_id AND n.fan_id=m.fan_id AND n.direction='out' AND NOT n.is_unsent),?) GROUP BY m.fan_id",
                (self.db, *fans, since, since)).fetchall()
        return {r[0]: (_dt(r[1]), _dt(r[2])) for r in rows}

    def account_hold(self):
        """Account-level reason NOT to send right now (runtime / policy / binding / credential), else None.

        The same facts the supervisor uses to decide whether an account may run, re-read at the wire so a
        credential change, a paused account or a revoked binding stops a reply that was prepared before it.
        A store without those tables (fixtures) or a row that is simply absent expresses no opinion."""
        from . import supervisor
        try:
            row = supervisor.read_account(self.path, self.db)
        except Exception:
            return None
        if row is None:
            return None
        if row["runtime_enabled"] is not None and not row["runtime_enabled"]:
            return "account-runtime-disabled"
        if row["binding_revoked_at"] is not None:
            return "account-binding-revoked"
        if row["policy_enabled"] is not None and not row["policy_enabled"]:
            return "account-policy-disabled"
        if row["connection_status"] is not None:
            owner = self.catalog_owner
            if owner.conn is not None:
                try:
                    with runner.readonly(self.path) as con:
                        rev = con.execute("SELECT revision FROM of_provider_connection WHERE account_id=?", (self.db,)).fetchone()
                except Exception:
                    rev = None
                if rev is not None and rev[0] is not None and rev[0] != owner.conn.revision:
                    owner.next_conn_check = 0.0       # reload the session at the next tick
                    return "account-credential-changed"
            if row["connection_status"] != "verified":
                return "account-connection-not-verified"
            if row["last_error"] is not None:
                return "account-connection-check-failing"
            try:
                if row["expires_at"] is not None and supervisor._naive_utc(row["expires_at"]) <= runner.now():
                    return "account-connection-expired"
            except (TypeError, ValueError):
                return "account-connection-expired"
        return None

    def held_reasons(self, fans):
        """``{fan: reason}`` for the in-flight fans that must stop now (takeover, pause, restriction, not owned)."""
        out = {}
        for fan in fans:
            try:
                held = fan_hold_reason(self.path, self.db, fan, True)
                child = self.children.get(fan)
                if held == "fan-unresolved-delivery" and child is not None and child.phase == "sending" and child.pilot is not None:
                    # Admission itself sets the unresolved pointer. Re-read an
                    # exact active owner/receipt pair before excluding that own
                    # pointer; uncertainty or another sender still holds.
                    with runner.readonly(self.path) as con:
                        own = con.execute("SELECT r.attempt_id FROM native_pilot_receipts r "
                            "JOIN native_pilots p ON p.pilot_id=r.pilot_id "
                            "JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id "
                            "WHERE p.pilot_id=? AND p.owner_token=? AND p.state='active' "
                            "AND p.account_id=? AND p.fan_id=? AND r.state='dispatching' "
                            "AND a.state='dispatching' AND a.owner_token=p.owner_token "
                            "AND a.account_id=p.account_id AND a.fan_id=p.fan_id",
                            (child.pilot.pilot_id, child.pilot.owner_token, self.db, fan)).fetchall()
                    if len(own) == 1:
                        held = fan_hold_reason(self.path, self.db, fan, True, own_attempt=own[0][0])
                    else:
                        # It may have settled between the first policy read
                        # and the owner lookup. Do not cancel on that stale read.
                        held = fan_hold_reason(self.path, self.db, fan, True)
            except Exception:
                held = None
            if held:
                out[fan] = held
            elif not self._owned(fan):
                out[fan] = "fan-not-owned"
        return out

    def _status(self, fan, state, reason=None, **kw):
        if self.board is not None:
            self.board.set(fan, state, reason, **kw)

    def preparing_count(self):
        return sum(1 for f in self.tasks if not getattr(self.children.get(f), "first_bubble_sent", False))

    def has_preparation_slot(self):
        """A fan whose first bubble is out only waits at the send gate for the rest of its bubbles. That wait is a
        parked coroutine (no worker thread, see Gateway.send_async), so it does not count against the preparation
        bound. Turns that have not sent their first bubble are refused at the hard live-turn cap
        (LIVE_TURNS_CAP_PER_SLOT x concurrency); parked turns finishing their bubbles are never refused by it."""
        return (self.preparing_count() < self.tuning.concurrency
                and len(self.tasks) < self.tuning.concurrency * LIVE_TURNS_CAP_PER_SLOT)

    async def _run_fan(self, fan, first=None):
        """One fan's whole turn as a task. Never raises: every outcome becomes a hold / board state."""
        child = self.child(fan)
        child.last_active = runner.now()
        child.cancel_reason = None
        child.phase = "preparing"
        child.first_bubble_sent = False
        child.priority = first
        before = child.turns_started
        try:
            await child.serve_fan()
        except Refused as exc:
            child.note(exc.reason)
            if exc.reason.startswith(ACCOUNT_BLOCK_PREFIXES):
                # Account-wide (allowance, model budget): stop starting turns for everyone until it clears.
                wait = BUDGET_HOLD_S if exc.reason.startswith("model-budget") else self.hold_s
                self.account_block = (exc.reason, runner.now() + timedelta(seconds=wait))
            else:
                self._hold_fan(fan, exc.reason)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            child.note("service-error:" + type(exc).__name__)
            self._hold_fan(fan, "service-error")
            return
        if child.account_block_reason:
            reason, child.account_block_reason = child.account_block_reason, None
            self.account_block = (reason, runner.now() + timedelta(seconds=RELEASE_BACKOFF_S.get(reason, self.hold_s)))
        if child.budget_block:
            child.budget_block = False
            self.account_block = ("model-budget-exhausted", runner.now() + timedelta(seconds=BUDGET_HOLD_S))
        if child.backoff_s:
            # A requeued / released input (provider 429, model failure, takeover, stale draft): leave the fan alone while it backs off.
            self.fan_holds[fan] = (runner.now() + timedelta(seconds=child.backoff_s), child.release_reason or "retry-backoff")
            child.backoff_s = 0.0
            child.release_reason = None
        if child.turns_started == before:
            # Nothing was ready after all (quiet window in the ledger's own clock, a claim that already exists): look at
            # this fan again after a poll interval instead of starting a task for it on every tick.
            self.fan_holds.setdefault(fan, (runner.now() + timedelta(seconds=self.poll_s), "not-ready"))
            if self.fan_holds[fan][0] <= runner.now():
                self.fan_holds[fan] = (runner.now() + timedelta(seconds=self.poll_s), "not-ready")
        return child.turns_started > before

    def _reap(self):
        for fan, task in list(self.tasks.items()):
            if task.done():
                del self.tasks[fan]
                if not task.cancelled() and task.exception() is not None:
                    runner.emit({"event": "turn-task-error", "fan_id": fan, "reason": type(task.exception()).__name__})
                    self._hold_fan(fan, "service-error")

    async def cancel_inflight(self, reason, fans=None):
        """Ask in-flight replies to stop (they leave the send queue / abort the model call at their next check).
        Nothing is torn down: a POST already on the wire finishes and is reconciled by the ledger."""
        for fan in list(self.tasks) if fans is None else fans:
            child = self.children.get(fan)
            if child is not None and fan in self.tasks and child.cancel_reason is None:
                child.cancel_reason = reason
                runner.emit({"event": "turn-cancel-requested", "fan_id": fan, "reason": reason})

    async def drain_tasks(self, timeout_s):
        """Wait for every in-flight task; whatever is left after the grace is hard-cancelled."""
        tasks = list(self.tasks.values())
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._reap()

    async def quiesce(self, reason):
        """Stop starting turns, cancel what is queued or generating, wait for any send on the wire to settle."""
        await self.cancel_inflight(reason)
        await self.drain_tasks(self.tuning.drain_grace_s)

    def renew_runner_lease(self):
        if self.runner_lease is None:
            return
        now = time.monotonic()
        if now < self.next_lease_renew:
            return
        self.next_lease_renew = now + self.tuning.runner_lease_ttl_s / 3
        try:
            ok = self.runner_lease.renew(runner.now())
        except Exception:
            runner.emit({"event": "runner-lease-renew-failed"})
            return
        if not ok:
            self.runner_lost = True
            runner.emit({"event": "runner-lease-lost"})

    def queue_event(self, ready, waiting, oldest_s):
        now = time.monotonic()
        if now < self.next_queue_event:
            return
        self.next_queue_event = now + self.tuning.status_every_s
        self.queue_stats = {"pending": len(ready) + len(waiting), "ready": len(ready), "quiet": len(waiting),
                            "in_flight": len(self.tasks), "send_waiting": self.send_gate.waiting,
                            "preparing": self.preparing_count(), "oldest_wait_s": round(oldest_s, 1), "concurrency": self.tuning.concurrency}
        runner.emit({"event": "engine-queue", **self.queue_stats})

    @classmethod
    def owed_hold(cls, reason) -> bool:
        """A fan held for a reason that is the ENGINE's to resolve (a failed model call waiting to be retried, a provider
        cooldown, a transient error) is still owed a reply, so it counts toward a stall. A fan held for a person's decision
        (takeover, pause, restriction, not owned, an unsettled or unknown delivery) is not."""
        reason = str(reason or "")
        return not (reason.startswith(("fan-", "delivery-", "canonical-")) or reason in cls.SLOW_HOLDS)

    async def stall_check(self, ready, waiting, blocked, now):
        """B5: tell someone when work is owed and not moving.

        "Owed" = fans the engine itself should answer now (ready or in their quiet window, not in a fan hold such as a
        takeover or an unsettled send), measured from the first unanswered message of the burst. When the oldest has
        waited past ``stall_alert_s`` (default 10 min) the engine emits ``send-stall`` (once per change of state) and
        keeps an ``engine_alert`` row open; ``moving`` says whether anything was sent in the same window (a slow but
        moving queue is a backlog, nothing sent is a stall). The row is cleared when no owed reply is past the threshold.
        Cheap: evaluated at most once per ``status_every_s``; a failure here never touches a reply."""
        mono = time.monotonic()
        if mono < self.next_stall_check:
            return
        self.next_stall_check = mono + self.tuning.status_every_s
        threshold = self.tuning.stall_alert_s
        # Candidates: fans the engine is about to start (ready / quiet window) and fans it has set aside for a reason that
        # is its own to resolve (a failed model call waiting for its retry, a provider cooldown). Fans held for a person's
        # decision are not owed. Ages run from the first unanswered message of the burst.
        aged = [(( now - datetime.fromisoformat(first)).total_seconds(), fan, mid)
                for fan, mid, first in ready + waiting
                if first is not None and not ((h := self.fan_holds.get(fan)) and now < h[0] and not self.owed_hold(h[1]))]
        held = [fan for fan, (until, reason) in self.fan_holds.items() if now < until and self.owed_hold(reason)
                and fan not in {f for _a, f, _m in aged}]
        try:
            if held:
                bursts = await asyncio.to_thread(self.burst_state, held[:200])
                aged += [((now - datetime.fromisoformat(first)).total_seconds(), fan, None) for fan, (_n, first) in bursts.items()]
            aged.sort(reverse=True)
            # A message the engine already worked and deliberately did not answer stays "unanswered" forever (it is never
            # regenerated): it is not owed. Look at the oldest few only; this runs at most every status_every_s.
            owed = []
            for age, fan, mid in aged:
                if age <= threshold:
                    break
                if mid is not None and await asyncio.to_thread(self.already_claimed, fan, mid):
                    continue
                owed.append(age)
                if len(owed) >= 50:
                    break
            oldest, count = (owed[0] if owed else 0.0), len(owed)
        except Exception as exc:
            runner.emit({"event": "send-stall-check-failed", "reason": type(exc).__name__})
            return
        try:
            if oldest <= threshold:
                if self.stalled:
                    self.stalled = False
                    await asyncio.to_thread(self.ledger.clear_alert, self.db, Ledger.ALERT_SEND_STALL, now)
                    runner.emit({"event": "send-stall-cleared", "account": self.db})
                return
            sent_recently = await asyncio.to_thread(self.ledger.sent_since, self.db, now - timedelta(seconds=threshold))
            detail = {"oldest_wait_s": round(oldest, 1), "owed_past_threshold": count, "moving": bool(sent_recently),
                      "in_flight": len(self.tasks), "send_waiting": self.send_gate.waiting, "blocked": blocked or None,
                      "threshold_s": threshold}
            try:
                opened = await asyncio.to_thread(self.ledger.raise_alert, self.db, Ledger.ALERT_SEND_STALL, now, detail)
            except Exception as exc:        # the event below is the signal even when the durable row cannot be written
                opened = not self.stalled
                detail["alert_row"] = type(exc).__name__
            self.stalled = True
            if opened or mono >= self._stall_reported:
                self._stall_reported = mono + 300.0          # a still-stalled account repeats the event every 5 minutes
                runner.emit({"event": "send-stall", "account": self.db, **detail})
        except Exception as exc:
            runner.emit({"event": "send-stall-check-failed", "reason": type(exc).__name__})

    async def tick_all(self):
        await runner.database_call(self.ledger, self.renew_runner_lease)
        if self.runner_lost:
            await self.cancel_inflight("runner-lease-lost")
            self.stop.set()
            return
        self._reap()
        pending, controlled = await asyncio.to_thread(self.pending_fans)
        now = runner.now()
        # Takeover / pause / restriction landing while a reply is queued or being generated stops it now.
        if self.tasks:
            for fan, reason in (await asyncio.to_thread(self.held_reasons, list(self.tasks))).items():
                await self.cancel_inflight(reason, [fan])
        # Burst coalescing: a fan whose messages are still arriving waits for the quiet window, but never longer
        # than the hard maximum from the first unanswered message.
        bursts = await asyncio.to_thread(self.burst_state, [fan for fan, _ in pending])
        from .ledger import _dt
        ready, waiting, oldest = [], [], 0.0
        for fan, mid in pending:
            newest, first = bursts.get(fan, (None, None))
            if newest is not None:
                oldest = max(oldest, (now - datetime.fromisoformat(first)).total_seconds())
            quiet_for = (now - datetime.fromisoformat(newest)).total_seconds() if newest else None
            waited = (now - datetime.fromisoformat(first)).total_seconds() if first else None
            if newest is not None and quiet_for < self.quiet_s and waited < self.tuning.burst_max_wait_s:
                waiting.append((fan, mid, first))
            else:
                ready.append((fan, mid, first))
        # R8: oldest NEED first. pending_fans orders by each fan's newest unanswered message, so a fan who kept typing
        # (earliest unresolved input oldest, newest input latest) would start behind every quieter fan. The user-visible
        # clock runs from the earliest unresolved input, so start in that order (stable for ties and unknown starts).
        ready.sort(key=lambda r: (r[2] is None, r[2] or ""))
        # Start work, oldest first, up to the concurrency bound. The rest stays in the database (the durable queue).
        blocked = self.account_block and now < self.account_block[1] and self.account_block[0]
        if self.account_block and not blocked:
            self.account_block = None
        if not blocked:
            # Backpressure: replies prepared now would only wait for the send gate. When the projected wait (the
            # account's next allowed send, e.g. a provider 429 cooldown, plus the replies already queued) is beyond
            # the horizon, start nothing new; the messages stay in the database and are answered when it clears.
            policy = await asyncio.to_thread(self.ledger.allowance_status, self.db, None, now=now)
            self.send_gate.set_limit(
                self.tuning.send_concurrency
                if policy and policy.get("delivery_mode") == "concurrent" else 1)
            gate_in = 0.0
            if policy and policy.get("delivery_mode") == "concurrent":
                stamp = policy.get("cooldown_until")
                if stamp:
                    gate_in = max(0.0, (datetime.fromisoformat(stamp) - now).total_seconds())
                if gate_in > 0:
                    blocked = "provider-cooldown"
            else:
                if policy and policy["next_dispatch_at"]:
                    gate_in = max(0.0, (datetime.fromisoformat(policy["next_dispatch_at"]) - now).total_seconds())
                interval = max(self.tuning.min_send_interval_s, (policy or {}).get("throttle_seconds") or 0)
                horizon = interval * (self.tuning.concurrency + 1) + 30
                if gate_in + self.send_gate.waiting * interval > horizon:
                    blocked = "send-backlog" if gate_in <= horizon else "provider-cooldown"
            if policy and policy.get("daily_limit") is not None and policy["daily_used"] >= policy["daily_limit"]:
                blocked = "allowance-daily-cap"       # the account's daily cap: start nothing, spend no model call on a reply that cannot go
        started = 0
        for fan, mid, first in ready:
            if self.stop.is_set():
                break
            if fan in self.tasks:
                continue
            hold = self.fan_holds.get(fan)
            if hold and now < hold[0]:
                self._status(fan, "held", hold[1], message_id=mid, waiting_since=first)
                continue
            if blocked:
                self._status(fan, "blocked", blocked, message_id=mid, waiting_since=first)
                continue
            if not self.has_preparation_slot():
                self._status(fan, "waiting", "concurrency-limit", message_id=mid, waiting_since=first)
                continue
            # A durable hold is not preparation work. Otherwise an older held
            # fan can take the only slot on every poll, only to be refused by
            # its child, indefinitely starving an eligible neighbour. The
            # child and final wire gate still re-read authority independently.
            reason = await asyncio.to_thread(fan_hold_reason, self.path, self.db, fan, True)
            if reason:
                self._hold_fan(fan, reason)
                self._status(fan, "held", reason, message_id=mid, waiting_since=first)
                continue
            if self.already_claimed(fan, mid):
                continue
            self._status(fan, "preparing", None, message_id=mid, waiting_since=first)
            self.tasks[fan] = asyncio.get_running_loop().create_task(self._run_fan(fan, first), name=f"fan-{fan}")
            started += 1
        for fan, mid, first in waiting:
            self._status(fan, "quiet", "burst-quiet-window", message_id=mid, waiting_since=first)
        live = {f for f, _ in pending} | set(self.tasks) | set(self.unresolved)
        if self.board is not None:
            for fan in list(self.board._known) + list(self.board._dirty):
                if fan not in live:
                    self.board.clear(fan)
            for fan in self.tasks:
                self._status(fan, "sending" if getattr(self.children.get(fan), "phase", "") == "sending" else "preparing")
        self.queue_event(ready, waiting, oldest)
        await self.stall_check(ready, waiting, blocked, now)
        try:
            await asyncio.to_thread(self._twin_controls, controlled)
        except Exception as exc:
            runner.emit({"event": "twin-controls-error", "reason": (str(exc) or type(exc).__name__)[:120]})
        # Give back the lease of any fan quiet for a while (bounds renewal tasks and ownership).
        for fan, child in list(self.children.items()):
            if child.pilot is not None and fan not in controlled and fan not in self.tasks and \
                    (runner.now() - child.last_active).total_seconds() > FAN_IDLE_RELEASE_S:
                await self.release_child(fan)
        if not self.tasks and not ready:
            await self.run_postsale_scan()
            if await self.run_followups():
                return  # a follow-up turn started: re-read controls and the queue first
        if self.board is not None and self.board.pending():
            try:
                await asyncio.to_thread(self.board.flush, runner.now())
            except Exception:
                runner.emit({"event": "work-board-flush-failed"})
        self.note(None)
        waiters = [asyncio.ensure_future(self.stop.wait())] + list(self.tasks.values())
        try:
            await asyncio.wait(waiters, timeout=self.poll_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiters[0].cancel()
            await asyncio.gather(waiters[0], return_exceptions=True)

    async def tick(self):
        try:
            await self.ensure_session()
        except Refused as exc:
            if self.all_fans and self.tasks:
                # Credential / connection problem: queued and generating replies stop; a send already on the wire settles.
                await self.cancel_inflight("account-" + exc.reason)
            raise
        await self.reconcile_pass()
        policy = None if self.all_fans else self.ledger.allowance_status(self.db, self.args.fan)
        if policy is not None:
            self.send_gate.set_limit(
                self.tuning.send_concurrency if policy.get("delivery_mode") == "concurrent" else 1)
        gate_stamp = None
        if policy:
            gate_stamp = policy.get("cooldown_until") if policy.get("delivery_mode") == "concurrent" else policy["next_dispatch_at"]
        if gate_stamp and not self.all_fans:
            # (All-fans mode keeps preparing replies while the send gate is closed: only the projected send
            # backlog limits new work, see tick_all. Single-fan mode keeps the original behaviour.)
            wait = (datetime.fromisoformat(gate_stamp) - runner.now()).total_seconds()
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

    async def reconcile_pass(self):
        """Settle crashed / unknown admitted sends from the provider's history (autoreconcile). Best effort."""
        now = time.monotonic()
        if now < getattr(self, "_next_reconcile", 0.0):
            return
        self._next_reconcile = now + self.tuning.reconcile_wait_s
        from .autoreconcile import reconcile_unresolved
        try:
            seen = set()
            for result in await reconcile_unresolved(self):
                if result["outcome"] == "unresolved":
                    self.unresolved[result["fan_id"]] = result["reason"]
                    seen.add(result["fan_id"])
                    if self.board is not None:
                        self.board.set(result["fan_id"], "blocked", result["reason"])
            for fan in [f for f in self.unresolved if f not in seen]:
                del self.unresolved[fan]       # settled (proven, or opted-in absent): no longer blocked
        except store.DB_ERRORS:
            pass            # a busy / locked store: try again at the next interval
        except Exception as exc:
            runner.emit({"event": "reconcile-error", "reason": type(exc).__name__})

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
        policy = self.ledger.allowance_status(self.db, self.args.fan, now=runner.now())
        if policy is None or not policy["enabled"] or policy["used"] >= policy["limit"]:
            raise Refused("allowance-disabled" if policy is not None and not policy["enabled"]
                          else "allowance-exhausted" if policy else "allowance-missing")
        if policy.get("daily_limit") is not None and policy["daily_used"] >= policy["daily_limit"]:
            raise Refused("allowance-daily-cap")       # the account's daily cap: no model call is spent on a reply that cannot be sent
        self.note(None)
        if job is not None:
            return await self.followup_turn(job)
        await self.turn()

    async def turn(self):
        pilot, ledger, args = self.pilot, self.ledger, self.args
        snap = await runner.database_call(self.ledger, ledger.snapshot, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now())
        self.reply_decision = None
        self.tolerate_newer = False
        if snap is None or ledger.input_claimed(pilot_id=pilot.pilot_id, message_id=snap.message_id):
            return await self.wait(self.poll_s)
        from . import pacing
        flags = pacing.settings(self.path, self.db)
        if any(flags.values()):
            try:
                self.reply_decision = await runner.database_call(self.ledger, pacing.prepare, ledger, pilot, snap, now=runner.now(), quiet_s=self.quiet_s)
            except Refused as exc:
                if exc.reason == "source-changed":
                    return await self.wait(self.poll_s)
                raise
        paced = self.reply_decision and self.reply_decision["schedule"]
        if paced:
            if runner.now() < self.reply_decision["eligible_at"]:
                self.note("reply-target-wait")
                # Waiting for a durable due time is not preparation work. Leave
                # the bounded slot free for other fans, retaining the original
                # schedule/earliest input rather than polling the oldest few.
                self.backoff_s = (self.reply_decision["eligible_at"] - runner.now()).total_seconds()
                self.release_reason = "reply-target-wait"
                return await self.wait(self.poll_s)
        else:
            quiet_for = (runner.now() - datetime.fromisoformat(snap.ingested_at)).total_seconds()
            burst_age = (runner.now() - datetime.fromisoformat(snap.earliest_created_at or snap.created_at)).total_seconds()
            if quiet_for < self.quiet_s and burst_age < self.tuning.burst_max_wait_s:
                # Still typing: wait for the quiet window, but a continuously typing fan is answered after the hard maximum.
                return await self.wait(self.poll_s)
            # Past the hard maximum the reply goes out even if the fan typed on while it was prepared; those later messages
            # are answered next. Before it, a newer message makes the draft stale and it is regenerated.
            self.tolerate_newer = burst_age >= self.tuning.burst_max_wait_s
        wait = self.cooldown_left()
        if wait > 0:
            # FASTT's own short post-send cooldown (fans.automation_paused_until) would make
            # ai_chatter skip the fan; wait it out instead of claiming and abandoning the input.
            return await self.wait(min(wait + 0.5, self.poll_s * 10))
        try:
            sources = runner.read_sources(self.path, self.db, args.fan, getattr(snap, "member_ids", (snap.message_id,)))
            key = await runner.database_call(self.ledger, ledger.claim_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, snapshot=snap, now=runner.now(),
                                     **({"reply_authority": self.reply_decision["authority"]} if self.reply_decision else {}))
        except Refused as exc:
            if exc.reason in ("input-already-claimed", "source-changed"):
                return await self.wait(self.poll_s)
            raise
        await self._note_unlabelled(snap)
        questions = self.identity_questions(sources)
        if questions:
            # Identity rule: the AI never answers "are you a bot / AI / real?". The chat is paused and
            # flagged so a human answers it, then hands back with the normal dashboard controls.
            from .identity import HUMAN_REQUESTED, wants_human
            asked_for_human = any(wants_human((src or {}).get("body")) for src in questions.values())
            await self.identity_handoff("fan-asked", attention=HUMAN_REQUESTED if asked_for_human else "identity-question")
            await runner.database_call(self.ledger, ledger.finish_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now())
            # The handoff answers nothing: close this schedule so the fan's next message gets a fresh admission budget
            # instead of re-using this one (3 admissions, then reply-regeneration-budget-held on every later message).
            if self.reply_decision:
                await runner.database_call(self.ledger, pacing.finish, ledger, self.reply_decision, now=runner.now(), sent=False)
            return
        held = await self.safety_check()
        if held:
            # R1: a stop-contact / distress / consent concern is now an enforced hold (case + audited control). The AI
            # answers nothing; a person owns the case. Same close-out as the identity hand-off above.
            await runner.database_call(self.ledger, ledger.finish_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now())
            if self.reply_decision:
                await runner.database_call(self.ledger, pacing.finish, ledger, self.reply_decision, now=runner.now(), sent=False)
            return
        if self.reply_decision:
            self.reply_decision["admitted_at"] = runner.now()
        error = await self.execute(key, sources, source_id=snap.message_id)
        if self.reply_decision:
            receipts = ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)
            await runner.database_call(self.ledger, pacing.finish, ledger, self.reply_decision, now=runner.now(), error=error,
                          sent=any(r["state"] == "sent" for r in receipts))

    async def _note_unlabelled(self, snap):
        """Surface outbound messages from an UNKNOWN sender after this fan's message: they are not an answer (engine v2)."""
        if not self.ledger.unlabelled_clause("m", runner.now())[0]:
            return
        try:
            with runner.readonly(self.path) as con:
                n = con.execute("SELECT count(*) FROM messages WHERE account_id=? AND fan_id=? AND direction='out' "
                                "AND automation_kind IS NULL AND sent_by_employee_id IS NULL AND created_at>=?",
                                (self.db, self.args.fan, snap.earliest_created_at or snap.created_at)).fetchone()[0]
        except Exception:
            return
        if n:
            runner.emit({"event": "unlabelled-outbound-not-an-answer", "fan_id": self.args.fan, "count": int(n),
                         "source_message_id": snap.message_id})

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
        if job is None and self.reply_decision:
            gateway.reply_decision = self.reply_decision
        # A requeued input already owns bubble numbers (its refused attempts): continue after them.
        prior = ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)
        gateway.sources, gateway.input_key, gateway.bubble = sources, key, max((r["bubble_no"] for r in prior), default=0)
        self.backoff_s = 0.0
        from .contextual import ContextualAuthority
        contextual = ContextualAuthority(self.path, self.db, args.fan, source_id, emit=runner.emit, hold_check=gateway.commercial_hold_check) if job is None else None
        gateway.contextual = contextual
        gateway.followup = job is not None
        if job is not None and job["kind"] == DISCOUNT_KIND:
            # The one card this job may remove (re-read here, not trusted from the queue row).
            row = await asyncio.to_thread(self.offer_row, offer_of(job["kind"], job_payload(job)))
            gateway.expected_unsend = int(row["offer_message_id"]) if row and row["offer_message_id"] else None
        from .turn_timing import TurnTiming
        timing = TurnTiming(forward=runner.emit)
        claimed_at = runner.now()
        model = NativePilotModel(
            api_key=self.conn.model_api_key, account_id=self.db, fan_id=args.fan, deadline=time.monotonic() + TURN_MAX_S,
            should_stop=gateway.stopped, on_event=lambda event: timing.on_event(event, runner.now()),
            reserve_call=lambda: runner.database_call(ledger, ledger.count_model_call, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now()),
            budget=DailyBudget(self.scope, self.conn.model_id, endpoint=ENDPOINT))
        runner.emit({"event": "generating", "source_message_id": str(source_id), "engine": "fastt-native"})
        run_id = await runner.database_call(self.ledger, runner.begin_native_run, self.path, self.db)
        stats = error = None
        try:
            unanswered = await runner.database_call(self.ledger, ledger.unanswered_reply_ids, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token,
                                                     input_key=key, now=runner.now()) if job is None else ()
            with NativeEngine(self.db, args.fan, chat=model.chat, send=gateway.send, send_async=gateway.send_async, emit=runner.emit,
                              content=self.content, context=self.context, contextual=contextual,
                              **({"reply_format": True} if job is None and self.reply_decision
                                 and self.reply_decision["authority"]["flags"].get("native_reply_format_enabled") else {}),
                              **({"unanswered_reply_ids": unanswered} if unanswered else {}),
                              **({"own_offers_only": True} if self.strict else {}), open_offers_ok=True,
                              arm_unsend=gateway.arm_unsend, unsent=lambda: gateway.unsent_ids,
                              unlinked_paid=lambda: self.fan_has_unlinked_payment(args.fan),
                              card_of=lambda mid: ledger.card_evidence(account_id=self.db, fan_id=args.fan, provider_message_id=str(mid)),
                              **({"followups": self.followups} if self.followups is not None else {})) as native:
                stats = await native.run(run_id=run_id, **({"payload": job_payload(job)} if job is not None else {}))
            if gateway.stopped_reason or model.stopped_reason:
                raise Refused(gateway.stopped_reason or model.stopped_reason)
            if not isinstance(stats, dict) or stats.get("errors") or stats.get("cap_hit"):
                raise Refused("native-turn-failed")
            if job is None and not any(r["state"] == "sent" for r in
                    ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)):
                external = int((stats or {}).get("skipped_manual") or 0) > 0
                if external:
                    runner.emit({"event": "external-message-hold", "fan_id": args.fan})
                raise Refused("native-no-reply-empty-output" if (stats or {}).get("dropped_empty")
                              else EXTERNAL_HOLD if external else "native-no-reply")
        except Exception as exc:
            from .engine import PilotBoundaryError
            # A boundary refusal carries its fixed reason (identity held, media claim held, persona not approved, ...);
            # it used to be flattened into "native-turn-failed", which hid it and abandoned the message.
            error = exc.reason if isinstance(exc, Refused) else (
                gateway.stopped_reason or model.stopped_reason
                or (str(exc) if isinstance(exc, PilotBoundaryError) and str(exc) else "native-turn-failed"))
            if error == "native-turn-failed" and getattr(self, "cancel_reason", None) and not gateway.stopped_reason:
                # R8: a reply still waiting in the send queue when it is cancelled (credential change, lease loss, stop,
                # takeover) leaves through SendGate.acquire, which does not set gateway.stopped_reason, so the cancel was
                # reported as "native-turn-failed": the input was marked failed and the fan was never answered. Name the
                # real cause; the existing release/backoff handling then gives the claim back (nothing is on the wire).
                error = self.cancel_reason
        finally:
            await runner.database_call(self.ledger, runner.finish_native_run, self.path, run_id, stats, error, account=self.db)
            if isinstance(stats, dict):
                # FASTT's own per-turn counters (sell lane, offers, packs, skips): operator visibility only.
                runner.emit({"event": "turn-stats", "stats": {k: v for k, v in stats.items()
                    if v and isinstance(v, (int, float, str, bool, dict)) and k not in ("model", "mode")}})
        receipts = ledger.receipts(pilot_id=pilot.pilot_id, input_key=key)
        try:
            # R8 measurement only: one metadata event per turn; a failure here never changes the turn.
            runner.emit(timing.record(
                account=self.db, fan=args.fan, first_input_at=getattr(self, "priority", None), claimed_at=claimed_at,
                generation_started_at=timing.first_model_start or claimed_at,
                generation_done_at=timing.last_model_end or runner.now(), finished_at=runner.now(), receipts=receipts,
                outcome=("job" if job is not None else "error:" + error if error else
                         "sent" if any(r["state"] == "sent" for r in receipts) else "no-reply"),
                requeues=ledger._requeues.get(key, 0)))
        except Exception:
            pass
        from .identity import IDENTITY_HELD
        if error == IDENTITY_HELD:
            await self.identity_handoff("reply-held")
        if error:
            if self.stop.is_set() and not receipts:
                return error  # clean stop: the unsent claim is dropped on release and answered after restart
            # Answer the input again later only when no send attempt stands for it (the ledger re-checks):
            # a model timeout/error, or a POST that certainly did not reach OnlyFans (a provider 429, or a
            # last-instant hold). A definite non-429 refusal and every follow-up job are not retried.
            if job is None and gateway.not_sent is None and (error.startswith(RELEASE_PREFIXES) or error in RELEASE_BACKOFF_S):
                # Not the message's fault and nothing stands on the wire for it: give the claim back, keep no draft.
                released = await asyncio.to_thread(ledger.release_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token,
                                                   input_key=key, now=runner.now())
                if released:
                    self.release_reason = error
                    self.backoff_s = RELEASE_BACKOFF_S.get(error, self.poll_s)
                    if error.startswith(ACCOUNT_BLOCK_PREFIXES) or error.startswith("account-"):
                        self.account_block_reason = error
                    runner.emit({"event": "turn-released", "reason": error, "backoff_s": self.backoff_s,
                                 **({"fan_id": self.args.fan} if self.strict else {})})
                    return error
            retry = None
            if job is None:
                if gateway.not_sent is not None:
                    if gateway.not_sent.reason.startswith("account-"):
                        retry = ("account", None)
                    else:
                        retry = ("provider", gateway.not_sent.retry_after) if gateway.not_sent.retry else None
                elif error in MODEL_RETRY_REASONS or error in ("reply-configuration-changed", "reply-schedule-invalidated"):
                    retry = ("model", None)
                elif error.startswith("model-budget"):
                    retry = ("budget", None)      # the account's daily model budget: pending until it allows, no retry count
            await self.after_failure(error, key, retry=retry)
            return error
        try:
            runner.verify_attribution(self.path, self.db, args.fan, receipts, content=self.content,
                                      automation_employee_id=self.sentinels.automation)
        except Refused as exc:
            await runner.database_call(self.ledger, ledger.stop, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, reason=exc.reason, now=runner.now())
            await runner.report_to_twin(ledger, pilot, exc.reason)
            await self.drop_pilot()
            raise
        await runner.report_to_twin(ledger, pilot)
        await runner.database_call(self.ledger, ledger.finish_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now())
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

    def fan_has_unlinked_payment(self, fan):
        """True when ``fan`` has a paid ppv_message since the current binding that no message explains (excluded or not)."""
        with runner.readonly(self.path) as con:
            return int(fan) in postsale.unlinked_paid_fans(con, self.db)

    def postsale_block(self, job):
        """Why a discount / post-buy job must not run now (a fixed reason), or None."""
        from .followups import job_payload, offer_of
        kind = job["kind"]
        if not postsale.settings(self.path, self.db)["discount" if kind == DISCOUNT_KIND else "post_buy"]:
            return "feature-disabled"
        offer = self.offer_row(offer_of(kind, job_payload(job)))
        if offer is None or int(offer["fan_id"]) != int(job["fan_id"]):
            return "offer-mismatch"
        if self.fan_has_unlinked_payment(job["fan_id"]):
            return "unlinked-payment"      # discount and post-buy both put a priced card on the wire
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
            # The scan proves an explicit price request below before queuing.
            cards = postsale.unpaid_cards(con, self.db, now, 0, include_contextual=True) if st["discount"] else []
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
            if fan in self.tasks:
                continue          # a fan message is being answered: the nudge would be stale anyway
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

    def identity_questions(self, sources):
        """Only new questions trigger a handoff; all sources remain model context."""
        from .identity import sincere_identity_question
        questions = {int(mid): src for mid, src in sources.items()
                     if sincere_identity_question((src or {}).get("body"))}
        pending = self.ledger.unhandled_source_ids(pilot_id=self.pilot_id, source_ids=questions)
        return {mid: src for mid, src in questions.items() if mid in pending}

    def control_store(self, url):
        """Reuse one schema-validated pool; every control operation reads fresh rows."""
        owner = getattr(self, "catalog_owner", self)
        if not hasattr(owner, "_control_store_lock"):
            owner._control_store_lock = threading.Lock()
        with owner._control_store_lock:
            current = getattr(owner, "_twin_store", None)
            if current is None:
                from onlyfans_runtime.store import Store
                current = owner._twin_store = Store(url)
                owner._control_store_url = url
            elif getattr(owner, "_control_store_url", url) != url:
                raise Refused("control-store-target-changed")
            return current

    async def safety_check(self):
        """Run the shared inbound detectors (``operator_cases.scan``) for this fan; True when a hold is now enforced.

        ``shadow`` (the default per creator) records the verdict and returns False; ``live`` opens the operator case
        and applies the audited control. Without the Twin control path (a SQLite fixture) there is nothing to scan.
        """
        url, tenant = os.environ.get("DATABASE_URL", ""), os.environ.get("ONLYFANS_WORKER_TENANT", "")
        if not url or not tenant:
            return False

        def run():
            from onlyfans_runtime import operator_cases
            from onlyfans_runtime.store import Store
            return operator_cases.scan(self.control_store(url), tenant, self.db, self.args.fan)
        try:
            enforced = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 - existing holds stay in force; detection never becomes a send path
            runner.emit({"event": "safety-scan-failed", "fan_id": self.args.fan, "error": type(exc).__name__})
            return False
        if enforced:
            runner.emit({"event": "safety-hold", "fan_id": self.args.fan, "categories": sorted(set(enforced))})
        return bool(enforced)

    async def identity_handoff(self, trigger, attention="identity-question"):
        """Pause the AI for this fan and flag the chat for a human (the identity rule, see identity.py).

        Same control path as ``stop_after_card`` (``Store.control``, action pause_automation) so the
        runtime stop, the dashboard state and the control audit change together; the fan is then
        flagged ``requires_attention``. A human takes over, answers and hands back with the normal
        controls. If the Twin control path is unavailable the runtime stop alone is set (fail closed).
        """
        import hashlib
        from db.engine import get_session
        from onlyfans_runtime import runtime_stop
        from .identity import IDENTITY_REASON
        HUMAN_REQUESTED_REASON = "Fan asked for a real person; a human must answer."
        from .reporting import report_attention
        fan, via = self.args.fan, "twin-control"
        try:
            from onlyfans_runtime.store import Store
            from db.models import OfConversationState
            from sqlalchemy.orm import Session
            url, tenant = os.environ.get("DATABASE_URL", ""), os.environ.get("ONLYFANS_WORKER_TENANT", "")
            if not url or not tenant:
                raise RuntimeError("twin-control-unavailable")

            def pause():
                store = self.control_store(url)
                with Session(store.engine) as s:
                    row = s.get(OfConversationState, (self.db, fan))
                    if row is None or row.mode != "automated":
                        return None        # already not automated: nothing to pause
                    rev = row.revision
                key = hashlib.sha256(f"identity-handoff:{self.db}:{fan}:{rev}".encode()).hexdigest()[:64]
                return store.control(tenant, self.db, fan, mode="paused", expected_revision=rev,
                                     command_key=key, actor="identity-guard",
                                     reason=HUMAN_REQUESTED_REASON if attention == "human-requested" else IDENTITY_REASON,
                                     action="pause_automation")
            await asyncio.to_thread(pause)
            from onlyfans_runtime import operator_cases
            await asyncio.to_thread(lambda: operator_cases.hold(
                self.control_store(url), tenant, self.db, fan, "identity", reason=attention[:64],
                evidence={"trigger": trigger, "attention": attention}, origin="detector"))
        except Exception as exc:  # fail closed: the runtime stop is still written
            async with get_session() as session:
                await session.run_sync(lambda s: runtime_stop.hold(s, self.db, fan, runner.now()))
                await session.commit()
            via = "runtime-stop-only:" + (str(exc)[:60] or type(exc).__name__)
        try:
            await report_attention(account_id=self.db, fan_id=fan, reason=attention)
        except Exception as exc:
            via += ";attention-failed:" + type(exc).__name__
        runner.emit({"event": "identity-handoff", "trigger": trigger, "account_id": self.db, "fan_id": fan, "via": via})

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
            from db.models import OfConversationState
            from sqlalchemy.orm import Session
            url, tenant = os.environ.get("DATABASE_URL", ""), os.environ.get("ONLYFANS_WORKER_TENANT", "")
            if not url or not tenant:
                raise RuntimeError("twin-control-unavailable")

            def pause():
                store = self.control_store(url)
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

    async def provider_throttled(self, retry_after):
        """Apply a provider 429 at account level.

        Legacy mode returns to the 20 s default gap. Concurrent mode records the
        provider's ``Retry-After`` as a durable account cooldown and keeps the
        configured request rate. Either way an alert and event are left, and a
        failure here never costs the reply that is already being deferred.
        """
        try:
            info = await asyncio.to_thread(self.ledger.throttle_backoff, self.db, runner.now(), retry_after=retry_after)
        except Exception as exc:
            info = {"changed": False, "before": None, "after": None, "alert": False, "error": type(exc).__name__}
        runner.emit({"event": "provider-throttled", "account": self.db, "retry_after": retry_after, **info})

    async def after_failure(self, reason, key, retry=None):
        """A failed turn: stop for the fan if the ledger says so, else abandon only this input.

        Model failures retain their one-retry bound. A definite provider 429 keeps an unsent message pending
        with a persistent account cooldown. Unknown, dispatching and partly sent inputs are never replayed."""
        pilot, ledger = self.pilot, self.ledger
        if reason.startswith("account-"):
            self.account_block_reason = reason
            runner.emit({"event": "account-blocked", "reason": reason,
                         **({"fan_id": self.args.fan} if self.strict else {})})
        if retry is not None and ledger.status(pilot.pilot_id)["state"] == "active":
            kind, after = retry
            provider_defer = kind == "provider" and reason == "provider-rate-limited"
            account_defer = kind == "account"
            prior = ledger._requeues.get(key, 0)
            backoff = (max(5.0, after) if after is not None else min(RATE_LIMIT_BACKOFF_S * (prior + 1), RATE_LIMIT_BACKOFF_MAX_S)) \
                if provider_defer else (300.0 if account_defer else self.hold_s)
            # Provider refusals must not consume the separate one-model-retry budget.
            provider_attempts = sum(r["state"] == "not_sent" for r in ledger.receipts(pilot_id=pilot.pilot_id, input_key=key))
            retry_limit = (None if provider_defer or account_defer or kind == "budget" else MAX_REQUEUES_PROVIDER if kind == "provider"
                           else MODEL_RETRY_LIMIT + provider_attempts)
            if kind == "budget":
                backoff = BUDGET_HOLD_S
                self.budget_block = True
            elif kind == "model":
                backoff = min(MODEL_BACKOFF_MAX_S, MODEL_BACKOFF_MIN_S * 2 ** prior)
                if reason == "media-claim-unbacked":      # a regenerate, not an outage: twice at most, straight away
                    backoff, retry_limit = 5.0, 2 + provider_attempts
            count = await runner.database_call(self.ledger, ledger.requeue_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key, now=runner.now(),
                                         max_requeues=retry_limit,
                                         not_before=runner.now() + timedelta(seconds=backoff)
                                         if provider_defer or account_defer else None)
            if provider_defer:
                await self.provider_throttled(after)
            if count:
                runner.emit({"event": "turn-requeued", "reason": reason, "retry": count, "backoff_s": backoff,
                             **({"fan_id": self.args.fan} if self.strict else {})})
                self.backoff_s = backoff
                self.release_reason = reason
                await self.wait(backoff)
                return
        await runner.report_to_twin(ledger, pilot, reason)
        state = ledger.status(pilot.pilot_id)
        if state["state"] != "active":
            # delivery-unknown (or any ledger stop): nothing is resent; wait for an operator.
            runner.emit({"event": "stopped", "reason": state["reason"] or reason})
            await self.drop_pilot()
            raise Refused(state["reason"] or reason)
        # Preserve an unsent terminal outcome instead of calling it a completed reply.
        # These states remain claimed, so no sent/unknown/failed input is replayed.
        await runner.database_call(self.ledger, ledger.finish_input, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key,
                            now=runner.now(), outcome="no_reply" if reason.startswith("native-no-reply") else "failed")
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
        self.ledger = Ledger(self.path, allowance=True, min_send_interval_s=self.tuning.min_send_interval_s,
                             lock_scope=self.db if self.all_fans else None,
                             per_account_send_gap=bool(self.tuning.per_account_send_gap),
                             tenant_id=os.environ.get("ONLYFANS_WORKER_TENANT") or "default",
                             send_concurrency=self.tuning.send_concurrency,
                             conversation_min_gap_s=self.tuning.conversation_min_gap_s,
                             dispatch_lease_s=self.tuning.dispatch_lease_s)
        self.send_gate.set_limit(self.tuning.send_concurrency if self.ledger.delivery_mode(self.db) == "concurrent" else 1)
        for var, why in self.tuning.clamped:
            runner.emit({"event": "setting-clamped", "setting": var, "why": why})
        if self.all_fans:
            self._size_thread_pool()
            await self._take_runner_lease()
            self.board = WorkBoard(self.ledger, self.db)
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

    def _size_thread_pool(self):
        """Blocking sends and reads run in threads. A reply waiting in the send queue holds one, so the pool has to
        be larger than the concurrency bound or the sender that would free the queue could never get a thread."""
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=max(self.tuning.concurrency * LIVE_TURNS_PER_SLOT + 12,
                                               self.tuning.send_concurrency * 2 + 12),
                               thread_name_prefix="fastt"))

    async def _take_runner_lease(self):
        """One live engine process per account. While another holder's lease is live this process waits (it does
        not exit, so a rolling deploy cannot trip the supervisor's crash-loop guard)."""
        import socket
        self.runner_lease = RunnerLease(self.ledger, self.db, f"{socket.gethostname()}:{os.getpid()}", self.tuning.runner_lease_ttl_s)
        while not self.stop.is_set():
            try:
                gen = await runner.database_call(self.ledger, self.runner_lease.acquire, runner.now())
                runner.emit({"event": "runner-lease-acquired", "generation": gen})
                return
            except Refused as exc:
                self.note(exc.reason)
                await self.idle(min(5.0, self.hold_s))
        raise Refused("service-stopping")

    async def shutdown(self):
        from db.engine import engine
        from twin_bridge.coordination import clear_owner
        pilot = self.pilot
        try:
            if self.tasks:
                # Drain: queued and generating replies stop at once; a send on the wire settles. Their claims are
                # given back, so the messages are answered after the restart.
                self.stop.set()
                await self.cancel_inflight("service-stopping")
                await self.drain_tasks(self.tuning.drain_grace_s)
            if self.board is not None:
                try:
                    for fan in list(self.board._known):
                        self.board.clear(fan)
                    await asyncio.to_thread(self.board.flush, runner.now())
                except Exception:
                    pass
            for fan in list(self.children):
                await self.release_child(fan)
            task, self._renew_task = self._renew_task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if pilot is not None:
                try:
                    if not self.lease.lost:
                        await runner.database_call(self.ledger, self.ledger.release_service, pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=runner.now())
                except Refused:
                    pass
                clear_owner(pilot.account_id, pilot.fan_id, pilot.owner_token)
            self.restore_catalog()
            if self.runner_lease is not None:
                try:
                    await runner.database_call(self.ledger, self.runner_lease.release, runner.now())
                except Exception:
                    pass
            runner.emit({"event": "finished", "persistent": True})
        finally:
            try:
                if self.intake is not None:
                    await self.intake.stop()
            finally:
                try:
                    if self.client is not None:
                        from of_client import close_client_http
                        close_client_http(self.client)
                finally:
                    try:
                        control = getattr(self, "_twin_store", None)
                        if control is not None and callable(getattr(control, "close", None)):
                            await asyncio.to_thread(control.close)
                            self._twin_store = None
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
            from of_client import close_client_http
            close_client_http(client)
        await engine.dispose()


async def serve(args, **kwargs):
    if not args.enable_send:
        return await preflight(args)
    service = Service(args, **kwargs)
    loop = asyncio.get_running_loop()
    installed = []
    previous = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, service.stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows' Proactor loop has no add_signal_handler; a plain handler still
            # makes shutdown cooperative for the supervised process and tests.
            try:
                previous.append((sig, signal.signal(sig, lambda *_a: service.stop.set())))
            except (ValueError, OSError):
                pass
    try:
        await service.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        for sig, handler in previous:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


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

