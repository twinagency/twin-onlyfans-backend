"""Local status/stop controls; never starts an engine or makes network calls."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .ledger import Ledger, Refused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "stop"))
    parser.add_argument("--pilot-id", required=True)
    args = parser.parse_args()
    try:
        from twin_bridge.coordination import sqlite_path
        path = sqlite_path()
        if not path:
            raise Refused("sqlite-required-for-pilot")
        ledger = Ledger(path)
        if args.action == "stop":
            # Ownership is used privately; never expose the token in output or
            # pass it through command-line arguments/process listings.
            con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                con.execute("PRAGMA query_only=ON")
                row = con.execute("SELECT owner_token FROM native_pilots WHERE pilot_id=?", (args.pilot_id,)).fetchone()
            finally:
                con.close()
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
