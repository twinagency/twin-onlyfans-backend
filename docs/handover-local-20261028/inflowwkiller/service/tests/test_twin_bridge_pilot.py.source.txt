"""Automatic pilot regressions: real SQLite/worker, synthetic external clients."""
import asyncio
import os
import shutil
import sys
import types
from datetime import timedelta
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["SESSION_SECRET"] = "synthetic-pilot-test-secret"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
source_path = HERE / "test_twin_bridge_delivery.py"
h = types.ModuleType("pilot_fixture")
h.__file__ = str(source_path)
source = source_path.read_text(encoding="utf-8").split("\ntry:\n    run(main())")[0]
exec(compile(source, str(source_path), "exec"), h.__dict__)

from sqlalchemy import select, update  # noqa: E402
from twin_bridge import pilot  # noqa: E402

checks = 0


def check(label, got, want):
    global checks
    checks += 1
    assert got == want, f"{label}: {got!r} != {want!r}"
    print("  ok  " + label, flush=True)


class Clock:
    def __init__(self, hook=None):
        self.elapsed = 20.0
        self.hook = hook

    def monotonic(self):
        return self.elapsed

    def now(self):
        return h.BASE_TIME + timedelta(seconds=self.elapsed)

    async def sleep(self, seconds):
        self.elapsed += seconds
        if self.hook:
            await self.hook(self)


async def invoke(*, clock=None, twin=None, adapter=None, **overrides):
    clock = clock or Clock()
    events = []
    params = dict(account_id=h.ACCOUNT, fan_id=h.FAN, send_limit=3,
                  enable_send=True, max_seconds=120, interval_seconds=2,
                  client=twin or h.FakeTwin(), adapter=adapter or h.FakeAdapter(),
                  emit=events.append, sleep=clock.sleep,
                  monotonic=clock.monotonic, utcnow=clock.now)
    params.update(overrides)
    result = await pilot.run_pilot(**params)
    return result, events


async def reset():
    os.environ.update(h.ENV)
    os.environ["TWIN_BRIDGE_SEND_LIMIT"] = "3"
    await h.reset_all()


