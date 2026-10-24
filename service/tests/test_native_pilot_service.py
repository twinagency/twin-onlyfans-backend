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
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    a3 = led2.begin_attempt(**call2, input_key=key2, bubble_no=1, revision=p2.revision, now=t3)  # crash after this
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
    f.sql("UPDATE twin_bridge_send_attempts SET state='failed' WHERE state='delivery_unknown'")  # ... and resolves it
    used_before = f.sql("SELECT used_units FROM of_delivery_policy")[0][0]
    p3 = led3.activate_service(pilot_id=PID, account_id=A, fan_id=F, owner_token="synthetic-owner-svc-0003", now=t4, ttl_s=120)
    check(p3.state == "active" and f.sql("SELECT state FROM native_pilot_receipts WHERE attempt_id=?", (a3.id,))[0][0] == "delivery_unknown")
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
    A, F = L.ACCT, L.FAN
    events = []
    clock = Clock(datetime.now(timezone.utc).replace(tzinfo=None, microsecond=123000))
    reports = []

    async def no_report(ledger, pilot, run_error=None):
        reports.append(run_error)

    async def no_ensure(scope, fan):
        pass

    async def fast_idle(self, seconds):
        clock.add(seconds)  # virtual time passes while the process waits
        try:
            await asyncio.wait_for(self.stop.wait(), 0.002)
        except asyncio.TimeoutError:
            pass

    class LiveHTTP(R.FakeHTTP):
        def __init__(self, f):
            super().__init__([], A, F, clock().isoformat() + "Z")
            self.f, self.sent, self.posted_at = f, [], []

        def get(self, url, **kwargs):
            rows = self.f.sql("SELECT message_id,body,created_at FROM messages WHERE direction='in' AND fan_id=?", (F,))
            inbound = [{"id": mid, "text": body, "createdAt": str(created).replace(" ", "T") + "Z", "isFromMe": False,
                        "fromUser": {"id": F}, "withUser": {"id": F}} for mid, body, created in sorted(rows, reverse=True)]
            self.history = sorted(list(self.sent) + inbound, key=lambda m: (m['createdAt'], m['id']), reverse=True)
            return super().get(url, **kwargs)

        def request(self, method, url, json=None, **kwargs):
            self.at = clock().isoformat() + "Z"
            self.posted_at.append(clock())
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

        def __init__(self, account, fan, *, chat, send, emit, content, context):
            self.send, self.fan = send, fan

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def run(self, run_id):
            f = holder["f"]
            for text in FakeEngine.texts:
                raw = await asyncio.to_thread(self.send, self.fan, text, {})
                f.source(raw["id"], direction="out", body=raw["text"], created_at=clock().isoformat(sep=" "))
                f.sql("UPDATE messages SET automation_kind='ai_chatter',sent_by_employee_id=5 WHERE message_id=?", (raw["id"],))
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
        """n POSTs AND the stored outbound rows (the engine's attribution write follows the POST)."""
        await until(lambda: len(http.posts) == n and
                    f.sql("SELECT count(*) FROM messages WHERE direction='out'")[0][0] == (n if outs is None else outs), label, events)

    def kinds(name):
        return [e for e in events if e.get("event") == name]

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
            os.kill(os.getpid(), signal.SIGTERM)
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

        # ── a stop seen at admission leaves no attempt; one seen at the wire is fail-closed and never POSTs ──
        original = service.fan_hold_reason
        for trip, label in ((1, "admission"), (2, "wire")):
            f, http, client = make("loop-" + label, limit=100, used=4, throttle=1)
            calls = {"n": 0, "armed": False}

            def stops_late(path, account, fan, calls=calls, trip=trip):
                if not calls["armed"]:
                    return original(path, account, fan)
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
                await until(lambda: kinds("turn-abandoned") or kinds("stopped"), label + " stop outcome", events)
                check(http.posts == [], label + ": nothing reached the wire")
                check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == (4 if trip == 1 else 5))
                if trip == 1:
                    check(kinds("turn-abandoned")[0]["reason"] == "fan-restricted")
                    check(f.sql("SELECT state FROM native_pilots")[0][0] == "active", "an admission-time stop is not an unknown send")
                else:
                    check(f.sql("SELECT state FROM native_pilot_receipts")[0][0] == "delivery_unknown")
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
        f.sql("UPDATE twin_bridge_send_attempts SET state='failed' WHERE state='delivery_unknown'")
        await until(lambda: kinds("ready"), "ready after operator resolution", events)
        await replied(f, http, 2, "reply to the message that arrived while held", outs=1)
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 6)
        fan_says(f, "third")
        await replied(f, http, 3, "reply to the new message", outs=2)
        check(f.sql("SELECT used_units FROM of_delivery_policy")[0][0] == 7)
        check(f.sql("SELECT count(*) FROM native_pilot_receipts WHERE state='delivery_unknown'")[0][0] == 1)
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
        clock.add(seconds)
        try:
            await asyncio.wait_for(self.stop.wait(), 0.002)
        except asyncio.TimeoutError:
            pass

    class LiveHTTP:
        """One account socket, many fans: history and POSTs are routed by the fan in the URL."""

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
            if fan in self.fail_for:
                self.posts.append((fan, json["text"]))
                raise self.fail_for[fan]
            self.posts.append((fan, json["text"]))
            result = {"id": 7000 + len(self.posts), "text": json["text"], "isFromMe": True, "fromUser": {"id": int(A)},
                      "withUser": {"id": fan}, "createdAt": clock().isoformat() + "Z", "price": 0, "media": []}
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
        def __init__(self, account, fan, *, chat, send, emit, content, context, own_offers_only=False):
            self.send, self.fan, self.content = send, fan, content
            check(own_offers_only is True, "all-fans turns scope open offers to their own fan")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def run(self, run_id):
            f = holder["f"]
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
        return sorted(r[0] for r in f.sql("SELECT pilot_id FROM native_pilots"))

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
            os.kill(os.getpid(), signal.SIGTERM)
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

        def fake_dispatch(authority, tenant, account, transport):
            if not grants:
                return None
            grant = grants.pop(0)
            try:
                transport.post_once(grant)
            except Exception as exc:
                return type(exc).__name__
            return "accepted"

        class Grant:
            def __init__(self, fan, body):
                self.fan_id, self.body = fan, body

        import onlyfans_runtime.fastt_manual as manual
        import onlyfans_runtime.store as twin_store
        import onlyfans_runtime.text_delivery as delivery
        env = {"DATABASE_URL": "postgresql://synthetic/synthetic", "ONLYFANS_WORKER_TENANT": "synthetic-tenant"}
        with patch.dict(os.environ, env), patch("twin_bridge.coordination._store_target", return_value=f.path), \
                patch.object(twin_store, "Store", FakeStore), \
                patch.object(delivery, "TextAuthority", lambda store: store), \
                patch.object(manual, "dispatch_next_manual", fake_dispatch):
            task, patcher = await start(client)
            says(f, FA, "a is chatting")
            grants.extend([Grant(FB, "human hello for B"), Grant(FF, "never sent: not FASTT-owned")])
            await until(lambda: len(posted(http, FA)) == 1 and (FB, "<p>human hello for B</p>") in http.posts,
                        "A answered and the human message for B sent", events)
            await settle()
            check(not posted(http, FF), "a queued message for a non-owned fan is never sent by this runner")
            check(("reconcile", FA) in calls and ("admit", FA) in calls, "controls run for the fan with activity")
            says(f, FB, "b answers the human")
            await until(lambda: len(posted(http, FB)) == 2, "B (now with human history) is answered too", events)
            check(("reconcile", FB) in calls)
            check(not any(c[1] in (FC, FD, FF) for c in calls), "no controls for fans without activity or not owned")
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
    print(f"native pilot service: all cases passed ({CHECKS} checks)")


if __name__ == "__main__":
    main()
