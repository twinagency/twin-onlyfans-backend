"""R8 timing/capacity scenarios on the existing virtual-time FASTT harness (fastt_harness.Sim / vtime).

Not a parallel harness: every scenario drives ``fastt_harness.run_sim`` (real Service, Ledger, SendGate, admission,
runner send boundary, Intake; fake provider and fake model) and reads the harness' own recorders. Added here:

* the user-visible clock: earliest unresolved qualifying fan input -> first substantive reply (``Sim.wait_first``;
  later bubbles of the same reply and any filler do not restart it);
* a scenario library (100 simultaneous fans, several creators, 50-creator roster, bursts, multi-bubble, media,
  backlog, provider 429 / auth failure, worker restart, competing manual takeover);
* gap vs concurrent delivery on the same scenario and seed.

    python tests/fastt_r8_load.py [scenario ...] [--mode gap|concurrent|both] [--json out.json] [--list]

A synthetic provider allowance is NOT live provider capacity: every number is a property of the synthetic provider
configured below, of the engine code, and of the harness' one-process SQLite/virtual-time execution.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
import time as _wall
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_harness as H  # noqa: E402

WINDOW_S = 180.0
ONE = lambda rng: 1  # noqa: E731

# Delivery configurations. "gap" is the production default (one send per 20 s per account). "concurrent" uses the
# shipped POLICY defaults of the opt-in mode (1 request/s, burst 5, 4 in flight, 4 sender slots). "concurrent_fast"
# is the configuration the existing concurrency acceptance suite uses (10 requests/s): it is a SYNTHETIC allowance.
MODES = {
    "gap": dict(delivery_mode="gap", concurrency=4, throttle=20, min_interval=20),
    "concurrent": dict(delivery_mode="concurrent", provider_rate=1.0, provider_burst=5, provider_max_concurrent=4,
                       conversation_gap=0, provider_latency=0.12, min_interval=0, throttle=0, concurrency=8,
                       tuning_extra={"send_concurrency": 4}),
    "concurrent_fast": dict(delivery_mode="concurrent", provider_rate=10.0, provider_burst=10, provider_max_concurrent=8,
                            conversation_gap=0, provider_latency=0.12, min_interval=0, throttle=0, concurrency=16,
                            tuning_extra={"send_concurrency": 8}),
}


def fans(base, n):
    return [base + i for i in range(n)]


def roster(n, per, base=100000):
    return {str(base + i): fans(10000, per) for i in range(n)}


def pctile(values, q):
    return H.pct(values, q)


def summarise(sim, name, mode, t0, excluded=()):
    """Everything the gate states, from the harness' recorders."""
    waits = [x for a in sim.accounts for x in sim.wait_first[a]]
    lats = [x for a in sim.accounts for x in sim.latencies[a]]
    excl = set(excluded)
    stranded = {}
    for (acct, fan), pend in sim.inbound_at.items():
        if pend and (acct, fan) not in excl:
            stranded[(acct, fan)] = len(pend)
    stranded_n = len(stranded)
    taken_n = sum(1 for (acct, fan), pend in sim.inbound_at.items() if pend and (acct, fan) in excl)
    answered = len(waits)
    eligible = answered + stranded_n
    within = sum(1 for w in waits if w <= WINDOW_S)
    ev = {}
    for e in sim.events:
        ev[e["event"]] = ev.get(e["event"], 0) + 1
    samples = [sum(v[0] for v in per.values()) for _t, per in sim.backlog_samples]
    third = max(1, len(samples) // 3)
    growth = None
    if len(samples) >= 3:
        growth = {"first_third_mean": round(sum(samples[:third]) / third, 1),
                  "last_third_mean": round(sum(samples[-third:]) / third, 1),
                  "peak_fans_waiting": max(samples), "final_fans_waiting": samples[-1]}
    rate_v = sum(p.rate_violations for p in sim.providers.values())
    return {
        "scenario": name, "mode": mode, "accounts": len(sim.accounts),
        "fans": sum(len(v) for v in sim.accounts.values()), "inbound_messages": sim.inbound_total,
        "replies_sent": sum(sim.sends.values()), "eligible_turns": eligible, "answered_turns": answered,
        "within_180s": within, "share_within_180s": round(within / eligible, 4) if eligible else None,
        "user_visible_s": {"p50": _r(pctile(waits, .5)), "p95": _r(pctile(waits, .95)), "p99": _r(pctile(waits, .99)),
                           "max": _r(max(waits) if waits else None)},
        "newest_input_to_reply_s": {"p50": _r(pctile(lats, .5)), "p95": _r(pctile(lats, .95))},
        "stranded_due_turns": stranded_n, "operator_owned_unanswered": taken_n,
        "duplicate_sends": sim.duplicates, "cross_account_sends": sim.cross_account,
        "takeover_violations": sim.takeover_violations, "rate_violations": rate_v,
        "stale_credential_posts": sum(p.stale_credential_posts for p in sim.providers.values()),
        "max_provider_inflight": max((p.max_active for p in sim.providers.values()), default=0),
        "retries": {k: ev.get(k, 0) for k in ("turn-requeued", "turn-released", "turn-abandoned", "provider-throttled",
                                             "account-blocked", "turn-cancel-requested")},
        "queue": growth, "failures_sample": sim.failures[:3],
        "wall_s": round(_wall.time() - t0, 1),
    }


def _r(v):
    return None if v is None else round(v, 1)


async def settle(sim, expect_per_account, *, limit_s, step=10.0):
    """Wait (virtual) until every account has sent its expected replies or the limit passes."""
    waited = 0.0
    while waited < limit_s and any(sim.sends[a] < n for a, n in expect_per_account.items()):
        await asyncio.sleep(step)
        waited += step
    await asyncio.sleep(30)


# ── scenarios: each returns (accounts, sim_overrides, async driver(sim) -> excluded fan keys) ───────────────────────
def sc_one_creator_100(seed):
    accounts = {"111000": fans(1000, 100)}

    async def drive(sim):
        await asyncio.sleep(3)
        for fan in accounts["111000"]:
            sim.fan_says("111000", fan, "hey")
        await settle(sim, {"111000": 100}, limit_s=4000)
        return ()
    return accounts, {}, drive


def sc_several_creators(seed):
    accounts = {str(111000 + 1000 * i): fans(1000 * (i + 1), 40) for i in range(5)}

    async def drive(sim):
        await asyncio.sleep(3)
        for acct, fs in accounts.items():
            for fan in fs:
                sim.fan_says(acct, fan, "hey")
        await settle(sim, {a: 40 for a in accounts}, limit_s=2500)
        return ()
    return accounts, {}, drive


def sc_roster50(seed):
    """50 creators, each at 3 fans/minute of fresh bursts for 15 virtual minutes (150 replies/min fleet)."""
    accounts = roster(50, 60)
    rng = random.Random(seed)

    async def drive(sim):
        await asyncio.sleep(10)
        t = 0.0
        duration = 900.0
        pending = []
        while t < duration:
            gap = rng.uniform(0.2, 0.6)
            await asyncio.sleep(gap)
            t += gap
            acct = rng.choice(list(accounts))
            fan = rng.choice(accounts[acct])
            sim.fan_says(acct, fan, "hey")
        await asyncio.sleep(400)
        return ()
    return accounts, {}, drive


def sc_bursts(seed):
    """Each of 60 fans sends 3 messages 2-4 s apart (quiet window and hard maximum), two replies bubbles each."""
    acct = "111000"
    accounts = {acct: fans(2000, 60)}
    rng = random.Random(seed)

    async def burst(sim, fan):
        for k in range(3):
            sim.fan_says(acct, fan, f"m{k}")
            if k < 2:
                await asyncio.sleep(rng.uniform(2, 4))

    async def drive(sim):
        await asyncio.sleep(3)
        await asyncio.gather(*(burst(sim, f) for f in accounts[acct]))
        await settle(sim, {acct: 60}, limit_s=3000)
        return ()
    return accounts, {"bubbles": lambda r: 2}, drive


def sc_multi_bubble(seed):
    acct = "111000"
    accounts = {acct: fans(3200, 40)}

    async def drive(sim):
        await asyncio.sleep(3)
        for fan in accounts[acct]:
            sim.fan_says(acct, fan, "hey")
        await settle(sim, {acct: 80}, limit_s=3000)
        return ()
    return accounts, {"bubbles": lambda r: 2}, drive


def sc_media(seed):
    acct = "111000"
    accounts = {acct: fans(7000, 40)}
    root = Path(tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None))
    manifest = root / "paid.json"
    manifest.write_text(json.dumps({
        "account_id": acct, "fan_id": "*",
        "items": [{"catalog_id": 1, "kind": "video", "price_cents": 300,
                   "media": [{"id": 7101, "type": "photo"}, {"id": 7102, "type": "video"}], "preview_ids": [7101]}],
    }), encoding="utf-8")

    async def drive(sim):
        sim.paid_fans.update(accounts[acct][:10])          # a quarter of the fans receive the reviewed paid card
        await asyncio.sleep(3)
        for fan in accounts[acct]:
            sim.fan_says(acct, fan, "what is included?")
        await settle(sim, {acct: 40}, limit_s=2500)
        return ()
    return accounts, {"content_manifest": str(manifest)}, drive