async def main():
    os.environ.update(h.ENV)
    await h.init_db()
    for name, override, reason in (
        ("explicit enable", {"enable_send": False}, "explicit-enable-required"),
        ("zero cap", {"send_limit": 0}, "invalid-send-limit"),
        ("boolean cap", {"send_limit": True}, "invalid-send-limit"),
        ("cap above config", {"send_limit": 4}, "configured-send-limit-exceeded"),
        ("wrong account", {"account_id": "different"}, "account-scope-mismatch"),
        ("wrong fan", {"fan_id": h.FAN + 1}, "singleton-fan-scope-required"),
        ("unbounded time", {"max_seconds": float("inf")}, "invalid-timing"),
    ):
        await reset()
        twin, adapter = h.FakeTwin(), h.FakeAdapter()
        result, _ = await invoke(twin=twin, adapter=adapter, **override)
        check(name, result["reason"], reason)
        check(name + " makes no external calls", (twin.drafts, adapter.history_reads,
                                                   len(adapter.posts)), (0, 0, 0))

    await reset()
    os.environ["TWIN_BRIDGE_FAN_ALLOWLIST"] = f"{h.FAN},{h.FAN+1}"
    result, _ = await invoke()
    check("extra allowlisted fan refused", result["reason"], "singleton-fan-scope-required")

    await reset()
    await h.freeze(message_id=4999)
    async with h.get_session() as session:
        await session.execute(update(h.TwinBridgeTurn).values(state="parked", reason="external-outbound"))
    await h.seed_turn(message_id=5001)

    class FreshTwin(h.FakeTwin):
        async def request_draft(self, event):
            self.acks = 0  # synthetic status is scoped to the current request
            return await super().request_draft(event)

    class UniqueAdapter(h.FakeAdapter):
        async def send_text(self, *args, **kwargs):
            self._provider_id = str(777000111 + len(self.posts))
            return await super().send_text(*args, **kwargs)

    seeded = 1
    async def arrival(clock):
        nonlocal seeded
        completed = [t for t in await h.turns_for() if t.state == "delivered"]
        if len(completed) == seeded and seeded < 4:
            seeded += 1
            await h.seed_turn(message_id=5000 + seeded, received_at=clock.now())

    twin, adapter = FreshTwin(), UniqueAdapter()
    result, events = await invoke(twin=twin, adapter=adapter, clock=Clock(arrival))
    check("one start automatically completes three turns", twin.drafts, 3)
    check("three provider calls total", len(adapter.posts), 3)
    check("cap stops the loop", result["reason"], "send-limit-reached")
    check("durable budget is 3/3", (result["used"], result["limit"]), (3, 3))
    check("each reply has one attempt", len(await h.attempts_for()), 3)
    check("all three delivered and acknowledged", len([t for t in await h.turns_for()
                                                       if t.state == "delivered"]), 3)
    check("old parked turn stays parked", len([t for t in await h.turns_for()
                                               if t.state == "parked"]), 1)
    async with h.get_session() as session:
        fourth = await session.get(h.TwinBridgeInput, (h.ACCOUNT, h.FAN, 5004))
        check("fourth input waits without generation", fourth.state, "pending")
    check("progress never contains message text", "synthetic draft" in str(events), False)
    restarted, restarted_adapter = FreshTwin(), UniqueAdapter()
    result, _ = await invoke(twin=restarted, adapter=restarted_adapter)
    check("restart keeps exhausted cap", result["reason"], "send-limit-reached")
    check("restart sends or generates nothing", (restarted.drafts, len(restarted_adapter.posts)), (0, 0))

    await reset()
    await h.seed_turn()
    twin, adapter = h.FakeTwin(ack_failures=1), h.FakeAdapter()
    result, _ = await invoke(send_limit=1, twin=twin, adapter=adapter)
    check("ack retry at exhausted cap succeeds", (await h.turns_for())[0].state, "delivered")
    check("ack retry never repeats POST", len(adapter.posts), 1)
    check("ack retry uses existing bounded recovery", twin.acks, 2)
    check("ack completion exits at cap", result["reason"], "send-limit-reached")

    for label, adapter, expected in (
        ("unknown send", h.FakeAdapter(send_error=h.platform.PlatformUnknown("dispatch-uncertain")),
         "delivery-unresolved"),
        ("external reply", h.FakeAdapter(history=[{"id": 6000, "isFromMe": True}]), "turn-parked"),
    ):
        await reset()
        await h.seed_turn()
        result, _ = await invoke(adapter=adapter)
        check(label + " stops automatically", result["reason"], expected)
        count = len(adapter.posts)
        again, _ = await invoke(adapter=adapter)
        check(label + " restart performs no further POST", len(adapter.posts), count)

    await reset()
    await h.freeze()
    async with h.get_session() as session:
        await session.execute(update(h.TwinBridgeConversation).values(state="paused"))
    twin, adapter = h.FakeTwin(), h.FakeAdapter()
    result, _ = await invoke(twin=twin, adapter=adapter)
    check("paused conversation stays stopped", result["reason"], "conversation-paused")
    check("pause causes zero generation or sends", (twin.drafts, len(adapter.posts)), (0, 0))

    await reset()
    await h.seed_turn(fan_id=h.FAN + 1)
    await h.turns.form_turns(now=h.BASE_TIME + timedelta(seconds=9),
                            scope=h.scope(TWIN_BRIDGE_FAN_ALLOWLIST=str(h.FAN + 1)))
    result, _ = await invoke()
    check("other conversation's open work refuses pilot", result["reason"], "other-conversation-work")

    await reset()
    await h.freeze()
    twin, adapter = h.FakeTwin(), h.FakeAdapter()
    await h.worker.run_once(now=h.BASE_TIME + timedelta(seconds=10), scope=h.scope(),
                            client=twin, adapter=adapter, enable_send=False)
    result, _ = await invoke(twin=twin, adapter=adapter, send_limit=1)
    check("existing ready draft sends automatically", len(adapter.posts), 1)
    check("existing draft is not regenerated", twin.drafts, 1)
    check("existing draft reaches confirmed delivery", (await h.turns_for())[0].state, "delivered")

    await reset()
    await h.freeze()
    async with h.get_session() as session:
        await session.execute(update(h.TwinBridgeConversation).values(send_budget_limit=0))
    twin, adapter = h.FakeTwin(), h.FakeAdapter()
    result, _ = await invoke(twin=twin, adapter=adapter)
    check("persisted zero cap is not raised by pilot", result["reason"], "send-limit-reached")
    check("zero cap makes no external calls", (twin.drafts, len(adapter.posts)), (0, 0))

    await reset()
    twin, adapter = h.FakeTwin(), h.FakeAdapter()
    result, _ = await invoke(twin=twin, adapter=adapter, max_seconds=5)
    check("idle pilot stops at time bound", result["reason"], "time-limit-reached")
    check("idle pilot has no external calls", (twin.drafts, adapter.history_reads, len(adapter.posts)), (0, 0, 0))

    await reset()
    async def change_scope(clock):
        os.environ["TWIN_BRIDGE_FAN_ALLOWLIST"] = str(h.FAN + 1)
    result, _ = await invoke(clock=Clock(change_scope))
    check("changed configuration stops before another pass", result["reason"], "configuration-changed")


async def execute():
    try:
        await main()
    finally:
        await h.engine.dispose()


try:
    h.run(execute())
finally:
    shutil.rmtree(h._TMP_ROOT, ignore_errors=True)
print(f"{checks} checks; all cases passed (automatic pilot)")
