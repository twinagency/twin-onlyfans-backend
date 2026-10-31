"""Load profiles for the FASTT engine on the synthetic harness (no real provider, no real fan).

    python tests/fastt_load.py <profile> [--minutes N] [--out FILE.json]

Profiles: peak (6,250 sends/h across 50 accounts, one hot creator), peak2x (12,500/h across 100), daily (50,000 sends over a
diurnal day across 50), impossible (2x peak on 50 accounts: more than the 20 s rule allows), chaos (every fault at once).
Everything is virtual time: model latency, quiet windows, the 20 s account interval and queueing are exact; the
wall-clock cost is reported separately. Output: one JSON document with per-account latency statistics, backlog age over
time, failures and the zero-tolerance counters.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_harness as H  # noqa: E402

MAX_PER_ACCOUNT_PER_HOUR = 3600 // 20          # 180: the 20 s rule, regardless of any code


def lognorm(median, sigma, slow_p=0.0, slow=(10, 40)):
    mu = math.log(median)

    def draw(rng):
        if slow_p and rng.random() < slow_p:
            return rng.uniform(*slow)
        return min(45.0, rng.lognormvariate(mu, sigma))
    return draw


def diurnal(hour):
    """Relative traffic by hour of day (UTC-ish): quiet night, evening peak. Mean 1.0 over 24 h; peak/avg = 3.0."""
    shape = [0.30, 0.22, 0.18, 0.15, 0.15, 0.20, 0.30, 0.45, 0.60, 0.75, 0.85, 0.95,
             1.05, 1.10, 1.15, 1.30, 1.50, 1.90, 2.40, 3.00, 2.80, 2.20, 1.50, 0.80]
    mean = sum(shape) / 24
    return shape[hour % 24] / mean


async def traffic(sim, rng, rates, duration_s, shape=None, start_hour=0):
    """Poisson bursts per account: rates[acct] = replies/hour wanted. A burst is 1-3 messages 2-4 s apart."""
    async def one(acct, fans, rate):
        t = 0.0
        while t < duration_s:
            mult = shape((start_hour * 3600 + t) / 3600) if shape else 1.0
            lam = max(rate * mult, 0.01) / 3600
            gap = rng.expovariate(lam)
            await asyncio.sleep(gap)
            t += gap
            if t >= duration_s:
                break
            fan = rng.choice(fans)
            n = rng.choice((1, 1, 1, 2, 3))
            asyncio.ensure_future(burst(acct, fan, n))

    async def burst(acct, fan, n):
        for k in range(n):
            sim.fan_says(acct, fan, "m")
            if k < n - 1:
                await asyncio.sleep(rng.uniform(2, 4))
    await asyncio.gather(*(one(a, sim.accounts[a], r) for a, r in rates.items() if r > 0))


async def chaos(sim, rng, duration_s):
    accts = list(sim.accounts)
    counts = {"takeover_queued": 0, "takeover_generating": 0, "takeover_sending": 0, "kills": 0, "reconnects": 0,
              "dups": 0, "faults_429": 0, "faults_timeout": 0, "credential": 0}
    stage = lambda acct, fan: ("takeover_sending" if getattr(sim.services[acct].children.get(fan), "phase", "") == "sending"
                               and fan in sim.services[acct].tasks else
                               "takeover_generating" if fan in sim.services[acct].tasks else "takeover_queued")

    async def random_faults():
        t = 0.0
        while t < duration_s:
            gap = rng.uniform(1, 4)
            await asyncio.sleep(gap)
            t += gap
            acct = rng.choice(accts)
            roll = rng.random()
            p = sim.providers[acct]
            if roll < 0.12:
                p.faults.append(("429", rng.choice((None, 30, 60, 120))))
                counts["faults_429"] += 1
            elif roll < 0.14:
                p.faults.append((rng.choice(("timeout_delivered", "timeout_lost")),))
                counts["faults_timeout"] += 1
            elif roll < 0.30:
                fan = rng.choice(sim.accounts[acct])
                pend = [m for m in p.chats.get(fan, []) if not m["isFromMe"]]
                if pend:
                    sim.duplicate_event(acct, rng.choice(pend))
                    counts["dups"] += 1
            elif roll < 0.33:
                sim.disconnect(acct)
                counts["reconnects"] += 1
            elif roll < 0.55:
                fan = rng.choice(sim.accounts[acct])
                if not sim.takeovers.get((acct, fan)) or sim.takeovers[(acct, fan)][-1][1] is not None:
                    counts[stage(acct, fan)] += 1
                    sim.takeover(acct, fan)
                    asyncio.ensure_future(release(acct, fan))

    async def release(acct, fan):
        await asyncio.sleep(rng.uniform(40, 200))
        if rng.random() < 0.5:
            sim.human_says(acct, fan, "human")
        sim.handback(acct, fan)

    async def kills():
        for frac in (0.33, 0.66):
            await asyncio.sleep(duration_s * (frac if frac < 0.5 else 0.33))
            for acct in rng.sample(accts, max(1, len(accts) // 5)):
                await sim.kill_service(acct)
                sim.start_service(acct)
                counts["kills"] += 1

    async def credentials():
        await asyncio.sleep(duration_s * 0.5)
        for acct in rng.sample(accts, max(1, len(accts) // 6)):
            sim.change_credentials(acct)
            counts["credential"] += 1
    await asyncio.gather(random_faults(), kills(), credentials())
    return counts


PROFILES = {
    # accounts, hot-creator multiplier, fleet sends/h, fans/account, model latency, minutes of traffic, idle poll (s)
    # Durations are virtual minutes of steady traffic after a 2 minute warm-up; a 10 minute drain follows. The poll is 5 s (the
    # engine's default is 2 s): the harness runs every account in ONE python thread, and each idle poll costs ~20 ms of SQLite
    # planning there, so a 2 s poll across 100 accounts is not affordable. It adds at most 5 s (mean 2.5 s) to a reply latency.
    "peak": dict(accounts=50, fleet_per_h=6250, hot=2.0, fans=120, latency=lognorm(1.5, .8, .01), minutes=25, poll=5.0),
    "peak2x": dict(accounts=100, fleet_per_h=12500, hot=1.6, fans=100, latency=lognorm(1.5, .8, .01), minutes=12, poll=5.0),
    "peak_bal": dict(accounts=50, fleet_per_h=6250, hot=1.0, fans=120, latency=lognorm(1.5, .8, .01), minutes=25, poll=5.0),
    "peak2x_bal": dict(accounts=100, fleet_per_h=12500, hot=1.0, fans=100, latency=lognorm(1.5, .8, .01), minutes=12, poll=5.0),
    "avg": dict(accounts=50, fleet_per_h=50000 / 24, hot=2.0, fans=120, latency=lognorm(1.5, .8, .01), minutes=40, poll=5.0),
    "impossible": dict(accounts=50, fleet_per_h=12500, hot=1.0, fans=150, latency=lognorm(1.5, .8, .01), minutes=15, poll=5.0),
    "slowmodel": dict(accounts=20, fleet_per_h=2500, hot=2.0, fans=100, latency=lognorm(14, .7, .05, (30, 45)), minutes=25, poll=5.0),
    "chaos": dict(accounts=20, fleet_per_h=2200, hot=2.0, fans=80, latency=lognorm(6, .9, .03, (20, 45)), minutes=30, poll=5.0, chaos=True),
}


def build(profile, minutes=None, accounts=None):
    p = dict(PROFILES[profile])
    if accounts:
        # Scale the fleet DOWN keeping the per-account rate: accounts are independent processes, so the per-account
        # latency / backlog / zero-tolerance counters do not depend on how many siblings run (the shared database is the
        # one exception and is measured separately).
        p["fleet_per_h"] = p["fleet_per_h"] * accounts / p["accounts"]
        p["accounts"] = accounts
    n = p["accounts"]
    accounts = {str(100000 + i): [10000 + k for k in range(p["fans"])] for i in range(n)}
    # sends/h: reply count per burst averages (1.2 bubbles) -> bursts/h = fleet / 1.2. One hot creator gets p["hot"] x.
    bursts_per_h = p["fleet_per_h"] / 1.2
    weights = [p["hot"]] + [1.0] * (n - 1)
    scale = bursts_per_h / sum(weights)
    rates = {a: w * scale for a, w in zip(accounts, weights)}
    return p, accounts, rates


def run(profile, minutes=None, out=None, seed=7, n_accounts=None, drain=600.0):
    p, accounts, rates = build(profile, minutes, n_accounts)
    minutes = minutes or p["minutes"]
    rng = random.Random(seed)
    hot = next(iter(accounts))
    root = tempfile.mkdtemp(dir="/dev/shm")
    wall0 = time.time()
    result = {}

    async def scenario(sim):
        sim.fast_idle = True          # idle polls skip the SQLite pending query (see PROFILES note); busy accounts run the real one
        sim.takeovers_open = bool(p.get('chaos'))
        warm = 120.0
        await asyncio.sleep(warm)                     # services start, sockets connect
        t0 = sim.loop._vt
        duration = minutes * 60.0
        stop = asyncio.Event()
        sampler = asyncio.ensure_future(sim.backlog_sampler(60.0, stop))
        chaos_counts = {}
        jobs = [traffic(sim, rng, rates, duration, diurnal if p.get("diurnal") else None)]
        if p.get("chaos"):
            async def c():
                chaos_counts.update(await chaos(sim, rng, duration))
            jobs.append(c())
        await asyncio.gather(*jobs)
        await asyncio.sleep(drain)                    # drain: whatever the 20 s rule allows to finish
        stop.set()
        sampler.cancel()
        # per-account results over the traffic window
        horizon = sim.loop._vt - t0
        per = {}
        for acct in sim.accounts:
            pr = sim.providers[acct]
            per_hour = {}
            for (when, fan, body) in pr.post_log:
                h = int(((when - sim.clock.start).total_seconds() - t0) // 3600)
                per_hour[h] = per_hour.get(h, 0) + 1
            per[acct] = {"sends": sim.sends[acct], "latency_s": H.stats(sim.latencies[acct]),
                         "oldest_wait_at_send_s": H.stats(sim.wait_first[acct]),
                         "max_sends_in_an_hour": max(per_hour.values()) if per_hour else 0,
                         "min_gap_s": None if pr.min_gap is None else round(pr.min_gap, 2),
                         "rate_violations": pr.rate_violations, "stale_credential_posts": pr.stale_credential_posts}
        unanswered = sum(len(v) for v in sim.inbound_at.values())
        samples = []
        for t, per_acct in sim.backlog_samples:
            if not per_acct:
                samples.append((t, 0, 0, 0.0))
                continue
            samples.append((t, sum(v[0] for v in per_acct.values()), len(per_acct), max(v[1] for v in per_acct.values())))
        every = max(1, len(samples) // 14)
        series = [{"t_min": round((t - t0) / 60), "fans_waiting": n, "accounts_with_backlog": a, "oldest_s": round(o)}
                  for t, n, a, o in samples[::every]]
        all_lat = [x for a in sim.accounts for x in sim.latencies[a]]
        tot_sends = sum(sim.sends.values())
        events = {}
        for e in sim.events:
            events[e["event"]] = events.get(e["event"], 0) + 1
        res = {
            "profile": profile, "accounts": len(accounts), "fleet_sends_per_h_planned": round(p["fleet_per_h"]), "minutes_of_traffic": minutes,
            "virtual_seconds": round(horizon), "wall_seconds": round(time.time() - wall0),
            "sends_total": tot_sends, "sends_per_hour_fleet": round(tot_sends / (minutes / 60)),
            "inbound_messages": sim.inbound_total, "replies_per_inbound": round(tot_sends / max(1, sim.inbound_total), 2),
            "fleet_latency_s": H.stats(all_lat), "hot_account": {"id": hot, **per[hot]},
            "worst_account_by_p95": max(({"id": a, **v} for a, v in per.items() if v["latency_s"].get("n")),
                                        key=lambda x: x["latency_s"]["p95"]),
            "duplicates": sim.duplicates, "cross_account_sends": sim.cross_account, "takeover_violations": sim.takeover_violations,
            "rate_violations_under_20s": sum(v["rate_violations"] for v in per.values()),
            "stale_credential_posts": sum(v["stale_credential_posts"] for v in per.values()),
            "unanswered_at_end": unanswered, "failures_sample": sim.failures[:10],
            "events": {k: events[k] for k in sorted(events) if k in (
                "turn-released", "turn-requeued", "turn-abandoned", "turn-cancel-requested", "delivery-reconcile", "held",
                "runner-lease-acquired", "runner-lease-lost", "intake-connected", "setting-clamped", "reconcile-error",
                "turn-task-error", "work-board-flush-failed")},
            "abandoned_reasons": {k: sum(1 for e in sim.events if e["event"] == "turn-abandoned" and e.get("reason") == k)
                                  for k in {e.get("reason") for e in sim.events if e["event"] == "turn-abandoned"}},
            "released_reasons": {k: sum(1 for e in sim.events if e["event"] == "turn-released" and e.get("reason") == k)
                                 for k in {e.get("reason") for e in sim.events if e["event"] == "turn-released"}},
            "backlog_over_time": series, "chaos": chaos_counts,
            "max_sends_per_hour_per_account_by_rule": MAX_PER_ACCOUNT_PER_HOUR,
            "per_account": per,
        }
        result.update(res)
        return res

    H.run_sim(scenario, root=root, accounts=accounts, concurrency=4, model_latency=p["latency"], poll_s=p["poll"],
              rng=random.Random(seed + 1), bubbles=lambda r: 1 if r.random() < 0.83 else 2)
    if out:
        Path(out).write_text(json.dumps(result, indent=1, default=str))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", choices=sorted(PROFILES))
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--out")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--accounts", type=int, help="scale the fleet down to N accounts at the same per-account rate")
    ap.add_argument("--drain", type=float, default=600.0, help="virtual seconds to let the queue drain after the traffic stops")
    args = ap.parse_args()
    res = run(args.profile, args.minutes, args.out, args.seed, args.accounts, args.drain)
    summary = {k: v for k, v in res.items() if k != "per_account"}
    print(json.dumps(summary, indent=1, default=str))
