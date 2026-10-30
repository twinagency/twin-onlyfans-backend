"""Mirror canonical conversation control into dedicated native OF lists.

These lists are advisory assignments, never send authorization. No AI controls,
messages, account activation, or existing audience lists are modified.
Run in the worker role with the normal verified connection loader.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

NAMES = {"ai": "Twin AI assigned", "human": "Twin Human control", "hold": "Twin Paused or pending"}


def assignment(row):
    if not row:
        return None
    if row.get("takeover_state") == "requested":
        return "hold"
    if row.get("mode") == "automated" and row.get("takeover_state") == "none":
        return "ai"
    if (row.get("mode") == "human_controlled" and row.get("takeover_state") == "effective"
            and not row.get("unresolved_attempt_id")):
        return "human"
    return "hold"


class Mirror:
    """Durable delta ledger. Repeating interrupted add/remove operations is safe.

    Every fan is first recorded as pending. A crash after any provider request
    therefore retries that fan; it cannot accidentally record partial success.
    A single process owns this file; the CLI holds an OS lock for its lifetime.
    """

    def __init__(self, path, account):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS applied (fan INTEGER PRIMARY KEY, state TEXT)")
        saved = self.get("account")
        if saved is not None and saved != account:
            self.db.close()
            raise ValueError("ledger belongs to a different account")
        self.put("account", account)

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def lists(self, client):
        result = {}
        for state, name in NAMES.items():
            saved = self.get("list:" + state)
            if saved:
                result[state] = int(saved)
                continue
            # Unique, persisted names recover a create with an unknown outcome.
            # Never adopt or edit a pre-existing agency list with a generic name.
            import uuid
            key = "name:" + state
            unique = self.get(key)
            new_name = unique is None
            if unique is None:
                unique = name + " " + uuid.uuid4().hex[:8]
                self.put(key, unique)
            found = None
            offset = 0
            while not new_name:
                page = client.get_lists(limit=100, offset=offset)
                rows = page.get("list") if isinstance(page, dict) else page
                if not isinstance(rows, list):
                    raise ValueError("invalid list page")
                matches = [r for r in rows if r.get("name") == unique]
                if len(matches) > 1:
                    raise ValueError("duplicate managed list name")
                if matches:
                    found = matches[0]["id"]
                    break
                more = page.get("hasMore") if isinstance(page, dict) else None
                if more is False or (more is None and len(rows) < 100):
                    break
                if not rows or offset >= 1_000_000:
                    raise ValueError("incomplete list discovery")
                offset += len(rows)
            if found is None:
                found = client.create_list(unique)["id"]
            self.put("list:" + state, int(found))
            result[state] = int(found)
            client.set_list_pinned_to_chat(int(found), True)
        return result

    def changed(self, snapshot):
        applied = dict(self.db.execute("SELECT fan,state FROM applied"))
        for fan in sorted(set(snapshot) | set(applied)):
            desired = assignment(snapshot.get(fan))
            if fan not in applied or applied[fan] != desired:
                yield fan

    def sync_fan(self, client, lists, fan, current):
        # Re-read canonical state immediately before work, not a queued stale mode.
        desired = assignment(current(fan))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO applied VALUES (?,?)", (fan, "pending"))
        # Remove the old assignment before publishing the new one. Any error
        # leaves pending and gets retried; it never reports a completed handover.
        for state, lid in lists.items():
            if state != desired:
                client.remove_user_from_list(lid, fan)
        # A control transition during removals gets a fresh decision next pass.
        if assignment(current(fan)) != desired:
            return False
        if desired is not None:
            client.add_user_to_list(lists[desired], fan)
        if assignment(current(fan)) != desired:
            return False
        with self.db:
            if desired is None:
                self.db.execute("DELETE FROM applied WHERE fan=?", (fan,))
            else:
                self.db.execute("UPDATE applied SET state=? WHERE fan=?", (desired, fan))
        return True


def snapshot(target, account, fan=None):
    from twin_bridge import store
    out = {}
    with store.readonly(target) as con:
        cursor = 0
        while True:
            sql = ("SELECT fan_id,mode,takeover_state,unresolved_attempt_id "
                   "FROM of_conversation_state WHERE account_id=? AND fan_id>? ")
            args = [account, cursor]
            if fan is not None:
                sql += "AND fan_id=? "
                args.append(fan)
            rows = con.execute(sql + "ORDER BY fan_id LIMIT 1000", tuple(args)).fetchall()
            for row in rows:
                out[int(row["fan_id"])] = dict(row)
            if len(rows) < 1000:
                return out
            next_cursor = int(rows[-1]["fan_id"])
            if next_cursor <= cursor:
                raise ValueError("snapshot made no progress")
            cursor = next_cursor


async def run(args):
    from native_pilot.connection import load_verified_connection, build_client
    from native_pilot.scope import AccountScope
    scope = AccountScope.parse(args.account)
    target = os.environ["DATABASE_URL"]
    Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
    with open(args.ledger + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mirror = Mirror(args.ledger, scope.db_id)
        selected = str(args.fan) if args.fan is not None else "all"
        if mirror.get("scope") not in (None, selected):
            mirror.db.close()
            raise ValueError("ledger belongs to a different fan scope")
        mirror.put("scope", selected)
        client = None
        try:
            while True:
                try:
                    rows = await asyncio.to_thread(snapshot, target, scope.db_id, args.fan)
                    changes = list(mirror.changed(rows))
                    result = {"account": scope.db_id, "tracked": len(rows), "changes": len(changes), "apply": args.apply, "completed": 0}
                    if args.apply:
                        conn = await load_verified_connection(scope)
                        client = await asyncio.to_thread(build_client, conn)
                        me = await asyncio.to_thread(client.me)
                        if str(me.get("id")) != scope.provider_id:
                            raise ValueError("account identity mismatch")
                        lists = mirror.lists(client)
                        result["lists"] = lists
                        def current(fan):
                            return snapshot(target, scope.db_id, fan).get(fan)
                        for fan in changes[:args.max_changes]:
                            if datetime.now(timezone.utc) >= conn.expires_at:
                                raise ValueError("verified connection expired")
                            # SQLite ledger stays on this event-loop thread.
                            # This dedicated process does no message delivery.
                            if mirror.sync_fan(client, lists, fan, current):
                                result["completed"] += 1
                            await asyncio.sleep(args.request_gap)
                    result["remaining"] = len(changes) - result["completed"]
                    print(json.dumps(result), flush=True)
                except Exception as exc:
                    print(json.dumps({"error": type(exc).__name__, "account": scope.db_id}), flush=True)
                    if args.once:
                        raise SystemExit(1) from None
                finally:
                    if client is not None:
                        client.http.close()
                        client = None
                if args.once:
                    break
                await asyncio.sleep(args.interval)
        finally:
            mirror.db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--fan", type=int)
    group.add_argument("--all-fans", action="store_true")
    p.add_argument("--ledger", required=True)
    p.add_argument("--apply", action="store_true", help="Write native lists; default is read-only shadow")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--max-changes", type=int, default=50)
    p.add_argument("--request-gap", type=float, default=1)
    args = p.parse_args()
    if args.interval < 10 or args.max_changes < 1 or args.request_gap < 0.5 or (args.fan is not None and args.fan < 1):
        p.error("invalid sync bounds")
    logging.disable(logging.CRITICAL)  # Never expose session/proxy text in CLI errors.
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
