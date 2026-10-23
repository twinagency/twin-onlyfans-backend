"""The thin runner loop: one account, one fan, FASTT's real ai_chatter.run, one durable sender.

Boundaries (rebuilt from the original native pilot runner):
  * Exact account + exact fan. `only_fan_ids` is a singleton; `force_ids` is never passed.
  * Fresh input only (live inbound recorded after activation), then a quiet debounce before generation.
  * ai_chatter.run decides, shapes, paces and "sends"; its client is the FasttSendGate, so every
    bubble is a durable TextAuthority intent bound by OfDeliveryPolicy (cap, throttle, used_units kept).
  * Any unknown/held send halts the loop for good. A pause, takeover or newer input only ends the turn.
  * After the approved paid card is accepted the durable per-fan stop is applied; the loop then idles.
  * This process never reads the provider inbox: the existing worker keeps ingesting into PostgreSQL.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Fan, Message, OfConversationState, OfMessageDelivery
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime, OfWork
from onlyfans_read.contract import UNRESOLVED_OUTCOMES
from . import fastt_owner
from .fastt_capabilities import Scope, ScopedCapabilities
from .fastt_errors import FasttHalt, FasttStop, FasttTurnAbort
from .fastt_gate import FasttSendGate, reconcile_stop
from .store import Refused

log = logging.getLogger("fastt_runner")

MAX_LIMIT_UNITS = 100
MIN_THROTTLE_SECONDS = 20


@dataclass
class Snapshot:
    runtime_enabled: bool
    owned: bool
    mode: str | None
    takeover: str | None
    revision: int | None
    input_revision: int | None
    unresolved: bool
    muted: bool
    last_direction: str | None
    last_message_id: int | None
    work_target: int | None
    quiet_for: float | None
    policy_enabled: bool
    used_units: int
    limit_units: int
    active_intent: str | None


class FasttRunner:
    def __init__(self, store, authority, gate: FasttSendGate, scope: Scope, *, quiet_seconds=7.0,
                 poll_seconds=1.0, dry_run=False, config_overrides=None, run_ai=None, emit=None,
                 refresh_transport=None, allow_synthetic=False, pinned_model=None):
        self.store, self.authority, self.gate, self.scope = store, authority, gate, scope
        self.quiet, self.poll, self.dry_run = float(quiet_seconds), float(poll_seconds), dry_run
        self.config_overrides = config_overrides or {}
        self._run_ai = run_ai
        self._emit = emit or (lambda event, **kw: log.info("fastt %s %s", event, kw))
        self.refresh_transport, self.allow_synthetic = refresh_transport, allow_synthetic
        self.pinned_model = pinned_model
        self.handled_input: int | None = None
        self.halted: str | None = None
        self.turns = 0
        self.results: list[dict] = []

    # -- state ---------------------------------------------------------------------------------------
    def snapshot(self) -> Snapshot:
        sc = self.scope
        with Session(self.store.engine) as s:
            runtime = s.get(OfRuntime, sc.account_id)
            state = s.get(OfConversationState, (sc.account_id, sc.fan_id))
            fan = s.get(Fan, (sc.account_id, sc.fan_id))
            policy = s.get(OfDeliveryPolicy, sc.account_id)
            work = s.get(OfWork, (sc.account_id, sc.fan_id))
            last = s.execute(select(Message.message_id, Message.direction).where(
                Message.account_id == sc.account_id, Message.fan_id == sc.fan_id, Message.is_unsent.is_(False))
                .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1)).first()
            hold = s.scalar(select(OfMessageDelivery.message_id).where(
                OfMessageDelivery.account_id == sc.account_id, OfMessageDelivery.fan_id == sc.fan_id,
                OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES)).limit(1))
            now = self.authority._db_now(s)
            quiet = (now - work.last_input_at).total_seconds() if work is not None else None
            return Snapshot(
                runtime_enabled=bool(runtime and runtime.enabled), owned=fastt_owner.owned_by_fastt(s, sc.account_id, sc.fan_id),
                mode=state.mode if state else None, takeover=state.takeover_state if state else None,
                revision=state.revision if state else None, input_revision=state.input_revision if state else None,
                unresolved=bool(state and state.unresolved_attempt_id) or hold is not None,
                muted=bool(fan and fan.is_muted), last_direction=last[1] if last else None,
                last_message_id=last[0] if last else None, work_target=work.target_message_id if work else None,
                quiet_for=quiet, policy_enabled=bool(policy and policy.enabled),
                used_units=policy.used_units if policy else 0, limit_units=policy.limit_units if policy else 0,
                active_intent=policy.active_intent_id if policy else None)

    def preflight(self, *, live: bool) -> list[str]:
        """Reasons the runner must not start (empty list means it may)."""
        problems = []
        snap = self.snapshot()
        with Session(self.store.engine) as s:
            policy = s.get(OfDeliveryPolicy, self.scope.account_id)
            if policy is None:
                problems.append("no delivery policy")
            else:
                if live and not (policy.enabled and policy.media_enabled):
                    problems.append("delivery policy must be enabled with media for a live run")
                if live and policy.transport_kind != "native" and not self.allow_synthetic:
                    problems.append("delivery policy transport must be native")
                if policy.limit_units > MAX_LIMIT_UNITS:
                    problems.append(f"policy limit {policy.limit_units} exceeds {MAX_LIMIT_UNITS}")
                if policy.throttle_seconds < MIN_THROTTLE_SECONDS and not self.allow_synthetic:
                    problems.append(f"policy throttle {policy.throttle_seconds}s is below {MIN_THROTTLE_SECONDS}s")
        if not snap.runtime_enabled:
            problems.append("account runtime is not enabled")
        if snap.mode is None:
            problems.append("conversation state missing")
        if snap.unresolved:
            problems.append("unresolved or held delivery for this fan")
        return problems

    def reconcile(self):
        """Crash repair before anything else: project accepted receipts, then apply a missed stop."""
        self.authority.repair(self.scope.tenant_id, self.scope.account_id)
        return reconcile_stop(self.store, self.scope.tenant_id, self.scope.account_id, self.scope.fan_id)

    # -- one evaluation -------------------------------------------------------------------------------
    async def tick(self) -> str:
        snap = await asyncio.to_thread(self.snapshot)
        if not snap.owned:
            return self._halt("not_runtime_owner")
        if snap.unresolved:
            return self._halt("unresolved_delivery")
        if snap.mode != "automated" or snap.takeover != "none" or snap.muted or not snap.runtime_enabled:
            return "idle_not_automated"
        if snap.limit_units and snap.used_units >= snap.limit_units:
            return self._halt("cap_exhausted")
        if snap.last_direction != "in" or snap.work_target is None or snap.last_message_id is None \
                or snap.work_target < snap.last_message_id:
            return "idle_nothing_to_answer"
        if snap.input_revision == self.handled_input:
            return "idle_already_handled"
        if snap.quiet_for is None or snap.quiet_for < self.quiet:
            return "waiting_quiet"
        return await self._turn(snap)

    async def _turn(self, snap: Snapshot) -> str:
        sc = self.scope
        self.handled_input = snap.input_revision          # never run the same input twice
        if self.refresh_transport is not None:
            await asyncio.to_thread(self.refresh_transport, self.gate)
        try:
            await asyncio.to_thread(self.gate.begin_turn, expected_input_revision=snap.input_revision)
        except FasttTurnAbort as stop:
            return "turn_aborted:" + stop.reason
        run_ai = self._run_ai
        if run_ai is None:
            from automations import ai_chatter
            run_ai = ai_chatter.run
        payload = {"only_fan_ids": [sc.fan_id], "max_replies": 1, "dry_run": self.dry_run}
        self.turns += 1
        outcome = "turn_done"
        try:
            with ScopedCapabilities(sc, self.gate, config_overrides=self.config_overrides, dry_run=self.dry_run,
                                pinned_model=self.pinned_model):
                result = await run_ai(sc.account_id, payload, run_id=0)
            self.results.append(result if isinstance(result, dict) else {})
            self._emit("turn", sent=self.gate.turn.sent if self.gate.turn else 0, result=_brief(result))
        except FasttHalt as stop:
            return self._halt(stop.reason)
        except FasttStop as stop:
            outcome = "turn_aborted:" + stop.reason
            self._emit("turn_aborted", reason=stop.reason)
        finally:
            sent = self.gate.turn.sent if self.gate.turn else 0
            stopped = bool(self.gate.turn and self.gate.turn.stopped)
            self.gate.end_turn()
        if stopped:
            self._emit("stopped_after_paid_card")
            outcome = "stopped_after_paid_card"
        return outcome if sent == 0 or outcome != "turn_done" else "replied"

    def _halt(self, reason):
        if self.halted is None:
            self.halted = reason
            self._emit("halt", reason=reason)
        return "halt:" + reason

    async def run(self, stop: threading.Event, *, max_ticks=None) -> str:
        await asyncio.to_thread(self.reconcile)
        ticks = 0
        while not stop.is_set() and self.halted is None:
            action = await self.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if action in {"replied", "stopped_after_paid_card"} or action.startswith("turn_aborted"):
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(self.poll)
        return self.halted or "stopped"


def _brief(result):
    if not isinstance(result, dict):
        return None
    return {k: result.get(k) for k in ("status", "reason", "replies_sent", "offers_made", "errors", "candidates") if k in result}
