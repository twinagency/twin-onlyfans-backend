"""One explicitly enabled, bounded automatic pilot; import starts nothing.

Run from service: python -m twin_bridge.pilot --account ID --fan ID --enable-send --send-limit 3
The existing worker owns all generation, dispatch, acknowledgement and recovery.
This module only schedules serial passes in one process, preserving its owner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timezone

from sqlalchemy import select

from db.engine import engine, get_session
from db.onlyfans_pilot_models import TwinBridgeConversation, TwinBridgeTurn

from . import worker
from .platform import OnlyFansTextAdapter
from .scope import load_scope


def _emit(value: dict) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _snapshot(account: str, fan: int, limit: int) -> dict:
    async with get_session() as session:
        conversation = await session.get(TwinBridgeConversation, (account, fan))
        rows = list((await session.execute(
            select(TwinBridgeTurn.fan_id, TwinBridgeTurn.state).where(
                TwinBridgeTurn.account_id == account,
                TwinBridgeTurn.state.in_(list(worker.OPEN_STATES) + [
                    "delivery_unknown", "attention",
                ]),
            )
        )).all())
    # run_once discovers account-wide open work. Refuse a mixed backlog rather
    # than allowing this singleton pilot to advance a different conversation.
    foreign = any(int(row.fan_id) != fan for row in rows)
    states = {row.state for row in rows if int(row.fan_id) == fan}
    used = int(conversation.send_budget_used) if conversation else 0
    cap = min(limit, int(conversation.send_budget_limit)) if conversation else limit
    return {
        "used": used, "limit": cap,
        "sent_pending": "sent" in states,
        "blocked": (
            "other-conversation-work" if foreign else
            "delivery-unresolved" if states & {"dispatching", "delivery_unknown"} else
            "attention-required" if "attention" in states else
            "conversation-paused" if conversation and conversation.state != "active" else
            None
        ),
    }


async def run_pilot(*, account_id: str, fan_id: int, send_limit: int, enable_send: bool = False,
                    max_seconds: float = 900, interval_seconds: float = 2,
                    client=None, adapter=None, emit=_emit, sleep=asyncio.sleep,
                    monotonic=time.monotonic, utcnow=_now) -> dict:
    """Advance only the selected pilot, never reset evidence or retry a POST.

    The time limit prevents starting another pass; an admitted pass is allowed
    to settle. At the send cap, only an outstanding confirmed-send ack continues.
    Injection seams support isolated tests with the real worker and database.
    """
    def stopped(reason: str, state=None) -> dict:
        result = {"event": "stopped", "reason": reason}
        if state is not None:
            result.update(used=state["used"], limit=state["limit"])
        emit(result)
        return result

    if enable_send is not True:
        return stopped("explicit-enable-required")
    if isinstance(fan_id, bool) or not isinstance(fan_id, int) or fan_id <= 0:
        return stopped("invalid-fan")
    if isinstance(send_limit, bool) or not isinstance(send_limit, int) or send_limit <= 0:
        return stopped("invalid-send-limit")
    configured_limit = worker.send_limit()
    if configured_limit <= 0 or send_limit > configured_limit:
        return stopped("configured-send-limit-exceeded")
    if (not math.isfinite(max_seconds) or not 0 < max_seconds <= 3600 or
            not math.isfinite(interval_seconds) or not 0 < interval_seconds <= 60):
        return stopped("invalid-timing")
    scope = load_scope()
    if not scope.enabled:
        return stopped("scope-disabled")
    if scope.account_id != account_id:
        return stopped("account-scope-mismatch")
    if scope.fan_allowlist != frozenset({str(fan_id)}):
        return stopped("singleton-fan-scope-required")

    deadline = monotonic() + max_seconds
    adapter = adapter if adapter is not None else OnlyFansTextAdapter(scope.account_id)
    emit({"event": "running", "send_limit": send_limit, "max_seconds": max_seconds})
    while True:
        if load_scope() != scope or worker.send_limit() != configured_limit:
            return stopped("configuration-changed")
        state = await _snapshot(scope.account_id, fan_id, send_limit)
        # Preserve the worker's ack-only behavior during a manual pause. Unknown
        # delivery or unrelated work never qualifies for this exception.
        if state["blocked"] and not (
            state["blocked"] == "conversation-paused" and state["sent_pending"]
        ):
            return stopped(state["blocked"], state)
        cap_reached = state["used"] >= state["limit"]
        if cap_reached and not state["sent_pending"]:
            return stopped("send-limit-reached", state)
        if monotonic() >= deadline:
            return stopped("time-limit-reached", state)
        try:
            report = await worker.run_once(
                now=utcnow(), scope=scope, client=client, adapter=adapter,
                enable_send=not cap_reached and state["blocked"] is None,
                send_limit_override=send_limit,
            )
        except Exception:
            # No retry on an unclassified exception; do not log credentials,
            # transport text or private conversation content.
            return stopped("worker-error", state)
        if any((report.frozen, report.submitted, report.polls, report.dispatched,
                report.delivered, report.acked, report.parked, report.unknown)):
            emit({"event": "progress", "frozen": report.frozen,
                  "drafted": report.submitted, "delivered": report.delivered,
                  "acknowledged": report.acked, "parked": report.parked,
                  "unknown": report.unknown})
        if report.unknown or report.parked or "worker-error" in report.reasons:
            return stopped(
                "delivery-unresolved" if report.unknown else
                "turn-parked" if report.parked else "worker-error",
                await _snapshot(scope.account_id, fan_id, send_limit),
            )
        # No transaction is held during this wait; the next pass uses a fresh
        # clock and the SAME process owner, so there is no lease handoff delay.
        await sleep(min(interval_seconds, max(0, deadline - monotonic())))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Automatic single-chat Twin pilot")
    parser.add_argument("--account", required=True)
    parser.add_argument("--fan", type=int, required=True)
    parser.add_argument("--send-limit", type=int, required=True)
    parser.add_argument("--enable-send", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=900)
    args = parser.parse_args(argv)

    async def execute():
        try:
            return await run_pilot(account_id=args.account, fan_id=args.fan, send_limit=args.send_limit,
                                   enable_send=args.enable_send,
                                   max_seconds=args.max_seconds)
        finally:
            await engine.dispose()
    try:
        result = asyncio.run(execute())
    except KeyboardInterrupt:
        _emit({"event": "stopped", "reason": "operator-stop"})
        return 0
    except Exception:
        _emit({"event": "stopped", "reason": "pilot-error"})
        return 1
    return 0 if result["reason"] in {"send-limit-reached", "time-limit-reached"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
