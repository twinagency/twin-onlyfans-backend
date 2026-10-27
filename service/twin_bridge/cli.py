"""Manual control seams for the Twin bridge - no scheduler, no dashboard.

    python -m twin_bridge.cli status   [--account ID] [--fan ID]
    python -m twin_bridge.cli pause    --fan ID --reason CODE
    python -m twin_bridge.cli resume   --fan ID
    python -m twin_bridge.cli takeover --fan ID          # a human took the chat
    python -m twin_bridge.cli cancel   --fan ID
    python -m twin_bridge.cli reconcile-writes --fan ID  # clear an unresolved local-writer marker
    python -m twin_bridge.cli run-once [--enable-send] [--batch N]
    python -m twin_bridge.cli recover                    # dispatching -> unknown

Defaults stay disabled: `run-once` only forms turns and asks Twin for drafts.
Nothing is sent unless BOTH `--enable-send` (or TWIN_BRIDGE_SEND_ENABLED=1) AND
a positive TWIN_BRIDGE_SEND_LIMIT are set. Importing this module starts nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

from sqlalchemy import select

from db.engine import get_session
from db.onlyfans_pilot_models import TwinBridgeConversation, TwinBridgeTurn

from . import turns as turns_mod
from .coordination import (
    TAKEOVER_BLOCKED,
    human_takeover,
    mark_cancelled,
    pause_conversation,
    reconcile_writes,
    resume_conversation,
)
from .delivery import recover_dispatching
from .scope import load_scope
from .worker import run_once, send_enabled, send_limit

# Test/ops seam: the account-bound platform adapter for `run-once`. A real run
# constructs the adapter HERE, so the documented command exercises the whole
# delivery path; tests replace the factory with a fake transport.
ADAPTER_FACTORY = None


def _build_adapter(account_id: str):
    if ADAPTER_FACTORY is not None:
        return ADAPTER_FACTORY(account_id)
    from .platform import OnlyFansTextAdapter
    return OnlyFansTextAdapter(account_id)


async def _status(args) -> dict:
    scope = load_scope()
    account = getattr(args, "account", None) or scope.account_id
    async with get_session() as session:
        conversations = (
            await session.execute(
                select(TwinBridgeConversation).where(
                    TwinBridgeConversation.account_id == str(account)
                )
            )
        ).scalars().all()
        stmt = select(TwinBridgeTurn).where(TwinBridgeTurn.account_id == str(account))
        fan = getattr(args, "fan", None)
        if fan is not None:
            stmt = stmt.where(TwinBridgeTurn.fan_id == int(fan))
        open_turns = (
            await session.execute(
                stmt.where(TwinBridgeTurn.state.notin_(["delivered", "cancelled"]))
                .order_by(TwinBridgeTurn.created_at)
            )
        ).scalars().all()
    return {
        "account": account,
        "scope_enabled": scope.enabled,
        "send_enabled": send_enabled(),
        "send_limit": send_limit(),
        "conversations": [
            {
                "fan_id": int(c.fan_id),
                "state": c.state,
                "reason": c.reason,
                "revision": int(c.revision),
                "owned": c.owner_token is not None,
                "send_budget": f"{int(c.send_budget_used)}/{int(c.send_budget_limit)}",
            }
            for c in conversations
        ],
        "open_turns": [
            {
                "fan_id": int(t.fan_id),
                "turn_id": t.turn_id,
                "state": t.state,
                "reason": t.reason,
                "generation_attempts": int(t.generation_attempts),
                "status_polls": int(t.status_polls),
                "ack_attempts": int(t.ack_attempts),
                "provider_message_id": t.provider_message_id,
            }
            for t in open_turns
        ],
    }


async def _pause(args) -> dict:
    scope = load_scope()
    account = args.account or scope.account_id
    now = datetime.utcnow()
    changed = await pause_conversation(
        account_id=str(account), fan_id=int(args.fan), reason=str(args.reason), now=now,
    )
    return {"paused": changed, "fan_id": int(args.fan), "reason": args.reason}


async def _resume(args) -> dict:
    scope = load_scope()
    account = args.account or scope.account_id
    now = datetime.utcnow()
    changed = await resume_conversation(
        account_id=str(account), fan_id=int(args.fan), now=now,
    )
    return {"resumed": changed, "fan_id": int(args.fan)}


async def _takeover(args) -> dict:
    scope = load_scope()
    account = args.account or scope.account_id
    now = datetime.utcnow()
    decision = await human_takeover(
        account_id=str(account), fan_id=int(args.fan), now=now,
    )
    return {"takeover": decision, "fan_id": int(args.fan)}


async def _cancel(args) -> dict:
    scope = load_scope()
    account = args.account or scope.account_id
    changed = await mark_cancelled(
        account_id=str(account), fan_id=int(args.fan), now=datetime.utcnow(),
    )
    return {"cancelled": changed, "fan_id": int(args.fan)}


async def _reconcile_writes(args) -> dict:
    """Clear an unresolved local-writer marker once its outcome is KNOWN.

    Only an operator can assert that an uncertain/crashed local write is
    settled, so nothing clears these durable markers automatically.
    """
    scope = load_scope()
    account = args.account or scope.account_id
    cleared = reconcile_writes(account_id=str(account), fan_id=int(args.fan))
    return {"reconciled": cleared, "fan_id": int(args.fan)}


async def _run_once(args) -> dict:
    scope = load_scope()
    if not scope.enabled:
        return {"error": "scope-disabled", "problems": list(scope.problems)}
    account = getattr(args, "account", None) or scope.account_id
    adapter = None
    limit_override = 0
    if args.enable_send:
        # A POSITIVE limit is mandatory and there is NO default allowance. An
        # EXPLICIT CLI cap is authoritative: zero/negative refuses before any
        # generation/dispatch work, and only an ABSENT CLI cap may fall back to
        # the environment.
        cli_limit = getattr(args, "send_limit", None)
        env_limit = send_limit()
        if cli_limit is not None:
            if int(cli_limit) <= 0:
                return {"error": "invalid-send-limit", "send_limit": int(cli_limit)}
            chosen = int(cli_limit)
        else:
            chosen = env_limit
        if int(chosen or 0) <= 0:
            return {"error": "send-limit-required"}
        limit_override = int(chosen)
        adapter = _build_adapter(str(account))
    report = await run_once(
        now=datetime.utcnow(), scope=scope, batch_size=int(args.batch),
        enable_send=bool(args.enable_send), adapter=adapter,
        send_limit_override=limit_override if args.enable_send else None,
    )
    return {
        "send_enabled": bool(args.enable_send), "send_limit": limit_override,
        "conversations": report.conversations, "frozen": report.frozen,
        "submitted": report.submitted, "polls": report.polls,
        "dispatched": report.dispatched, "acked": report.acked,
        "delivered": report.delivered, "parked": report.parked,
        "unknown": report.unknown, "held": report.held,
        "actions": report.actions, "reasons": report.reasons,
    }


async def _recover(args) -> dict:
    scope = load_scope()
    recovered = await recover_dispatching(
        now=datetime.utcnow(), account_id=scope.account_id if scope.enabled else None,
    )
    return {"recovered": [{"fan_id": fan, "turn_id": turn} for _acct, fan, turn in recovered]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="twin_bridge.cli", description="Twin bridge manual controls")
    parser.add_argument("--account", help="Fastt account id (defaults to the configured scope)")
    # Kept at the parent level so `--fan N <command>` also parses.
    parser.add_argument("--fan", type=int, default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    def _with_fan(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        # Registered on EACH subparser so the DOCUMENTED `pause --fan N` form
        # (option AFTER the subcommand) parses. The parent's own `--fan` is a
        # suppressed default, so `--fan N pause` still works too.
        p.add_argument("--fan", type=int, default=argparse.SUPPRESS,
                       help="fan id for the per-conversation commands")
        return p

    _with_fan("status", "show conversation and turn state")

    pause = _with_fan("pause", "pause a conversation (persistent)")
    pause.add_argument("--reason", default="operator-pause")

    _with_fan("resume", "resume a paused conversation explicitly")
    _with_fan("takeover", "record a human takeover before a manual send")
    _with_fan("cancel", "tombstone a conversation's unsent bridge work")
    _with_fan("reconcile-writes",
              "clear an unresolved local-writer marker after the outcome is known")

    run = sub.add_parser("run-once", help="form turns and advance one bounded pass")
    run.add_argument("--batch", type=int, default=turns_mod.DEFAULT_BATCH_SIZE)
    run.add_argument("--enable-send", action="store_true",
                     help="permit the platform POST (needs TWIN_BRIDGE_SEND_LIMIT)")
    run.add_argument("--send-limit", type=int, default=None,
                     help="durable pilot send cap (no default; required to send)")

    sub.add_parser("recover", help="recover interrupted dispatches as delivery-unknown")
    return parser


HANDLERS = {
    "status": _status,
    "pause": _pause,
    "resume": _resume,
    "takeover": _takeover,
    "cancel": _cancel,
    "reconcile-writes": _reconcile_writes,
    "run-once": _run_once,
    "recover": _recover,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "fan"):
        args.fan = None
    if args.command in ("pause", "resume", "takeover", "cancel",
                        "reconcile-writes") and args.fan is None:
        print(json.dumps({"error": "--fan is required"}))
        return 2
    # PostgreSQL: the bridge tables live in the separate fastt_runtime schema; create them if absent.
    from . import store
    from .coordination import store_target
    store.ensure_runtime_schema(store_target())
    result = asyncio.run(HANDLERS[args.command](args))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if isinstance(result, dict) and (
        result.get("error") or result.get("takeover") == TAKEOVER_BLOCKED
    ):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    sys.exit(main())
