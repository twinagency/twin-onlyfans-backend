"""Persistent FASTT runner (native_pilot.service): synthetic, no network, no live service.

Proves, on the ledger and on the assembled supervised loop (real ledger, real
guarded_send, real OFClient send path with a fake HTTP socket; only the model
engine and the verified-connection loader are replaced):

1. the process runs past the old 30 minute pilot deadline by renewing its lease,
   and a lapsed lease is never silently revived;
2. the account's EXISTING delivery allowance is the cap: used_units is spent one
   per admission, never exceeded, never reset, and the throttle is waited out;
3. an unknown outcome holds the fan and is never resent, across a simulated
   restart, until an operator resolves it;
4. SIGTERM is a clean stop (lease released, state and counts kept, immediate
   re-adoption) and a restart adopts the same pilot;
5. an operator stop (skip_list manual_restrict / conversation mode) pauses sending
   while the process keeps running;
6. live intake (fake socket) persists only the scoped fan's plain text, once;
7. ALL-FANS mode (one runner per account): every automated fan is answered, a paused /
   stopped / never-enabled / non-owned fan never is, an unknown send holds only that fan,
   the delivery allowance is shared and enforced, and a human send for fan B goes out
   while fan A is being answered.

The SQLite backend uses scratch tables; test_native_pilot_service_pg.py runs the
same suite on a real PostgreSQL schema through the ``Backend`` seam below.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

# Engine v2 enforces a 20 s account send interval floor. These older suites drive a virtual clock with
# 1 s throttles; the floor is a code constant, so they lower it here (production cannot).
import native_pilot.tuning as _tuning  # noqa: E402
_tuning.MIN_SEND_INTERVAL_FLOOR_S = 0.0
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The existing suite explicitly exercises compatibility with the legacy shared cap.
os.environ.setdefault("FASTT_FAN_DAILY_REPLY_LIMIT", "0")
# This suite asserts the serial order (oldest inbound first, one turn at a time). Engine v2 prepares replies concurrently by
# default; the concurrent behaviour is covered by test_fastt_engine_v2.py, so here the bound is 1.
os.environ.setdefault("FASTT_PREPARE_CONCURRENCY", "1")

CHECKS = 0
PID_SUFFIX = "svc-synthetic"


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


class SqliteBackend:
    pg = False

    def __init__(self, L):
        self.L = L

    def fixture(self, root, name):
        return self.L.Fixture(root, name)

    def policy(self, f, *, enabled=True, limit=100, used=4, throttle=20, nxt=None, busy=None):
        L = self.L
        f.sql("CREATE TABLE IF NOT EXISTS of_delivery_policy(account_id TEXT PRIMARY KEY,enabled INTEGER,limit_units INTEGER,"
              "used_units INTEGER,throttle_seconds INTEGER,next_dispatch_at TEXT,active_intent_id TEXT)")
        f.sql("DELETE FROM of_delivery_policy")
        if limit is not None:
            f.sql("INSERT INTO of_delivery_policy VALUES(?,?,?,?,?,?,?)", (L.ACCT, int(enabled), limit, used, throttle, nxt, busy))

    def prepare(self, f):
        """Scratch tables check_local / the control predicate / attribution read."""
        L = self.L
        f.sql("CREATE TABLE accounts(id TEXT)")
        f.sql("INSERT INTO accounts VALUES(?)", (L.ACCT,))
        f.sql("CREATE TABLE automation_rules(account_id TEXT,is_enabled INTEGER)")
        f.sql("CREATE TABLE scheduled_jobs(account_id TEXT,status TEXT,kind TEXT)")
        f.sql("CREATE TABLE account_ai_config(account_id TEXT,persona TEXT,chatter_config_json TEXT)")
        f.sql("CREATE TABLE content_offers(account_id TEXT,fan_id INTEGER,status TEXT,mode TEXT,item_id INTEGER)")
        f.sql("CREATE TABLE pending_offer(account_id TEXT,fan_id INTEGER)")
        f.sql("ALTER TABLE messages ADD COLUMN automation_kind TEXT")
        f.sql("ALTER TABLE messages ADD COLUMN sent_by_employee_id INTEGER")
        f.sql("CREATE TABLE automation_runs(id INTEGER PRIMARY KEY,account_id TEXT,kind TEXT,started_at TEXT,"
              "completed_at TEXT,status TEXT,stats_json TEXT,error_text TEXT)")
        f.sql("CREATE TABLE skip_list(account_id TEXT,fan_id INTEGER,reason TEXT)")
        f.sql("CREATE TABLE of_conversation_state(account_id TEXT,fan_id INTEGER,mode TEXT,takeover_state TEXT)")
        os.environ["DATABASE_URL"] = self.L.database_url(f.path)

    def set_skip(self, f, reason):
        L = self.L
        f.sql("DELETE FROM skip_list")
        if reason:
            f.sql("INSERT INTO skip_list(account_id,fan_id,reason) VALUES(?,?,?)", (L.ACCT, L.FAN, reason))

    def set_state(self, f, mode, takeover="none"):
        L = self.L
        # One statement per transition: a delete+insert would show the running loop a missing row.
        if f.sql("SELECT 1 FROM of_conversation_state"):
            f.sql("UPDATE of_conversation_state SET mode=?,takeover_state=?", (mode, takeover))
        else:
            f.sql("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state) VALUES(?,?,?,?)",
                  (L.ACCT, L.FAN, mode, takeover))


    # Multi-fan helpers (all-fans mode): one conversation row + control state per fan.
    def add_fan(self, f, fan):
        L = self.L
        f.sql("INSERT INTO twin_bridge_conversations VALUES(?,?,'active',NULL,4,NULL,NULL,1,3,3,?)", (L.ACCT, fan, L.NOW.isoformat()))

    def fan_state(self, f, fan, mode, takeover="none"):
        L = self.L
        if f.sql("SELECT 1 FROM of_conversation_state WHERE fan_id=?", (fan,)):
            f.sql("UPDATE of_conversation_state SET mode=?,takeover_state=? WHERE fan_id=?", (mode, takeover, fan))
        else:
            f.sql("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state) VALUES(?,?,?,?)",
                  (L.ACCT, fan, mode, takeover))

    def fan_skip(self, f, fan, reason):
        L = self.L
        f.sql("DELETE FROM skip_list WHERE fan_id=?", (fan,))
        if reason:
            f.sql("INSERT INTO skip_list(account_id,fan_id,reason) VALUES(?,?,?)", (L.ACCT, fan, reason))


def refuses(L, reason, fn):
    L.refuses(reason, fn)
    global CHECKS


def ts(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


# ── ledger level ──────────────────────────────────────────────────────────────

async def settle_unknown(backend,f,ledger):
    from native_pilot.recovery import snapshot,reconcile,project,_matches
    from native_pilot.reporting import report_outcomes
    from twin_bridge.platform import escape_plain_text
    from native_pilot.ledger import _dt
    import hashlib
    row=f.sql("SELECT r.pilot_id,r.attempt_id,p.account_id,p.fan_id FROM native_pilot_receipts r "
        "JOIN native_pilots p ON p.pilot_id=r.pilot_id WHERE r.state='delivery_unknown'")[0]
    pilot,attempt,account,fan=row
    intent=snapshot(ledger,pilot_id=pilot,attempt_id=attempt,account_id=account,fan_id=fan)
    stamp=datetime.fromisoformat(_dt(intent["created_at"]))
    mid=900000+attempt
    raw={"id":mid,"fromUser":{"id":int(account)},"toUser":{"id":fan},"createdAt":stamp.isoformat()+"Z",
        "text":escape_plain_text(intent["body"]),"media":[],"mediaCount":0,"price":0,"previews":[],
        "lockedText":False,"responseType":"message"}
    # The service fixture advances virtual time while pacing and waiting.
    # Validate its synthetic provider receipt against that same clock.
    with patch("onlyfans_runtime.text_delivery.utcnow", return_value=stamp+timedelta(minutes=20)):
        _matches(raw,intent)
        reconcile(ledger,pilot_id=pilot,attempt_id=attempt,account_id=account,fan_id=fan,
            provider_message_id=mid,history=[raw],complete=True,
            command_key=hashlib.sha256((pilot+str(attempt)).encode()).hexdigest(),
            actor="op-session:"+"a"*64,reason="Synthetic positive recovery proof",now=stamp+timedelta(minutes=20))
        if backend.pg:
            await project(ledger,pilot_id=pilot,attempt_id=attempt,account_id=account,fan_id=fan)
            await report_outcomes(account_id=account,fan_id=fan,receipts=ledger.receipts(pilot_id=pilot))
        else:
            f.source(mid,direction="out",body=intent["body"],created_at=stamp.isoformat(sep=" "),ingested_at=stamp.isoformat(sep=" "))


def ledger_level(backend, root):
    from native_pilot.ledger import Ledger, Refused, SERVICE_BUDGET_CEILING
    L = backend.L
    f = backend.fixture(root, "svc-ledger")
    backend.policy(f, limit=6, used=4, throttle=20)
    led = Ledger(f.path, allowance=True)
    TOK, PID, T0 = "synthetic-owner-svc-0001", PID_SUFFIX, L.NOW
    A, F = L.ACCT, L.FAN

    def activate(token, now, **kw):
        return led.activate_service(pilot_id=PID, account_id=A, fan_id=F, owner_token=token, now=now, ttl_s=120, **kw)

    refuses(L, "invalid-pilot-id", lambda: led.activate_service(pilot_id="plain-pilot", account_id=A, fan_id=F,
                                                                owner_token=TOK, now=T0, ttl_s=120))
    p = activate(TOK, T0)
    check(p.baseline_used == 3 and p.end_limit == 3 + SERVICE_BUDGET_CEILING and p.state == "active")
    check(ts(p.deadline) == T0 + timedelta(seconds=120) and ts(p.cutoff) == T0)
    refuses(L, "owner-live", lambda: activate("another-owner-0000000001", T0 + timedelta(seconds=10)))

    # 1. Renewal carries the pilot far past the old 30 minute deadline.
    f.source()
    for k in range(1, 46):
        led.renew(pilot_id=PID, owner_token=TOK, now=T0 + timedelta(seconds=60 * k), ttl_s=120)
    t = T0 + timedelta(minutes=45)
    check(ts(f.sql("SELECT deadline FROM native_pilots")[0][0]) == t + timedelta(seconds=120))
    check(ts(f.sql("SELECT owner_expires_at FROM twin_bridge_conversations")[0][0]) == t + timedelta(seconds=120))
    call = dict(pilot_id=PID, owner_token=TOK)
    snap = led.snapshot(**call, now=t)
    check(snap is not None)
    key = led.claim_input(**call, snapshot=snap, now=t)

    # 2. The allowance is the cap: one unit per admission, throttle honoured, never exceeded.
    a1 = led.begin_attempt(**call, input_key=key, bubble_no=1, revision=p.revision, now=t)
    if backend.pg:
        from native_pilot.service import fan_hold_reason, fan_commercial_hold_reason
        # The real admission writes its own unresolved pointer. It must allow
        # this proved attempt while fencing new sends and every other attempt.
        led.validate_dispatch(**call, attempt_id=a1.id, now=t)
        check(fan_hold_reason(f.path, A, F, True) == "fan-unresolved-delivery")
        check(fan_hold_reason(f.path, A, F, True, own_attempt=a1.id) is None)
        check(fan_commercial_hold_reason(f.path, A, F, True, own_attempt=a1.id) is None)
        check(fan_hold_reason(f.path, A, F, True, own_attempt=a1.id + 1) == "fan-unresolved-delivery")
        backend.set_state(f, "paused")
        check(fan_hold_reason(f.path, A, F, True, own_attempt=a1.id) == "fan-paused")
        backend.set_state(f, "automated")
    used, nxt = f.sql("SELECT used_units,next_dispatch_at FROM of_delivery_policy")[0]
    check(used == 5 and ts(nxt) == t + timedelta(seconds=20))
    check(f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 4)
    led.record_sent(**call, attempt_id=a1.id, provider_message_id="1001", validated_response={"id": 1001, "text": "r"},
                    evidence={"verified": True}, now=t)
    attempts = f.sql("SELECT count(*) FROM twin_bridge_send_attempts")[0][0]
    refuses(L, "allowance-throttled", lambda: led.begin_attempt(**call, input_key=key, bubble_no=2, revision=p.revision,
                                                                now=t + timedelta(seconds=5)))
    check(f.sql("SELECT count(*) FROM twin_bridge_send_attempts")[0][0] == attempts, "a refused admission leaves no attempt")
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5)
    a2 = led.begin_attempt(**call, input_key=key, bubble_no=2, revision=p.revision, now=t + timedelta(seconds=20))
    led.record_sent(**call, attempt_id=a2.id, provider_message_id="1002", validated_response={"id": 1002, "text": "r"},
                    evidence={"verified": True}, now=t)
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6)
    attempts = f.sql("SELECT count(*) FROM twin_bridge_send_attempts")[0][0]
    refuses(L, "allowance-exhausted", lambda: led.begin_attempt(**call, input_key=key, bubble_no=3, revision=p.revision,
                                                                now=t + timedelta(seconds=100)))
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6, "never past limit_units")
    for kwargs, reason in ((dict(enabled=False, limit=20, used=6, nxt=None), "allowance-disabled"),
                           (dict(limit=20, used=6, busy="twin-intent"), "allowance-busy"),
                           (dict(limit=None), "allowance-missing")):
        backend.policy(f, **kwargs)
        refuses(L, reason, lambda: led.begin_attempt(**call, input_key=key, bubble_no=3, revision=p.revision,
                                                     now=t + timedelta(seconds=100)))
    check(f.sql("SELECT count(*) FROM twin_bridge_send_attempts")[0][0] == attempts)
    backend.policy(f, limit=20, used=6)

    # A lapsed lease is never revived; a restarted process adopts the same pilot.
    refuses(L, "authority-lost", lambda: led.renew(**call, now=t + timedelta(seconds=500), ttl_s=120))
    t2 = t + timedelta(seconds=500)
    led2 = Ledger(f.path, allowance=True)
    TOK2 = "synthetic-owner-svc-0002"
    p2 = led2.activate_service(pilot_id=PID, account_id=A, fan_id=F, owner_token=TOK2, now=t2, ttl_s=120)
    check(p2.pilot_id == PID and ts(p2.cutoff) == T0 and p2.baseline_used == 3 and p2.revision > p.revision)
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6 and
          f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 5, "adoption resets no used count")
    refuses(L, "pilot-owner-mismatch", lambda: led.renew(**call, now=t2, ttl_s=120))
    # The old process's claim is stopped, never regenerated.
    snap = led2.snapshot(pilot_id=PID, owner_token=TOK2, now=t2)
    refuses(L, "input-already-claimed", lambda: led2.claim_input(pilot_id=PID, owner_token=TOK2, snapshot=snap, now=t2))

    # 3. Unknown outcome across restarts: nothing is resent, an operator resolves it.
    f.source(200, created_at=(t2 + timedelta(seconds=1)).isoformat(sep=" "), ingested_at=(t2 + timedelta(seconds=2)).isoformat(sep=" "))
    t3 = t2 + timedelta(seconds=30)
    call2 = dict(pilot_id=PID, owner_token=TOK2)
    snap = led2.snapshot(**call2, now=t3)
    key2 = led2.claim_input(**call2, snapshot=snap, now=t3)
    a3 = led2.begin_attempt(**call2, input_key=key2, bubble_no=1, revision=p2.revision, now=t3)
    from native_pilot.recovery import freeze, reconcile
    freeze(led2,**call2,attempt_id=a3.id,body="Synthetic recovered reply.",terms=None)  # crash after this
    t4 = t3 + timedelta(seconds=1000)
    led3 = Ledger(f.path, allowance=True)
    refuses(L, "delivery-unresolved", lambda: led3.activate_service(pilot_id=PID, account_id=A, fan_id=F,
                                                                    owner_token="synthetic-owner-svc-0003", now=t4, ttl_s=120))
    check(f.sql("SELECT owner_token FROM native_pilots")[0][0] == TOK2, "a refused adoption changes nothing")
    led2.record_unknown(**call2, attempt_id=a3.id, now=t4)
    check(f.sql("SELECT state FROM native_pilots")[0][0] == "stopped")
    refuses(L, "conversation-not-active", lambda: led3.activate_service(pilot_id=PID, account_id=A, fan_id=F,
                                                                        owner_token="synthetic-owner-svc-0003", now=t4, ttl_s=120))
    f.sql("UPDATE twin_bridge_conversations SET state='active'")  # operator resumes ...
    refuses(L, "delivery-unresolved", lambda: led3.activate_service(pilot_id=PID, account_id=A, fan_id=F,
                                                                    owner_token="synthetic-owner-svc-0003", now=t4, ttl_s=120))
    asyncio.run(settle_unknown(backend,f,led2))  # exact positive evidence settles both ledgers; no resend
    used_before = f.sql("SELECT used_units FROM of_delivery_policy")[0][0]
    p3 = led3.activate_service(pilot_id=PID, account_id=A, fan_id=F, owner_token="synthetic-owner-svc-0003", now=t4, ttl_s=120)
    check(p3.state == "active" and f.sql("SELECT state FROM native_pilot_receipts WHERE attempt_id=?", (a3.id,))[0][0] == "sent")
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == used_before == 7, "the unknown send keeps its unit")

    # 4. Clean release: unsent claim dropped (regenerated later), claimed-with-receipt kept, immediate re-adoption.
    call3 = dict(pilot_id=PID, owner_token="synthetic-owner-svc-0003")
    f.source(300, created_at=(t4 + timedelta(seconds=1)).isoformat(sep=" "), ingested_at=(t4 + timedelta(seconds=2)).isoformat(sep=" "))
    t5 = t4 + timedelta(seconds=30)
    snap = led3.snapshot(**call3, now=t5)
    key3 = led3.claim_input(**call3, snapshot=snap, now=t5)
    led3.release_service(**call3, now=t5)
    check(not f.sql("SELECT 1 FROM native_pilot_inputs WHERE input_key=?", (key3,)), "an unsent claim is dropped on clean release")
    check(f.sql("SELECT owner_token,reserved FROM twin_bridge_conversations")[0][0] is None)
    check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "active")
    refuses(L, "pilot-owner-mismatch", lambda: led3.release_service(pilot_id=PID, owner_token="forged-owner-0000000001", now=t5))
    p4 = led3.activate_service(pilot_id=PID, account_id=A, fan_id=F, owner_token="synthetic-owner-svc-0004", now=t5, ttl_s=120)
    check(p4.state == "active", "re-adoption needs no waiting for the lease")
    # The same message can be answered again after a clean release.
    call4 = dict(pilot_id=PID, owner_token="synthetic-owner-svc-0004")
    t6 = t5 + timedelta(seconds=60)
    snap = led3.snapshot(**call4, now=t6)
    check(led3.claim_input(**call4, snapshot=snap, now=t6) == key3)

    # 5. An attempt that CERTAINLY never reached the wire: refunded (budget unit AND allowance unit), never blocking,
    #    never convertible once it may have been sent; the throttle stays in place.
    backend.policy(f, limit=100, used=10, throttle=20)
    t7 = t6 + timedelta(seconds=30)
    led3.renew(**call4, now=t7, ttl_s=3600)
    budget0 = f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0]
    a = led3.begin_attempt(**call4, input_key=key3, bubble_no=1, revision=p4.revision, now=t7)
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 11)
    check(f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == budget0 + 1)
    check(led3.record_not_sent(**call4, attempt_id=a.id, now=t7) is True)
    used, nxt = f.sql("SELECT used_units,next_dispatch_at FROM of_delivery_policy")[0]
    check(used == 10 and ts(nxt) == t7 + timedelta(seconds=20), "the allowance unit is refunded, the throttle stays")
    check(f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == budget0)
    check(f.sql("SELECT state,reason FROM twin_bridge_send_attempts WHERE id=?", (a.id,))[0] == ("refused", "native-pilot-not-sent"))
    st = led3.status(PID)["attempts"]
    check(st["not_sent"] == 1 and Ledger.counted(st) == sum(st.values()) - 1, "a refused attempt does not count against the limit")
    sent0 = st.get("sent", 0)
    check(led3.record_not_sent(**call4, attempt_id=a.id, now=t7) is False, "identical replay is a no-op")
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 10, "a replay never refunds twice")
    check(led3.status(PID)["state"] == "active" and f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "active")
    refuses(L, "attempt-owner-mismatch", lambda: led3.record_not_sent(pilot_id=PID, owner_token="forged-owner-0000000001",
                                                                       attempt_id=a.id, now=t7))
    a2 = led3.begin_attempt(**call4, input_key=key3, bubble_no=2, revision=p4.revision, now=t7 + timedelta(seconds=20))
    led3.record_sent(**call4, attempt_id=a2.id, provider_message_id="1301", validated_response={"id": 1301, "text": "r"},
                     evidence={"verified": True}, now=t7)
    refuses(L, "attempt-not-dispatching", lambda: led3.record_not_sent(**call4, attempt_id=a2.id, now=t7))
    refuses(L, "attempt-already-exists", lambda: led3.begin_attempt(**call4, input_key=key3, bubble_no=1, revision=p4.revision,
                                                                    now=t7 + timedelta(seconds=40)))
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 11 and (led3.status(PID)["attempts"]["not_sent"], led3.status(PID)["attempts"]["sent"]) == (1, sent0 + 1))
    check(led3.requeue_input(**call4, input_key=key3, now=t7, max_requeues=2) == 0, "an input with a sent bubble is never requeued")

    # A delivery_unknown attempt is never converted either, and blocks until an operator resolves it.
    t8 = t7 + timedelta(seconds=200)
    led3.finish_input(**call4, input_key=key3, now=t8)
    f.source(401, created_at=(t8 - timedelta(seconds=5)).isoformat(sep=" "), ingested_at=(t8 - timedelta(seconds=4)).isoformat(sep=" "))
    key4 = led3.claim_input(**call4, snapshot=led3.snapshot(**call4, now=t8), now=t8)
    check(key4 != key3 and f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key4,))[0][0] == "generating")
    a3 = led3.begin_attempt(**call4, input_key=key4, bubble_no=1, revision=p4.revision, now=t8)
    check(led3.requeue_input(**call4, input_key=key4, now=t8, max_requeues=2) == 0, "a dispatching attempt is never requeued over")
    led3.record_not_sent(**call4, attempt_id=a3.id, now=t8)

    # 6. Requeue: bounded, durable, only without a standing send attempt, and the same input is claimed again.
    check(f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key4,))[0][0] == "generating")
    check(led3.requeue_input(**call4, input_key=key4, now=t8, max_requeues=1) == 1)
    check(f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key4,))[0][0] == "requeued:1")
    check(not led3.input_claimed(pilot_id=PID, message_id=401), "a requeued input is not 'worked'")
    refuses(L, "input-not-generating", lambda: led3.begin_attempt(**call4, input_key=key4, bubble_no=2, revision=p4.revision,
                                                                  now=t8 + timedelta(seconds=30)))
    t9 = t8 + timedelta(seconds=60)
    check(led3.claim_input(**call4, snapshot=led3.snapshot(**call4, now=t9), now=t9) == key4, "the same input, claimed again")
    check(f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key4,))[0][0] == "generating")
    check(f.sql("SELECT count(*) FROM native_pilot_inputs WHERE message_id=401")[0][0] == 1)
    check(led3.input_claimed(pilot_id=PID, message_id=401))
    refuses(L, "input-already-claimed", lambda: led3.claim_input(**call4, snapshot=led3.snapshot(**call4, now=t9), now=t9))
    check(led3.requeue_input(**call4, input_key=key4, now=t9, max_requeues=1) == 0, "bounded: once is once")
    check(f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key4,))[0][0] == "generating")
    check(led3.requeue_input(**call4, input_key=key4, now=t9, max_requeues=2) == 2)
    # The count survives a restart (it lives in the state): a fresh ledger reads it back at the next claim.
    led4 = Ledger(f.path, allowance=True)
    check(led4.claim_input(**call4, snapshot=led4.snapshot(**call4, now=t9), now=t9) == key4)
    check(led4.requeue_input(**call4, input_key=key4, now=t9, max_requeues=2) == 0, "the bound holds across a new process")
    # A provider cooldown is durable across Ledger instances and never consumes allowance.
    used_before = f.sql("SELECT used_units FROM of_delivery_policy")[0][0]
    due = t9 + timedelta(seconds=900)
    check(led4.requeue_input(**call4, input_key=key4, now=t9, max_requeues=None, not_before=due) == 3)
    restarted = Ledger(f.path, allowance=True)
    check(ts(restarted.allowance_status(A)["next_dispatch_at"]) == due)
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == used_before)
    check(f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key4,))[0][0] == "requeued:3")
    check(restarted.claim_input(**call4, snapshot=restarted.snapshot(**call4, now=t9), now=t9) == key4)
    refuses(L, "allowance-throttled", lambda: restarted.begin_attempt(**call4, input_key=key4, bubble_no=2,
            revision=p4.revision, now=t9 + timedelta(seconds=10)))
    # A follow-up input is never requeued (its job is settled once).
    f.sql("UPDATE native_pilot_inputs SET source_hash='followup:x' WHERE input_key=?", (key4,))
    check(led4.requeue_input(**call4, input_key=key4, now=t9, max_requeues=9) == 0)
    f.sql("UPDATE native_pilot_inputs SET source_hash=? WHERE input_key=?", (snap.source_hash, key4))
    refuses(L, "pilot-owner-mismatch", lambda: led4.requeue_input(pilot_id=PID, owner_token="forged-owner-0000000001",
                                                                  input_key=key4, now=t9, max_requeues=2))
    # A clean release drops a claim whose only receipt is a not_sent one (nothing was sent: answered after restart).
    led4.release_service(**call4, now=t9)
    check(not f.sql("SELECT 1 FROM native_pilot_inputs WHERE input_key=?", (key4,)), "not_sent receipts do not pin a claim")


# ── assembled loop ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def add(self, seconds):
        self.t += timedelta(seconds=seconds)


async def until(pred, label, events, timeout=20.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timeout waiting for " + label + "; last events: " + json.dumps(events[-6:], default=str))
        await asyncio.sleep(0.003)


async def loop_level(backend, root):
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.dotenv_values = lambda *a, **k: {"OPENROUTER_API_KEY": "synthetic-unused-key"}
    L = backend.L
    import test_native_pilot_runner as R
    from curl_cffi.requests.exceptions import ProxyError
    from native_pilot import attribution, connection as conn_mod, engine as engine_mod, runner, service
    from native_pilot.attribution import Sentinels
    from native_pilot.ledger import Ledger
    from native_pilot.scope import AccountScope
    from of_client import OFClient
    from twin_bridge import coordination

    logging.disable(logging.CRITICAL)
    service.MODEL_RETRY_LIMIT = 1   # engine v2 default is 12 (never drop a reply); this suite asserts the bounded variant
    A, F = L.ACCT, L.FAN
    events = []
    clock = Clock(datetime.now(timezone.utc).replace(tzinfo=None, microsecond=123000))
    reports = []

    async def no_report(ledger, pilot, run_error=None):
        reports.append(run_error)

    async def no_ensure(scope, fan):
        pass

    async def fast_idle(self, seconds):
        # A PostgreSQL heartbeat observes time; it does not independently
        # advance the shared clock while a different thread owns a transaction.
        if not backend.pg or asyncio.current_task() is not self._renew_task:
            clock.add(seconds)
        try:
            await asyncio.wait_for(self.stop.wait(), 0.02 if backend.pg else 0.002)
        except asyncio.TimeoutError:
            pass

    class LiveHTTP(R.FakeHTTP):
        def __init__(self, f):
            super().__init__([], A, F, clock().isoformat() + "Z")
            self.f, self.sent, self.posted_at = f, [], []
            self.codes = []          # scripted answers for the next POSTs: an HTTP error status, or None = succeed
            self.refuse_headers = {}

        def get(self, url, **kwargs):
            rows = self.f.sql("SELECT message_id,body,created_at FROM messages WHERE direction='in' AND fan_id=?", (F,))
            inbound = [{"id": mid, "text": body, "createdAt": str(created).replace(" ", "T") + "Z", "isFromMe": False,
                        "fromUser": {"id": F}, "withUser": {"id": F}} for mid, body, created in sorted(rows, reverse=True)]
            self.history = sorted(list(self.sent) + inbound, key=lambda m: (m['createdAt'], m['id']), reverse=True)
            return super().get(url, **kwargs)

        def request(self, method, url, json=None, **kwargs):
            self.at = clock().isoformat() + "Z"
            self.posted_at.append(clock())
            if self.codes:
                code = self.codes.pop(0)
                if code:
                    self.posts.append(dict(json))
                    return R.FakeResponse({"error": "refused"}, status=code, headers=self.refuse_headers)
            response = super().request(method, url, json=json, **kwargs)
            self.sent.append(response.data)
            return response

    class FakeIntake:
        started = []

        def __init__(self, scope, fan, path):
            pass

        def start(self, client):
            FakeIntake.started.append(client)

        async def stop(self):
            pass

    holder = {}

    class FakeEngine:
        texts = ["neutral reply"]
        card = False

        fail_models = 0        # the next N turns end in a model timeout before anything is sent
        die_after_first = False  # the model times out after the first bubble went out
        skip_manual = False      # the turn sends nothing: a human chatted inside the cautious-resume window

        def __init__(self, account, fan, *, chat, send, emit, content, context, **_extra):
            self.send, self.fan, self.chat = send, fan, chat

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def run(self, run_id):
            f = holder["f"]
            if FakeEngine.skip_manual:
                return {"skipped_manual": 1}
            if FakeEngine.fail_models > 0:
                FakeEngine.fail_models -= 1
                self.chat.__self__.stopped_reason = getattr(FakeEngine, "failure_reason", "model-timeout")
                return {"errors": 1}
            for i, text in enumerate(FakeEngine.texts):
                if FakeEngine.die_after_first and i == 1:
                    self.chat.__self__.stopped_reason = "model-timeout"
                    return {"errors": 1}
                raw = await asyncio.to_thread(self.send, self.fan, text, {})
                f.source(raw["id"], direction="out", body=raw["text"], created_at=clock().isoformat(sep=" "))
                f.sql("UPDATE messages SET automation_kind='ai_chatter',sent_by_employee_id=5 WHERE message_id=?", (raw["id"],))
                if FakeEngine.card:   # the confirmed bubble is the priced card
                    f.sql("UPDATE messages SET automation_kind='ai_upseller',price_cents=300 WHERE message_id=?", (raw["id"],))
            return {}

    def make(name, **policy):
        f = backend.fixture(root, name)
        backend.prepare(f)
        backend.policy(f, **{"limit": 100, "used": 4, "throttle": 20, **policy})
        holder["f"] = f
        http = LiveHTTP(f)
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = A, A, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        client.me = lambda: {"id": int(A), "username": "synthetic-creator"}
        return f, http, client

    fake_conn = conn_mod.PilotConnection(
        scope=AccountScope.parse(A), tenant_id="synthetic-tenant", revision=3, owner_id=int(A),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30), capture={}, x_hash="x", proxy_url=None,
        model_id="openrouter/deepseek-v4.1-flash", model_api_key="synthetic-key", daily_cost_cap_cents=100)

    async def fake_load(scope):
        return fake_conn

    async def fake_sentinels():
        return Sentinels(automation=5, human=6)

    args = argparse.Namespace(account=A, fan=F, handle="synthetic-creator", enable_send=True, twin_context=False,
                              content_manifest=None, service_id=None)
    pid = f"svc-{A}-{F}"
    inbound_ids = iter(range(5000, 6000))

    def fan_says(f, text="neutral fan text"):
        clock.add(1)
        mid = next(inbound_ids)
        f.source(mid, body=text, created_at=clock().isoformat(sep=" "), ingested_at=clock().isoformat(sep=" "))
        return mid

    async def replied(f, http, n, label, outs=None):
        """Wait for POST, attribution and terminal claim persistence separately."""
        await until(lambda: len(http.posts) == n and
                    f.sql("SELECT count(*) FROM messages WHERE direction='out'")[0][0] == (n if outs is None else outs), label, events)
        await until(lambda: not f.sql("SELECT 1 FROM native_pilot_inputs WHERE state='generating'"),
                    label + " claim settled", events)

    def kinds(name):
        return [e for e in events if e.get("event") == name]

    async def settle_loop(n=60):
        for _ in range(n):
            await asyncio.sleep(0.003)

    with patch.object(runner, "emit", events.append), patch.object(runner, "now", clock), \
            patch.object(runner, "report_to_twin", no_report), patch.object(service, "ensure_fan_conversation", no_ensure), \
            patch.object(service.Service, "idle", fast_idle), patch.object(engine_mod, "NativeEngine", FakeEngine), \
            patch.object(conn_mod, "load_verified_connection", fake_load), patch.object(attribution, "require_sentinels", fake_sentinels):

        def start(client, **kw):
            options = dict(ttl_s=120, renew_s=30, poll_s=2, hold_s=15, quiet_s=0, intake_factory=FakeIntake)
            options.update(kw)
            patcher = patch.object(conn_mod, "build_client", return_value=client)
            patcher.start()
            task = asyncio.get_running_loop().create_task(service.serve(args, **options))
            return task, patcher

        async def stop(task, patcher):
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 20)
            patcher.stop()

        # ── runs past the old deadline, replies, respects throttle and allowance ──
        f, http, client = make("loop-main", limit=7, used=4, throttle=20)
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        check(kinds("ready")[0]["persistent"] is True and "deadline_utc" not in kinds("ready")[0])
        start_clock = clock()
        await until(lambda: clock() >= start_clock + timedelta(minutes=45), "45 virtual minutes", events)
        check(not task.done() and not kinds("lease-lost") and not kinds("stopped"))
        check(ts(f.sql("SELECT deadline FROM native_pilots")[0][0]) > start_clock + timedelta(minutes=30),
              "the lease is renewed beyond the old 30 minute deadline")
        fan_says(f)
        await replied(f, http, 1, "first reply after 45 minutes")
        check(http.posts[0]["text"] == "<p>neutral reply</p>")
        used, nxt = f.sql("SELECT used_units,next_dispatch_at FROM of_delivery_policy")[0]
        check(used == 5 and nxt is not None)
        fan_says(f)
        await replied(f, http, 2, "second reply")
        admitted = [ts(r[0]) for r in f.sql("SELECT created_at FROM twin_bridge_send_attempts WHERE turn_id LIKE 'native-pilot-%' ORDER BY id")]
        check(len(admitted) == 2 and admitted[1] - admitted[0] >= timedelta(seconds=20), "the 20 second throttle is waited out")
        fan_says(f)
        await replied(f, http, 3, "third reply")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 7)
        fan_says(f)
        await until(lambda: any(e.get("reason") == "allowance-exhausted" for e in kinds("held")), "allowance exhausted", events)
        for _ in range(30):
            await asyncio.sleep(0.003)
        check(len(http.posts) == 3 and f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 7, "never past limit_units")
        check(not task.done(), "an exhausted allowance holds sending, it does not stop the process")

        # ── SIGTERM = clean stop; restart adopts at once, counts untouched ──
        events.clear()
        await stop(task, patcher)
        check(kinds("finished") and http.closed)
        check(f.sql("SELECT state FROM native_pilots")[0][0] == "active")
        check(f.sql("SELECT owner_token FROM twin_bridge_conversations")[0][0] is None)
        check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "active")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 7 and len(http.posts) == 3)
        revision = f.sql("SELECT revision FROM native_pilots")[0][0]
        events.clear()
        http.closed = False
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready after restart", events)
        check(f.sql("SELECT revision FROM native_pilots")[0][0] > revision and kinds("ready")[0]["pilot_id"] == pid)
        fan_says(f)
        backend.policy(f, limit=100, used=7, throttle=20)  # an operator raises the existing allowance
        await replied(f, http, 4, "reply after restart")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 8)
        await stop(task, patcher)

        # ── operator stop pauses sending; the process keeps running ──
        f, http, client = make("loop-hold", limit=100, used=4, throttle=1)
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        for label, setter, clear in (("skip-list", lambda: backend.set_skip(f, "manual_restrict"), lambda: backend.set_skip(f, None)),
                                     ("mode", lambda: backend.set_state(f, "human_controlled"), lambda: backend.set_state(f, "automated")),
                                     ("takeover", lambda: backend.set_state(f, "automated", "effective"), lambda: backend.set_state(f, "automated"))):
            before = len(http.posts)
            setter()
            await until(lambda: any(str(e.get("reason", "")).startswith("fan-") for e in kinds("held")), label + " held", events)
            fan_says(f)
            for _ in range(30):
                await asyncio.sleep(0.003)
            check(len(http.posts) == before and not task.done(), label)
            events.clear()
            clear()
            await replied(f, http, before + 1, label + " resumed")
            check(kinds("resumed"), label)
            events.clear()
        check(service.fan_hold_reason(f.path, A, F) is None)
        backend.set_skip(f, "info")  # a soft skip reason is not an operator stop
        check(service.fan_hold_reason(f.path, A, F) is None)
        backend.set_skip(f, None)
        await stop(task, patcher)

        # ── a stop seen while pacing or at admission leaves no attempt; one seen at the wire never POSTs and is
        #    CERTAINLY not sent (receipt not_sent, allowance unit refunded, input requeued) ──
        original = service.fan_hold_reason
        for trip, label in ((1, "pace"), (2, "admission"), (3, "wire")):
            f, http, client = make("loop-" + label, limit=100, used=4, throttle=1)
            calls = {"n": 0, "armed": False}

            def stops_late(path, account, fan, calls=calls, trip=trip, **kwargs):
                if not calls["armed"]:
                    return original(path, account, fan, **kwargs)
                calls["n"] += 1
                return "fan-restricted" if calls["n"] >= trip else None

            real_run = FakeEngine.run

            async def armed_run(self, run_id, calls=calls, real_run=real_run):
                calls["armed"] = True
                return await real_run(self, run_id)
            events.clear()
            with patch.object(service, "fan_hold_reason", stops_late), patch.object(FakeEngine, "run", armed_run):
                task, patcher = start(client)
                await until(lambda: kinds("ready"), "ready", events)
                fan_says(f)
                await until(lambda: kinds("turn-abandoned") or kinds("stopped") or kinds("turn-requeued") or kinds("turn-released"), label + " stop outcome", events)
                check(http.posts == [], label + ": nothing reached the wire")
                check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 4, label + ": no allowance unit stays spent")
                check(f.sql("SELECT state FROM native_pilots")[0][0] == "active", label + ": a stop at the wire is not an unknown send")
                check(not f.sql("SELECT 1 FROM native_pilot_receipts WHERE state='delivery_unknown'"))
                if trip < 3:
                    # Engine v2: a stop seen before admission RELEASES the claim (no draft kept, answered after the hold) instead of abandoning it.
                    check((kinds("turn-released") or kinds("turn-abandoned"))[0]["reason"] == "fan-restricted")
                    check(not f.sql("SELECT 1 FROM native_pilot_receipts"), label + ": no attempt was admitted")
                else:
                    check(f.sql("SELECT state FROM native_pilot_receipts")[0][0] == "not_sent")
                    check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "active")
                    check(kinds("turn-requeued")[0]["reason"] == "fan-held-before-post" and kinds("turn-requeued")[0]["retry"] == 1)
                    check(not kinds("stopped"))
                await stop(task, patcher)

        # ── a pause landing while the turn waits out the account throttle is seen THERE, not a throttle later ──
        from native_pilot.ledger import _dt
        due = clock() + timedelta(days=10)
        f, http, client = make("loop-pace-hold", limit=100, used=4, throttle=20)
        calls = {"n": 0, "armed": False}

        def stops_while_waiting(path, account, fan):
            if not calls["armed"]:
                return original(path, account, fan)
            calls["n"] += 1
            if calls["n"] >= 3:
                calls.setdefault("seen_at", clock())
                return "fan-paused"
            return None

        real_run = FakeEngine.run

        async def armed_run(self, run_id):
            # A different sender extends the gate while generation is in flight.
            # Initial account cooldowns are now waited out before generation begins.
            f.sql("UPDATE of_delivery_policy SET next_dispatch_at=?", (_dt(due),))
            calls["armed"] = True
            return await real_run(self, run_id)
        events.clear()
        with patch.object(service, "fan_hold_reason", stops_while_waiting), patch.object(FakeEngine, "run", armed_run):
            task, patcher = start(client)
            await until(lambda: kinds("ready"), "ready", events)
            fan_says(f)
            await until(lambda: kinds("turn-abandoned") or kinds("turn-released"), "abandoned while pacing", events)
            check((kinds("turn-released") or kinds("turn-abandoned"))[0]["reason"] == "fan-paused")
            # Without the check in pace() this turn waits out the (here: days long) throttle and ends
            # "allowance-wait-timeout"; with it the pause is seen on the 3rd one second step.
            check(calls["n"] == 3, "the hold is re-read on every pacing step")
            check(http.posts == [] and not f.sql("SELECT 1 FROM native_pilot_receipts"))
            check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 4)
            await stop(task, patcher)

        # ── a clean provider 429 is "not sent": nothing stops, the unit comes back, the SAME input is answered later ──
        f, http, client = make("loop-429", limit=100, used=4, throttle=1)
        http.codes, http.refuse_headers = [429], {}
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        mid = fan_says(f)
        await until(lambda: kinds("turn-requeued"), "429 requeued", events)
        check(len(http.posts) == 1 and kinds("turn-requeued")[0]["reason"] == "provider-rate-limited")
        check(kinds("turn-requeued")[0]["retry"] == 1 and kinds("turn-requeued")[0]["backoff_s"] == 60.0)
        check(f.sql("SELECT state FROM native_pilots")[0][0] == "active" and f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "active")
        check(not kinds("stopped") and not kinds("turn-abandoned") and not f.sql("SELECT 1 FROM native_pilot_receipts WHERE state='delivery_unknown'"))
        await replied(f, http, 2, "answered again after the 429", outs=1)
        check(http.posted_at[1] - http.posted_at[0] >= timedelta(seconds=60), "the retry waits out the back-off")
        check(sorted(r[0] for r in f.sql("SELECT state FROM native_pilot_receipts")) == ["not_sent", "sent"])
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5, "only the accepted send keeps its unit")
        check(f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "complete" and f.sql("SELECT count(*) FROM native_pilot_inputs")[0][0] == 1)
        check(f.sql("SELECT count(*) FROM messages WHERE direction='out'")[0][0] == 1, "exactly one message reached the fan")
        check(reports.count(None) >= 1 and "provider-rate-limited" not in reports, "a requeued blip does not flag the fan for a human")
        await stop(task, patcher)

        # A long provider refusal stays pending beyond the old two-retry and ten-attempt limits.
        f, http, client = make("loop-429-bound", limit=100, used=4, throttle=1)
        http.codes, http.refuse_headers = [429] * 12, {"retry-after": "7"}
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        fan_says(f)
        await replied(f, http, 13, "the original input recovers after twelve definite refusals", outs=1)
        check([e["retry"] for e in kinds("turn-requeued")] == list(range(1, 13)))
        check([e["backoff_s"] for e in kinds("turn-requeued")] == [7.0] * 12)
        check(not kinds("turn-abandoned") and not kinds("stopped"))
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5)
        check(f.sql("SELECT count(*) FROM native_pilot_inputs")[0][0] == 1)
        check(f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "complete")
        check(sorted(r[0] for r in f.sql("SELECT state FROM native_pilot_receipts")) == ["not_sent"] * 12 + ["sent"])
        check(all((b-a).total_seconds() >= 7 for a,b in zip(http.posted_at,http.posted_at[1:])))
        await settle_loop()
        check(len(http.posts) == 13, "a recovered input is never replayed")
        fan_says(f, "a newer message")
        await replied(f, http, 14, "the next message is answered normally", outs=2)
        await stop(task, patcher)

        # Another definite refusal (403) is certain too but is not retried; ambiguous statuses stay unknown.
        for code in (403, 408, 500, 503):
            f, http, client = make("loop-http-%d" % code, limit=100, used=4, throttle=1)
            http.codes = [code]
            events.clear()
            task, patcher = start(client)
            await until(lambda: kinds("ready"), "ready", events)
            fan_says(f)
            if code == 403:
                await until(lambda: kinds("turn-abandoned"), "403 abandoned", events)
                check(kinds("turn-abandoned")[0]["reason"] == "provider-refused" and not kinds("turn-requeued"))
                check(f.sql("SELECT state FROM native_pilots")[0][0] == "active" and f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 4)
                check(f.sql("SELECT state FROM native_pilot_receipts")[0][0] == "not_sent" and len(http.posts) == 1)
            else:
                await until(lambda: kinds("stopped"), "%d unknown stop" % code, events)
                check(kinds("stopped")[0]["reason"] == "delivery-unknown" and not kinds("turn-requeued"))
                check(f.sql("SELECT state FROM native_pilots")[0][0] == "stopped" and f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5)
                check(f.sql("SELECT state FROM native_pilot_receipts")[0][0] == "delivery_unknown" and len(http.posts) == 1)
                check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "attention")
            await stop(task, patcher)

        # A 429 on a LATER bubble (an earlier one already went out): the rest is dropped, never regenerated.
        f, http, client = make("loop-429-late", limit=100, used=4, throttle=1)
        http.codes = [None, 429]
        FakeEngine.texts = ["first bubble", "second bubble"]
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        fan_says(f)
        await until(lambda: kinds("turn-abandoned"), "late 429 abandoned", events)
        check(len(http.posts) == 2 and not kinds("turn-requeued") and kinds("turn-abandoned")[0]["reason"] == "provider-rate-limited")
        check(sorted(r[0] for r in f.sql("SELECT state FROM native_pilot_receipts")) == ["not_sent", "sent"])
        check(f.sql("SELECT state FROM native_pilots")[0][0] == "active" and f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5)
        await settle_loop()
        check(len(http.posts) == 2, "a partly answered input is never regenerated")
        FakeEngine.texts = ["neutral reply"]
        await stop(task, patcher)

        # Provider deferrals and the one model retry have independent budgets.
        for failures in (1, 2):
            f, http, client = make("loop-mixed-retry-" + str(failures), limit=100, used=4, throttle=1)
            http.codes, http.refuse_headers = [429], {"Retry-After": "7"}
            calls = {"n": 0}
            real_run = FakeEngine.run
            async def mixed_run(self, run_id, failures=failures):
                calls["n"] += 1
                if calls["n"] == 2:
                    FakeEngine.fail_models = failures
                return await real_run(self, run_id)
            events.clear()
            with patch.object(FakeEngine, "run", mixed_run):
                task, patcher = start(client)
                await until(lambda: kinds("ready"), "ready", events)
                fan_says(f)
                if failures == 1:
                    await replied(f, http, 2, "provider refusal then model timeout recovers", outs=1)
                    check(not kinds("turn-abandoned"))
                    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5)
                else:
                    await until(lambda: kinds("turn-abandoned"), "second model failure remains bounded", events)
                    check(kinds("turn-abandoned")[0]["reason"] == "model-timeout")
                    check(len(http.posts) == 1 and f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 4)
                check([e["reason"] for e in kinds("turn-requeued")] == ["provider-rate-limited", "model-timeout"])
                check(FakeEngine.fail_models == 0)
                await stop(task, patcher)

        # ── an external (untagged) message inside the cautious-resume window ends the turn with a NAMED reason ──
        f, http, client = make("loop-external-hold", limit=100, used=4, throttle=1)
        FakeEngine.skip_manual = True
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        fan_says(f)
        await until(lambda: kinds("external-message-hold"), "external hold is reported", events)
        await until(lambda: kinds("turn-abandoned"), "external hold ends the turn", events)
        check(kinds("turn-abandoned")[0]["reason"] == "native-no-reply-external-message")
        check(not http.posts)
        FakeEngine.skip_manual = False
        await stop(task, patcher)

        # ── an identity question hands the chat to a human AND closes the reply schedule (no spent schedule left behind) ──
        from native_pilot import pacing as _pacing
        from native_pilot.service import Service as _Service

        async def _no_handoff(self, trigger, attention="identity-question"):
            return None   # the control path needs the Twin tables; only the schedule close is under test here
        f, http, client = make("loop-identity-closes-schedule", limit=100, used=4, throttle=1)
        finished = []
        decision = {"schedule": None, "authority": None, "eligible_at": clock()}
        events.clear()
        with patch.object(_pacing, "settings", lambda *a, **k: {_pacing.PACING: True}), \
                patch.object(_pacing, "prepare", lambda *a, **k: dict(decision)), \
                patch.object(_pacing, "finish", lambda ledger, d, **k: finished.append(k)), \
                patch.object(_Service, "identity_handoff", _no_handoff):
            task, patcher = start(client)
            await until(lambda: kinds("ready"), "ready", events)
            fan_says(f, "are you a bot?")
            await until(lambda: finished, "the identity hand-off closes the pacing schedule", events)
            check(finished[0].get("sent") is False and not finished[0].get("error"))
            check(not http.posts)
            await stop(task, patcher)

        # ── a model timeout/error abandons an input only after ONE requeue; never once a send attempt stands ──
        f, http, client = make("loop-model-retry", limit=100, used=4, throttle=1)
        FakeEngine.fail_models = 1
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        fan_says(f)
        await replied(f, http, 1, "answered after one model failure")
        check([e["reason"] for e in kinds("turn-requeued")] == ["model-timeout"] and not kinds("turn-abandoned"))
        check(f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "complete" and f.sql("SELECT count(*) FROM native_pilot_inputs")[0][0] == 1)
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5)
        await stop(task, patcher)


        # A rejected prompt placeholder is regenerated once through the same durable retry path.
        # Repeated placeholders remain unsent and exhaust the retry rather than spinning forever.
        for failures in (1, 2):
            f, http, client = make(f"loop-placeholder-{failures}", limit=100, used=4, throttle=1)
            FakeEngine.fail_models = failures
            events.clear()
            with patch.object(FakeEngine, "failure_reason", "instruction-placeholder-held", create=True), \
                    patch.object(service, "MODEL_RETRY_LIMIT", 1):
                task, patcher = start(client)
                await until(lambda: kinds("ready"), "ready", events)
                fan_says(f)
                if failures == 1:
                    await replied(f, http, 1, "placeholder regenerated and answered once")
                    check(not kinds("turn-abandoned"))
                    check(f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "complete")
                else:
                    await until(lambda: kinds("turn-abandoned"), "repeated placeholder bounded", events)
                    check(not http.posts, "placeholder text never reaches transport")
                    check(f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "failed")
                check([e["reason"] for e in kinds("turn-requeued")] == ["instruction-placeholder-held"])
                check(FakeEngine.fail_models == 0)
                await stop(task, patcher)

        f, http, client = make("loop-model-bound", limit=100, used=4, throttle=1)
        FakeEngine.fail_models = 2
        events.clear()
        model_bound = patch.object(service, "MODEL_RETRY_LIMIT", 1)   # engine v2 default is 1; the bound itself is still honoured
        model_bound.start()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        fan_says(f)
        await until(lambda: kinds("turn-abandoned"), "model failure twice: abandoned", events)
        check(len(kinds("turn-requeued")) == 1 and kinds("turn-abandoned")[0]["reason"] == "model-timeout")
        await settle_loop()
        check(http.posts == [] and FakeEngine.fail_models == 0, "exactly one retry, no more")
        check(f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "failed",
              "a terminal model failure is recorded as failed, not as a completed reply")
        fan_says(f, "next message")
        await replied(f, http, 1, "the fan is answered on his next message")
        await stop(task, patcher)
        model_bound.stop()

        f, http, client = make("loop-model-after-send", limit=100, used=4, throttle=1)
        FakeEngine.texts, FakeEngine.die_after_first = ["first bubble", "second bubble"], True
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        fan_says(f)
        await until(lambda: kinds("turn-abandoned"), "model failed after a send: abandoned", events)
        check(not kinds("turn-requeued") and len(http.posts) == 1, "never regenerated once a send attempt stands")
        await settle_loop()
        check(len(http.posts) == 1)
        FakeEngine.texts, FakeEngine.die_after_first = ["neutral reply"], False
        await stop(task, patcher)

        # ── unknown outcome: hold, never resend, across a restart, until an operator resolves ──
        f, http, client = make("loop-unknown", limit=100, used=4, throttle=1)
        http.fail = ProxyError("synthetic transport failure")
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        first = fan_says(f, "first")
        await until(lambda: any(e.get("event") == "stopped" for e in events), "unknown stop", events)
        check(len(http.posts) == 1 and f.sql("SELECT state FROM native_pilots")[0][0] == "stopped")
        check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "attention")
        used_after = f.sql("SELECT used_units FROM of_delivery_policy")[0][0]
        check(used_after == 5, "the unknown send keeps its allowance unit")
        http.fail = None
        fan_says(f, "second")
        for _ in range(40):
            await asyncio.sleep(0.003)
        check(len(http.posts) == 1 and not task.done(), "no resend, process still supervised")
        check(any(e.get("event") == "held" for e in events))
        await stop(task, patcher)
        events.clear()
        task, patcher = start(client)  # the restart
        await until(lambda: any(e.get("event") == "held" for e in events), "held after restart", events)
        for _ in range(40):
            await asyncio.sleep(0.003)
        check(len(http.posts) == 1 and not kinds("ready"), "a restart cannot send over an unresolved attempt")
        f.sql("UPDATE twin_bridge_conversations SET state='active'")
        for _ in range(40):
            await asyncio.sleep(0.003)
        check(len(http.posts) == 1 and not kinds("ready"), "resuming alone does not clear an unresolved send")
        from native_pilot.ledger import Ledger
        await stop(task,patcher)
        await settle_unknown(backend,f,Ledger(f.path,allowance=True))
        task,patcher=start(client)
        await until(lambda: kinds("ready"), "ready after operator resolution", events)
        await replied(f, http, 2, "reply to the message that arrived while held", outs=2)
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6)
        fan_says(f, "third")
        await replied(f, http, 3, "reply to the new message", outs=3)
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 7)
        check(f.sql("SELECT count(*) FROM native_pilot_receipts WHERE state='delivery_unknown'")[0][0] == 0)
        await stop(task, patcher)
        _ = first

        # ── the lease is lost (another owner adopted): the process stops sending but stays up ──
        f, http, client = make("loop-lease", limit=100, used=4, throttle=1)
        events.clear()
        task, patcher = start(client)
        await until(lambda: kinds("ready"), "ready", events)
        far = (clock() + timedelta(days=1)).isoformat(sep=" ")
        f.sql("UPDATE twin_bridge_conversations SET owner_token='someone-else-0000000001',owner_expires_at=?", (far,))
        await until(lambda: kinds("lease-lost"), "lease lost", events)
        fan_says(f)
        for _ in range(30):
            await asyncio.sleep(0.003)
        check(http.posts == [] and not task.done())
        await stop(task, patcher)

        # ── D6: a paid card never ends the conversation unless --stop-after-card is passed ──
        # The production paths (compose fastt-runner, supervisor children) never pass the flag.
        base_argv = ["--account", A, "--fan", str(F), "--handle", "synthetic-creator", "--enable-send"]
        check(service.build_parser().parse_args(base_argv).stop_after_card is False, "stop-after-card is off by default")
        check(service.build_parser().parse_args(["--account", A, "--all-fans", "--handle", "h", "--enable-send"]).stop_after_card is False,
              "all-fans: off by default")
        check(service.build_parser().parse_args(base_argv + ["--stop-after-card"]).stop_after_card is True,
              "stop-after-card is an explicit flag")
        FakeEngine.card = True
        # Attribution of a priced card needs the catalogue plan; this section tests only the stop wiring.
        attribution_off = patch.object(runner, "verify_attribution", lambda *a, **k: None)
        attribution_off.start()
        for label, flag in (("default", None), ("flag-off", False)):
            f, http, client = make("loop-card-" + label, limit=100, used=4, throttle=1)
            events.clear()
            with patch.object(args, "stop_after_card", flag, create=True):
                task, patcher = start(client)
                await until(lambda: kinds("ready"), "ready", events)
                fan_says(f)
                await replied(f, http, 1, label + ": the card goes out")
                check(f.sql("SELECT count(*) FROM messages WHERE price_cents=300")[0][0] == 1)
                fan_says(f, "i want another")
                await replied(f, http, 2, label + ": the chat goes on after the card")
                check(not kinds("stopped-after-card") and not task.done(), label + ": no stop after a card")
                check(service.fan_hold_reason(f.path, A, F) is None, label + ": no runtime stop was written")
                await stop(task, patcher)
        if backend.pg:   # the stop writes the real skip_list / Twin control rows: full schema only
            f, http, client = make("loop-card-flag", limit=100, used=4, throttle=1)
            events.clear()
            with patch.object(args, "stop_after_card", True, create=True):
                task, patcher = start(client)
                await until(lambda: kinds("ready"), "ready", events)
                fan_says(f)
                await replied(f, http, 1, "flag: the card goes out")
                await until(lambda: kinds("stopped-after-card"), "stopped after the card", events)
                check(service.fan_hold_reason(f.path, A, F) is not None, "flag: the runtime stop is written")
                fan_says(f, "i want another")
                for _ in range(30):
                    await asyncio.sleep(0.003)
                check(len(http.posts) == 1, "flag: nothing is sent after the card")
                await stop(task, patcher)
        FakeEngine.card = False
        attribution_off.stop()

    from db.engine import engine
    await engine.dispose()


# ── intake (fake socket) ──────────────────────────────────────────────────────

async def intake_level(backend, root):
    L = backend.L
    from native_pilot.intake import Intake
    from native_pilot.scope import AccountScope
    A, F = L.ACCT, L.FAN
    f = backend.fixture(root, "intake")
    scope = AccountScope.parse(A)
    stored, out, flagged = [], [], []

    async def transcode(account, message):
        check(account == scope.db_id)
        f.source(message["id"], body=message["text"], created_at=str(message["createdAt"]).replace("T", " ").replace("Z", ""))
        stored.append(message["id"])

    async def attention(account, fan, reason):
        flagged.append((account, fan, reason))
        return True

    def msg(mid, sender=F, **extra):
        return {"api2_chat_message": {"id": mid, "text": "<p>hi</p>", "createdAt": "2026-10-24T01:02:03.123000+00:00",
                                      "fromUser": {"id": sender}, **extra}}

    class Socket:
        def __init__(self, frames, fail=False):
            self.frames, self.fail, self.stopped = frames, fail, False

        async def events(self):
            if self.fail:
                raise ConnectionError("synthetic")
            for frame in self.frames:
                yield frame
            await asyncio.sleep(0.01)

        def stop(self):
            self.stopped = True

    sockets = [Socket([], fail=True),
               Socket([{"connected": True}, msg(7001), msg(7001), msg(7002, sender=int(A)), msg(7003, sender=F + 1),
                       msg(7004, media=[{"id": 1}], mediaCount=1), msg(7005, isTip=True), msg(7006, price=3),
                       msg(7009, giphyId="g1"), msg(7010, lockedText=True), msg(7004, media=[{"id": 1}], mediaCount=1),
                       {"unrelated": 1}, "not-a-dict", {"api2_chat_message": {"id": "x", "fromUser": {"id": F}}},
                       msg(7007)]), Socket([])]
    made = []

    def factory(client):
        made.append(client)
        return sockets[min(len(made) - 1, len(sockets) - 1)]

    intake = Intake(scope, F, f.path, socket_factory=factory, transcode=transcode, attention=attention,
                    emit=out.append, backoff_s=0.001)
    intake.start("client-object")
    for _ in range(300):
        if 7007 in stored:
            break
        await asyncio.sleep(0.01)
    await intake.stop()
    # D2: unsupported fan input is no longer dropped. It is stored (so the ledger holds the burst and a tip
    # reaches the existing tip ledger path) AND the fan is flagged for a human. Text is unchanged.
    check(stored == [7001, 7004, 7005, 7006, 7009, 7010, 7007], stored)
    check(flagged == [(scope.db_id, F, "unsupported-input:media"), (scope.db_id, F, "unsupported-input:tip"),
                      (scope.db_id, F, "unsupported-input:paid"), (scope.db_id, F, "unsupported-input:gif"),
                      (scope.db_id, F, "unsupported-input:paid")], flagged)
    check(len(made) >= 2 and all(c == "client-object" for c in made), "reconnects with backoff on the same client")
    check(sockets[1].stopped, "the socket is stopped with the intake")
    unsupported = sorted(e["message_id"] for e in out if e["event"] == "unsupported-input")
    check(unsupported == [7004, 7005, 7006, 7009, 7010], "a repeated frame is a duplicate, not a second unsupported event")
    check(all(e["persisted"] and e["attention"] for e in out if e["event"] == "unsupported-input"))
    check({e["reason"] for e in out if e["event"] == "unsupported-input"} ==
          {"unsupported-input:media", "unsupported-input:tip", "unsupported-input:paid", "unsupported-input:gif"})
    check([e["message_id"] for e in out if e["event"] == "inbound-persisted"] == [7001, 7007])
    check(any(e["event"] == "intake-error" for e in out) and any(e["event"] == "intake-connected" for e in out))
    # The account's own echo and another fan are never filed as this fan's inbound.
    check(not f.sql("SELECT 1 FROM messages WHERE message_id IN (7002,7003)"))
    h = Intake(scope, F, f.path, transcode=transcode, attention=attention, emit=out.append)
    check(await h.handle(msg(7008, createdAt="not-a-date")) == "invalid")
    check(len(flagged) == 5, "plain text and invalid frames never flag attention")
    check(await h.handle(msg(7004, media=[{"id": 1}], mediaCount=1)) == "duplicate" and len(flagged) == 5,
          "an already stored unsupported frame is not re-flagged (a human may have cleared it)")

    # A failing attention write never loses the stored row and never turns into a reply.
    async def attention_broken(account, fan, reason):
        raise RuntimeError("synthetic")
    out.clear()
    h = Intake(scope, F, f.path, transcode=transcode, attention=attention_broken, emit=out.append)
    check(await h.handle(msg(7011, media=[{"id": 2}], mediaCount=1)) == "unsupported")
    check(7011 in stored)
    ev = [e for e in out if e["event"] == "unsupported-input"]
    check(len(ev) == 1 and ev[0]["persisted"] is True and ev[0]["attention"] is False)
    check(any(e["event"] == "intake-attention-failed" and e["message_id"] == 7011 for e in out))

    # A failing store still tries the attention flag, and says so.
    async def transcode_broken(account, message):
        raise RuntimeError("synthetic")
    out.clear()
    before = len(flagged)
    h = Intake(scope, F, f.path, transcode=transcode_broken, attention=attention, emit=out.append)
    check(await h.handle(msg(7012, isTip=True)) == "failed")
    check(len(flagged) == before + 1 and flagged[-1][2] == "unsupported-input:tip")
    check(any(e["event"] == "intake-persist-failed" and e["message_id"] == 7012 for e in out))

    # (A stored media/tip/paid inbound row already holds the ledger snapshot: test_native_pilot_ledger invalids.)
    from native_pilot.intake import unsupported_reason
    check(unsupported_reason(msg(1)["api2_chat_message"]) is None)
    check(unsupported_reason(msg(1, isTip=True, media=[{"id": 1}])["api2_chat_message"]) == "unsupported-input:tip",
          "money outranks media in the reason")


async def multi_intake_level(backend, root):
    """Account-wide intake: every fan's plain text, never our own echoes, still idempotent."""
    L = backend.L
    from native_pilot.intake import Intake
    from native_pilot.scope import AccountScope
    scope, stored, out = AccountScope.parse(L.ACCT), [], []
    f = backend.fixture(root, "multi-intake")
    backend.prepare(f)
    for fan in (L.FAN + 5, L.FAN + 6):
        backend.add_fan(f, fan)

    async def transcode(account, message):
        stored.append((message["fromUser"]["id"], message["id"]))
        f.source(message["id"], fan_id=message["fromUser"]["id"], body="hi", created_at="2026-10-24 01:02:03.123000")

    def msg(mid, sender, **extra):
        return {"api2_chat_message": {"id": mid, "text": "<p>hi</p>", "createdAt": "2026-10-24T01:02:03.123000+00:00",
                                      "fromUser": {"id": sender}, **extra}}
    flagged = []

    async def attention(account, fan, reason):
        flagged.append((fan, reason))
        return True
    intake = Intake(scope, None, f.path, transcode=transcode, attention=attention, emit=out.append)
    check(await intake.handle(msg(8101, L.FAN + 5)) == "persisted")
    check(await intake.handle(msg(8102, L.FAN + 6)) == "persisted")
    check(await intake.handle(msg(8102, L.FAN + 6)) == "duplicate")
    check(await intake.handle(msg(8103, int(L.ACCT))) == "other-sender", "our own echo is never filed as a fan's inbound")
    check(await intake.handle(msg(8104, L.FAN + 7, mediaCount=1, media=[{"id": 1}])) == "unsupported")
    check(await intake.handle(msg(8105, "not-an-id")) == "other-sender")
    check(stored == [(L.FAN + 5, 8101), (L.FAN + 6, 8102), (L.FAN + 7, 8104)], stored)
    check(flagged == [(L.FAN + 7, "unsupported-input:media")], "attention is per sender fan in all-fans mode")


