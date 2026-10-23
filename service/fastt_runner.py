"""FASTT runner: a second, manually started process beside the existing native worker.

Same image and role (ONLYFANS_PROCESS_ROLE=worker). It does not ingest: the worker keeps reading the
provider inbox into PostgreSQL. This process owns exactly ONE fan's automated replies (single active
sender) and runs FASTT's real automations.ai_chatter.run for that fan with a scoped client
(durable admission per POST), a scoped model gateway and pilot config. It never starts server.py,
the stock scheduler, the OF WebSocket pump or any other automation.

  python -m service.fastt_runner                       preflight only: checks, no claim, no model, no send
  python -m service.fastt_runner --dry-run             real model + ai_chatter, provider writes DISABLED
  python -m service.fastt_runner --enable-send         live: claims the fan, then replies and sends

Required env: ONLYFANS_PROCESS_ROLE=worker, ONLYFANS_WORKER_TENANT, ONLYFANS_CONNECTION_KEY, DATABASE_URL.
Scope env/args (defaults are the approved test fan): ONLYFANS_FASTT_ACCOUNT / --account (of-176774528),
ONLYFANS_FASTT_FAN / --fan (318022380). Anything other than one account and one numeric fan is refused.
Optional: ONLYFANS_FASTT_CONFIG_JSON (path) = extra ai_chatter config overrides, applied on top of the
saved account config after the disabled-lanes list. Stop with SIGTERM/SIGINT; the stop is also durable
(a paused conversation, a revoked owner marker or an exhausted OfDeliveryPolicy all end sending).
Exit codes: 0 stopped cleanly, 2 refused to start, 3 halted on a held/unknown send or scope violation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_ACCOUNT = "of-176774528"
DEFAULT_FAN = "318022380"


def _emit(event, **kw):
    print(json.dumps({"schemaVersion": "of-fastt-runner.v1", "event": event, **kw}, default=str), flush=True)


def _parse(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default=os.environ.get("ONLYFANS_FASTT_ACCOUNT", DEFAULT_ACCOUNT))
    p.add_argument("--fan", default=os.environ.get("ONLYFANS_FASTT_FAN", DEFAULT_FAN))
    p.add_argument("--quiet-seconds", type=float, default=7.0)
    p.add_argument("--poll-seconds", type=float, default=1.0)
    p.add_argument("--max-model-calls", type=int, default=80)
    p.add_argument("--dry-run", action="store_true", help="real model, provider writes disabled")
    p.add_argument("--enable-send", action="store_true", help="live: claim the fan and send")
    args = p.parse_args(argv)
    if args.dry_run and args.enable_send:
        p.error("--dry-run and --enable-send are mutually exclusive")
    from onlyfans_read.contract import ID_PATTERN
    if not re.fullmatch(r"[1-9][0-9]{0,18}", str(args.fan)) or not ID_PATTERN.fullmatch(args.account):
        p.error("exactly one account id and one numeric fan id are required (no lists, no wildcards)")
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        p.error("ONLYFANS_PROCESS_ROLE=worker is required")
    tenant = os.environ.get("ONLYFANS_WORKER_TENANT", "")
    if tenant == "deployment" or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", tenant):
        p.error("An explicit ONLYFANS_WORKER_TENANT is required")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", os.environ.get("ONLYFANS_CONNECTION_KEY", "")):
        p.error("An explicit 32-byte ONLYFANS_CONNECTION_KEY in hexadecimal is required")
    if not 1 <= args.quiet_seconds <= 120 or not .1 <= args.poll_seconds <= 30 or not 1 <= args.max_model_calls <= 1000:
        p.error("quiet/poll/model-call bounds out of range")
    return args, tenant


def _config_overrides():
    path = os.environ.get("ONLYFANS_FASTT_CONFIG_JSON")
    if not path:
        return {}
    value = json.loads(Path(path).read_text())
    if type(value) is not dict:
        raise ValueError("ONLYFANS_FASTT_CONFIG_JSON must hold one JSON object")
    return value


async def _amain(args, tenant, store, stop):
    from onlyfans_runtime.fastt_capabilities import Scope
    from onlyfans_runtime.fastt_credentials import load_credentials
    from onlyfans_runtime.fastt_gate import FasttSendGate
    from onlyfans_runtime.fastt_loop import FasttRunner
    from onlyfans_runtime.scoped_model import ScopedModelGateway
    from onlyfans_runtime import fastt_owner
    from onlyfans_runtime.text_delivery import TextAuthority
    import llm_client

    scope = Scope(tenant, args.account, int(args.fan))
    authority = TextAuthority(store)
    live = args.enable_send
    transport, model = load_credentials(store, tenant, scope.account_id, need_model=bool(live or args.dry_run))
    gate = FasttSendGate(store, authority, transport, tenant_id=tenant, account_id=scope.account_id,
                         fan_id=scope.fan_id, stop_event=stop)
    gate.dry_run = bool(args.dry_run)

    def refresh(g):
        # Follow delivery-policy revisions (the connector is bound to one) without re-opening the model key.
        from sqlalchemy.orm import Session
        from db.onlyfans_delivery_models import OfDeliveryPolicy
        with Session(store.engine) as s:
            policy = s.get(OfDeliveryPolicy, scope.account_id)
            revision = policy.revision if policy is not None else None
        if revision is not None and getattr(g.transport, "policy_revision", revision) != revision:
            g.transport, _ = load_credentials(store, tenant, scope.account_id, need_model=False)
    runner = FasttRunner(store, authority, gate, scope, quiet_seconds=args.quiet_seconds,
                         poll_seconds=args.poll_seconds, dry_run=bool(args.dry_run),
                         config_overrides=_config_overrides(), emit=_emit, refresh_transport=refresh,
                         pinned_model=model.model_id if model is not None else None)
    problems = runner.preflight(live=live)
    if problems:
        _emit("refused", problems=problems)
        return 2
    snap = runner.snapshot()
    _emit("preflight_ok", account=scope.account_id, fan=scope.fan_id, mode=snap.mode, takeover=snap.takeover,
          owned=snap.owned, used_units=snap.used_units, limit_units=snap.limit_units,
          send=live, dry_run=bool(args.dry_run))
    if not (live or args.dry_run):
        return 0
    llm_client.register_scoped_gateway(ScopedModelGateway(store, model, max_calls=args.max_model_calls))
    if live:
        fastt_owner.claim(store, tenant, scope.account_id, scope.fan_id, actor="fastt-runner")
    status = await runner.run(stop)
    _emit("stopped", reason=status, turns=runner.turns, accepted=len(gate.accepted))
    return 3 if runner.halted else 0


def main(argv=None):
    args, tenant = _parse(argv)
    from sqlalchemy.engine import make_url
    try:
        parsed = make_url(os.environ.get("DATABASE_URL", ""))
        if parsed.drivername not in {"postgresql", "postgresql+psycopg2"} or not parsed.database:
            raise ValueError()
        url = parsed.update_query_dict({"connect_timeout": "5"}).render_as_string(hide_password=False)
    except Exception:
        print("An explicit PostgreSQL DATABASE_URL is required", file=sys.stderr)
        return 2
    from onlyfans_runtime.store import Store
    stop = threading.Event()
    try:
        store = Store(url)
    except Exception:
        _emit("startup_unavailable")
        return 2
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        return loop.run_until_complete(_amain(args, tenant, store, stop))
    except Exception as exc:
        # Type only: messages can carry SQL, URLs or credential-bearing text.
        _emit("failed", error=type(exc).__name__)
        return 2
    finally:
        store.close()
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
