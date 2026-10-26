"""Management controls; explicit receipt reconciliation reads scoped history and never sends."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from twin_bridge import store

from .ledger import Ledger, Refused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "stop", "reconcile"))
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