# ── all-fans mode ─────────────────────────────────────────────────────────────

async def multi_level(backend, root):
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.dotenv_values = lambda *a, **k: {"OPENROUTER_API_KEY": "synthetic-unused-key"}
    L = backend.L
    import copy
    from urllib.parse import urlsplit
    import test_native_pilot_runner as R
    from curl_cffi.requests.exceptions import ProxyError
    from native_pilot import attribution, connection as conn_mod, engine as engine_mod, runner, service
    from native_pilot.attribution import Sentinels
    from native_pilot.scope import AccountScope
    from of_client import OFClient

    logging.disable(logging.CRITICAL)
    A = L.ACCT
    FA, FB, FC, FD, FE, FF = (L.FAN + n for n in range(6))  # A,B automated; C paused; D skip_list stop; E no state row; F not FASTT-owned
    OWNED = {FA, FB, FC, FD, FE}
    events = []
    clock = Clock(datetime.now(timezone.utc).replace(tzinfo=None, microsecond=123000))

    async def no_report(ledger, pilot, run_error=None):
        pass

    async def no_ensure(scope, fan):
        pass

    async def fast_idle(self, seconds):
        if not backend.pg or asyncio.current_task() is not self._renew_task:
            clock.add(seconds)
        try:
            await asyncio.wait_for(self.stop.wait(), 0.02 if backend.pg else 0.002)
        except asyncio.TimeoutError:
            pass

    class LiveHTTP:
        """One account socket, many fans: history and POSTs are routed by the fan in the URL."""

        def __init__(self, f):
            self.f, self.sent, self.posts, self.fail_for, self.closed = f, [], [], {}, False
            self.codes_for = {}     # fan -> scripted answers for its next POSTs (HTTP error status, or None = succeed)

        @staticmethod
        def fan_of(url):
            return int(urlsplit(url).path.split("/")[-2])

        def get(self, url, **kwargs):
            fan = self.fan_of(url)
            rows = self.f.sql("SELECT message_id,body,created_at FROM messages WHERE direction='in' AND fan_id=?", (fan,))
            inbound = [{"id": mid, "text": body, "createdAt": str(created).replace(" ", "T") + "Z", "isFromMe": False,
                        "fromUser": {"id": fan}, "withUser": {"id": fan}} for mid, body, created in rows]
            history = sorted([m for m in self.sent if m["withUser"]["id"] == fan] + inbound,
                             key=lambda m: (m["createdAt"], m["id"]), reverse=True)
            return R.FakeResponse({"list": copy.deepcopy(history), "hasMore": False})

        def request(self, method, url, json=None, **kwargs):
            fan = self.fan_of(url)
            if fan in self.fail_for:
                self.posts.append((fan, json["text"]))
                raise self.fail_for[fan]
            self.posts.append((fan, json["text"]))
            if self.codes_for.get(fan):
                code = self.codes_for[fan].pop(0)
                if code:
                    return R.FakeResponse({"error": "refused"}, status=code)
            result = {"id": 7000 + len(self.posts), "text": json["text"], "isFromMe": True, "fromUser": {"id": int(A)},
                      "withUser": {"id": fan}, "createdAt": clock().isoformat() + "Z", "price": 0, "media": [],
                      "mediaCount": 0, "responseType": "message", "previews": [], "lockedText": False}
            self.sent.append(result)
            return R.FakeResponse(result)

        def close(self):
            self.closed = True

    class FakeIntake:
        scopes = []

        def __init__(self, scope, fan, path):
            FakeIntake.scopes.append(fan)

        def start(self, client):
            pass

        async def stop(self):
            pass

    holder = {}

    class FakeEngine:
        fail_for = {}      # fan -> number of coming turns that end in a model timeout before anything is sent

        def __init__(self, account, fan, *, chat, send, emit, content, context, own_offers_only=False, **_extra):
            self.send, self.fan, self.content, self.chat = send, fan, content, chat
            check(own_offers_only is True, "all-fans turns scope open offers to their own fan")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        gate = None        # an asyncio.Event: the model is "hung" until the test sets it
        waiting = []       # fans whose model call is currently hung
        after_gate = None  # called once the hung model returns, before the engine tries to send

        async def run(self, run_id):
            f = holder["f"]
            if FakeEngine.fail_for.get(self.fan):
                FakeEngine.fail_for[self.fan] -= 1
                self.chat.__self__.stopped_reason = "model-timeout"
                return {"errors": 1}
            if FakeEngine.gate is not None:
                FakeEngine.waiting.append(self.fan)
                await FakeEngine.gate.wait()
                if FakeEngine.after_gate is not None:
                    FakeEngine.after_gate(self.fan)
            if self.content is not None:
                check(self.content.catalog_ids == {71, 72}, "every child receives the reviewed account plan")
                rows = f.sql("SELECT id,enabled FROM catalog_items ORDER BY id")
                check(len(rows) == 2 and all(row[1] for row in rows), "all reviewed items enabled before generation")
            raw = await asyncio.to_thread(self.send, self.fan, "neutral reply", {})
            f.source(raw["id"], fan_id=self.fan, direction="out", body=raw["text"],
                     created_at=raw["createdAt"].removesuffix("Z").replace("T", " "))
            f.sql("UPDATE messages SET automation_kind='ai_chatter',sent_by_employee_id=5 WHERE message_id=?", (raw["id"],))
            return {}

    def make(name, **policy):
        f = backend.fixture(root, name)
        backend.prepare(f)
        backend.policy(f, **{"limit": 100, "used": 4, "throttle": 20, **policy})
        for fan in (FB, FC, FD, FE, FF):
            backend.add_fan(f, fan)
        holder["f"] = f
        http = LiveHTTP(f)
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = A, A, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        client.me = lambda: {"id": int(A), "username": "synthetic-creator"}
        backend.fan_state(f, FA, "automated")
        backend.fan_state(f, FB, "automated")
        backend.fan_state(f, FC, "paused")
        backend.fan_state(f, FD, "automated")
        backend.fan_skip(f, FD, "manual_restrict")
        backend.fan_state(f, FF, "automated")  # E has no row at all
        return f, http, client

    fake_conn = conn_mod.PilotConnection(
        scope=AccountScope.parse(A), tenant_id="synthetic-tenant", revision=3, owner_id=int(A),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30), capture={}, x_hash="x", proxy_url=None,
        model_id="openrouter/deepseek-v4.1-flash", model_api_key="synthetic-key", daily_cost_cap_cents=100)

    async def fake_load(scope):
        return fake_conn

    async def fake_sentinels():
        return Sentinels(automation=5, human=6)

    args = argparse.Namespace(account=A, fan=None, all_fans=True, handle="synthetic-creator", enable_send=True,
                              twin_context=False, content_manifest=None, service_id=None)
    ids = iter(range(5000, 9000))

    def says(f, fan, text="neutral fan text"):
        clock.add(1)
        mid = next(ids)
        f.source(mid, fan_id=fan, body=text, created_at=clock().isoformat(sep=" "), ingested_at=clock().isoformat(sep=" "))
        return mid

    def posted(http, fan):
        return [p for p in http.posts if p[0] == fan]

    def kinds(name):
        return [e for e in events if e.get("event") == name]

    def pilots(f):
        return sorted(r[0] for r in f.sql("SELECT pilot_id FROM native_pilots WHERE state<>'watermark'"))

    async def settle(n=60):
        for _ in range(n):
            await asyncio.sleep(0.003)

    refuses_args = argparse.Namespace(**{**vars(args), "fan": FA})  # both scopes at once is refused
    try:
        service.Service(refuses_args)
        check(False, "--fan together with --all-fans must be refused")
    except service.Refused as exc:
        check(exc.reason == "fan-scope-invalid")

    with patch.object(runner, "emit", events.append), patch.object(runner, "now", clock), \
            patch.object(runner, "report_to_twin", no_report), patch.object(service, "ensure_fan_conversation", no_ensure), \
            patch.object(service.Service, "idle", fast_idle), patch.object(engine_mod, "NativeEngine", FakeEngine), \
            patch.object(service.Service, "_owned", lambda self, fan: fan in OWNED), \
            patch.object(conn_mod, "load_verified_connection", fake_load), patch.object(attribution, "require_sentinels", fake_sentinels):

        async def start(client):
            # Returns once the account socket is up: only messages after that moment are answerable.
            patcher = patch.object(conn_mod, "build_client", return_value=client)
            patcher.start()
            before = len(FakeIntake.scopes)
            options = dict(ttl_s=120, renew_s=30, poll_s=2, hold_s=15, quiet_s=0, intake_factory=FakeIntake)
            task = asyncio.get_running_loop().create_task(service.serve(args, **options))
            await until(lambda: len(FakeIntake.scopes) > before, "intake started", events)
            return task, patcher

        async def stop(task, patcher):
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 20)
            patcher.stop()

        # ── two automated fans are both answered; paused / stopped / never-enabled / non-owned never ──
        f, http, client = make("multi-main", limit=100, used=4, throttle=20)
        events.clear()
        FakeIntake.scopes.clear()
        task, patcher = await start(client)
        check(FakeIntake.scopes == [None], "the socket covers the whole account (no single-fan filter)")
        mb = says(f, FB, "from b")      # B writes first: oldest first
        ma = says(f, FA, "from a")
        for fan in (FC, FD, FE, FF):
            says(f, fan, "from someone who must not be answered")
        await until(lambda: len(posted(http, FA)) == 1 and len(posted(http, FB)) == 1, "A and B answered", events)
        check([p[0] for p in http.posts[:2]] == [FB, FA], "oldest inbound first")
        await settle()
        check(all(not posted(http, fan) for fan in (FC, FD, FE, FF)), "paused / stopped / no-state / non-owned: no POST")
        check(pilots(f) == sorted([f"svc-{A}-{FA}", f"svc-{A}-{FB}"]), pilots(f))
        used, nxt = f.sql("SELECT used_units,next_dispatch_at FROM of_delivery_policy")[0]
        check(used == 6 and nxt is not None, "one allowance unit per send, shared by both fans")
        admitted = [ts(r[0]) for r in f.sql("SELECT created_at FROM twin_bridge_send_attempts WHERE turn_id LIKE 'native-pilot-%' ORDER BY id")]
        check(len(admitted) == 2 and admitted[1] - admitted[0] >= timedelta(seconds=20), "the account throttle spans fans")
        check(not task.done() and not kinds("lease-lost"))
        # Each fan has its own conversation reservation, claim and receipt.
        check(f.sql("SELECT count(*) FROM native_pilot_inputs")[0][0] == 2)
        check(f.sql("SELECT count(DISTINCT pilot_id) FROM native_pilot_receipts WHERE state='sent'")[0][0] == 2)

        # An operator stop appearing later is honoured; Twin enabling C later makes it answerable.
        backend.fan_state(f, FA, "human_controlled")
        says(f, FA, "a again while human controlled")
        await settle()
        check(len(posted(http, FA)) == 1, "a fan moved to human_controlled is not answered")
        backend.fan_state(f, FC, "automated")
        await until(lambda: len(posted(http, FC)) == 1, "C answered once Twin enables it", events)
        backend.fan_state(f, FA, "automated")
        await until(lambda: len(posted(http, FA)) == 2, "A answered again after Twin resumes it", events)
        await stop(task, patcher)
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 8)

        # ── an unknown send on fan A holds A only; B keeps being answered; nothing is resent ──
        f, http, client = make("multi-unknown", limit=100, used=4, throttle=1)
        events.clear()
        http.fail_for[FA] = ProxyError("synthetic transport failure")
        task, patcher = await start(client)
        says(f, FA, "a first")
        await until(lambda: any(e.get("event") == "stopped" for e in events), "A unknown stop", events)
        check(len(posted(http, FA)) == 1 and f.sql("SELECT state FROM native_pilots WHERE pilot_id=?", (f"svc-{A}-{FA}",))[0][0] == "stopped")
        check(f.sql("SELECT state FROM twin_bridge_conversations WHERE fan_id=?", (FA,))[0][0] == "attention")
        says(f, FB, "b while a is held")
        await until(lambda: len(posted(http, FB)) == 1, "B still answered", events)
        http.fail_for.clear()
        says(f, FA, "a second")
        says(f, FB, "b second")
        await until(lambda: len(posted(http, FB)) == 2, "B answered again", events)
        await settle()
        check(len(posted(http, FA)) == 1, "the unknown send is never resent and A stays held")
        check(f.sql("SELECT state FROM native_pilots WHERE pilot_id=?", (f"svc-{A}-{FB}",))[0][0] == "active")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 7, "the unknown send kept its unit")
        await stop(task, patcher)

        # ── the allowance is shared across fans and refuses when exhausted ──
        f, http, client = make("multi-allowance", limit=6, used=4, throttle=1)
        events.clear()
        task, patcher = await start(client)
        says(f, FA)
        says(f, FB)
        await until(lambda: len(http.posts) == 2, "both answered", events)
        says(f, FA)
        says(f, FB)
        await settle(120)
        check(len(http.posts) == 2, "limit_units is the cap for the whole account")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6)
        check(any(str(e.get("reason", "")).startswith("allowance-") for e in kinds("held")))
        await stop(task, patcher)

        # ── Twin's controls run for every fan with activity; a human send for B goes out while A chats ──
        f, http, client = make("multi-human", limit=100, used=4, throttle=1)
        events.clear()
        calls, grants = [], []

        class FakeStore:
            def __init__(self, url):
                self.engine = self

            def connect(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *a, **k):
                return self

            def scalar(self):
                return 3

            def reconcile_runtime_stop(self, tenant, account, fan):
                calls.append(("reconcile", fan))

            def admit_new_conversation(self, tenant, account, fan):
                calls.append(("admit", fan))

        def fake_dispatch(authority, tenant, account, transport, *, campaign_first, scan_cursors):
            if not grants:
                return None
            grant = grants.pop(0)
            try:
                with patch("onlyfans_runtime.text_delivery.utcnow", side_effect=clock):
                    transport.post_once(grant)
            except Exception as exc:
                return type(exc).__name__
            return "accepted"

        def Grant(fan, body):
            from onlyfans_runtime.text_delivery import Dispatch
            return Dispatch(account_id=A, fan_id=fan, intent_id="human-"+str(fan),
                attempt_id="attempt-"+str(fan), token="synthetic-manual-token", body=body,
                payload_hash="a"*64, origin="manual", deadline=clock()+timedelta(seconds=30))

        import onlyfans_runtime.fastt_manual as manual
        import onlyfans_runtime.store as twin_store
        import onlyfans_runtime.text_delivery as delivery
        env = {"DATABASE_URL": "postgresql://synthetic/synthetic", "ONLYFANS_WORKER_TENANT": "synthetic-tenant"}
        with patch.dict(os.environ, env), patch("twin_bridge.coordination._store_target", return_value=f.path), \
                patch.object(twin_store, "Store", FakeStore), \
                patch.object(delivery, "TextAuthority", lambda store: store), \
                patch.object(manual, "dispatch_next_operator", fake_dispatch), \
                patch("onlyfans_runtime.campaigns.CampaignService.queue_due", return_value=0) as due_campaigns:
            task, patcher = await start(client)
            says(f, FA, "a is chatting")
            grants.extend([Grant(FB, "human hello for B"), Grant(FF, "never sent: not FASTT-owned")])
            await until(lambda: len(posted(http, FA)) == 1 and (FB, "<p>human hello for B</p>") in http.posts,
                        "A answered and the human message for B sent", events)
            await settle()
            check(not posted(http, FF), "a queued message for a non-owned fan is never sent by this runner")
            check(("reconcile", FA) in calls and ("admit", FA) in calls, "controls run for the fan with activity")
            due_campaigns.assert_called_with("synthetic-tenant", A)
            says(f, FB, "b answers the human")
            await until(lambda: len(posted(http, FB)) == 2, "B (now with human history) is answered too", events)
            check(("reconcile", FB) in calls)
            check(not any(c[1] in (FC, FD, FF) for c in calls), "no controls for fans without activity or not owned")
            await stop(task, patcher)
        # ── iso45 6.E: a Pause acknowledged while the model is HUNG is final: nothing is sent after the ack,
        #    the other fan keeps being answered, and Hand back -> Resume answers the next message ──
        f, http, client = make("multi-hung", limit=100, used=4, throttle=1)
        events.clear()
        FakeEngine.gate, FakeEngine.waiting = asyncio.Event(), []
        task, patcher = await start(client)
        says(f, FA, "a, while the model hangs")
        await until(lambda: FA in FakeEngine.waiting, "model call for A is hung", events)
        backend.fan_state(f, FA, "paused")                  # Twin's acknowledged Pause (display + stop together)
        backend.fan_skip(f, FA, "manual_restrict")
        FakeEngine.gate.set()
        await settle(120)
        check(not posted(http, FA), "nothing is sent to A after the Pause ack, whatever the model returned")
        check(f.sql("SELECT count(*) FROM twin_bridge_send_attempts WHERE turn_id LIKE 'native-pilot-%'")[0][0] == 0,
              "a pre-wire refusal opens no send attempt (nothing uncertain)")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 4, "and spends no allowance unit")
        check(not any(e.get("event") == "stopped" for e in events), "the fan is not stranded as delivery-unknown")
        says(f, FB, "b while a is paused")
        await until(lambda: len(posted(http, FB)) == 1, "B is answered while A is paused", events)
        check(not posted(http, FA))
        # Hand back / Resume (what Twin writes), then a NEW message from A is answered; the old one never is.
        FakeEngine.gate = None
        backend.fan_skip(f, FA, None)
        backend.fan_state(f, FA, "automated")
        says(f, FA, "a again after resume")
        await until(lambda: len(posted(http, FA)) == 1, "A answered after Resume", events)
        await settle()
        check(len(posted(http, FA)) == 1, "the message that met the Pause is never regenerated or resent")
        await stop(task, patcher)

        # A Pause that lands AFTER admission but before the wire is caught by the wire re-check: no POST at all.
        # Nothing was handed to the transport, so the attempt is CERTAINLY not sent: the unit is refunded, the fan
        # is not stranded as delivery-unknown, and once the Pause is lifted the SAME message is answered (once).
        f, http, client = make("multi-wire", limit=100, used=4, throttle=1)
        events.clear()
        task, patcher = await start(client)
        from native_pilot.ledger import Ledger as _Ledger
        real_begin = _Ledger.begin_attempt

        def begin_then_pause(self, **kw):
            attempt = real_begin(self, **kw)
            if kw["pilot_id"] == f"svc-{A}-{FA}":
                backend.fan_skip(f, FA, "manual_restrict")      # Pause ack lands between admission and the wire
            return attempt
        with patch.object(_Ledger, "begin_attempt", begin_then_pause):
            says(f, FA, "a, paused at the wire")
            await until(lambda: kinds("turn-requeued"), "wire re-check refusal", events)
        await settle()
        check(not posted(http, FA), "no POST after the Pause ack even when it lands at the wire")
        check(not kinds("stopped"), "not a stranded delivery-unknown")
        check(f.sql("SELECT state FROM native_pilots WHERE pilot_id=?", (f"svc-{A}-{FA}",))[0][0] == "active")
        check(f.sql("SELECT state FROM native_pilot_receipts")[0][0] == "not_sent")
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 4, "the unit was refunded")
        says(f, FB, "b unaffected")
        await until(lambda: len(posted(http, FB)) == 1, "B still answered", events)
        backend.fan_skip(f, FA, None)                            # the Pause is lifted
        await until(lambda: len(posted(http, FA)) == 1, "the held message is answered after the Pause", events)
        await settle()
        check(len(posted(http, FA)) == 1 and f.sql("SELECT count(*) FROM messages WHERE direction='out' AND fan_id=?", (FA,))[0][0] == 1)
        await stop(task, patcher)

        # A provider 429 cools down the account connection; both fans resume without dropping A.
        f, http, client = make("multi-429", limit=100, used=4, throttle=1)
        events.clear()
        http.codes_for[FA] = [429]
        task, patcher = await start(client)
        says(f, FA, "a, rate limited")
        says(f, FB, "b meanwhile")
        await until(lambda: len(posted(http, FA)) == 2 and len(posted(http, FB)) == 1, "A retried after B", events)
        await settle()
        check(sorted(p[0] for p in http.posts) == sorted([FA, FA, FB]), "both fans recover after the shared cooldown")
        check(kinds("turn-requeued") and kinds("turn-requeued")[0]["fan_id"] == FA and not kinds("stopped"))
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6, "the refused attempt's unit was refunded")
        check(sorted(r[0] for r in f.sql("SELECT state FROM native_pilot_receipts WHERE pilot_id=?", (f"svc-{A}-{FA}",))) == ["not_sent", "sent"])
        check(f.sql("SELECT count(*) FROM messages WHERE direction='out' AND fan_id=?", (FA,))[0][0] == 1)
        # a model failure: requeued once for that fan, the other fan is unaffected
        FakeEngine.fail_for = {FA: 1}
        says(f, FA, "a again")
        await until(lambda: len(kinds("turn-requeued")) == 2, "model failure requeued", events)
        await until(lambda: len(posted(http, FA)) == 3, "A answered after the model retry", events)
        check([e["reason"] for e in kinds("turn-requeued")] == ["provider-rate-limited", "model-timeout"])
        FakeEngine.fail_for = {}
        await stop(task, patcher)
        # The wildcard account plan reaches both children; reviewed catalogue state is
        # enabled as one account operation and restored once, preserving mixed prior states.
        f, http, client = make("multi-content", limit=100, used=4, throttle=1)
        f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,"
              "preview_media_ids TEXT,price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER,pricing_mode TEXT)")
        f.sql("INSERT INTO catalog_items VALUES(71,?,'video','[501]','[]',300,0,0,'fixed')", (A,))
        f.sql("INSERT INTO catalog_items VALUES(72,?,'video','[502]','[]',400,0,1,'fixed')", (A,))
        manifest = root / "multi-content-plan.json"
        manifest.write_text(json.dumps({"account_id": A, "fan_id": "*", "price_range_cents": [300, 500], "items": [
            {"catalog_id": item, "kind": "video", "price_cents": price,
             "media": [{"id": media, "type": "video"}], "preview_ids": []}
            for item, media, price in [(71, 501, 300), (72, 502, 400)]
        ]}))
        events.clear()
        with patch.object(args, "content_manifest", str(manifest)):
            task, patcher = await start(client)
            says(f, FA)
            says(f, FB)
            await until(lambda: len(http.posts) == 2, "both fans served with the account content plan", events)
            await stop(task, patcher)
        check([(row[0], bool(row[1])) for row in f.sql("SELECT id,enabled FROM catalog_items ORDER BY id")]
              == [(71, False), (72, True)], "all reviewed catalogue states restored exactly")

        # ── D1a: the activation watermark is persisted, so a restart never drops an input that
        #    arrived after activation; pre-activation backlog and answered inputs are never replayed ──
        f, http, client = make("multi-restart", limit=100, used=4, throttle=1)
        says(f, FA, "arrived before activation")
        clock.add(5)
        events.clear()
        task, patcher = await start(client)
        first = f.sql("SELECT cutoff FROM native_pilots")
        check(len(first) == 1, "the activation instant is persisted once, as one marker row")
        await stop(task, patcher)
        says(f, FB, "arrived after activation, while the process was down")
        clock.add(5)
        task, patcher = await start(client)
        await until(lambda: len(posted(http, FB)) == 1, "input between activation and restart is answered", events,
                    timeout=6)
        await settle()
        check(not posted(http, FA), "pre-activation backlog is never replayed")
        check(f.sql("SELECT cutoff FROM native_pilots WHERE fan_id=0")[0][0] == first[0][0],
              "a restart reuses the persisted activation instant")
        await stop(task, patcher)
        task, patcher = await start(client)
        await settle()
        check(len(posted(http, FB)) == 1 and not posted(http, FA), "a third start replays nothing that was answered")
        await stop(task, patcher)

        # ── D1b: ineligible newer rows never starve an older eligible input (window patched small) ──
        f, http, client = make("multi-fair", limit=100, used=4, throttle=1)
        extra = [L.FAN + 100 + n for n in range(10)]
        for n, fan in enumerate(extra):
            backend.add_fan(f, fan)
            if n >= 5:
                OWNED.add(fan)
                backend.fan_state(f, fan, "paused")       # owned but operator-paused
        events.clear()
        with patch.object(service, "CANDIDATES_MAX", 4):
            task, patcher = await start(client)
            says(f, FB, "oldest eligible")
            for fan in extra:                              # five non-owned, five paused: all newer than FB
                says(f, fan, "newer but must not be answered")
            says(f, FA, "newest eligible")
            await until(lambda: len(posted(http, FB)) == 1 and len(posted(http, FA)) == 1,
                        "eligible inputs behind more than CANDIDATES_MAX ineligible rows are served", events, timeout=6)
            check([p[0] for p in http.posts[:2]] == [FB, FA], "oldest pending first")
            check(all(not posted(http, fan) for fan in extra), "ineligible fans are never answered")
            await stop(task, patcher)
        OWNED.difference_update(extra)

        # ── D1c: fans held by an unknown send never freeze the others (window patched small) ──
        f, http, client = make("multi-held", limit=100, used=4, throttle=1)
        held_fans = [L.FAN + 200 + n for n in range(3)]
        for fan in held_fans:
            backend.add_fan(f, fan)
            backend.fan_state(f, fan, "automated")
            OWNED.add(fan)
            http.fail_for[fan] = ProxyError("synthetic transport failure")
        events.clear()
        with patch.object(service, "CANDIDATES_MAX", 2):
            task, patcher = await start(client)
            for fan in held_fans:
                says(f, fan, "first")
                await until(lambda fan=fan: len(posted(http, fan)) == 1, "unknown send", events, timeout=6)
            for fan in held_fans:
                says(f, fan, "second, held behind the unknown send")
            says(f, FB, "fresh fan behind three held fans")
            await until(lambda: len(posted(http, FB)) == 1, "a held fan never freezes the others", events, timeout=6)
            check(all(len(posted(http, fan)) == 1 for fan in held_fans), "held fans are never resent")
            await stop(task, patcher)
        OWNED.difference_update(held_fans)
    from db.engine import engine
    await engine.dispose()