def sc_backlog(seed):
    """150 fans waiting before the service has served anyone, then new traffic keeps arriving (no starvation)."""
    acct = "111000"
    accounts = {acct: fans(5000, 300)}
    rng = random.Random(seed)

    async def drive(sim):
        await asyncio.sleep(3)
        for fan in accounts[acct][:150]:
            sim.fan_says(acct, fan, "hey")
        # fresh arrivals while the backlog drains: one every 8 s for 20 min, half from fans already waiting
        for i in range(150):
            await asyncio.sleep(8)
            pool = accounts[acct][:150] if i % 2 else accounts[acct][150:]
            sim.fan_says(acct, rng.choice(pool), "again")
        await settle(sim, {acct: 1}, limit_s=2500)
        await asyncio.sleep(2000)
        return ()
    return accounts, {}, drive


def sc_429(seed):
    acct = "111000"
    accounts = {acct: fans(2000, 40)}

    async def drive(sim):
        await asyncio.sleep(3)
        p = sim.providers[acct]
        for fan in accounts[acct]:
            sim.fan_says(acct, fan, "hey")
        await asyncio.sleep(25)
        p.faults.extend([("429", 30), ("429", 60), ("429", None)])      # three refusals mid-burst
        await settle(sim, {acct: 40}, limit_s=3000)
        return ()
    return accounts, {}, drive


