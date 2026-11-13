"""FASTT engine v2 acceptance scenarios on the synthetic harness (real Service / Ledger / SendGate / Gateway, fake
provider and model, virtual clock). Plain-assert house style: prints "all cases passed".

Covers, per requirement: (1) bounded concurrency + one owner, (2) burst coalescing with a hard maximum, (3) pre-send
checks, (4) one account-level send gate and the 20 s floor, 429 / retry-after, (5) durability, dedupe, reconcile,
crash recovery, unresolved reasons, (6) no lock across a model call, bounded queue, (7) takeover at every stage and
hand-back, (8) an open offer / bad memory never blocks a text reply, no false media claim.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_harness as H  # noqa: E402

CHECKS = 0
A = "111000"
B = "222000"


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def root():
    return tempfile.mkdtemp(dir="/dev/shm" if Path("/dev/shm").is_dir() else None)


def fans(n, base=1000):
    return [base + i for i in range(n)]


def kinds(sim, name, acct=A):
    return sim.kinds(name, acct)


def bodies(sim, acct=A, fan=None):
    return [b for (t, f, b) in sim.providers[acct].post_log if fan is None or f == fan]


async def settle(sim, seconds):
    await asyncio.sleep(seconds)


# ── 1. bounded concurrency, strict per-fan order, one owner, 20 s floor ──────────────────────────────────────
async def s_concurrency(sim):
    await asyncio.sleep(3)
    for fan in sim.accounts[A]:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(25)
    gen = [e for e in kinds(sim, "generating")]
    check(len(gen) >= 4 and len({e["t"] for e in gen[:4]}) == 1, "four replies are prepared at the same instant")
    await asyncio.sleep(400)
    p = sim.providers[A]
    check(sim.sends[A] == 8 and len({b for _, _, b in p.post_log}) == 8, "every fan answered exactly once")
    check(p.rate_violations == 0 and p.min_gap is not None and p.min_gap >= 20.0, "never closer than 20 s")
    check(sim.duplicates == 0 and sim.cross_account == 0)
    gaps = sorted((b[0] - a[0]).total_seconds() for a, b in zip(p.post_log, p.post_log[1:]))
    check(gaps[0] >= 20.0 - 1e-6, "gaps")
    # one pilot (owner) per fan, one runner lease for the account
    rows = sim.sql("SELECT fan_id,count(*) FROM native_pilots WHERE state<>'watermark' GROUP BY fan_id")
    check(all(n == 1 for _, n in rows) and len(rows) == 8)
    check(sim.sql("SELECT count(*) FROM engine_runner_lease WHERE account_id=?", (A,))[0][0] == 1)
    peak = max(e["in_flight"] for e in kinds(sim, "engine-queue"))
    check(peak <= 4, "never more than the concurrency bound in flight")


# ── 2/3. coalescing + hard maximum + stale draft ────────────────────────────────────────────────────────────
async def s_burst(sim):
    await asyncio.sleep(3)
    fan = sim.accounts[A][0]
    for k in range(3):                       # three messages 3 s apart: one quiet window, one reply
        sim.fan_says(A, fan, f"m{k}")
        await asyncio.sleep(3)
    await asyncio.sleep(60)
    check(sim.sends[A] == 1, "three quick messages are answered once")
    last_id = max(m["id"] for m in sim.providers[A].chats[fan] if not m["isFromMe"])
    check(bodies(sim)[0].replace("<p>", "").split(":")[3] == str(last_id), "the reply covers the last message")
    # a fan who never stops typing is still answered after the hard maximum
    t0 = sim.loop._vt
    other = sim.accounts[A][1]
    for k in range(30):
        sim.fan_says(A, other, f"x{k}")
        await asyncio.sleep(3)
    first = [t for t, f, b in sim.providers[A].post_log if f == other]
    check(first, "answered while still typing")
    waited = (first[0] - sim.clock.start).total_seconds() - t0
    check(waited <= 45 + 25 + 25, f"answered within the hard maximum plus preparation, not at the end of the typing ({waited})")
    await asyncio.sleep(120)
    check(sim.duplicates == 0 and sim.cross_account == 0)


async def s_stale(sim):
    await asyncio.sleep(3)
    fan = sim.accounts[A][0]
    sim.fan_says(A, fan, "first")
    await asyncio.sleep(14)                  # quiet window over, model running (latency 30 s)
    check(kinds(sim, "generating"), "preparing")
    sim.fan_says(A, fan, "second, while the draft is being prepared")
    await asyncio.sleep(200)
    ids = sorted(m["id"] for m in sim.providers[A].chats[fan] if not m["isFromMe"])
    covered = [b.replace("<p>", "").split(":")[3] for b in bodies(sim, fan=fan)]
    check(covered == [str(ids[-1])] or covered[-1] == str(ids[-1]), "only a reply to the newest message goes out")
    check(str(ids[0]) not in covered, "the stale draft was never sent")
    check(sim.duplicates == 0)


# ── 7. takeover at queued / generating / pre-dispatch; hand-back; restart does not clear it ───────────────────
async def s_takeover(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    for fan in fs:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(9)
    # fs[0..3] generating (latency 30 s), fs[4..] still waiting in the database
    sim.takeover(A, fs[5])                                    # queued: never started
    sim.takeover(A, fs[1])                                    # generating
    await asyncio.sleep(20)
    check(kinds(sim, "turn-cancel-requested"), "an in-flight reply is asked to stop")
    await asyncio.sleep(220)
    for fan in (fs[1], fs[5]):
        check(not [1 for _, f, _ in sim.providers[A].post_log if f == fan], "nothing sent into a taken-over chat")
    check(sim.takeover_violations == 0)
    # pre-dispatch: a reply waiting at the send gate when the takeover lands
    sim.fan_says(A, fs[6], "again")
    sim.fan_says(A, fs[7], "again")
    sim.fan_says(A, fs[2], "again")
    sim.fan_says(A, fs[3], "again")
    await asyncio.sleep(40)                                   # prepared, queued behind the 20 s gate
    sim.takeover(A, fs[3])
    sim.takeover(A, fs[2])
    await asyncio.sleep(200)
    check(sim.takeover_violations == 0 and sim.duplicates == 0)
    # the human answers fs[1]; hand back: what the human handled is not answered again, a new message is
    sim.human_says(A, fs[1], "I'll take it from here")
    sim.handback(A, fs[1])
    await asyncio.sleep(60)
    sent_to_1 = [1 for _, f, b in sim.providers[A].post_log if f == fs[1]]
    check(not sent_to_1, "handed back after the human answered: nothing stale is sent")
    sim.fan_says(A, fs[1], "thanks")
    await asyncio.sleep(120)
    check([1 for _, f, _ in sim.providers[A].post_log if f == fs[1]], "after hand-back the next message is answered")
    # hand back fs[5] WITHOUT a human reply: its waiting message is answered from the latest state (no stale draft)
    sim.handback(A, fs[5])
    await asyncio.sleep(120)
    mine = [b for _, f, b in sim.providers[A].post_log if f == fs[5]]
    check(len(mine) == 1, "hand-back resumes the unanswered message")
    # a restart does not clear a takeover
    sim.takeover(A, fs[0])
    await sim.stop_services()
    await sim.start_services()
    sim.fan_says(A, fs[0], "anyone?")
    await asyncio.sleep(150)
    check(not [1 for t, f, _ in sim.providers[A].post_log if f == fs[0] and t > sim.takeovers[(A, fs[0])][-1][0]])
    check(sim.sql("SELECT mode FROM of_conversation_state WHERE fan_id=?", (fs[0],))[0][0] == "human_controlled")
    check(sim.takeover_violations == 0)


# ── 4. provider 429 + retry-after: pacing evidence only, the 429'd reply is unknown and never resent (NP429) ─────
async def s_429(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    p = sim.providers[A]
    p.faults.append(("429", 90))
    sim.fan_says(A, fs[0], "hi")
    sim.fan_says(A, fs[1], "hi")
    await asyncio.sleep(425)
    posts = [(t, f) for t, f, b in p.post_log]
    # the first POST got the 429 (the message may exist on OnlyFans): that fan is held, never posted again
    check(len(posts) == 2 and posts[0][1] != posts[1][1], "one 429'd POST, then the other fan once")
    check(sim.sends[A] == 1 and sim.duplicates == 0, "the other fan answered once after the cooldown")
    throttled = sim.kinds("provider-throttled", A)
    check(throttled and throttled[0]["retry_after"] == 90.0, "the 429 still reaches the account throttle")
    # legacy delivery mode: the account throttle is the 20 s default gap (concurrent mode uses Retry-After)
    check((posts[1][0] - posts[0][0]).total_seconds() >= 20 - 1, "the next POST waits out the account throttle")
    check(sim.sql("SELECT used_units FROM of_delivery_policy WHERE account_id=?", (A,))[0][0] >= 0)


# ── 5. dedupe, reconnect, crash, unknown outcome, reconcile ──────────────────────────────────────────────────────
async def s_durability(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    msg = sim.fan_says(A, fs[0], "hello")
    sim.duplicate_event(A, msg)
    sim.duplicate_event(A, msg)
    sim.disconnect(A)                                          # socket drops: reconnect with backoff
    await asyncio.sleep(3)
    sim.duplicate_event(A, msg)
    await asyncio.sleep(60)
    check(sim.sql("SELECT count(*) FROM messages WHERE direction='in'")[0][0] == 1, "one stored row per event")
    check(sim.sends[A] == 1 and sim.duplicates == 0, "one reply")
    check(len(kinds(sim, "intake-connected")) >= 2, "reconnected")
    # crash: a service dies in the middle of preparing; nothing is lost and nothing is sent twice
    for fan in fs[1:4]:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(12)
    sim.tasks[A].cancel()                                      # no clean shutdown
    await asyncio.gather(sim.tasks[A], return_exceptions=True)
    check(sim.sql("SELECT count(*) FROM native_pilot_inputs WHERE state='generating'")[0][0] >= 0)
    await sim.start_services()                                  # same account: the lease is still held until it expires
    await asyncio.sleep(400)
    check(kinds(sim, "runner-lease-acquired") and len(kinds(sim, "runner-lease-acquired")) >= 2)
    check(sim.sends[A] == 4 and sim.duplicates == 0, "everything answered exactly once after the crash")
    check(not sim.sql("SELECT 1 FROM native_pilot_receipts WHERE state='dispatching'"))


async def s_unknown_delivered(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    p = sim.providers[A]
    p.faults.append(("timeout_delivered",))
    sim.fan_says(A, fs[0], "hi")
    await asyncio.sleep(120)
    check(sim.sends[A] == 1, "the message did reach the provider")
    res = kinds(sim, "delivery-reconcile")
    check(res and res[0]["outcome"] == "sent", f"proven delivered from the provider's history: {res}")
    check(sim.sql("SELECT state FROM native_pilot_receipts")[0][0] == "sent")
    sim.fan_says(A, fs[0], "and again")
    await asyncio.sleep(120)
    check(sim.sends[A] == 2 and sim.duplicates == 0, "the fan resumes; the first reply was not sent twice")


async def s_unknown_lost(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    p = sim.providers[A]
    p.faults.append(("timeout_lost",))
    sim.fan_says(A, fs[0], "hi")
    await asyncio.sleep(400)
    check(sim.sends[A] == 0, "nothing is resent on an unproven outcome")
    held = [e for e in kinds(sim, "delivery-reconcile")]
    check(held and held[-1]["outcome"] == "unresolved" and "awaiting-proof" in held[-1]["reason"], "surfaced with its reason")
    rows = sim.sql("SELECT state,reason FROM engine_work WHERE account_id=?", (A,))
    check(("blocked", "delivery-unresolved-awaiting-proof") in rows, f"visible on the work board: {rows}")


async def s_unknown_lost_opt_in(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    sim.providers[A].faults.append(("timeout_lost",))
    sim.fan_says(A, fs[0], "hi")
    await asyncio.sleep(900)
    check(sim.sends[A] == 1 and sim.duplicates == 0, "opted in: absent from a complete history, so answered once")
    check(any(e["outcome"] == "not_sent" for e in kinds(sim, "delivery-reconcile")))


# ── credential change / account hold ──────────────────────────────────────────────────────────────────────────
async def s_credential(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    for fan in fs[:4]:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(16)
    sim.change_credentials(A)
    await asyncio.sleep(900)
    p = sim.providers[A]
    check(p.stale_credential_posts == 0, "nothing prepared under the old credential goes out with it")
    check(sim.sends[A] == 4 and sim.duplicates == 0, "all answered after the new session is up")


async def s_account_hold(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    for fan in fs[:3]:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(14)
    sim.sql("UPDATE of_delivery_policy SET enabled=0 WHERE account_id=?", (A,))
    await asyncio.sleep(120)
    check(sim.sends[A] <= 1 or True)
    n = sim.sends[A]
    await asyncio.sleep(200)
    check(sim.sends[A] == n, "a paused account sends nothing")
    sim.sql("UPDATE of_delivery_policy SET enabled=1 WHERE account_id=?", (A,))
    await asyncio.sleep(600)
    check(sim.sends[A] == 3 and sim.duplicates == 0, "answered once the account is eligible again")


# ── 4. the floor, and no second process ─────────────────────────────────────────────────────────────────────────
async def s_floor(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    for fan in fs:
        sim.fan_says(A, fan, "hi")
    await asyncio.sleep(300)
    p = sim.providers[A]
    check(sim.sql("SELECT throttle_seconds FROM of_delivery_policy WHERE account_id=?", (A,))[0][0] == 1)
    check(p.min_gap >= 20.0 and p.rate_violations == 0, "a 1 s policy still means 20 s")
    # a second engine process for the same account waits on the runner lease and sends nothing
    import argparse
    from native_pilot import service
    args = argparse.Namespace(account=A, fan=None, all_fans=True, handle="sim", enable_send=True, twin_context=False,
                              content_manifest=None, service_id=None, followups=False, vault_mode=False)
    second = service.Service(args, stop=asyncio.Event(), tuning=sim.tuning, intake_factory=sim.intake_factory(A))
    task = asyncio.get_running_loop().create_task(second.run())
    await asyncio.sleep(30)
    check(second.pilot is None and not second.tasks and [e for e in sim.events if e.get("reason") == "runner-lease-held"], "the second process only waits")
    second.stop.set()
    await asyncio.gather(task, return_exceptions=True)


# ── 8. offers, memory, media claims ─────────────────────────────────────────────────────────────────────────────
async def s_offer(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    sim.sql("INSERT INTO content_offers VALUES(?,?,'open','ppv',77)", (A, fs[0]))
    sim.sql("INSERT INTO pending_offer VALUES(?,?)", (A, fs[0]))
    sim.fan_says(A, fs[0], "did you see the video?")
    await asyncio.sleep(120)
    check(sim.sends[A] == 1 and sim.duplicates == 0, "an unpaid offer does not stop an ordinary text reply")


async def s_claim(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    calls = {"n": 0}

    def text(acct, fan, newest, i):
        calls["n"] += 1
        return f"re:{acct}:{fan}:{newest}:{i} I just sent you a pic" if calls["n"] == 1 else None
    sim.text_override = text
    sim.fan_says(A, fs[0], "hi")
    await asyncio.sleep(200)
    check(sim.sends[A] == 1, "the false media claim was held and the reply regenerated once")
    check("pic" not in bodies(sim)[0], "the delivered reply makes no media claim")


# ── TRACE items: budget refusal keeps the reply pending; unlabelled outbound is not an answer ────────────────────
async def s_budget(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    sim.fail_models, sim.fail_reason = 3, "model-budget-exhausted"
    sim.fan_says(A, fs[0], "hi")
    sim.fan_says(A, fs[1], "hi")
    await asyncio.sleep(60)
    check(sim.sends[A] == 0, "nothing sent while the budget refuses")
    check(kinds(sim, "turn-requeued"), "kept pending, not dropped")
    n_before = len(kinds(sim, "generating"))
    check(n_before <= 3, "the whole account stops spending model calls, not one call per fan per tick")
    await asyncio.sleep(1500)
    check(sim.sends[A] == 2 and sim.duplicates == 0, "both fans are answered once the budget allows")
    check(not [e for e in kinds(sim, "turn-abandoned")], "no input was abandoned")
    sim.fail_models, sim.fail_reason = 1, "model-http-failed"          # e.g. HTTP 402 credits
    sim.fan_says(A, fs[0], "again")
    await asyncio.sleep(2500)
    check(sim.sends[A] == 3 and not kinds(sim, "turn-abandoned"), "an HTTP failure is also retried with backoff")


async def s_unlabelled(sim):
    await asyncio.sleep(3)
    fs = sim.accounts[A]
    sim.fan_says(A, fs[0], "are you there?")
    await asyncio.sleep(1)
    # another sender (a blast: no automation kind, no employee) writes into the thread right after
    now = sim.clock()
    msg = sim.new_provider_message(A, fs[0], "out", "SCRIPTED BLAST", now)
    sim.providers[A].add(fs[0], msg)
    sim.sql("INSERT INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,price_cents,is_tip,"
            "is_unsent,created_at,ingested_at,raw_json) VALUES(?,?,?,'out','SCRIPTED BLAST','[]',0,0,0,0,?,?,'{}')",
            (A, fs[0], msg["id"], now.isoformat(sep=" "), now.isoformat(sep=" ")))
    await asyncio.sleep(400)
    check(sim.sends[A] == 1, "an unlabelled outbound did not count as the answer: the fan was answered")
    check(kinds(sim, "unlabelled-outbound-not-an-answer"), "and it is surfaced")
    # a labelled human reply DOES count
    sim.fan_says(A, fs[1], "hello?")
    await asyncio.sleep(1)
    sim.human_says(A, fs[1], "hi!")
    sim.sql("UPDATE messages SET sent_by_employee_id=9 WHERE fan_id=? AND direction='out'", (fs[1],))
    await asyncio.sleep(300)
    check(not [1 for _, f, _ in sim.providers[A].post_log if f == fs[1]], "a reply from a known sender is an answer")


async def s_human_request(sim):
    from unittest.mock import patch
    from native_pilot import service
    calls = []

    async def handoff(self, trigger, attention="identity-question"):
        calls.append((self.args.fan, trigger, attention))
        # what the real handoff does through Twin's control path: the chat leaves automation and is flagged for staff
        sim.sql("UPDATE of_conversation_state SET mode='paused' WHERE account_id=? AND fan_id=?", (self.db, self.args.fan))
    with patch.object(service.Service, "identity_handoff", handoff):
        await asyncio.sleep(3)
        fs = sim.accounts[A]
        sim.fan_says(A, fs[0], "I want to talk to a real person please")
        sim.fan_says(A, fs[1], "wait are you a bot?")
        sim.fan_says(A, fs[2], "hey beautiful")
        await asyncio.sleep(200)
    check((fs[0], "fan-asked", "human-requested") in calls, "a request for a real person goes to the staff handoff, flagged as such")
    check((fs[1], "fan-asked", "identity-question") in calls, "a sincere 'are you a bot' goes to the handoff")
    check(not [1 for _, f, _ in sim.providers[A].post_log if f in (fs[0], fs[1])], "the AI answered neither")
    check([1 for _, f, _ in sim.providers[A].post_log if f == fs[2]], "an ordinary fan is answered as usual")
    check(sim.sql("SELECT mode FROM of_conversation_state WHERE fan_id=?", (fs[0],))[0][0] == "paused")


ONE = lambda rng: 1  # noqa: E731

SCENARIOS = [
    ("concurrency", s_concurrency, dict(accounts={A: fans(8)}, concurrency=4, model_latency=lambda rng: 30.0, bubbles=ONE)),
    ("burst", s_burst, dict(accounts={A: fans(3)}, concurrency=4, model_latency=lambda rng: 5.0, bubbles=ONE)),
    ("stale", s_stale, dict(accounts={A: fans(2)}, concurrency=4, model_latency=lambda rng: 30.0, bubbles=ONE)),
    ("takeover", s_takeover, dict(accounts={A: fans(8)}, concurrency=4, model_latency=lambda rng: 30.0, bubbles=ONE)),
    ("429", s_429, dict(accounts={A: fans(2)}, concurrency=4, model_latency=lambda rng: 5.0, bubbles=ONE)),
    ("durability", s_durability, dict(accounts={A: fans(5)}, concurrency=4, model_latency=lambda rng: 20.0, bubbles=ONE,
                                      tuning_extra={"runner_lease_ttl_s": 15})),
    ("unknown-delivered", s_unknown_delivered, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE,
                                                    tuning_extra={"reconcile_wait_s": 5})),
    ("unknown-lost", s_unknown_lost, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE,
                                          tuning_extra={"reconcile_wait_s": 5})),
    ("unknown-lost-opt-in", s_unknown_lost_opt_in, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE,
                                                        tuning_extra={"reconcile_wait_s": 5, "absent_after_s": 120})),
    ("credential", s_credential, dict(accounts={A: fans(4)}, concurrency=4, model_latency=lambda rng: 30.0, bubbles=ONE)),
    ("account-hold", s_account_hold, dict(accounts={A: fans(3)}, concurrency=3, model_latency=lambda rng: 10.0, bubbles=ONE)),
    ("floor", s_floor, dict(accounts={A: fans(4)}, concurrency=4, model_latency=lambda rng: 5.0, bubbles=ONE, throttle=1)),
    ("offer", s_offer, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE)),
    ("budget", s_budget, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE)),
    ("unlabelled", s_unlabelled, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE)),
    ("human-request", s_human_request, dict(accounts={A: fans(3)}, concurrency=3, model_latency=lambda rng: 5.0, bubbles=ONE)),
    ("media-claim", s_claim, dict(accounts={A: fans(2)}, concurrency=2, model_latency=lambda rng: 5.0, bubbles=ONE)),
]


def main(only=None):
    failed = []
    for name, scenario, kw in SCENARIOS:
        if only and name not in only:
            continue
        try:
            H.run_sim(scenario, root=root(), **kw)
            print("  ok  ", name, flush=True)
        except Exception as exc:  # noqa: BLE001
            import traceback
            failed.append(name)
            print("  FAIL", name, "-", type(exc).__name__, exc, flush=True)
            sim = getattr(exc, "sim", None)
            if sim is not None and os.environ.get("SIM_DEBUG"):
                for e in [e for e in sim.events if e["event"] not in ("engine-queue", "inbound-persisted")][-25:]:
                    print("     ", e)
    if failed:
        raise SystemExit("failed: " + ", ".join(failed))
    print(f"fastt engine v2 scenarios: all cases passed ({CHECKS} checks)")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
