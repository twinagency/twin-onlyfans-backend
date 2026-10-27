"""Explicit repeat of the existing Twin pilot, with a separate additional allowance.

Importing does nothing. This schedules the existing worker; it does not generate
replies, import native-engine history, or change account-wide send settings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


class FreshClient:
    """Reject late historical ingestion before asking Twin for any draft."""

    def __init__(self, client, path, account, fan, cutoff):
        self.client, self.path = client, path
        self.account, self.fan, self.cutoff = account, fan, cutoff

    def __getattr__(self, name):
        return getattr(self.client, name)

    def validate(self, event):
        from .client import TwinClientError
        try:
            if event.get("accountId") != self.account or event.get("fanId") != str(self.fan):
                raise ValueError("scope")
            con = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                con.execute("PRAGMA query_only=ON")
                con.execute("BEGIN")
                row = con.execute(
                    "SELECT member_ids_json FROM twin_bridge_turns WHERE account_id=? AND fan_id=? AND turn_id=?",
                    (self.account, self.fan, event.get("messageId")),
                ).fetchone()
                ids = json.loads(row[0]) if row else []
                if not ids or not isinstance(ids, list) or any(type(i) is not int for i in ids):
                    raise ValueError("members")
                for mid in ids:
                    receipt = con.execute(
                        "SELECT occurred_at,received_at FROM twin_bridge_inputs WHERE account_id=? AND fan_id=? AND provider_message_id=?",
                        (self.account, self.fan, mid),
                    ).fetchone()
                    if not receipt or any(datetime.fromisoformat(t) <= self.cutoff for t in receipt):
                        raise ValueError("pre-cutoff")
            finally:
                con.close()
        except Exception:
            raise TwinClientError("repeat-source-refused", uncertain=False) from None

    async def request_draft(self, event):
        self.validate(event)
        return await self.client.request_draft(event)

    async def read_status(self, event):
        self.validate(event)
        return await self.client.read_status(event)


async def run(args):
    import httpx
    from db.engine import engine
    from native_pilot.runner import check_local
    from of_client import OFClient
    from . import repeat, worker
    from .coordination import sqlite_path
    from .pilot import run_pilot
    from .platform import OnlyFansTextAdapter
    from .scope import load_scope
    from .turns import scope_fingerprint

    grant = None
    platform_client = None
    twin = None
    reason = "startup-error"
    path = sqlite_path()
    owner = worker._PROCESS_OWNER_TOKEN
    try:
        scope = load_scope()
        if not path or not scope.enabled or scope.account_id != args.account or scope.fan_allowlist != frozenset({str(args.fan)}):
            raise RuntimeError("scope")
        check_local(path, args.account, args.fan)
        twin = worker._default_client()
        async with httpx.AsyncClient(trust_env=False, timeout=5) as health:
            response = await health.get(twin.base_url + "/healthz", headers={"Authorization": "Bearer " + os.environ["TWIN_BRIDGE_TOKEN"]})
            if response.status_code != 200:
                raise RuntimeError("twin-health")
        platform_client = await asyncio.to_thread(OFClient.from_account, args.account, timeout_s=20)
        me = await asyncio.to_thread(platform_client.me)
        if str(me.get("id")) != args.account or str(me.get("username", "")).lower() != args.handle.lower():
            raise RuntimeError("creator-mismatch")
        cutoff = now()
        deadline = cutoff + timedelta(seconds=args.max_seconds)
        grant = repeat.arm(
            path, run_id=args.run_id, account_id=args.account, fan_id=args.fan,
            expected_used=args.expected_used, expected_revision=args.expected_revision,
            extra_replies=args.extra_replies, scope_fingerprint=scope_fingerprint(scope),
            owner_token=owner, now=cutoff, deadline=deadline,
        )
        # This process alone receives the cumulative cap; persisted used is never reset.
        os.environ["TWIN_BRIDGE_SEND_LIMIT"] = str(grant["end_limit"])
        repeat.discard_old_inputs(path, run_id=args.run_id, owner_token=owner, now=now())
        fresh = FreshClient(twin, path, args.account, args.fan, cutoff)

        async def between_passes(seconds):
            await asyncio.sleep(seconds)
            current = now()
            if current < deadline:
                repeat.discard_old_inputs(path, run_id=args.run_id, owner_token=owner, now=current)

        emit({"event": "ready", "engine": "twin", "account_id": args.account,
              "fan_id": args.fan, "additional_replies": args.extra_replies,
              "run_id": args.run_id, "cutoff": cutoff.isoformat() + "Z",
              "deadline": deadline.isoformat() + "Z"})
        result = await run_pilot(
            account_id=args.account, fan_id=args.fan, send_limit=grant["end_limit"],
            enable_send=True, max_seconds=args.max_seconds, client=fresh,
            adapter=OnlyFansTextAdapter(args.account, client=platform_client),
            sleep=between_passes, emit=emit,
        )
        reason = result["reason"]
    finally:
        try:
            if grant is not None:
                emit({"event": "finished", **repeat.stop(
                    path, run_id=args.run_id, owner_token=owner, now=now(), reason=reason,
                )})
        finally:
            if twin is not None:
                await twin.aclose()
            if platform_client is not None:
                platform_client.http.close()
            await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-send", action="store_true")
    parser.add_argument("--account", required=True)
    parser.add_argument("--fan", required=True, type=int)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-used", type=int, required=True)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--extra-replies", type=int, required=True)
    parser.add_argument("--max-seconds", type=int, default=1800)
    args = parser.parse_args()
    if not args.enable_send or not 1 <= args.extra_replies <= 30 or not 60 <= args.max_seconds <= 1800:
        parser.error("explicit enable and bounded allowance/duration required")
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except (Exception, KeyboardInterrupt):
        emit({"event": "not-running", "reason": "repeat-stopped-or-preflight-refused"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