def sc_auth_failure(seed):
    acct = "111000"
    accounts = {acct: fans(2000, 20)}

    async def drive(sim):
        await asyncio.sleep(3)
        sim.providers[acct].faults.append(("401",))
        for fan in accounts[acct]:
            sim.fan_says(acct, fan, "hey")
        await asyncio.sleep(120)
        sim.change_credentials(acct)                        # the operator reconnects: the connection revision moves
        await settle(sim, {acct: 20}, limit_s=2000)
        return ()
    return accounts, {}, drive


def sc_restart(seed):
    acct = "111000"
    accounts = {acct: fans(3700, 40)}

    async def drive(sim):
        await asyncio.sleep(3)
        for fan in accounts[acct]:
            sim.fan_says(acct, fan, "hey")
        await asyncio.sleep(50)
        await sim.kill_service(acct)                        # a crash: no release, no hand-back
        await asyncio.sleep(5)
        sim.start_service(acct)
        await settle(sim, {acct: 40}, limit_s=3000)
        return ()
    return accounts, {}, drive


def sc_competing_manual(seed):
    """A person takes over 8 of 40 fans mid-burst (answers some, hands one back); the AI must serve the rest."""
    acct = "111000"
    accounts = {acct: fans(4000, 40)}
    taken = accounts[acct][-8:]
    keep_taken = set()

    async def drive(sim):
        await asyncio.sleep(3)
        for fan in accounts[acct]:
            sim.fan_says(acct, fan, "hey")
        await asyncio.sleep(15)
        for i, fan in enumerate(taken):
            sim.takeover(acct, fan)
            if i % 2 == 0:
                sim.human_says(acct, fan, "it's me")
        await asyncio.sleep(200)
        sim.handback(acct, taken[0])
        sim.fan_says(acct, taken[0], "back to the bot?")    # a fresh input after hand-back must be answered
        for fan in taken[1:]:
            keep_taken.add((acct, fan))
        await settle(sim, {acct: 33}, limit_s=3000)
        return keep_taken
    return accounts, {"takeovers_open": True}, drive


