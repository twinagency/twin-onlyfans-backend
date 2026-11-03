"""B4: burst safety on the synthetic engine harness (fake provider and model, virtual clock, SQLite; no network, no real
credential). 1,000 fans message one creator inside a minute.

Asserted: nothing lost (every fan not handed to a human is answered), no duplicates, no cross-fan send, the gap between
sends never under the account's gap, in-flight work bounded by the concurrency, the engine's queue window bounded and the
policy ``queued_count`` (the Twin path's 1000 cap) never raised by the engine; at the cap the behaviour is BACKPRESSURE
(the messages stay in the database and are answered as the gate frees up), never a drop; a model timeout on one fan does
not stall the others; a human takeover mid-burst stops the AI for that fan and only that fan.
BURST_N=<n> shrinks the burst for a quick run; the default is 1000. Plain script."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time as _wall
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_harness as H  # noqa: E402

N = int(os.environ.get("BURST_N", "1000"))
A = "111000"
GAP = 10          # per-account gap (the shared floor) so a 1,000 burst drains in about 2.8 virtual hours
CHECKS = 0
ONE = lambda rng: 1  # noqa: E731


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


async def scenario(sim):
    fs = sim.accounts[A]
    t0 = _wall.time()
    await asyncio.sleep(3)
    sim.fail_models = 1                                  # the very first model call times out: that fan is retried, nobody waits for it
    taken = fs[N - 5]                                    # late in the queue: a human takes this one over mid-burst
    for i, fan in enumerate(fs):
        sim.fan_says(A, fan, "hi")
        if i % 17 == 16:
            await asyncio.sleep(1.0)                    # about 60 s for the whole burst
    check(sim.inbound_total == N, "all inbound messages were delivered to the engine's intake")
    await asyncio.sleep(300)
    sim.takeover(A, taken)
    p = sim.providers[A]
    budget = N * (GAP + 3) + 900                         # the gate drains one reply per GAP; allow every reply + slack
    step = 600
    waited = 0
    while waited < budget and sim.sends[A] < N - 1:
        await asyncio.sleep(step)
        waited += step
    await asyncio.sleep(120)
    # ── nothing lost, no duplicates ────────────────────────────────────────────────────────────────────────────
    answered = {f for _t, f, _b in p.post_log}
    check(sim.duplicates == 0 and sim.cross_account == 0, f"no duplicate / cross-fan sends {sim.failures[:3]}")
    check(taken not in {f for t, f, _b in p.post_log if t > sim.takeovers[(A, taken)][-1][0]}, "no send to the taken-over fan after takeover")
    check(sim.takeover_violations == 0, "takeover respected")
    missing = [f for f in fs if f not in answered and f != taken]
    check(not missing, f"every fan except the taken-over one was answered; missing {len(missing)}")
    check(sim.sends[A] in (N - 1, N), f"one reply per fan ({sim.sends[A]})")
    unanswered = sim.sql("SELECT count(*) FROM engine_work WHERE account_id=?", (A,))[0][0]
    check(unanswered <= 1, f"nothing is left waiting except the human's fan ({unanswered})")
    # ── rate and bounds ────────────────────────────────────────────────────────────────────────────────────────
    check(p.rate_violations == 0 and p.min_gap is not None and p.min_gap >= GAP - 0.01, f"never under the {GAP} s gap ({p.min_gap})")
    q = sim.kinds("engine-queue", A)
    from native_pilot.service import LIVE_TURNS_CAP_PER_SLOT
    live_cap = sim.tuning.concurrency * LIVE_TURNS_CAP_PER_SLOT
    print("  max in_flight %d, max preparing %s (concurrency %d, live-turn cap %d)" % (
        max(e["in_flight"] for e in q), max(e.get("preparing", -1) for e in q), sim.tuning.concurrency, live_cap), flush=True)
    # Turns that already sent their first bubble only wait at the send gate for later bubbles (no worker thread), so
    # in-flight is bounded by the hard live-turn cap; the turns still PREPARING stay bounded by the concurrency itself.
    check(q and max(e["in_flight"] for e in q) <= live_cap, "live turns bounded by the live-turn cap")
    check(q and max(e["preparing"] for e in q) <= sim.tuning.concurrency, "turns still preparing bounded by the concurrency")
    check(max(e["pending"] for e in q) <= sim.tuning.queue_max, f"queue window bounded by queue_max ({max(e['pending'] for e in q)})")
    check(sim.sql("SELECT queued_count FROM of_delivery_policy WHERE account_id=?", (A,))[0][0] <= 1000, "policy queued_count within its 1000 cap")
    check(max(e["pending"] for e in q) >= min(N, sim.tuning.queue_max) * 0.5 if N > 50 else True, "the window really filled (backpressure, not a trickle)")
    check(not [e for e in sim.events if e.get("event") in ("turn-abandoned",)], "no turn was abandoned (dropped)")
    # ── a model timeout on one fan does not stall the others ─────────────────────────────────────────────────────
    timed = sim.kinds("turn-requeued", A)
    check(any(e["reason"] == "model-timeout" for e in timed), "the model timeout happened and was requeued, not dropped")
    lat = sim.latencies[A]
    from fastt_harness import stats
    print("  burst %d fans, %d replies, latency s %s, wall %.0fs" % (N, sim.sends[A], stats(lat), _wall.time() - t0), flush=True)
    return {"sends": sim.sends[A], "latency": stats(lat)}


async def faults(sim):
    """A provider 429 on one reply and model timeouts on others, mid-burst: nobody else stalls, nothing is lost."""
    fs = sim.accounts[A]
    p = sim.providers[A]
    await asyncio.sleep(3)
    sim.fail_models = 3                                  # three model calls time out
    p.faults.append(("429", 30))                         # the first POST is refused with 429 / Retry-After 30
    for fan in fs:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(120)
    refused_at = p.post_log[0][0]
    check(sim.kinds("provider-throttled", A), "the 429 was seen")
    handed = fs[5]
    sim.human_says(A, handed, "hey it's me")             # a human answers one fan while the burst is running
    sim.takeover(A, handed)
    await asyncio.sleep(len(fs) * 25 + 600)
    answered = {f for _t, f, _b in p.post_log}
    check(sim.duplicates == 0 and sim.cross_account == 0, f"no duplicate or cross-fan send {sim.failures[:3]}")
    check(sim.takeover_violations == 0 and handed not in {f for t, f, _b in p.post_log if t > sim.takeovers[(A, handed)][-1][0]},
          "the human's fan got no AI send after takeover")
    check(set(fs) - answered <= {handed}, f"everyone else was answered ({len(set(fs) - answered)} missing)")
    accepted = [t for t, _f, _b in p.post_log][1:]
    gaps = [(b - a).total_seconds() for a, b in zip(accepted, accepted[1:])]
    check(gaps and max(gaps) <= 30, f"after the cooldown the queue kept moving, largest gap {max(gaps):.0f}s (one 429 / 3 model timeouts stalled nobody)")
    check((accepted[0] - refused_at).total_seconds() <= 30 + 25, "the first accepted send came within the Retry-After plus a gap")
    reasons = [e["reason"] for e in sim.kinds("turn-requeued", A)]
    check("provider-rate-limited" in reasons and "model-timeout" in reasons, "both failures were requeued, not dropped")
    check(not sim.kinds("turn-abandoned", A), "nothing abandoned")


def main():
    root = tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None)
    H.run_sim(faults, root=root, accounts={A: [2000 + i for i in range(60)]}, concurrency=4, model_latency=lambda rng: 3.0, bubbles=ONE,
              throttle=GAP, min_interval=GAP, tuning_extra={"per_account_send_gap": 1})
    print("  ok   faults (60 fans: a 429 and 3 model timeouts mid-burst, a human takeover)", flush=True)
    root = tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None)
    fans = [1000 + i for i in range(N)]
    H.run_sim(scenario, root=root, accounts={A: fans}, concurrency=4, model_latency=lambda rng: 3.0, bubbles=ONE, throttle=GAP,
              min_interval=GAP, tuning_extra={"per_account_send_gap": 1})
    print(f"throughput burst: all cases passed ({CHECKS} checks, {N} fans)")


if __name__ == "__main__":
    main()
