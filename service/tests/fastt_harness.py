"""Synthetic-transport harness for the FASTT engine: real Service / Ledger / SendGate / Intake / Gateway on
SQLite, a virtual clock, a fake provider and a fake model. No network, no real provider, no real fan.

What is real: native_pilot.service.Service (the all-fans runner, its concurrency pool, quiet window, work board,
runner lease, reconcile pass), native_pilot.ledger (admission, the shared account send gate, receipts), the real
runner.Gateway send path (history preflight, pre-send checks, single-attempt POST) and the real Intake dedupe.
What is simulated: the provider (``Provider``: history, POST, 429, timeouts, rate-limit audit), the model
latency / failures (``FakeEngine``) and time (``vtime``).

``Sim`` records everything the acceptance criteria are stated in: per-account reply latency, backlog age over time,
duplicate sends, cross-account sends, sends after a takeover, sends closer than the account interval.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import random
import sqlite3
import statistics
import sys
import threading
import time as _harness_time
import types
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ["TWIN_BRIDGE_ENABLED"] = "0"
os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"
os.environ.setdefault("FASTT_FAN_DAILY_REPLY_LIMIT", "2000")

import vtime  # noqa: E402

SCHEMA = """
CREATE TABLE twin_bridge_conversations(account_id TEXT,fan_id INTEGER,state TEXT,reason TEXT,
 revision INTEGER,owner_token TEXT,owner_expires_at TEXT,reserved INTEGER,
 send_budget_limit INTEGER,send_budget_used INTEGER,updated_at TEXT,PRIMARY KEY(account_id,fan_id));
CREATE TABLE twin_bridge_send_attempts(id INTEGER PRIMARY KEY,account_id TEXT,fan_id INTEGER,turn_id TEXT,
 attempt_no INTEGER,state TEXT,reason TEXT,owner_token TEXT,revision INTEGER,provider_message_id TEXT,
 evidence_json TEXT,created_at TEXT,updated_at TEXT,UNIQUE(account_id,fan_id,turn_id,attempt_no));
CREATE TABLE twin_bridge_turns(account_id TEXT,fan_id INTEGER,state TEXT);
CREATE TABLE twin_bridge_writes(account_id TEXT,fan_id INTEGER,holder TEXT,state TEXT,expires_at TEXT);
CREATE TABLE messages(account_id TEXT,fan_id INTEGER,message_id INTEGER,direction TEXT,body TEXT,
 media_ids TEXT,media_count INTEGER,price_cents INTEGER,is_tip INTEGER,is_unsent INTEGER,
 created_at TEXT,ingested_at TEXT,raw_json TEXT,automation_kind TEXT,sent_by_employee_id INTEGER,
 PRIMARY KEY(account_id,fan_id,message_id));
CREATE INDEX ix_messages_fan ON messages(account_id,fan_id,created_at);
CREATE TABLE accounts(id TEXT);
CREATE TABLE automation_rules(account_id TEXT,is_enabled INTEGER);
CREATE TABLE scheduled_jobs(account_id TEXT,status TEXT,kind TEXT);
CREATE TABLE account_ai_config(account_id TEXT,persona TEXT,chatter_config_json TEXT);
CREATE TABLE content_offers(account_id TEXT,fan_id INTEGER,status TEXT,mode TEXT,item_id INTEGER);
CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,preview_media_ids TEXT,
 price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER,pricing_mode TEXT);
CREATE TABLE pending_offer(account_id TEXT,fan_id INTEGER);
CREATE TABLE automation_runs(id INTEGER PRIMARY KEY,account_id TEXT,kind TEXT,started_at TEXT,
 completed_at TEXT,status TEXT,stats_json TEXT,error_text TEXT);
CREATE TABLE skip_list(account_id TEXT,fan_id INTEGER,reason TEXT,added_at TEXT,PRIMARY KEY(account_id,fan_id));
CREATE TABLE of_conversation_state(account_id TEXT,fan_id INTEGER,mode TEXT,takeover_state TEXT,
 PRIMARY KEY(account_id,fan_id));
