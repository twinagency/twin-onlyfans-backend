"""Concurrent-delivery acceptance on the virtual-time engine harness.

Real Service, Ledger, SendGate, runner send boundary and intake; fake provider and
model. No network, credential or live account. The provider records actual overlap
of POST calls, so a shorter serial gap cannot be mistaken for concurrency.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_harness as H  # noqa: E402


CHECKS = 0
ONE = lambda rng: 1  # noqa: E731
CONCURRENT = dict(delivery_mode="concurrent", provider_rate=10.0, provider_burst=10,
                  provider_max_concurrent=8, conversation_gap=0, provider_latency=0.12,
                  min_interval=0, throttle=0, concurrency=16,
                  tuning_extra={"send_concurrency": 8})


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def root():
    return tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None)


async def drain(sim, account, expected, *, seconds=300):
    waited = 0
    while waited < seconds and sim.sends[account] < expected:
        await asyncio.sleep(5)
        waited += 5
    await asyncio.sleep(10)


async def hundred_fans(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    await asyncio.sleep(3)
    for fan in fans:
        sim.fan_says(acct, fan, "hey")
    await drain(sim, acct, len(fans))
    p = sim.providers[acct]
    check(sim.sends[acct] == len(fans), f"100 fans answered exactly once ({sim.sends[acct]})")
    check(sim.duplicates == 0 and sim.cross_account == 0, f"no duplicate/cross-fan reply {sim.failures[:3]}")
    check(p.max_active >= 2, f"provider requests really overlapped (max {p.max_active})")
    check(p.max_active <= 8, f"provider concurrency stayed bounded (max {p.max_active})")
    stats = H.stats(sim.latencies[acct])
    check(stats["max"] < 180, f"every reply inside 180 s (max {stats['max']})")
    check(stats["p95"] < 90, f"p95 inside the short window ({stats['p95']})")
    # The old account gap would place fan 100 around 100 * 20 s; position no longer multiplies the delay.
    check(stats["max"] < len(fans) * 5, f"not staggered by fan position ({stats['max']})")
    q = sim.kinds("engine-queue", acct)
    check(q and max(e["in_flight"] for e in q) >= 2, "more than one prepared reply was live")
    print("  100 fans latency s", stats, "provider overlap", p.max_active, flush=True)
    return stats


async def faults_and_takeover(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    taken = fans[7]
    await asyncio.sleep(3)
    sim.fail_models = 2
    sim.providers[acct].faults.append(("429", 20))
    for fan in fans:
        sim.fan_says(acct, fan, "hey")
    await asyncio.sleep(8)
    sim.takeover(acct, taken)
    await drain(sim, acct, len(fans) - 1, seconds=300)
    p = sim.providers[acct]
    check(sim.kinds("provider-throttled", acct), "429 was recorded")
    cooldown = sim.sql("SELECT provider_cooldown_until,next_dispatch_at FROM of_delivery_policy WHERE account_id=?", (acct,))
    check(cooldown and cooldown[0][0] is not None, f"durable account cooldown exists {cooldown}")
    check(sim.duplicates == 0 and sim.cross_account == 0, f"no duplicate after faults {sim.failures[:3]}")
    check(sim.takeover_violations == 0, "no AI POST after takeover")
    check(taken not in {f for t, f, _b in p.post_log if t > sim.takeovers[(acct, taken)][-1][0]},
          "the taken-over fan was not sent to after takeover")
    check(len({f for _t, f, _b in p.post_log}) >= len(fans) - 1, "all other fans were answered")
    check(p.max_active >= 2, f"other fans kept overlapping through the failures (max {p.max_active})")


async def duplicate_events(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    await asyncio.sleep(3)
    messages = []
    for fan in fans:
        messages.append((fan, sim.fan_says(acct, fan, "same event")))
    for fan, msg in messages:
        sim.duplicate_event(acct, msg)
        sim.duplicate_event(acct, msg)
    await drain(sim, acct, len(fans))
    check(sim.sends[acct] == len(fans), f"duplicate events do not multiply replies ({sim.sends[acct]})")
    check(sim.duplicates == 0, "no duplicate outbound text")


async def multiple_bubbles(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    await asyncio.sleep(3)
    for fan in fans:
        sim.fan_says(acct, fan, "tell me two things")
    await drain(sim, acct, len(fans) * 2)
    p = sim.providers[acct]
    check(sim.sends[acct] == len(fans) * 2,
          f"both bubbles were sent for every fan ({sim.sends[acct]})")
    check(sim.duplicates == 0 and sim.cross_account == 0,
          "multi-bubble sends stayed unique and scoped")
    for fan in fans:
        sequence = []
        for _t, posted_fan, body in p.post_log:
            if posted_fan == fan:
                try:
                    plain = body.replace("<p>", "").replace("</p>", "")
                    sequence.append(int(plain.rsplit(":", 1)[1]))
                except (IndexError, ValueError):
                    pass
        check(sequence == [0, 1], f"fan {fan} kept bubble order {sequence}")
    slots = sim.sql("SELECT count(*) FROM engine_dispatch_slot WHERE account_id=? AND state='held'", (acct,))[0][0]
    check(slots == 0, f"all multi-bubble dispatch slots settled ({slots})")


async def uncertain_delivery(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    await asyncio.sleep(3)
    sim.providers[acct].faults.append(("timeout_delivered",))
    for fan in fans:
        sim.fan_says(acct, fan, "hey")
    await asyncio.sleep(90)
    p = sim.providers[acct]
    check(sim.duplicates == 0 and sim.cross_account == 0, "an ambiguous POST was never retried")
    check(sim.sends[acct] >= len(fans) - 1, f"other fans continued ({sim.sends[acct]})")
    check(p.max_active >= 2, f"provider overlap survived one uncertain fan (max {p.max_active})")
    unresolved = sim.sql("SELECT count(*) FROM twin_bridge_send_attempts WHERE account_id=? "
                         "AND state='delivery_unknown'", (acct,))[0][0]
    check(unresolved <= 1, f"at most the ambiguous conversation is unresolved ({unresolved})")


async def restart_during_burst(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    await asyncio.sleep(3)
    for fan in fans:
        sim.fan_says(acct, fan, "hey")
    await asyncio.sleep(8)                     # some replies are prepared / on the wire
    await sim.kill_service(acct)               # hard crash, no shutdown hand-back
    await asyncio.sleep(1)
    sim.start_service(acct)
    await drain(sim, acct, len(fans) - 2, seconds=360)
    check(sim.duplicates == 0 and sim.cross_account == 0, "a restart never duplicates an accepted POST")
    check(sim.sends[acct] >= len(fans) - 2, f"the successor answered the remaining fans ({sim.sends[acct]})")
    unresolved = sim.sql("SELECT count(*) FROM twin_bridge_send_attempts WHERE account_id=? "
                         "AND state IN ('dispatching','delivery_unknown')", (acct,))[0][0]
    check(unresolved <= 2, f"only a genuine in-flight ambiguity remains held ({unresolved})")
    check(sim.providers[acct].max_active >= 2, "the successor still overlapped provider requests")


async def multiple_creators(sim):
    for acct, fans in sim.accounts.items():
        await asyncio.sleep(1)
        for fan in fans:
            sim.fan_says(acct, fan, "hey")
    for acct, fans in sim.accounts.items():
        await drain(sim, acct, len(fans), seconds=240)
        check(sim.sends[acct] == len(fans), f"{acct}: all {len(fans)} answered ({sim.sends[acct]})")
        check(sim.providers[acct].max_active >= 2,
              f"{acct}: provider overlap {sim.providers[acct].max_active}")
        check(sim.duplicates == 0 and sim.cross_account == 0, f"{acct}: no duplicate/cross-account")


async def paid_content(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    sim.paid_fans.update(fans[:4])
    await asyncio.sleep(3)
    for fan in fans:
        sim.fan_says(acct, fan, "what is included?")
    await drain(sim, acct, len(fans))
    p = sim.providers[acct]
    check(sim.sends[acct] == len(fans), f"paid-content burst answered {sim.sends[acct]} fans")
    check(p.max_active >= 2, f"paid-content provider calls overlapped (max {p.max_active})")
    check(sim.duplicates == 0 and sim.cross_account == 0, "paid-content sends stayed unique and scoped")
    cards = sim.sql(
        "SELECT fan_id,media_ids,media_count,price_cents FROM messages "
        "WHERE account_id=? AND direction='out' AND automation_kind='ai_chatter' AND price_cents=300",
        (acct,))
    check(len(cards) == 4 and {row[0] for row in cards} == set(sim.paid_fans),
          f"exactly four reviewed paid cards, once per selected fan ({cards})")
    check(all(row[1] == "[7101, 7102]" and row[2] == 2 for row in cards),
          f"paid cards preserved the reviewed media set ({cards})")
    check(all(row[3] == 300 for row in cards), "paid cards preserved the reviewed price")


async def auth_failure(sim):
    acct = "111000"
    fans = sim.accounts[acct]
    await asyncio.sleep(3)
    sim.providers[acct].faults.append(("401",))
    for fan in fans:
        sim.fan_says(acct, fan, "hey")
    await asyncio.sleep(20)
    check(sim.kinds("account-blocked", acct), "a 401 blocked the account, not only one fan")
    check(sim.duplicates == 0 and sim.cross_account == 0,
          "auth failure did not duplicate or cross conversations")
    retried = sim.sql(
        "SELECT i.state FROM native_pilot_inputs i JOIN native_pilots p ON p.pilot_id=i.pilot_id "
        "WHERE p.account_id=? AND i.state LIKE 'requeued:%'", (acct,))
    check(retried, f"the 401 fan's input remained pending for reconciliation ({retried})")


def main():
    hundred = H.run_sim(hundred_fans, root=root(), accounts={"111000": [1000 + i for i in range(100)]},
                        model_latency=lambda rng: 2.0, bubbles=ONE, **CONCURRENT)
    H.run_sim(faults_and_takeover, root=root(), accounts={"111000": [2000 + i for i in range(40)]},
              model_latency=lambda rng: 1.5, bubbles=ONE, **CONCURRENT)
    print("  ok   faults/429/takeover", flush=True)
    H.run_sim(duplicate_events, root=root(), accounts={"111000": [3000 + i for i in range(20)]},
              model_latency=lambda rng: 1.0, bubbles=ONE, **CONCURRENT)
    print("  ok   duplicate events", flush=True)
    H.run_sim(multiple_bubbles, root=root(), accounts={"111000": [3200 + i for i in range(12)]},
              model_latency=lambda rng: 1.0, bubbles=lambda rng: 2, **CONCURRENT)
    print("  ok   multiple bubbles", flush=True)
    H.run_sim(uncertain_delivery, root=root(), accounts={"111000": [3500 + i for i in range(20)]},
              model_latency=lambda rng: 1.0, bubbles=ONE, **CONCURRENT)
    print("  ok   uncertain delivery", flush=True)
    H.run_sim(restart_during_burst, root=root(), accounts={"111000": [3700 + i for i in range(24)]},
              model_latency=lambda rng: 1.0, bubbles=ONE, **CONCURRENT)
    print("  ok   restart during burst", flush=True)
    H.run_sim(multiple_creators, root=root(),
              accounts={"111000": [4000 + i for i in range(30)],
                        "222000": [5000 + i for i in range(30)],
                        "333000": [6000 + i for i in range(30)]},
              model_latency=lambda rng: 1.0, bubbles=ONE, **CONCURRENT)
    print("  ok   multiple creators", flush=True)
    paid_root = Path(root())
    manifest = paid_root / "paid.json"
    manifest.write_text(json.dumps({
        "account_id": "111000", "fan_id": "*",
        "items": [{"catalog_id": 1, "kind": "video", "price_cents": 300,
                   "media": [{"id": 7101, "type": "photo"}, {"id": 7102, "type": "video"}],
                   "preview_ids": [7101]}],
    }), encoding="utf-8")
    H.run_sim(paid_content, root=paid_root,
              accounts={"111000": [7000 + i for i in range(8)]},
              content_manifest=str(manifest), model_latency=lambda rng: 1.0, bubbles=ONE,
              **{**CONCURRENT, "provider_max_concurrent": 4, "tuning_extra": {"send_concurrency": 4}})
    print("  ok   paid content", flush=True)
    H.run_sim(auth_failure, root=root(),
              accounts={"111000": [8000 + i for i in range(6)]},
              model_latency=lambda rng: 1.0, bubbles=ONE, **CONCURRENT)
    print("  ok   auth failure", flush=True)
    print(f"concurrent delivery: all cases passed ({CHECKS} checks); 100-fan latency {hundred}")


if __name__ == "__main__":
    main()
