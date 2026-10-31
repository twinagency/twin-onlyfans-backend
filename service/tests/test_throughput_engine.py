"""Throughput controls on the synthetic engine harness (real Service / Ledger / SendGate on SQLite, fake provider and
model, virtual clock; no network, no real credential, no live database): B1 daily cap in the engine, B2 first-429
backoff, B3 per-account send gap / floor / per-account tuning, B5 stall alert. Plain-assert house style."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_harness as H  # noqa: E402

CHECKS = 0
A = "111000"
ONE = lambda rng: 1  # noqa: E731


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def root():
    return tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None)


def fans(n, base=1000):
    return [base + i for i in range(n)]


def gaps(sim, acct=A, after=None):
    times = [t for t, _f, _b in sim.providers[acct].post_log if after is None or t > after]
    return [(b - a).total_seconds() for a, b in zip(times, times[1:])]


def throttle(sim, acct=A):
    return sim.sql("SELECT throttle_seconds FROM of_delivery_policy WHERE account_id=?", (acct,))[0][0]


# ── B3: the gap. Off (default) = the 20 s floor binds whatever the row says ───────────────────────────────────────
async def s_gap_legacy(sim):
    await asyncio.sleep(3)
    for f in sim.accounts[A]:
        sim.fan_says(A, f, "hi")
    await asyncio.sleep(400)
    g = gaps(sim)
    check(sim.sends[A] == 6 and len(g) == 5, "all six answered")
    check(min(g) >= 19.9, f"flag off, row says 12: gap still >= 20 ({min(g):.2f})")


# ── B3: per-account gap on: the row's own 12 s is used, never under it ──────────────────────────────────────────────
async def s_gap_12(sim):
    await asyncio.sleep(3)
    for f in sim.accounts[A]:
        sim.fan_says(A, f, "hi")
    await asyncio.sleep(400)
    g = gaps(sim)
    check(sim.sends[A] == 6 and len(g) == 5, "all six answered")
    check(min(g) >= 11.99, f"never under the account's 12 s ({min(g):.2f})")
    check(max(g) < 19, f"and it really is faster than 20 s ({max(g):.2f})")
    check(sim.providers[A].rate_violations == 0, "provider audit saw no gap under 12 s")
    check(throttle(sim) == 12, "the row was not touched (no 429)")


# ── B3: a row under the 10 s floor is treated as 'not set' (20 s), never as 1 send per second ────────────────────
async def s_gap_unset(sim):
    await asyncio.sleep(3)
    for f in sim.accounts[A]:
        sim.fan_says(A, f, "hi")
    await asyncio.sleep(400)
    g = gaps(sim)
    check(len(g) == 3 and min(g) >= 19.9, f"row of 5 s with per-account gap on = 20 s default ({min(g):.2f})")


# ── B2: first 429 -> the account is back at 20 s, an alert is left, nothing lowers it again ──────────────────────
async def s_backoff(sim):
    await asyncio.sleep(3)
    p = sim.providers[A]
    p.faults.append(("429", 40))
    for f in sim.accounts[A]:
        sim.fan_says(A, f, "hi")
    await asyncio.sleep(120)
    check(throttle(sim) == 20, "the first 429 returned the account to 20 s")
    ev = sim.kinds("provider-throttled", A)
    check(ev and ev[0]["changed"] is True and ev[0]["before"] == 12 and ev[0]["after"] == 20, f"event {ev[:1]}")
    alerts = sim.sql("SELECT kind,state,count FROM engine_alert WHERE account_id=?", (A,))
    check(alerts == [("provider-throttle", "open", 1)], f"one open alert row {alerts}")
    audit = sim.sql("SELECT actor,before_json,after_json FROM of_delivery_policy_change WHERE account_id=?", (A,))
    check(len(audit) == 1 and json.loads(audit[0][1]) == {"throttle_seconds": 12} and json.loads(audit[0][2]) == {"throttle_seconds": 20},
          f"policy change audited {audit}")
    await asyncio.sleep(600)
    check(sim.sends[A] == 3 and sim.duplicates == 0, f"every fan answered exactly once after the cooldown ({sim.sends[A]})")
    after = p.post_log[1][0]
    g = gaps(sim, after=after)
    check(g and min(g) >= 19.9, f"sends after the 429 are >= 20 s apart ({min(g):.2f})")
    check(throttle(sim) == 20, "still 20 s: nothing lowered it again")
    # a second 429 later does not change the row again, only the alert count
    p.faults.append(("429", 30))
    sim.fan_says(A, sim.accounts[A][0], "again")
    await asyncio.sleep(300)
    check(throttle(sim) == 20)
    check(sim.sql("SELECT count FROM engine_alert WHERE account_id=?", (A,))[0][0] >= 2, "the alert count grew")
    check(len(sim.sql("SELECT 1 FROM of_delivery_policy_change WHERE account_id=?", (A,))) == 1, "no second policy change")


# ── B1 in the engine: the daily cap holds replies, spends no model call on them, and the window releases them ────
async def s_daily_cap(sim):
    await asyncio.sleep(3)
    sim.sql("UPDATE of_delivery_policy SET daily_limit_units=3 WHERE account_id=?", (A,))
    for f in sim.accounts[A]:
        sim.fan_says(A, f, "hi")
    await asyncio.sleep(600)
    check(sim.sends[A] == 3, f"exactly the cap was sent ({sim.sends[A]})")
    gen = len(sim.kinds("generating", A))
    board = sim.sql("SELECT state,reason FROM engine_work WHERE account_id=?", (A,))
    check(board and all(r == ("blocked", "allowance-daily-cap") for r in board), f"the rest are visibly blocked: {board}")
    await asyncio.sleep(600)
    check(len(sim.kinds("generating", A)) == gen, "no model call was spent while capped")
    check(sim.sends[A] == 3)
    # 25 hours pass for the three sends already made (aged in place; a virtual day of 2 s ticks would only slow the test)
    sim.sql("UPDATE native_pilot_receipts SET created_at=datetime(created_at,'-25 hours')")
    await asyncio.sleep(400)
    check(sim.sends[A] == 6 and sim.duplicates == 0, f"the window rolled and the other three went out ({sim.sends[A]})")
    # the lifetime limit_units column was never the thing that bound
    check(sim.sql("SELECT limit_units FROM of_delivery_policy WHERE account_id=?", (A,))[0][0] == 100000000)


# ── B5: owed work that is not moving raises send-stall, then clears ────────────────────────────────────────────────
async def s_stall(sim):
    await asyncio.sleep(3)
    # A durable account cooldown holds every fan without consuming the
    # one-model-retry budget: this is the engine-owned condition the stall
    # alert is meant to surface.
    until = (sim.clock() + timedelta(seconds=2000)).isoformat(sep=" ")
    sim.sql("UPDATE of_delivery_policy SET provider_cooldown_until=?,next_dispatch_at=? WHERE account_id=?",
            (until, until, A))
    for f in sim.accounts[A]:
        sim.fan_says(A, f, "hi")
    await asyncio.sleep(90)
    check(not sim.kinds("send-stall", A), "not yet: under the 120 s threshold the engine stays quiet")
    await asyncio.sleep(500)
    ev = sim.kinds("send-stall", A)
    check(ev and ev[0]["moving"] is False and ev[0]["oldest_wait_s"] > 120 and ev[0]["owed_past_threshold"] >= 1, f"stall event {ev[:1]}")
    row = sim.sql("SELECT state,count FROM engine_alert WHERE account_id=? AND kind='send-stall'", (A,))
    check(row and row[0][0] == "open", f"durable alert open {row}")
    sim.sql("UPDATE of_delivery_policy SET provider_cooldown_until=NULL,next_dispatch_at=NULL WHERE account_id=?", (A,))
    await asyncio.sleep(1500)
    check(sim.sends[A] == len(sim.accounts[A]), f"everyone was answered once the model recovered ({sim.sends[A]})")
    check(sim.kinds("send-stall-cleared", A), "cleared event")
    check(sim.sql("SELECT state FROM engine_alert WHERE account_id=? AND kind='send-stall'", (A,))[0][0] == "cleared", "alert row cleared")


# ── B3: per-account concurrency and quiet window from FASTT_ACCOUNT_TUNING ──────────────────────────────────────
def unit_tuning():
    from native_pilot.tuning import Tuning
    t = Tuning.from_env({})
    check((t.concurrency, t.quiet_s, t.burst_max_wait_s, t.min_send_interval_s, t.per_account_send_gap) == (4, 7.0, 45.0, 20.0, 0), "defaults unchanged")
    env = {"FASTT_ACCOUNT_TUNING": json.dumps({"of-111": {"concurrency": 8, "quiet_s": 3, "burst_max_wait_s": 20, "min_send_interval_s": 1}, "222": {"concurrency": 2}})}
    t = Tuning.from_env(env, account="of-111")
    check((t.concurrency, t.quiet_s, t.burst_max_wait_s) == (8, 3.0, 20.0), "account entry applies")
    check(t.min_send_interval_s == 20.0 and ("FASTT_ACCOUNT_TUNING.min_send_interval_s", "ignored") in t.clamped, "the gap is not per-account env tunable")
    check(Tuning.from_env(env, account="of-222").concurrency == 2, "bare id matches too")
    check(Tuning.from_env(env, account="of-999").concurrency == 4, "other accounts keep the defaults")
    check(Tuning.from_env(env).concurrency == 4, "no account = defaults")
    t = Tuning.from_env({"FASTT_ACCOUNT_TUNING": json.dumps({"111": {"concurrency": 500, "quiet_s": 0}})}, account="of-111")
    check((t.concurrency, t.quiet_s) == (16, 1.0) and len(t.clamped) == 2, "still clamped by the global bounds")
    t = Tuning.from_env({"FASTT_ACCOUNT_TUNING": "{nope"}, account="of-111")
    check(t.concurrency == 4 and t.clamped == (("FASTT_ACCOUNT_TUNING", "invalid"),), "bad JSON = defaults, reported")
    # the explicit global env still works and an account entry beats it
    t = Tuning.from_env({"FASTT_PREPARE_CONCURRENCY": "6", "FASTT_ACCOUNT_TUNING": json.dumps({"111": {"concurrency": 9}})}, account="of-111")
    check(t.concurrency == 9 and Tuning.from_env({"FASTT_PREPARE_CONCURRENCY": "6"}, account="of-111").concurrency == 6)


def unit_gap():
    from onlyfans_runtime import send_gap as g
    from native_pilot.tuning import Tuning
    check(g.effective_gap(20, 20, False) == 20 and g.effective_gap(1, 20, False) == 20 and g.effective_gap(60, 20, False) == 60, "legacy: unchanged")
    check(g.effective_gap(12, 10, True) == 12 and g.effective_gap(10, 10, True) == 10, "per-account: row decides")
    check(g.effective_gap(9, 10, True) == 20 and g.effective_gap(1, 10, True) == 20 and g.effective_gap(0, 10, True) == 20, "under the floor = default 20")
    check(g.effective_gap(12, 30, True) == 30, "an explicitly raised minimum still wins")
    check(g.effective_gap(15, 10, True) >= g.HARD_FLOOR_S, "never under 10 s")
    check(g.below_floor(9, {}) and g.below_floor(1, {}) and not g.below_floor(10, {}) and not g.below_floor(20, {}), "setter refuses < 10")
    check(not g.below_floor(1, {g.OVERRIDE_ENV: "1"}), "explicit override env allows it")
    check(Tuning.from_env({}).min_send_interval_s == 20.0 and Tuning.from_env({"FASTT_MIN_SEND_INTERVAL_S": "5"}).min_send_interval_s == 20.0, "flag off: env cannot lower below 20")
    t = Tuning.from_env({"FASTT_PER_ACCOUNT_SEND_GAP": "1", "FASTT_MIN_SEND_INTERVAL_S": "5"})
    check(t.per_account_send_gap == 1 and t.min_send_interval_s == 10.0, "flag on: the env floor is the shared 10 s ceiling, not lower")
    check(Tuning.from_env({"FASTT_PER_ACCOUNT_SEND_GAP": "1"}).min_send_interval_s == 10.0)


SCENARIOS = [
    ("gap-legacy", s_gap_legacy, dict(accounts={A: fans(6)}, concurrency=4, model_latency=lambda rng: 3.0, bubbles=ONE, throttle=12)),
    ("gap-12", s_gap_12, dict(accounts={A: fans(6)}, concurrency=4, model_latency=lambda rng: 3.0, bubbles=ONE, throttle=12, min_interval=12,
                              tuning_extra={"per_account_send_gap": 1})),
    ("gap-unset", s_gap_unset, dict(accounts={A: fans(4)}, concurrency=4, model_latency=lambda rng: 3.0, bubbles=ONE, throttle=5, min_interval=20,
                                    tuning_extra={"per_account_send_gap": 1})),
    ("backoff", s_backoff, dict(accounts={A: fans(3)}, concurrency=3, model_latency=lambda rng: 3.0, bubbles=ONE, throttle=12, min_interval=12,
                                tuning_extra={"per_account_send_gap": 1})),
    ("daily-cap", s_daily_cap, dict(accounts={A: fans(6)}, concurrency=4, model_latency=lambda rng: 3.0, bubbles=ONE)),
    ("stall", s_stall, dict(accounts={A: fans(3)}, concurrency=3, model_latency=lambda rng: 3.0, bubbles=ONE,
                            tuning_extra={"stall_alert_s": 120, "status_every_s": 15})),
]


def main(only=None):
    unit_gap()
    unit_tuning()
    print("  ok   units", flush=True)
    failed = []
    for name, scenario, kw in SCENARIOS:
        if only and name not in only:
            continue
        try:
            H.run_sim(scenario, root=root(), **kw)
            print("  ok  ", name, flush=True)
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print("  FAIL", name, "-", type(exc).__name__, exc, flush=True)
            sim = getattr(exc, "sim", None)
            if sim is not None and os.environ.get("SIM_DEBUG"):
                for e in [e for e in sim.events if e["event"] not in ("engine-queue", "inbound-persisted")][-25:]:
                    print("     ", e)
    if failed:
        raise SystemExit("failed: " + ", ".join(failed))
    print(f"throughput engine scenarios: all cases passed ({CHECKS} checks)")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