# ── follow-ups (--followups): FASTT's own nudge, ledger and supervised loop ──────────────

def followup_ledger_level(backend, root):
    from native_pilot.ledger import Ledger, Refused, FOLLOWUP_MARK
    L = backend.L
    f = backend.fixture(root, "fu-ledger")
    backend.prepare(f)
    backend.policy(f, limit=50, used=4, throttle=1)
    led = Ledger(f.path, allowance=True)
    A, F, TOK, PID, T0 = L.ACCT, L.FAN, "synthetic-owner-fu-0001", "svc-synthetic-fu", L.NOW
    p = led.activate_service(pilot_id=PID, account_id=A, fan_id=F, owner_token=TOK, now=T0, ttl_s=3000)
    call = dict(pilot_id=PID, owner_token=TOK)

    def at(sec):
        return (T0 + timedelta(seconds=sec)).isoformat(sep=" ")

    def out(mid, sec, kind="ai_chatter"):
        f.source(mid, direction="out", body="the card", created_at=at(sec), ingested_at=at(sec))
        f.sql("UPDATE messages SET automation_kind=? WHERE message_id=?", (kind, mid))

    def job(sec_run=900, now_sec=10):
        return led.followup_add(account_id=A, fan_id=F, kind="nudge", payload={"nudge_fan_ids": [F]},
                                run_at=T0 + timedelta(seconds=sec_run), now=T0 + timedelta(seconds=now_sec))

    f.source(100, created_at=at(1), ingested_at=at(2))                 # the fan's message
    out(101, 5)                                                         # our card (the anchor)
    refuses(L, "followup-kind", lambda: led.followup_add(account_id=A, fan_id=F, kind="aftercare", payload={}, run_at=T0, now=T0))
    refuses(L, "followup-offer", lambda: led.followup_add(account_id=A, fan_id=F, kind="post_buy", payload={}, run_at=T0, now=T0))
    refuses(L, "followup-run-at", lambda: job(sec_run=3 * 86400))
    j1 = job()
    row = f.sql("SELECT kind,state,last_in_id,run_at FROM native_pilot_followups")[0]
    check(row[0] == "nudge" and row[1] == "queued" and row[2] == 100)
    check(led.followups_due(account_id=A, now=T0 + timedelta(seconds=100)) == [], "not due yet")
    refuses(L, "followup-not-due", lambda: led.claim_followup(**call, job_id=j1, now=T0 + timedelta(seconds=100)))
    j2 = job(sec_run=950, now_sec=20)
    check(j2 != j1 and f.sql("SELECT state,reason FROM native_pilot_followups WHERE job_id=?", (j1,))[0] == ("dropped", "superseded"),
          "a newer nudge supersedes the queued one")
    check([d["job_id"] for d in led.followups_due(account_id=A, now=T0 + timedelta(seconds=1000))] == [j2])
    check(led.followups_due(account_id=A, now=T0 + timedelta(seconds=1000), fan_id=F + 1) == [])
    refuses(L, "followup-not-queued", lambda: led.claim_followup(**call, job_id=j1, now=T0 + timedelta(seconds=1000)))
    # A claim is exactly one input row for the anchor; begin_attempt proves the anchor again and spends the allowance.
    key, anchor = led.claim_followup(**call, job_id=j2, now=T0 + timedelta(seconds=1000))
    check(anchor["message_id"] == 101)
    check(f.sql("SELECT state FROM native_pilot_followups WHERE job_id=?", (j2,))[0][0] == "claimed")
    check(f.sql("SELECT source_hash,message_id,state FROM native_pilot_inputs WHERE input_key=?", (key,))[0][0].startswith(FOLLOWUP_MARK))
    refuses(L, "followup-not-queued", lambda: led.claim_followup(**call, job_id=j2, now=T0 + timedelta(seconds=1000)))
    a = led.begin_attempt(**call, input_key=key, bubble_no=1, revision=p.revision, now=T0 + timedelta(seconds=1001))
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 5, "the nudge spends one allowance unit like any send")
    led.record_sent(**call, attempt_id=a.id, provider_message_id="102", validated_response={"id": 102, "text": "nudge"},
                    evidence={"verified": True}, now=T0 + timedelta(seconds=1001))
    check(led.followup_finish(job_id=j2, state="done", reason=None, now=T0 + timedelta(seconds=1002)) is True)
    check(led.followup_finish(job_id=j2, state="failed", reason="x", now=T0 + timedelta(seconds=1003)) is False
          and f.sql("SELECT state FROM native_pilot_followups WHERE job_id=?", (j2,))[0][0] == "done", "a settled job stays settled")
    led.finish_input(**call, input_key=key, now=T0 + timedelta(seconds=1002))
    # One anchor is claimed at most once, whatever job points at it.
    f.source(102, direction="out", body="nudge", created_at=at(1001), ingested_at=at(1001))
    f.sql("UPDATE messages SET automation_kind='ai_chatter' WHERE message_id=102")
    j3 = job(sec_run=1000, now_sec=1010)
    key3, anchor3 = led.claim_followup(**call, job_id=j3, now=T0 + timedelta(seconds=2000))
    check(anchor3["message_id"] == 102, "a follow-up of a follow-up anchors on the newest message")
    led.followup_finish(job_id=j3, state="dropped", reason="test", now=T0 + timedelta(seconds=2001))
    led.finish_input(**call, input_key=key3, now=T0 + timedelta(seconds=2001))
    j4 = job(sec_run=1000, now_sec=2002)
    refuses(L, "input-already-claimed", lambda: led.claim_followup(**call, job_id=j4, now=T0 + timedelta(seconds=2100)))
    led.followup_finish(job_id=j4, state="dropped", reason="test", now=T0 + timedelta(seconds=2101))
    # The chat moved: each of these drops the job (job-specific reasons), and nothing is ever claimed.
    n_inputs = f.sql("SELECT count(*) FROM native_pilot_inputs")[0][0]
    j5 = job(sec_run=1000, now_sec=2150)                                     # queued behind our card (101/102)
    f.source(110, created_at=at(2200), ingested_at=at(2200))                 # the fan replied
    refuses(L, "followup-chat-moved", lambda: led.claim_followup(**call, job_id=j5, now=T0 + timedelta(seconds=2300)))
    out(111, 2250)                                                           # we answered: the anchor is ours again,
    refuses(L, "followup-fan-replied", lambda: led.claim_followup(**call, job_id=j5, now=T0 + timedelta(seconds=2300)))
    #                                                                         ...but the fan spoke after the job was queued
    j6 = job(sec_run=2300, now_sec=2260)                                     # queued behind that reply: valid again
    out(112, 2290, kind=None)                                                # a human typed in the meantime
    refuses(L, "followup-chat-moved", lambda: led.claim_followup(**call, job_id=j6, now=T0 + timedelta(seconds=2400)))
    f.sql("UPDATE messages SET automation_kind='ai_chatter' WHERE message_id=112")
    f.sql("UPDATE messages SET is_unsent=1 WHERE message_id=112")            # ...and unsent it: our card is the newest again
    j7 = job(sec_run=2300, now_sec=2261)
    far = (T0 + timedelta(days=2)).isoformat(sep=" ")
    f.sql("UPDATE native_pilots SET deadline=?", (far,))
    f.sql("UPDATE twin_bridge_conversations SET owner_expires_at=?", (far,))
    refuses(L, "followup-expired", lambda: led.claim_followup(**call, job_id=j7, now=T0 + timedelta(hours=7)))
    check(f.sql("SELECT count(*) FROM native_pilot_inputs")[0][0] == n_inputs, "no refused claim leaves an input row")
    # begin_attempt re-proves the anchor at the wire: a fan message arriving after the claim refuses the send.
    f.sql("UPDATE native_pilot_followups SET state='dropped' WHERE state='queued'")
    j8 = job(sec_run=2300, now_sec=2262)
    key8, anchor8 = led.claim_followup(**call, job_id=j8, now=T0 + timedelta(seconds=2400))
    used = f.sql("SELECT used_units FROM of_delivery_policy")[0][0]
    f.source(120, created_at=at(2401), ingested_at=at(2401))
    refuses(L, "source-changed", lambda: led.begin_attempt(**call, input_key=key8, bubble_no=1, revision=p.revision,
                                                           now=T0 + timedelta(seconds=2402)))
    check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == used, "a refused follow-up admission spends nothing")


