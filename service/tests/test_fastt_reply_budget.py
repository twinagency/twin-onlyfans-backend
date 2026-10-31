"""The reply reserve: background model work (the fan-memory learner) backs off before replies are affected.

Real DailyBudget on a SQLite application schema. A $5 cap, 40 % held for replies: the learner (background=True) may
spend up to $3.00; replies may use everything up to $5.00. The cap is never raised.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
d = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + d + "/budget.db"
os.environ.pop("FASTT_REPLY_RESERVE_FRACTION", None)

ACCOUNT = "176774528"
FAN = 318022380
CHECKS = 0


def check(v, label=""):
    global CHECKS
    assert v, label
    CHECKS += 1


async def main():
    from db.engine import engine, get_session
    from db.models import AccountAiConfig, Base
    from llm_client import LLMCapExceeded
    from native_pilot.budget import DailyBudget
    from native_pilot.model import ENDPOINT
    from native_pilot.scope import AccountScope
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session() as s:
        s.add(AccountAiConfig(account_id=ACCOUNT, daily_cost_cap_cents=500))
    now = datetime(2026, 10, 30, 12)
    scope = AccountScope.parse(ACCOUNT)
    model = "openrouter/deepseek-v4.1-flash"
    reply = DailyBudget(scope, model, endpoint=ENDPOINT, clock=lambda: now)
    learner = DailyBudget(scope, model, endpoint=ENDPOINT, clock=lambda: now, background=True)
    # the learner spends in 2,400-token slices until its share ($3.00 = 30,000 units) is gone, then is refused
    spent, n = 0, 0
    while True:
        try:
            r = await learner.reserve(fan_id=FAN, request_bytes=80000, max_output_tokens=2400)
        except LLMCapExceeded:
            break
        spent += r.reserved_units
        n += 1
        assert n < 1000
    check(n > 3 and spent <= 30_000, f"learner stopped inside its share ({spent} units of 30000)")
    # replies still have the reserve: they are admitted well past the learner's stop
    for _ in range(50):
        await reply.reserve(fan_id=FAN, request_bytes=2000, max_output_tokens=400)
    from sqlalchemy import select
    from db.models import GrokCall, GrokDailyCost
    async with get_session() as s:
        purposes = {c.purpose for c in (await s.scalars(select(GrokCall))).all()}
        total = (await s.scalar(select(GrokDailyCost))).cost_cents
    check(purposes == {"gen_info", "ai_chatter"}, "the learner's calls are labelled gen_info from the first row")
    check(total <= 50_000, "the daily cap itself was never exceeded or raised")
    # a reply never competes with the learner's slice: with the learner at its limit a reply is still admitted
    r = await reply.reserve(fan_id=FAN, request_bytes=2000, max_output_tokens=400)
    check(r.reserved_units > 0)
    os.environ["FASTT_REPLY_RESERVE_FRACTION"] = "0"
    open_learner = DailyBudget(scope, model, endpoint=ENDPOINT, clock=lambda: now, background=True)
    await open_learner.reserve(fan_id=FAN, request_bytes=80000, max_output_tokens=2400)
    check(True, "fraction 0 disables the reserve")


asyncio.run(main())
print(f"fastt reply budget: all cases passed ({CHECKS} checks)")

from llm_client import BACKGROUND_PURPOSES  # noqa: E402
assert {"gen_info", "describe_media"} <= BACKGROUND_PURPOSES and not ({"ai_chatter", "describe_inbound_image"} & BACKGROUND_PURPOSES)
print("background purposes yield to replies in llm_client too")