CREATE TABLE of_runtime(account_id TEXT PRIMARY KEY,enabled INTEGER,binding_id TEXT);
CREATE TABLE of_creator_bindings(id TEXT PRIMARY KEY,account_id TEXT,channel TEXT,revoked_at TEXT,handle TEXT);
CREATE TABLE of_provider_connection(account_id TEXT PRIMARY KEY,binding_id TEXT,owner_id TEXT,status TEXT,verified_at TEXT,
 last_error TEXT,expires_at TEXT,revision INTEGER);
CREATE TABLE of_delivery_policy(account_id TEXT PRIMARY KEY,enabled INTEGER,limit_units INTEGER,
 used_units INTEGER,throttle_seconds INTEGER,next_dispatch_at TEXT,active_intent_id TEXT,
 revision INTEGER NOT NULL DEFAULT 0,daily_limit_units INTEGER,queued_count INTEGER NOT NULL DEFAULT 0,
 delivery_mode TEXT NOT NULL DEFAULT 'gap',provider_requests_per_second REAL NOT NULL DEFAULT 1.0,
 provider_burst INTEGER NOT NULL DEFAULT 5,provider_max_concurrent INTEGER NOT NULL DEFAULT 4,
 conversation_min_gap_seconds INTEGER NOT NULL DEFAULT 0,provider_cooldown_until TEXT);
CREATE TABLE of_delivery_policy_change(account_id TEXT,revision INTEGER,actor TEXT,reason TEXT,before_json TEXT,
 after_json TEXT,created_at TEXT,PRIMARY KEY(account_id,revision));
"""


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def stats(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": round(statistics.median(values), 1), "p95": round(pct(values, .95), 1),
            "p99": round(pct(values, .99), 1), "max": round(max(values), 1)}


class ProviderHTTPError(Exception):
    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
        self.response = types.SimpleNamespace(status_code=status, headers=headers or {})


class Provider:
    """One account's fake OnlyFans: chats, a POST that audits the rules, scripted faults."""

    def __init__(self, sim, acct):
        self.sim, self.acct = sim, acct
        self.chats: dict[int, list[dict]] = {}
        self.posts = 0
        self.last_post_at = None
        self.min_gap = None
        self.faults: list = []          # scripted answers for the next POSTs: ("429", retry_after) | ("timeout_lost",) | ("timeout_delivered",)
        self.rate_violations = 0
        self.request_times: list = []
        self.active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()
        self.stale_credential_posts = 0
        self.post_log: list[tuple] = []

    def add(self, fan, msg):
        self.chats.setdefault(fan, []).append(msg)


