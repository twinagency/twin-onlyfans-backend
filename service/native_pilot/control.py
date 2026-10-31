"""Management controls; explicit receipt reconciliation reads scoped history and never sends."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone

from twin_bridge import store

from .ledger import Ledger, Refused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "stop", "reconcile", "delivery-mode"))
    parser.add_argument("--pilot-id")
    parser.add_argument("--intent-id")
    parser.add_argument("--account")
    parser.add_argument("--tenant")
    parser.add_argument("--fan", type=int)
    parser.add_argument("--attempt-id", type=int)
    parser.add_argument("--message-id", type=int)
    parser.add_argument("--command-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--mode", choices=("gap", "concurrent"))
    parser.add_argument("--requests-per-second", type=float)
    parser.add_argument("--burst", type=int)
    parser.add_argument("--max-concurrent", type=int)
    parser.add_argument("--conversation-gap", type=int)
    args = parser.parse_args()
    try:
        from twin_bridge.coordination import store_target
        path = store_target()
        if not path:
            raise Refused("pilot-store-required")
        store.ensure_runtime_schema(path)
        ledger = Ledger(path)
        if args.action == "reconcile":
            from .recovery import command
            print(json.dumps(asyncio.run(command(ledger, args)), sort_keys=True))
            return 0
        if args.action == "delivery-mode":
            if not args.account or args.mode is None:
                raise Refused("delivery-mode-arguments-required")
            if args.mode == "concurrent":
                if (args.requests_per_second is None or args.burst is None
                        or args.max_concurrent is None):
                    raise Refused("concurrent-controls-required")
                if not (0.05 <= args.requests_per_second <= 100 and 1 <= args.burst <= 200
                        and 1 <= args.max_concurrent <= 64
                        and 0 <= (args.conversation_gap or 0) <= 3600):
                    raise Refused("concurrent-controls-invalid")
            now_s = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
            with store.transaction(path) as con:
                row = con.execute("SELECT * FROM of_delivery_policy WHERE account_id=?", (args.account,)).fetchone()
                if row is None:
                    raise Refused("allowance-missing")
                tenant = args.tenant or os.environ.get("ONLYFANS_WORKER_TENANT") or "default"
                if row["active_intent_id"] or row["queued_count"] or con.execute(
                        "SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? "
                        "AND state IN ('dispatching','delivery_unknown') LIMIT 1",
                        (args.account,)).fetchone() or con.execute(
                        "SELECT 1 FROM engine_conversation_lease WHERE tenant_id=? AND account_id=? "
                        "AND state IN ('held','unresolved') LIMIT 1",
                        (tenant, args.account)).fetchone():
                    raise Refused("delivery-not-quiescent")
                before = {name: (row[name] if name in row.keys() else None)
                          for name in ("delivery_mode", "provider_requests_per_second", "provider_burst",
                                       "provider_max_concurrent", "conversation_min_gap_seconds")}
                if args.mode == "concurrent":
                    con.execute("UPDATE of_delivery_policy SET delivery_mode='concurrent',"
                                "provider_requests_per_second=?,provider_burst=?,provider_max_concurrent=?,"
                                "conversation_min_gap_seconds=?,provider_cooldown_until=NULL,"
                                "next_dispatch_at=NULL,revision=revision+1 WHERE account_id=?",
                                (args.requests_per_second, args.burst, args.max_concurrent,
                                 args.conversation_gap or 0, args.account))
                else:
                    con.execute("UPDATE of_delivery_policy SET delivery_mode='gap',"
                                "provider_cooldown_until=NULL,revision=revision+1 WHERE account_id=?",
                                (args.account,))
                updated = con.execute("SELECT * FROM of_delivery_policy WHERE account_id=?", (args.account,)).fetchone()
                after = {name: (updated[name] if name in updated.keys() else None)
                         for name in before}
                revision = con.execute("SELECT revision FROM of_delivery_policy WHERE account_id=?",
                                       (args.account,)).fetchone()[0]
                if store.has_table(con, "of_delivery_policy_change"):
                    con.execute("INSERT INTO of_delivery_policy_change"
                                "(account_id,revision,actor,reason,before_json,after_json,created_at) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (args.account, revision, args.actor or "operator",
                                 args.reason or "delivery mode change",
                                 json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), now_s))
            print(json.dumps({"account": args.account, "mode": args.mode, "revision": revision}, sort_keys=True))
            return 0
        if not args.pilot_id:
            raise Refused("pilot-id-required")
        if args.action == "stop":
            # Ownership is used privately; never expose the token in output or
            # pass it through command-line arguments/process listings.
            with store.readonly(path) as con:
                row = con.execute("SELECT owner_token FROM native_pilots WHERE pilot_id=?", (args.pilot_id,)).fetchone()
            if row is None:
                raise Refused("pilot-missing")
            ledger.stop(pilot_id=args.pilot_id, owner_token=row[0], reason="operator-stop",
                        now=datetime.now(timezone.utc).replace(tzinfo=None))
        print(json.dumps(ledger.status(args.pilot_id), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"event": "control-error", "reason": exc.reason if isinstance(exc, Refused) else "control-unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
