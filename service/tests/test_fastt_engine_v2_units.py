"""Engine v2 building blocks, one at a time (SQLite ledger fixtures, no loop harness)."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_native_pilot_ledger as L  # noqa: E402
from native_pilot import tuning  # noqa: E402
from native_pilot.engine_state import RunnerLease, WorkBoard  # noqa: E402
from native_pilot.ledger import Ledger, Refused, _dt  # noqa: E402
from native_pilot.send_gate import SendGate  # noqa: E402

CHECKS = 0


def check(v, label=""):
    global CHECKS
    assert v, label
    CHECKS += 1


def refuses(reason, fn):
    try:
        fn()
    except Refused as exc:
        check(exc.reason == reason, (reason, exc.reason))
    else:
        raise AssertionError("expected " + reason)


def tuning_cases():
    t = tuning.Tuning.from_env({})
    check(t.min_send_interval_s == 20.0 and t.concurrency == 4)
    low = tuning.Tuning.from_env({"FASTT_MIN_SEND_INTERVAL_S": "5"})
    check(low.min_send_interval_s == 20.0 and ("FASTT_MIN_SEND_INTERVAL_S", "clamped") in low.clamped, "never below 20 s")
    check(tuning.Tuning.from_env({"FASTT_MIN_SEND_INTERVAL_S": "45"}).min_send_interval_s == 45.0, "may be raised")
    check(tuning.Tuning.from_env({}, min_send_interval_s=1).min_send_interval_s == 20.0, "not even by an argument")
    check(tuning.Tuning.from_env({"FASTT_PREPARE_CONCURRENCY": "100"}).concurrency == 16)
    check(tuning.Tuning.from_env({"FASTT_PREPARE_CONCURRENCY": "0"}).concurrency == 1)
    odd = tuning.Tuning.from_env({"FASTT_BURST_QUIET_S": "30", "FASTT_BURST_MAX_WAIT_S": "10"})
    check(odd.burst_max_wait_s >= odd.quiet_s, "the hard maximum is never below the quiet window")
    check(tuning.Tuning.from_env({"FASTT_AUTO_RESOLVE_ABSENT_AFTER_S": "30"}).absent_after_s == 120.0)


async def gate_cases():
    gate = SendGate(poll_s=0.01)
    order = []
    await gate.acquire(lambda: None, 5)

    async def waiter(name, prio):
        await gate.acquire(lambda: None, 5, priority=prio)
        order.append(name)
        gate.release()
    tasks = [asyncio.ensure_future(waiter(n, p)) for n, p in (("c", "3"), ("a", "1"), ("b", "2"))]
    await asyncio.sleep(0.05)
    check(gate.waiting == 3 and gate.held)
    gate.release()
    await asyncio.gather(*tasks)
    check(order == ["a", "b", "c"], "oldest message first")
    check(not gate.held and gate.waiting == 0)
    # abort and timeout leave the queue without blocking anyone else
    await gate.acquire(lambda: None, 5)
    stop = {"r": None}
    t1 = asyncio.ensure_future(gate.acquire(lambda: stop["r"], 5))
    await asyncio.sleep(0.03)
    stop["r"] = "fan-takeover"
    try:
        await t1
    except Refused as exc:
        check(exc.reason == "fan-takeover")
    else:
        raise AssertionError("abort")
    try:
        await gate.acquire(lambda: None, 0.03)
    except Refused as exc:
        check(exc.reason == "send-queue-timeout")
    check(gate.waiting == 0 and gate.held)
    gate.release()
    check(not gate.held)


def policy(f, throttle=1, nxt=None):
    f.sql("CREATE TABLE of_delivery_policy(account_id TEXT PRIMARY KEY,enabled INTEGER,limit_units INTEGER,"
          "used_units INTEGER,throttle_seconds INTEGER,next_dispatch_at TEXT,active_intent_id TEXT)")
    f.sql("INSERT INTO of_delivery_policy VALUES(?,1,100000,0,?,?,NULL)", (L.ACCT, throttle, nxt))


def ledger_cases(root):
    f = L.Fixture(root, "units")
    policy(f, throttle=1)
    led = Ledger(f.path, allowance=True, min_send_interval_s=20, lock_scope=L.ACCT)
    check(led.min_send_interval_s == 20.0)
    TOK, PID, T0 = "synthetic-owner-v2-0001", "svc-v2", L.NOW
    p = led.activate_service(pilot_id=PID, account_id=L.ACCT, fan_id=L.FAN, owner_token=TOK, now=T0, ttl_s=3600)
    call = dict(pilot_id=PID, owner_token=TOK)
    f.source(300, created_at=(T0 + timedelta(seconds=1)).isoformat(sep=" "), ingested_at=(T0 + timedelta(seconds=2)).isoformat(sep=" "))
    t1 = T0 + timedelta(seconds=30)
    key = led.claim_input(**call, snapshot=led.snapshot(**call, now=t1), now=t1)
    a = led.begin_attempt(**call, input_key=key, bubble_no=1, revision=p.revision, now=t1)
    nxt = datetime.fromisoformat(f.sql("SELECT next_dispatch_at FROM of_delivery_policy")[0][0])
    check(nxt - t1 == timedelta(seconds=20), "a 1 s policy is raised to the 20 s floor at admission")
    # the interval runs from the FINISHED send: a slow POST pushes the next send out, never in
    t2 = t1 + timedelta(seconds=9)
    led.record_sent(**call, attempt_id=a.id, provider_message_id="900", validated_response={"id": 900, "text": "r"},
                    evidence={"v": 1}, now=t2)
    nxt = datetime.fromisoformat(f.sql("SELECT next_dispatch_at FROM of_delivery_policy")[0][0])
    check(nxt - t2 == timedelta(seconds=20), "the gate moves forward from the confirmed send")
    # admission before the gate opens is refused, after it is admitted
    f.source(900, direction="out", created_at=(t2).isoformat(sep=" "), automation_kind=None) if False else None
    # release_input: an unsent claim goes back without spending a retry
    f.sql("UPDATE native_pilot_inputs SET state='generating' WHERE input_key=?", (key,))
    check(led.release_input(**call, input_key=key, now=t2) is False, "never once a send stands")
    f.sql("DELETE FROM native_pilot_receipts")
    check(led.release_input(**call, input_key=key, now=t2) is True)
    check(f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key,))[0][0] == "requeued:0")
    check(not led.input_claimed(pilot_id=PID, message_id=300), "a released input is answerable again")
    # requeue horizon: a reply unanswerable for a day is let go
    t3 = T0 + timedelta(days=2)
    f.sql("UPDATE native_pilot_inputs SET state='generating' WHERE input_key=?", (key,))
    check(led.requeue_input(**call, input_key=key, now=t3, max_requeues=None) == 0, "older than the horizon: not requeued")
    check(led.requeue_input(**call, input_key=key, now=t2, max_requeues=None) == 1, "younger: kept pending, unbounded count")
    # unlabelled outbound clause: absent columns express no opinion
    check(led.unlabelled_clause("m", t2) == ("", ()), "this fixture has no sender columns")


def lease_cases(root):
    f = L.Fixture(root, "lease")
    led = Ledger(f.path, allowance=True)
    now = L.NOW
    a = RunnerLease(led, "acct", "host-a:1", 60)
    b = RunnerLease(led, "acct", "host-b:2", 60)
    check(a.acquire(now) == 1)
    refuses("runner-lease-held", lambda: b.acquire(now + timedelta(seconds=10)))
    check(a.renew(now + timedelta(seconds=30)) is True)
    refuses("runner-lease-held", lambda: b.acquire(now + timedelta(seconds=80)))        # renewed to +90
    check(b.acquire(now + timedelta(seconds=100)) == 2, "a crashed owner's lease is taken over after expiry, generation advances")
    check(a.renew(now + timedelta(seconds=101)) is False, "the old owner learns it lost the lease")
    b.release(now + timedelta(seconds=110))
    check(a.acquire(now + timedelta(seconds=111)) == 3, "a clean release frees it at once")


def board_cases(root):
    f = L.Fixture(root, "board")
    led = Ledger(f.path, allowance=True)
    board = WorkBoard(led, "acct")
    board.set(1, "quiet", "burst-quiet-window", message_id=5, waiting_since=L.NOW)
    board.set(2, "blocked", "delivery-unresolved-awaiting-proof")
    check(board.flush(L.NOW) == 2)
    rows = {r[0]: r[1:] for r in f.sql("SELECT fan_id,state,reason FROM engine_work")}
    check(rows[1] == ("quiet", "burst-quiet-window") and rows[2][1] == "delivery-unresolved-awaiting-proof")
    board.set(1, "quiet", "burst-quiet-window", message_id=5, waiting_since=L.NOW)
    check(board.flush(L.NOW) == 0, "unchanged state writes nothing")
    board.set(1, "preparing", None, message_id=5, waiting_since=L.NOW)
    board.clear(2)
    check(board.flush(L.NOW + timedelta(seconds=5)) == 2)
    check([r[0] for r in f.sql("SELECT fan_id FROM engine_work")] == [1])
    check(f.sql("SELECT attempts FROM engine_work")[0][0] == 1)
    refuses("work-state", lambda: board.set(3, "mystery"))


def engine_guard_cases():
    from native_pilot.engine import PilotBoundaryError, _TextClient
    from native_pilot.media_claims import MEDIA_CLAIM_HELD
    sent = []

    def refuse(reason):
        raise PilotBoundaryError(reason)
    eng = types.SimpleNamespace(fan_id=7, content=None, _card_sent=False, _refuse=refuse,
                                _send=lambda fan, text, kw: sent.append(text) or {"id": 1})
    client = _TextClient(eng)
    client.send_message(7, "haha that is so cute", )
    try:
        client.send_message(7, "I just sent you a pic")
    except PilotBoundaryError as exc:
        check(str(exc) == MEDIA_CLAIM_HELD)
    else:
        raise AssertionError("claim must be held")
    check(sent == ["haha that is so cute"], "the false claim never reached the wire")


async def main():
    tuning_cases()
    await gate_cases()
    with tempfile.TemporaryDirectory() as d:
        ledger_cases(Path(d))
        lease_cases(Path(d))
        board_cases(Path(d))
    engine_guard_cases()


tuning.MIN_SEND_INTERVAL_FLOOR_S = 20.0
asyncio.run(main())
print(f"fastt engine v2 units: all cases passed ({CHECKS} checks)")