class FakeClient:
    def __init__(self, sim, acct, generation):
        self.sim, self.acct, self.generation = sim, acct, generation
        self.account_id, self.user_id, self.platform = acct, acct, "onlyfans"
        self.http = types.SimpleNamespace(close=lambda: None)
        self.closed = False

    def me(self):
        return {"id": int(self.acct), "username": "sim"}

    def get_messages(self, fan, limit=10, order="desc", before_id=None, **_):
        p = self.sim.providers[self.acct]
        rows = sorted(p.chats.get(int(fan), []), key=lambda m: (m["createdAt"], m["id"]), reverse=True)
        if before_id is not None:
            rows = [m for m in rows if m["id"] < before_id]
        page = rows[:limit]
        return {"list": [dict(m) for m in page], "hasMore": len(rows) > limit}

    def _post(self, fan, text, *, price=0, media=None, previews=None, locked_text=False):
        sim, p = self.sim, self.sim.providers[self.acct]
        now = sim.clock()
        with p._active_lock:
            p.active += 1
            p.max_active = max(p.max_active, p.active)
            p.request_times.append(now)
        try:
            if sim.provider_latency:
                _harness_time.sleep(sim.provider_latency)
        finally:
            with p._active_lock:
                p.active -= 1
        p.post_log.append((now, fan, text))
        if self.generation != sim.generation[self.acct]:
            p.stale_credential_posts += 1
        if sim.delivery_mode != "concurrent" and p.last_post_at is not None:
            gap = (now - p.last_post_at).total_seconds()
            p.min_gap = gap if p.min_gap is None else min(p.min_gap, gap)
            if gap < sim.min_interval - 0.001:
                p.rate_violations += 1
        p.last_post_at = now
        p.posts += 1
        fault = p.faults.pop(0) if p.faults else None
        if fault and fault[0] == "429":
            p.last_post_at = None if sim.rate_limited_posts_do_not_count else p.last_post_at
            p.posts -= 1
            raise ProviderHTTPError(429, {"retry-after": str(fault[1])} if fault[1] is not None else {})
        if fault and fault[0] == "401":
            p.posts -= 1
            raise ProviderHTTPError(401, {})
        if fault and fault[0] == "timeout_lost":
            raise TimeoutError("read timeout")
        msg = sim.new_provider_message(self.acct, fan, "out", text, now, price=price, media=media,
                                       previews=previews, locked_text=locked_text)
        p.add(int(fan), msg)
        sim.on_post(self.acct, int(fan), msg, now)
        if fault and fault[0] == "timeout_delivered":
            raise TimeoutError("read timeout after delivery")
        return dict(msg)

    def send_text_once_body(self, fan, body):
        return self._post(fan, body)

    def post_json(self, url, json_body=None, **_):
        body = json_body or {}
        media_ids = body.get("mediaFiles") or []
        media = [{"id": int(mid), "type": self.sim.media_types.get(int(mid), "photo")}
                 for mid in media_ids]
        return self._post(int(url.rstrip("/").split("/")[-2]), body.get("text", ""),
                          price=body.get("price", 0), media=media,
                          previews=body.get("previews") or [],
                          locked_text=body.get("lockedText", False))

    def vault_media_by_id(self, media_id):
        return {"id": int(media_id), "type": self.sim.media_types.get(int(media_id), "photo"),
                "canView": True}


class FakeSocket:
    def __init__(self, queue):
        self.q = queue

    async def events(self):
        yield {"connected": True}
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev

    def stop(self):
        pass


_account_var: contextvars.ContextVar = contextvars.ContextVar("sim_account", default=None)


