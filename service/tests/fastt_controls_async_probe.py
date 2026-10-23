"""Subprocess probe for test_onlyfans_fastt_controls_pg.py (not collected by pytest).

db.engine binds its asyncpg engine from the environment at import time, so the
FASTT async seams run in a child process started with ONLYFANS_PROCESS_ROLE=worker
and an explicit PostgreSQL DATABASE_URL, exactly like the production worker role.
Usage: python fastt_controls_async_probe.py <scenario> <json-args>; prints one JSON.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def iso(value):
    return value.isoformat() if value is not None else None


async def last_look(a):
    from automations import ai_chatter
    inbound = datetime.fromisoformat(a["inbound_at"]) if a.get("inbound_at") else None
    return await ai_chatter._last_look(a["account"], a["fan"], inbound,
                                       sent_ids=frozenset(a.get("sent_ids", [])))


async def gather(a):
    from automations import ai_chatter
    out = await ai_chatter._gather(a["account"], {a["fan"]})
    c = out[a["fan"]]
    return {"last_human_out_at": iso(c.last_human_out_at), "last_dir": c.last_dir,
            "answered_after_in": [list(x) for x in c.answered_after_in]}


async def upserts(a):
    import automation_executor as ax
    from automations import _common as C, ai_chatter as A
    from automations import welcome_chatter_for_info as W
    from attribution import write_outbound_attribution
    acc, fan, now = a["account"], a["fan"], datetime.utcnow()
    trace = {}
    await C.set_automation_restrict(acc, fan, True)
    trace["restrict_status"] = await C.automation_restrict_status(acc, fan)
    await C.set_automation_restrict(acc, fan, False)
    trace["released_status"] = await C.automation_restrict_status(acc, fan)
    await C.mark_muted_creator_skip(acc, fan)
    await C.set_of_restricted_skip(acc, fan, True)
    await C.set_automation_restrict(acc, fan, True)       # must not demote of_restricted
    trace["after_restrict_over_of_restricted"] = await C.automation_restrict_status(acc, fan)
    await C.set_of_restricted_skip(acc, fan, False)
    await C._skip_and_rest(acc, fan, now)
    await C._set_status_and_pause(acc, fan, "active", now)
    await W._bump_attempt(acc, fan, now)
    await W._pause_fan(acc, fan, now)
    await W._add_to_skip_list(acc, fan, "info")
    trace["lease_first"] = await ax.acquire_fan_lease(acc, fan, "ai_chatter")
    trace["lease_second"] = await ax.acquire_fan_lease(acc, fan, "ai_chatter")
    await ax.release_fan_lease(acc, fan)
    trace["lease_after_release"] = await ax.acquire_fan_lease(acc, fan, "ai_chatter")
    await ax.start_fan_cooldown(acc, fan, cooldown_s=60)
    await A._save_rhythm(acc, fan, deferrals=2)
    await A._save_rhythm(acc, fan, deferrals=3)
    await A._save_ladder(acc, fan, rung_index=1)
    await A._save_ladder(acc, fan, rung_index=2)
    await A._park_pending_offer(acc, fan, None, now)
    await A._park_pending_offer(acc, fan, None, now)
    await write_outbound_attribution(account_id=acc, fan_id=fan, message_id=a["message_id"],
        sent_by_employee_id=None, automation_kind="ai_chatter", body="probe reply",
        price_cents=0, created_at=now)
    await write_outbound_attribution(account_id=acc, fan_id=fan, message_id=a["message_id"],
        sent_by_employee_id=None, automation_kind="ai_chatter", body="probe reply",
        price_cents=0, created_at=now)
    return trace


async def main():
    scenario, args = sys.argv[1], json.loads(sys.argv[2])
    result = await globals()[scenario](args)
    from db.engine import engine
    await engine.dispose()
    print("PROBE_RESULT " + json.dumps(result))

asyncio.run(main())