async def followup_loop_level(backend, root):
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.dotenv_values = lambda *a, **k: {"OPENROUTER_API_KEY": "synthetic-unused-key"}
    L = backend.L
    import copy
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    import test_native_pilot_runner as R
    from curl_cffi.requests.exceptions import ProxyError
    from native_pilot import attribution, connection as conn_mod, engine as engine_mod, runner, service
    from native_pilot.attribution import Sentinels
    from native_pilot.scope import AccountScope
    from of_client import OFClient

    logging.disable(logging.CRITICAL)
    A = L.ACCT
    FA, FB, FC, FD, FE = (L.FAN + n for n in range(5))
    OWNED = {FA, FB, FC, FD, FE}
    events = []
    clock = Clock(datetime.now(timezone.utc).replace(tzinfo=None, microsecond=123000))
    RealEngine = engine_mod.NativeEngine
    NUDGE = "hey are you still there"

    async def no_report(ledger, pilot, run_error=None):
        pass

    async def no_ensure(scope, fan):
        pass

    RENEW = 1799.0

    async def fast_idle(self, seconds):
        if seconds != RENEW:      # the lease renewals do not advance virtual time (keeps the scenarios arrangeable)
            clock.add(seconds)
        try:
            await asyncio.wait_for(self.stop.wait(), 0.02 if backend.pg else 0.002)
        except asyncio.TimeoutError:
            pass

    class LiveHTTP:
        def __init__(self, f):
            self.f, self.sent, self.posts, self.fail_for, self.closed = f, [], [], {}, False

        @staticmethod
        def fan_of(url):
            return int(urlsplit(url).path.split("/")[-2])

        def get(self, url, **kwargs):
            fan = self.fan_of(url)
            rows = self.f.sql("SELECT message_id,body,created_at FROM messages WHERE direction='in' AND fan_id=?", (fan,))
            inbound = [{"id": mid, "text": body, "createdAt": str(created).replace(" ", "T") + "Z", "isFromMe": False,
                        "fromUser": {"id": fan}, "withUser": {"id": fan}} for mid, body, created in rows]
            history = sorted([m for m in self.sent if m["withUser"]["id"] == fan] + inbound,
                             key=lambda m: (m["createdAt"], m["id"]), reverse=True)
            return R.FakeResponse({"list": copy.deepcopy(history), "hasMore": False})

        def request(self, method, url, json=None, **kwargs):
            fan = self.fan_of(url)
            self.posts.append((fan, json["text"]))
            if fan in self.fail_for:
                raise self.fail_for[fan]
            result = {"id": 7000 + len(self.posts), "text": json["text"], "isFromMe": True, "fromUser": {"id": int(A)},
                      "withUser": {"id": fan}, "createdAt": clock().isoformat() + "Z", "price": 0, "media": [],
                      "mediaCount": 0, "responseType": "message", "previews": [], "lockedText": False}
            self.sent.append(result)
            return R.FakeResponse(result)

        def close(self):
            self.closed = True

    class FakeIntake:
        def __init__(self, scope, fan, path):
            pass

        def start(self, client):
            FakeIntake.up = True

        async def stop(self):
            pass

    holder = {"offer": True, "engines": []}

    class FakeEngine:
        """ai_chatter stand-in: a normal turn replies with a card (open offer) and schedules FASTT's nudge job
        exactly as ai_chatter does (the REAL NativeEngine.enqueue_job decides what is kept); a job turn is the nudge."""
        _account = staticmethod(lambda account_id: None)

        def __init__(self, account, fan, *, chat, send, emit, content, context, own_offers_only=False, followups=None, **_extra):
            self.send, self.fan, self.fan_id, self.account_id = send, fan, fan, account
            self.followups, self._emit = followups, emit
            holder["engines"].append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        enqueue_job = RealEngine.enqueue_job

        async def run(self, run_id, payload=None):
            f = holder["f"]
            if payload is not None:
                holder.setdefault("payloads", []).append(payload)
            clock.add(10)      # typing time: a reply never shares a millisecond with the message it answers
            raw = await asyncio.to_thread(self.send, self.fan, NUDGE if payload is not None else "neutral reply", {})
            f.source(raw["id"], fan_id=self.fan, direction="out", body=raw["text"],
                     created_at=raw["createdAt"].removesuffix("Z").replace("T", " "))
            f.sql("UPDATE messages SET automation_kind='ai_chatter',sent_by_employee_id=5 WHERE message_id=?", (raw["id"],))
            if payload is None and holder["offer"]:
                f.sql("INSERT INTO content_offers VALUES(?,?,'open','ppv',71)", (A, self.fan))
                if holder.get("vault"):
                    # --vault-mode: the card came from the ask lane, which schedules no nudge of its own; the REAL
                    # engine hook queues it after the turn (state a priced card + FASTT's delay put on the engine).
                    self._card_sent, self._nudge_queued, self._nudge_min = True, False, holder["delay_min"]
                    self.content = SimpleNamespace(vault_mode=True)
                    await RealEngine._queue_card_nudge(self)
                else:
                    await self.enqueue_job(A, "ai_chatter", payload={"nudge_fan_ids": [self.fan]},
                                           run_at=clock() + timedelta(minutes=holder["delay_min"]))
                await self.enqueue_job(A, "ai_chatter", payload={"post_buy": 1}, run_at=clock())
            return {"status": "ok"}

    def make(name, **policy):
        f = backend.fixture(root, name)
        backend.prepare(f)
        backend.policy(f, **{"limit": 100, "used": 4, "throttle": 1, **policy})
        f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,"
              "preview_media_ids TEXT,price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER,pricing_mode TEXT)")
        f.sql("INSERT INTO catalog_items VALUES(71,?,'video','[501]','[]',300,0,1,'fixed')", (A,))
        for fan in (FB, FC, FD, FE):
            backend.add_fan(f, fan)
        for fan in OWNED:
            backend.fan_state(f, fan, "automated")
        holder["f"], holder["payloads"], holder["engines"] = f, [], []
        holder["offer"], holder["delay_min"] = True, 15
        http = LiveHTTP(f)
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = A, A, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        client.me = lambda: {"id": int(A), "username": "synthetic-creator"}
        return f, http, client

    fake_conn = conn_mod.PilotConnection(
        scope=AccountScope.parse(A), tenant_id="synthetic-tenant", revision=3, owner_id=int(A),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30), capture={}, x_hash="x", proxy_url=None,
        model_id="openrouter/deepseek-v4.1-flash", model_api_key="synthetic-key", daily_cost_cap_cents=100)

    async def fake_load(scope):
        return fake_conn

    async def fake_sentinels():
        return Sentinels(automation=5, human=6)

    manifest = root / "fu-plan.json"

    def write_manifest(fan):
        manifest.write_text(json.dumps({"account_id": A, "fan_id": fan, "price_range_cents": [300, 500], "items": [
            {"catalog_id": 71, "kind": "video", "price_cents": 300, "media": [{"id": 501, "type": "video"}], "preview_ids": []}]}))

    def make_args(all_fans, followups):
        return argparse.Namespace(account=A, fan=None if all_fans else FA, all_fans=all_fans, handle="synthetic-creator",
                                  enable_send=True, twin_context=False, content_manifest=str(manifest), service_id=None,
                                  followups=followups)
    ids = iter(range(5000, 9000))

    def says(f, fan, text="neutral fan text"):
        clock.add(1)
        mid = next(ids)
        f.source(mid, fan_id=fan, body=text, created_at=clock().isoformat(sep=" "), ingested_at=clock().isoformat(sep=" "))
        return mid

    def posted(http, fan):
        return [p for p in http.posts if p[0] == fan]

    def kinds(name):
        return [e for e in events if e.get("event") == name]

    def jobs(f, fan=None):
        return f.sql("SELECT state,reason FROM native_pilot_followups" + (" WHERE fan_id=%d" % fan if fan else "") + " ORDER BY created_at")

    async def settle(n=80):
        for _ in range(n):
            await asyncio.sleep(0.003)

    with patch.object(runner, "emit", events.append), patch.object(runner, "now", clock), \
            patch.object(runner, "report_to_twin", no_report), patch.object(service, "ensure_fan_conversation", no_ensure), \
            patch.object(service.Service, "idle", fast_idle), patch.object(engine_mod, "NativeEngine", FakeEngine), \
            patch.object(service.Service, "_owned", lambda self, fan: fan in OWNED), \
            patch.object(conn_mod, "load_verified_connection", fake_load), patch.object(attribution, "require_sentinels", fake_sentinels):

        async def start(client, args):
            patcher = patch.object(conn_mod, "build_client", return_value=client)
            patcher.start()
            # Virtual time runs fast while idle (every renew loop advances the shared clock): long leases keep it stable.
            options = dict(ttl_s=3600, renew_s=RENEW, poll_s=2, hold_s=15, quiet_s=0, intake_factory=FakeIntake)
            FakeIntake.up = False
            task = asyncio.get_running_loop().create_task(service.serve(args, **options))
            await until(lambda: FakeIntake.up and (args.all_fans or kinds("ready")), "intake up", events)
            return task, patcher

        async def stop(task, patcher):
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 20)
            patcher.stop()

        def used(f):
            return f.sql("SELECT used_units FROM of_delivery_policy")[0][0]

        for all_fans in ((False,) if os.environ.get('FU_SINGLE') else (True, False)):
            tag = "all-fans" if all_fans else "single-fan"
            write_manifest("*" if all_fans else FA)

            # ── flag off: the card turn runs, the nudge job is suppressed, nothing is queued or sent later ──
            f, http, client = make("fu-off-" + tag)
            events.clear()
            task, patcher = await start(client, make_args(all_fans, False))
            says(f, FA)
            await until(lambda: len(posted(http, FA)) == 1, tag + " off: card reply", events)
            clock.add(20 * 60)
            await settle()
            check(len(posted(http, FA)) == 1 and not kinds("native-followup-queued"), tag + " off: no nudge, nothing queued")
            check(f.sql("SELECT count(*) FROM native_pilot_followups")[0][0] == 0, tag + " off: no follow-up rows")
            check(all(e.followups is None for e in holder["engines"]), tag + " off: the engine gets no queue")
            await stop(task, patcher)

            # ── flag on: queued after the reply, run when due through the boundary, counted against the cap ──
            f, http, client = make("fu-on-" + tag)
            events.clear()
            task, patcher = await start(client, make_args(all_fans, True))
            says(f, FA)
            await until(lambda: len(posted(http, FA)) == 1, tag + ": card reply", events)
            await until(lambda: len(kinds("native-background-job-suppressed")) == 1 and jobs(f, FA), tag + ": job queued", events)
            check(jobs(f, FA) == [("queued", None)] and len(kinds("native-followup-queued")) == 1, tag + ": exactly the nudge job is queued")
            check(len(kinds("native-background-job-suppressed")) == 1, tag + ": the post-buy job is suppressed")
            check(used(f) == 5 and not f.sql("SELECT 1 FROM scheduled_jobs"), tag + ": nothing in scheduled_jobs")
            clock.add(16 * 60)           # past nudge_after_minutes (no wait for idle ticks: PostgreSQL ticks are slow)
            await until(lambda: len(posted(http, FA)) == 2, tag + ": nudge sent when due", events, timeout=60)
            await until(lambda: jobs(f, FA) == [("done", None)], tag + ": job settled", events)
            check(posted(http, FA)[1][1] == "<p>%s</p>" % NUDGE and holder["payloads"] == [{"nudge_fan_ids": [FA]}], tag + ": FASTT's nudge payload")
            check(used(f) == 6, tag + ": the nudge spent one allowance unit")
            check(f.sql("SELECT count(*) FROM native_pilot_receipts WHERE state='sent'")[0][0] == 2)
            check(kinds("followup-done") and not kinds("followup-dropped"), tag + ": done event")
            await settle()
            check(len(posted(http, FA)) == 2 and f.sql("SELECT count(*) FROM native_pilot_followups")[0][0] == 1,
                  tag + ": one attempt, never replayed")
            # He answers the nudge: the normal path takes over (and queues the next nudge behind that card).
            says(f, FA, "oh hey")
            await until(lambda: len(posted(http, FA)) == 3, tag + ": normal reply after the nudge", events)
            await until(lambda: jobs(f, FA)[-1] == ("queued", None), tag + ": next nudge queued behind the new card", events)
            await stop(task, patcher)

            # ── --vault-mode cards: the engine hook queues the nudge; due -> sent; replied / bought -> dropped ──
            holder["vault"] = True
            f, http, client = make("fu-vault-" + tag)
            events.clear()
            task, patcher = await start(client, make_args(all_fans, True))
            says(f, FA)
            await until(lambda: len(posted(http, FA)) == 1 and jobs(f, FA) and kinds("native-followup-queued"), tag + " vault: card reply", events)
            check(jobs(f, FA) == [("queued", None)] and len(kinds("native-followup-queued")) == 1, tag + " vault: the card queued the nudge")
            clock.add(16 * 60)
            await until(lambda: len(posted(http, FA)) == 2, tag + " vault: nudge sent when due", events, timeout=60)
            await until(lambda: jobs(f, FA) == [("done", None)], tag + " vault: job settled", events)
            check(posted(http, FA)[1][1] == "<p>%s</p>" % NUDGE and holder["payloads"] == [{"nudge_fan_ids": [FA]}], tag + " vault: stock payload")
            check(used(f) == 6 and not f.sql("SELECT 1 FROM scheduled_jobs"), tag + " vault: one allowance unit, no scheduled_jobs")
            await stop(task, patcher)
            f, http, client = make("fu-vault-drop-" + tag)
            holder["delay_min"] = 180
            events.clear()
            task, patcher = await start(client, make_args(all_fans, True))
            fans = [FA, FB] if all_fans else [FA]
            for fan in fans:
                says(f, fan)
                await until(lambda fan=fan: len(posted(http, fan)) == 1 and jobs(f, fan), tag + " vault: reply", events)
            holder["offer"] = False
            if all_fans:
                f.sql("UPDATE content_offers SET status='delivered' WHERE fan_id=?", (FB,))   # bought
            says(f, FA, "wait one more thing")                                                 # replied first
            f.sql("UPDATE native_pilot_followups SET run_at=? WHERE state='queued'",
                  ((clock() - timedelta(minutes=1)).isoformat(sep=" ", timespec="microseconds"),))
            await until(lambda: all(jobs(f, fan)[-1][0] == "dropped" for fan in fans), tag + " vault: stale jobs dropped", events, timeout=30)
            check(jobs(f, FA)[-1][1] in ("followup-fan-replied", "followup-chat-moved")
                  and (not all_fans or jobs(f, FB)[-1][1] == "offer-resolved"), tag + " vault: dropped reasons")
            check(all(NUDGE not in text for _, text in http.posts) and holder["payloads"] == [], tag + " vault: no nudge on the wire")
            await stop(task, patcher)
            holder["vault"], holder["delay_min"] = False, 15

            # ── dropped: the fan replied first / bought / paused / chat held / unreadable chat ──
            f, http, client = make("fu-drop-" + tag)
            holder["delay_min"] = 180        # virtual time passes fast while idle: leave room to arrange each case
            events.clear()
            task, patcher = await start(client, make_args(all_fans, True))
            fans = [FA] if not all_fans else [FA, FB, FC, FD]
            for fan in fans:
                says(f, fan)
                await until(lambda fan=fan: len(posted(http, fan)) == 1 and jobs(f, fan), tag + ": reply", events)
            check(all(jobs(f, fan) == [("queued", None)] for fan in fans))
            holder["offer"] = False
            if all_fans:
                backend.fan_state(f, FC, "human_controlled")                       # taken over / paused
                backend.fan_skip(f, FD, "manual_restrict")                         # chat held
                f.sql("UPDATE content_offers SET status='delivered' WHERE fan_id=?", (FB,))   # bought
                backend.fan_state(f, FE, "automated")
            says(f, FA, "wait one more thing")                                     # replied first (answered, no new job)
            f.sql("UPDATE native_pilot_followups SET run_at=? WHERE state='queued'",
                  ((clock() - timedelta(minutes=1)).isoformat(sep=" ", timespec="microseconds"),))     # every job is due now
            await until(lambda: all(jobs(f, fan)[-1][0] == "dropped" for fan in fans), tag + ": every stale job dropped", events, timeout=30)
            await settle()
            reasons = {fan: jobs(f, fan)[-1][1] for fan in fans}
            check(reasons[FA] in ("followup-fan-replied", "followup-chat-moved"), reasons)
            if all_fans:
                check(reasons[FB] == "offer-resolved" and reasons[FC] == "fan-human-controlled"
                      and reasons[FD] == "fan-restricted", reasons)
            check(all(NUDGE not in text for _, text in http.posts), tag + ": no nudge reached the wire")
            check(len(posted(http, FA)) == 2 and holder["payloads"] == [], tag + ": the dropped jobs never reached the engine")
            await stop(task, patcher)

            # ── the send cap holds a due nudge; it goes out once the allowance allows, still one unit ──
            f, http, client = make("fu-cap-" + tag, limit=5, used=4)
            events.clear()
            task, patcher = await start(client, make_args(all_fans, True))
            says(f, FA)
            await until(lambda: len(posted(http, FA)) == 1 and used(f) == 5 and jobs(f, FA), tag + ": reply spent the last unit", events)
            clock.add(16 * 60)
            await until(lambda: any(str(e.get("reason", "")).startswith("allowance-") for e in kinds("held")), tag + ": cap holds", events, timeout=30)
            await settle()
            check(len(posted(http, FA)) == 1 and used(f) == 5 and jobs(f, FA) == [("queued", None)], tag + ": nudge waits behind the cap")
            backend.policy(f, limit=10, used=5, throttle=1)
            await until(lambda: len(posted(http, FA)) == 2, tag + ": nudge sent after the cap is raised", events, timeout=30)
            check(used(f) == 6, tag + ": counted against the cap")
            await stop(task, patcher)

            # ── an unknown outcome on the nudge holds the fan and is never resent; a stale job is never sent late ──
            f, http, client = make("fu-unknown-" + tag)
            events.clear()
            task, patcher = await start(client, make_args(all_fans, True))
            says(f, FA)
            await until(lambda: len(posted(http, FA)) == 1, tag + ": reply", events)
            await until(lambda: jobs(f, FA) == [("queued", None)], tag + ": job queued", events)
            http.fail_for[FA] = ProxyError("synthetic transport failure")
            clock.add(16 * 60)
            await until(lambda: len(posted(http, FA)) == 2, tag + ": the one nudge attempt", events, timeout=30)
            await until(lambda: jobs(f, FA)[-1][0] in ("failed", "dropped") and f.sql("SELECT state FROM native_pilot_receipts WHERE state='delivery_unknown'"),
                        tag + ": unknown outcome recorded", events)
            http.fail_for.clear()
            await settle(150)
            check(len(posted(http, FA)) == 2, tag + ": never resent")
            check(f.sql("SELECT state FROM native_pilots WHERE pilot_id LIKE 'svc-%' AND fan_id>0")[0][0] == "stopped", tag + ": fan held")
            await stop(task, patcher)
            f, http, client = make("fu-expired-" + tag)
            events.clear()
            holder["delay_min"] = 180
            task, patcher = await start(client, make_args(all_fans, True))
            says(f, FA)
            await until(lambda: len(posted(http, FA)) == 1, tag + ": reply", events)
            await until(lambda: jobs(f, FA) == [("queued", None)], tag + ": job queued", events)
            # The job's time is further in the past than any live process would leave it (downtime).
            f.sql("UPDATE native_pilot_followups SET run_at=?", ((clock() - timedelta(hours=7)).isoformat(sep=" ", timespec="microseconds"),))
            await until(lambda: jobs(f, FA)[-1][0] == "dropped", tag + ": stale job dropped", events, timeout=30)
            check(jobs(f, FA)[-1][1] == "followup-expired" and len(posted(http, FA)) == 1, tag + ": never sent late")
            await stop(task, patcher)

    # The legacy worker/executor and the dashboard never touch the runner-owned queue.
    import re
    for path in Path(__file__).resolve().parents[1].rglob("*.py"):
        rel = path.relative_to(Path(__file__).resolve().parents[1]).parts
        if rel[0] in ("tests", "native_pilot", "db", "twin_bridge") or path.name.startswith("test_"):
            continue
        check("native_pilot_followups" not in path.read_text(errors="ignore"), "queue is not referenced by " + str(path))
    from db.engine import engine
    await engine.dispose()


def main():
    import test_native_pilot_ledger as L
    backend = SqliteBackend(L)
    with tempfile.TemporaryDirectory(prefix="native-pilot-service-") as temporary:
        root = Path(temporary)
        os.environ["DATABASE_URL"] = L.database_url(root / "bootstrap.db")
        os.environ["TWIN_BRIDGE_ENABLED"] = "0"
        os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"
        ledger_level(backend, root)
        print("ledger level ok", CHECKS)
        asyncio.run(loop_level(backend, root))
        print("loop level ok", CHECKS)
        asyncio.run(intake_level(backend, root))
        asyncio.run(multi_level(backend, root))
        asyncio.run(multi_intake_level(backend, root))
        followup_ledger_level(backend, root)
        asyncio.run(followup_loop_level(backend, root))
    print(f"native pilot service: all cases passed ({CHECKS} checks)")


if __name__ == "__main__":
    main()