class Sim:
    def __init__(self, root, accounts: dict[str, list[int]], *, concurrency=4, model_latency=None, rng=None,
                 min_interval=20, quiet_s=7.0, burst_max_wait_s=45.0, bubbles=None, tuning_extra=None,
                 throttle=20, poll_s=2.0, rate_limited_posts_do_not_count=False, db_dir=None,
                 delivery_mode="gap", provider_rate=1.0, provider_burst=5, provider_max_concurrent=4,
                 conversation_gap=0, provider_latency=0.0, content_manifest=None):
        from native_pilot.tuning import Tuning
        self.root = Path(root)
        self.accounts = accounts
        self.rng = rng or random.Random(1)
        self.model_latency = model_latency or (lambda rng: rng.lognormvariate(1.8, 0.6))
        self.bubbles = bubbles or (lambda rng: 1 if rng.random() < .8 else 2)
        self.min_interval = min_interval
        self.delivery_mode = delivery_mode
        self.provider_rate = float(provider_rate)
        self.provider_burst = int(provider_burst)
        self.provider_max_concurrent = int(provider_max_concurrent)
        self.conversation_gap = int(conversation_gap)
        self.provider_latency = float(provider_latency)
        self.content_manifest = content_manifest
        self.paid_fans: set[int] = set()
        self.media_types: dict[int, str] = {}
        self.rate_limited_posts_do_not_count = rate_limited_posts_do_not_count
        self.tuning = Tuning.from_env({}, concurrency=concurrency, quiet_s=quiet_s, burst_max_wait_s=burst_max_wait_s,
                                      **(tuning_extra or {}))
        self.throttle = throttle
        self.poll_s = poll_s
        self.path = (Path(db_dir) if db_dir else self.root) / "sim.db"
        self.providers = {a: Provider(self, a) for a in accounts}
        self.generation = {a: 1 for a in accounts}
        self.clients: dict[str, FakeClient] = {}
        self.sockets: dict[str, asyncio.Queue] = {}
        self.intakes: dict[str, object] = {}
        self.services: dict[str, object] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.not_owned: dict[str, set] = {a: set() for a in accounts}
        self.events: list[dict] = []
        self._mid = {}
        self.clock = None
        self.loop = None
        from datetime import timezone as _tz
        self.expires_at = datetime.now(_tz.utc) + timedelta(days=30)
        # metrics
        self.inbound_at: dict[tuple, dict[int, datetime]] = {}      # (acct,fan) -> {mid: time} still unanswered
        self.latencies: dict[str, list[float]] = {a: [] for a in accounts}
        self.wait_first: dict[str, list[float]] = {a: [] for a in accounts}
        self.bodies: dict[tuple, set] = {}
        self.sends = {a: 0 for a in accounts}
        self.turn_log: list[tuple] = []         # R8: (account, fan, earliest-unresolved-input -> reply seconds, inputs covered)
        self.duplicates = 0
        self.cross_account = 0
        self.takeovers: dict[tuple, list] = {}                       # (acct,fan) -> [[start, end|None]]
        self.takeover_violations = 0
        self.inbound_total = 0
        self.unanswered_n: dict[str, int] = {}
        self.takeovers_open = False
        self.fast_idle = False            # load profiles: skip the (SQLite-slow) pending query while the account has nothing unanswered
        self.backlog_samples: list[tuple] = []
        self.replies_per_inbound = []
        self.failures: list[str] = []

    # ── database ──────────────────────────────────────────────────────────────
    def build(self):
        if self.path.exists():
            self.path.unlink()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executescript(SCHEMA)
            for acct, fans in self.accounts.items():
                con.execute("INSERT INTO accounts VALUES(?)", (acct,))
                con.execute("INSERT INTO of_runtime VALUES(?,1,?)", (acct, "b-" + acct))
                con.execute("INSERT INTO of_creator_bindings VALUES(?,?,'onlyfans',NULL,'sim')", ("b-" + acct, acct))
                con.execute("INSERT INTO of_provider_connection VALUES(?,?,?,'verified','2026-10-01 00:00:00',NULL,'2099-01-01 00:00:00',1)",
                            (acct, "b-" + acct, acct))
                con.execute("INSERT INTO of_delivery_policy(account_id,enabled,limit_units,used_units,throttle_seconds,"
                            "next_dispatch_at,active_intent_id,delivery_mode,provider_requests_per_second,provider_burst,"
                            "provider_max_concurrent,conversation_min_gap_seconds) "
                            "VALUES(?,1,100000000,0,?,NULL,NULL,?,?,?,?,?)",
                            (acct, self.throttle, self.delivery_mode, self.provider_rate, self.provider_burst,
                             self.provider_max_concurrent, self.conversation_gap))
                con.executemany("INSERT INTO twin_bridge_conversations VALUES(?,?,'active',NULL,4,NULL,NULL,1,3,3,?)",
                                [(acct, f, "2026-10-16T12:00:00") for f in fans])
                con.executemany("INSERT INTO of_conversation_state VALUES(?,?,'automated','none')", [(acct, f) for f in fans])
            if self.content_manifest:
                manifest = json.loads(Path(self.content_manifest).read_text(encoding="utf-8"))
                account = manifest["account_id"]
                for item in manifest["items"]:
                    con.execute(
                        "INSERT INTO catalog_items VALUES(?,?,?,?,?,?,0,1,'fixed')",
                        (item["catalog_id"], account, item["kind"],
                         json.dumps([m["id"] for m in item["media"]]),
                         json.dumps(item["preview_ids"]), item["price_cents"]))
                    self.media_types.update({int(m["id"]): m["type"] for m in item["media"]})
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + self.path.as_posix()

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.path, timeout=30)) as con, con:
            return con.execute(sql, args).fetchall()

    # ── provider side ─────────────────────────────────────────────────────────
    def next_id(self, acct, fan):
        self._mid[acct] = self._mid.get(acct, 1_000_000) + 1
        return self._mid[acct]

    def new_provider_message(self, acct, fan, direction, text, when, *, price=0, media=None,
                             previews=None, locked_text=False):
        mid = self.next_id(acct, fan)
        stamp = when.replace(microsecond=(when.microsecond // 1000) * 1000)
        created = stamp.isoformat(timespec="milliseconds") + "Z"
        media = [dict(m) for m in (media or [])]
        previews = list(previews or [])
        base = {"id": mid, "text": text, "createdAt": created, "price": price, "media": media,
                "mediaCount": len(media), "previews": previews, "lockedText": locked_text,
                "responseType": "message"}
        if direction == "in":
            return {**base, "isFromMe": False, "fromUser": {"id": int(fan)}, "toUser": {"id": int(acct)}, "withUser": {"id": int(fan)}}
        return {**base, "isFromMe": True, "fromUser": {"id": int(acct)}, "toUser": {"id": int(fan)}, "withUser": {"id": int(fan)}}

    def on_post(self, acct, fan, msg, now):
        """Audit one accepted POST and record the outbound row the engine's attribution write would."""
        body = msg["text"].replace("<p>", "").replace("</p>", "")
        self.sends[acct] += 1
        key = (acct, fan)
        if body in self.bodies.setdefault(key, set()):
            self.duplicates += 1
            self.failures.append(f"duplicate {key} {body[:40]}")
        self.bodies[key].add(body)
        if not body.startswith(f"re:{acct}:{fan}:"):
            self.cross_account += 1
            self.failures.append(f"cross-account {key} {body[:40]}")
        for start, end in self.takeovers.get(key, []):
            if now >= start and (end is None or now < end):
                self.takeover_violations += 1
                self.failures.append(f"takeover-violation {key} at {now}")
        # latency: against the newest inbound this reply covers
        try:
            covered = int(body.split(":")[3])
        except (IndexError, ValueError):
            covered = None
        pend = self.inbound_at.get(key, {})
        if covered is not None and covered in pend:
            self.latencies[acct].append((now - pend[covered]).total_seconds())
            self.wait_first[acct].append((now - min(pend.values())).total_seconds())
            self.turn_log.append((acct, fan, (now - min(pend.values())).total_seconds(), len(pend)))
            for mid in [m for m in pend if m <= covered]:
                del pend[mid]
                self.unanswered_n[acct] -= 1

    # ── fan side ──────────────────────────────────────────────────────────────
    def fan_says(self, acct, fan, text="hey", when=None):
        """A fan message arrives at the provider and (through the real Intake) the database."""
        when = when or self.clock()
        msg = self.new_provider_message(acct, fan, "in", text, when)
        self.providers[acct].add(fan, msg)
        self.inbound_at.setdefault((acct, fan), {})[msg["id"]] = when
        self.unanswered_n[acct] = self.unanswered_n.get(acct, 0) + 1
        self.inbound_total += 1
        self.sockets[acct].put_nowait({"api2_chat_message": {**msg, "fromUser": {"id": fan}}})
        return msg

    def duplicate_event(self, acct, msg):
        self.sockets[acct].put_nowait({"api2_chat_message": {**msg, "fromUser": {"id": msg["fromUser"]["id"]}}})

    def disconnect(self, acct):
        self.sockets[acct].put_nowait(None)

    def human_says(self, acct, fan, text="human reply"):
        now = self.clock()
        msg = self.new_provider_message(acct, fan, "out", text, now)
        self.providers[acct].add(fan, msg)
        self.sql("INSERT INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,price_cents,is_tip,"
                 "is_unsent,created_at,ingested_at,raw_json) VALUES(?,?,?,'out',?,'[]',0,0,0,0,?,?,'{}')",
                 (acct, fan, msg["id"], text, now.replace(microsecond=(now.microsecond // 1000) * 1000).isoformat(sep=" "),
                  now.isoformat(sep=" ")))
        pend = self.inbound_at.get((acct, fan), {})
        self.unanswered_n[acct] = self.unanswered_n.get(acct, 0) - len(pend)
        pend.clear()                      # the human answered what was waiting
        return msg

    def takeover(self, acct, fan):
        self.sql("UPDATE of_conversation_state SET mode='human_controlled',takeover_state='active' WHERE account_id=? AND fan_id=?", (acct, fan))
        self.takeovers.setdefault((acct, fan), []).append([self.clock(), None])

    def handback(self, acct, fan):
        self.sql("UPDATE of_conversation_state SET mode='automated',takeover_state='none' WHERE account_id=? AND fan_id=?", (acct, fan))
        for span in self.takeovers.get((acct, fan), []):
            if span[1] is None:
                span[1] = self.clock()

    def change_credentials(self, acct):
        self.generation[acct] += 1
        self.sql("UPDATE of_provider_connection SET revision=? WHERE account_id=?", (self.generation[acct], acct))

    # ── wiring ────────────────────────────────────────────────────────────────
    def emit(self, event):
        event = dict(event)
        event.setdefault("account", _account_var.get())
        event["t"] = round(self.loop._vt, 1) if self.loop else 0
        self.events.append(event)

    def kinds(self, name, acct=None):
        return [e for e in self.events if e.get("event") == name and (acct is None or e.get("account") == acct)]

    def patches(self):
        from native_pilot import attribution, connection as conn_mod, engine as engine_mod, runner, service
        from native_pilot.attribution import Sentinels
        from native_pilot.scope import AccountScope
        sim = self

        class _Time(types.SimpleNamespace):
            pass
        import time as _t
        from native_pilot import model as model_mod
        timeshim = types.SimpleNamespace(**{k: getattr(_t, k) for k in dir(_t) if not k.startswith("_")})
        timeshim.monotonic = sim.clock.monotonic

        async def no_report(ledger, pilot, run_error=None):
            return None

        async def no_ensure(scope, fan):
            return None

        async def fake_load(scope):
            acct = scope.provider_id
            return conn_mod.PilotConnection(
                scope=scope, tenant_id="sim", revision=sim.generation[acct], owner_id=int(acct),
                expires_at=sim.expires_at, capture={}, x_hash="x",
                proxy_url=None, model_id="openrouter/deepseek-v4.1-flash", model_api_key="sim-key", daily_cost_cap_cents=100000)

        def fake_build(conn):
            acct = conn.scope.provider_id
            client = FakeClient(sim, acct, sim.generation[acct])
            sim.clients[acct] = client
            return client

        async def fake_sentinels():
            return Sentinels(automation=5, human=6)

        def owned(self_, fan):
            return fan not in sim.not_owned[self_.db]

        orig_pending = service.Service.pending_fans

        def pending_fans(self_):
            if sim.fast_idle and sim.unanswered_n.get(self_.db, 0) <= 0 and not sim.takeovers_open:
                return [], []
            return orig_pending(self_)

        sim_engine = _engine_factory(sim)
        return [patch.object(runner, "emit", sim.emit), patch.object(runner, "now", sim.clock),
                patch.object(runner, "report_to_twin", no_report), patch.object(service, "ensure_fan_conversation", no_ensure),
                patch.object(service, "time", timeshim), patch.object(model_mod, "time", timeshim), patch.object(engine_mod, "NativeEngine", sim_engine),
                patch.object(conn_mod, "load_verified_connection", fake_load), patch.object(conn_mod, "build_client", fake_build),
                patch.object(attribution, "require_sentinels", fake_sentinels), patch.object(service.Service, "_owned", owned), patch.object(service.Service, "pending_fans", pending_fans),
                patch("native_pilot.autoreconcile.runner.emit", sim.emit)]

    def intake_factory(self, acct):
        from native_pilot.intake import Intake
        sim = self

        def factory(scope, fan, path, **_kw):
            q = sim.sockets.setdefault(acct, asyncio.Queue())

            async def transcode(account_id, message):
                when = sim.clock()
                created = datetime.fromisoformat(message["createdAt"].replace("Z", "")).isoformat(sep=" ")
                sim.sql("INSERT OR IGNORE INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,"
                        "price_cents,is_tip,is_unsent,created_at,ingested_at,raw_json) VALUES(?,?,?,'in',?,'[]',0,0,0,0,?,?,'{}')",
                        (account_id, message["fromUser"]["id"], message["id"], message["text"], created, when.isoformat(sep=" ")))

            async def attention(*a, **k):
                return True
            intake = Intake(scope, fan, path, socket_factory=lambda client: FakeSocket(q), transcode=transcode,
                            attention=attention, emit=sim.emit, backoff_s=1.0)
            sim.intakes[acct] = intake
            return intake
        return factory

    # ── run ───────────────────────────────────────────────────────────────────
    async def start_services(self):
        for acct in self.accounts:
            self.start_service(acct)

    def start_service(self, acct):
        import argparse
        from native_pilot import service
        self.sockets.setdefault(acct, asyncio.Queue())
        args = argparse.Namespace(account=acct, fan=None, all_fans=True, handle="sim", enable_send=True,
                                  twin_context=False, content_manifest=self.content_manifest, service_id=None, followups=False,
                                  vault_mode=False)
        svc = service.Service(args, stop=asyncio.Event(), tuning=self.tuning, ttl_s=120, renew_s=30, poll_s=self.poll_s,
                              hold_s=15, connection_check_s=300, intake_factory=self.intake_factory(acct))
        self.services[acct] = svc

        def start():
            _account_var.set(acct)
            return asyncio.get_running_loop().create_task(svc.run(), name=f"svc-{acct}")
        self.tasks[acct] = contextvars.copy_context().run(start)

    async def kill_service(self, acct):
        """A crash: the process stops without any shutdown path (no release, no lease hand-back)."""
        self.tasks[acct].cancel()
        for t in list(self.services[acct].tasks.values()):
            t.cancel()
        await asyncio.gather(self.tasks[acct], *self.services[acct].tasks.values(), return_exceptions=True)

    async def stop_services(self):
        for svc in self.services.values():
            svc.stop.set()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def backlog_sampler(self, every=60.0, stop=None):
        while stop is None or not stop.is_set():
            await asyncio.sleep(every)
            now = self.clock()
            per = {}
            for (acct, fan), pend in self.inbound_at.items():
                if pend:
                    age = (now - min(pend.values())).total_seconds()
                    n = per.setdefault(acct, [0, 0.0])
                    n[0] += 1
                    n[1] = max(n[1], age)
            self.backlog_samples.append((round(self.loop._vt), {a: tuple(v) for a, v in per.items()}))

    def report(self, duration_s):
        out = {}
        for acct in self.accounts:
            p = self.providers[acct]
            out[acct] = {"sends": self.sends[acct], "latency_s": stats(self.latencies[acct]),
                         "min_gap_s": None if p.min_gap is None else round(p.min_gap, 2),
                         "rate_violations": p.rate_violations, "max_provider_inflight": p.max_active,
                         "sends_per_hour": round(self.sends[acct] * 3600 / duration_s, 1)}
        return out


import time as _real_time_mod  # noqa: E402
_real_time = _real_time_mod.time
_real_sleep = _real_time_mod.sleep


def _engine_factory(sim):
    class FakeEngine:
        def __init__(self, account, fan, *, chat, send, emit, content=None, context=None, **_extra):
            self.send, self.fan, self.chat, self.account = send, fan, chat, account
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def run(self, run_id, payload=None):
            model = self.chat.__self__
            acct, fan = self.account, self.fan
            rng = sim.rng
            latency = sim.model_latency(rng)
            await asyncio.sleep(latency)
            if sim.fail_models > 0:
                sim.fail_models -= 1
                model.stopped_reason = sim.fail_reason
                return {"errors": 1}
            if rng.random() < sim.model_failure_rate:
                model.stopped_reason = "model-timeout"
                return {"errors": 1}
            try:
                model._check()                       # the real stop check the model call makes (cancel / takeover / deadline)
            except Exception:
                return {"errors": 1}
            rows = sim.sql("SELECT message_id FROM messages WHERE account_id=? AND fan_id=? AND direction='in' "
                           "ORDER BY created_at DESC,message_id DESC LIMIT 1", (acct, fan))
            if not rows:
                return {}
            newest = rows[0][0]
            for i in range(sim.bubbles(rng)):
                text = f"re:{acct}:{fan}:{newest}:{i}"
                if sim.text_override is not None:
                    text = sim.text_override(acct, fan, newest, i) or text
                paid = self.content is not None and fan in sim.paid_fans and hasattr(self.content, "terms")
                terms = {}
                if paid:
                    item = self.content.item
                    terms = {
                        "price": item["price_cents"] / 100,
                        "locked_text": False,
                        "media_files": list(item["media_ids"]),
                        "previews": list(item["preview_ids"]),
                    }
                from native_pilot.media_claims import MEDIA_CLAIM_HELD, claims_media_sent
                if claims_media_sent(text):
                    # what the real boundary does (engine._TextClient): hold the bubble, end the turn with the reason
                    from native_pilot.engine import PilotBoundaryError
                    raise PilotBoundaryError(MEDIA_CLAIM_HELD)
                try:
                    raw = await vtime.to_thread_blocking(self.send, fan, text, terms)
                except Exception as exc:
                    sim.failures.append(f"engine-send {type(exc).__name__}: {exc}")
                    return {"errors": 1}
                sim.sql("INSERT OR IGNORE INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,"
                        "price_cents,is_tip,is_unsent,created_at,ingested_at,raw_json,automation_kind,sent_by_employee_id) "
                         "VALUES(?,?,?,'out',?,?,?,?,0,0,?,?,'{}','ai_chatter',5)",
                        (acct, fan, raw["id"], raw["text"],
                         json.dumps(item["media_ids"] if paid else []),
                         len(item["media_ids"]) if paid else 0,
                         item["price_cents"] if paid else 0,
                         raw["createdAt"].replace("T", " ").replace("Z", ""),
                         sim.clock().isoformat(sep=" ")))
            return {}
    return FakeEngine


Sim.model_failure_rate = 0.0
Sim.text_override = None
Sim.fail_models = 0
Sim.fail_reason = "model-timeout"


def run_sim(scenario, *, root, accounts, **kw):
    """Run ``scenario(sim)`` (an async function) on a fresh virtual loop with every patch installed."""
    async def main(loop, clock):
        sim = Sim(root, accounts, **kw)
        sim.loop, sim.clock = loop, clock
        sim.build()
        patches = sim.patches()
        for p in patches:
            p.start()
        # the executor: sends wait in the queue holding a thread, so give the loop's pool room
        from concurrent.futures import ThreadPoolExecutor
        loop.set_default_executor(ThreadPoolExecutor(max_workers=64, thread_name_prefix="sim"))
        try:
            await sim.start_services()
            try:
                return await scenario(sim)
            except BaseException as exc:
                exc.sim = sim
                raise
        finally:
            await sim.stop_services()
            for p in patches:
                p.stop()
    return vtime.run(main)