SCENARIOS = {
    "one_creator_100": sc_one_creator_100, "several_creators": sc_several_creators, "roster50": sc_roster50,
    "bursts": sc_bursts, "multi_bubble": sc_multi_bubble, "media": sc_media, "backlog": sc_backlog,
    "provider_429": sc_429, "auth_failure": sc_auth_failure, "worker_restart": sc_restart,
    "competing_manual": sc_competing_manual,
}


def run_one(name, mode, seed=7, model_latency=None):
    accounts, overrides, drive = SCENARIOS[name](seed)
    kw = dict(MODES[mode])
    kw.update({k: v for k, v in overrides.items() if k not in ("takeovers_open",)})
    kw.setdefault("bubbles", ONE)
    kw.setdefault("model_latency", model_latency or (lambda rng: max(0.5, rng.lognormvariate(0.7, 0.5))))
    t0 = _wall.time()
    out = {}

    async def scenario(sim):
        sim.fast_idle = False
        sim.takeovers_open = bool(overrides.get("takeovers_open"))
        stop = asyncio.Event()
        sampler = asyncio.ensure_future(sim.backlog_sampler(30.0, stop))
        excluded = await drive(sim)
        stop.set()
        sampler.cancel()
        out.update(summarise(sim, name, mode, t0, excluded))
        return out

    root = tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None)
    H.run_sim(scenario, root=root, accounts=accounts, rng=random.Random(seed + 1), poll_s=2.0, **kw)
    return out


def table(rows):
    head = ("scenario", "mode", "fans", "elig", "p50", "p95", "p99", "max", "<=180s", "stranded", "dup", "retry", "wall")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        u = r["user_visible_s"]
        share = "n/a" if r["share_within_180s"] is None else f"{100 * r['share_within_180s']:.1f}%"
        retry = sum(r["retries"].values())
        lines.append("| " + " | ".join(str(x) for x in (
            r["scenario"], r["mode"], r["fans"], r["eligible_turns"], u["p50"], u["p95"], u["p99"], u["max"], share,
            r["stranded_due_turns"], r["duplicate_sends"] + r["cross_account_sends"], retry, r["wall_s"])) + " |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--mode", default="both", choices=["gap", "concurrent", "concurrent_fast", "both", "all"])
    ap.add_argument("--json")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(SCENARIOS))
        return
    names = args.scenarios or list(SCENARIOS)
    modes = {"both": ["gap", "concurrent"], "all": ["gap", "concurrent", "concurrent_fast"]}.get(args.mode, [args.mode])
    rows = []
    for name in names:
        for mode in modes:
            r = run_one(name, mode, args.seed)
            rows.append(r)
            print(json.dumps({k: r[k] for k in ("scenario", "mode", "eligible_turns", "share_within_180s", "user_visible_s",
                                               "stranded_due_turns", "duplicate_sends", "wall_s")}), flush=True)
    print(table(rows))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str))


if __name__ == "__main__":
    main()
