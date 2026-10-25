"""Reproducible capacity harness for the FASTT all-fans runner (CP6 8.D/8.E: "capacity is measured").

Runs the REAL ``native_pilot.service.Service`` (all-fans mode: tick loop, ``pending_fans`` SQL, per-fan
child pilots, ledger admission/throttle/allowance, ``Gateway`` hold checks, real ``OFClient`` send path)
against a throwaway PostgreSQL database with the same fakes the existing service suites use:

* fake provider socket (history, POST, configurable latency / 429s) in place of OnlyFans;
* synthetic model in place of the LLM (configurable latency, timeouts, errors) behind a stand-in for
  ``NativeEngine`` (so the engine's prompt building / history gathering CPU is NOT measured: see "limits");
* fake intake (the harness inserts the inbound rows itself, as the websocket transcoder would).

Time is a SCALED REAL clock: ``--scale 30`` means one real second is 30 simulated seconds. ``runner.now``,
every ``Service.idle`` wait, model/provider latency and the fan simulator all use it, so the production
20 s throttle, 7 s quiet window, 2 s poll, 30 s lease renewal... run unchanged in simulated seconds.
Reported times are SIMULATED seconds unless the key says ``real``; CPU is reported both per real second
and per simulated second (the latter is the production figure: at scale S the process does S x the work
per real second, so ``cpu_per_sim_s`` is what one production process would use).

Safety: refuses a non-local database; imports nothing that talks to OnlyFans/OpenRouter; never reads
``.env``; every id is synthetic.

    ONLYFANS_TEST_PG_URL=postgresql://postgres:pw@127.0.0.1:59731/postgres \\
        python service/native_pilot/tools/load_test.py --scenario all --out result.json
    python ... --scenario tiny        # a few real seconds (what the unit test runs)

Scenarios: ``ceiling`` (compute-bound: no throttle, no model latency), ``burst`` (a), ``sweep`` (offered load
below/near/above the throttle ceiling), ``slow_model`` (b), ``rate_limit`` (c), ``control`` (d), ``tiny``.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import random
import resource
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

SERVICE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICE))
from native_pilot.tools.capacity_checks import evaluate_capacity
from native_pilot.tools.diagnostics import capture_waits

MODEL_TIMEOUT_S = 45.0   # native_pilot.model's per-call limit (no retry)
FAN_BASE = 1_000_000


# â”€â”€ configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class Cfg:
    name: str
    accounts: int = 2
    account_offset: int = 0
    send_concurrency: int = 1
    fans: int = 250
    sim_minutes: float = 60.0
    scale: float = 30.0
    throttle_s: float = 20
    seed: int = 7
    # arrivals
    burst: bool = False              # every fan sends 1-3 messages inside burst_window_s at t=0
    burst_window_s: float = 60.0
    single_messages: bool = False
    deterministic_arrivals: bool = False
    rate_per_min: float = 0.0        # steady Poisson arrivals per account (a burst of 1-2 bubbles each)
    # model
    model_lo_s: float = 0.4
    model_hi_s: float = 1.0
    p_model_timeout: float = 0.0     # latency beyond the 45 s limit: the call fails after 45 s
    p_model_error: float = 0.0       # immediate provider error
    # provider
    provider_latency_s: float = 0.6
    history_latency_s: float = 0.0
    p_429: float = 0.0
    storm: tuple | None = None       # (start_sim_s, end_sim_s): every POST is a 429
    # control events (scenario d)
    control_at_s: float | None = None
    resume_after_s: float = 600.0
    pause_n: int = 5
    takeover_n: int = 5
    # stop early when every inbound is answered (ceiling probe)
    stop_when_drained: bool = False
    prefill: bool = False            # messages exist (and are quiet) before the clock starts


def scenarios(args) -> dict:
    base = dict(accounts=args.accounts, fans=args.fans, scale=args.scale, throttle_s=args.throttle,
                seed=args.seed, sim_minutes=args.sim_minutes, account_offset=getattr(args, "account_offset", 0),
                send_concurrency=getattr(args, "concurrent_sends", 1))
    out = {}

    def add(name, **kw):
        out[name] = Cfg(name=name, **{**base, **kw})

    add("target_100k", accounts=10, fans=args.fans, throttle_s=args.throttle,
        rate_per_min=100000 / 60 / 10, single_messages=True, deterministic_arrivals=True,
        model_lo_s=6.0, model_hi_s=6.0,
        provider_latency_s=getattr(args, "provider_latency_seconds", 0.2),
        history_latency_s=getattr(args, "history_latency_seconds", 0.0))
    add("target_10k", accounts=1, rate_per_min=10000 / 60, single_messages=True,
        deterministic_arrivals=True, model_lo_s=6.0, model_hi_s=6.0,
        provider_latency_s=getattr(args, "provider_latency_seconds", 0.2),
        history_latency_s=getattr(args, "history_latency_seconds", 0.0))
    add("tiny", accounts=2, fans=8, sim_minutes=3, scale=30, throttle_s=2, burst=True, burst_window_s=5,
        rate_per_min=20.0, control_at_s=15, resume_after_s=40, pause_n=2, takeover_n=1)
    add("ceiling_1acct", accounts=1, fans=args.fans, sim_minutes=10, scale=20, throttle_s=1, burst=True,
        burst_window_s=1, model_lo_s=0.0, model_hi_s=0.0, provider_latency_s=0.0, stop_when_drained=True,
        prefill=True)
    add("ceiling_2acct", fans=args.fans, sim_minutes=10, scale=20, throttle_s=1, burst=True,
        burst_window_s=1, model_lo_s=0.0, model_hi_s=0.0, provider_latency_s=0.0, stop_when_drained=True,
        prefill=True)
    add("burst", burst=True, model_lo_s=0.8, model_hi_s=2.0)
    for rate in (1.5, 2.5, 3.5):
        add(f"sweep_{rate}", rate_per_min=rate, model_lo_s=0.8, model_hi_s=2.0)
    add("slow_model_ok", rate_per_min=2.0, model_lo_s=2.0, model_hi_s=5.0)
    add("slow_model_timeouts", rate_per_min=2.0, model_lo_s=2.0, model_hi_s=5.0, p_model_timeout=0.08,
        p_model_error=0.02)
    add("rate_limit", rate_per_min=2.0, model_lo_s=0.8, model_hi_s=2.0, p_429=0.05, storm=(1200.0, 1500.0))
    add("control", rate_per_min=2.0, burst=True, burst_window_s=300, model_lo_s=0.8, model_hi_s=2.0,
        control_at_s=1200.0, resume_after_s=900.0)
    return out


# â”€â”€ the simulated world â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def quantiles(values, qs=(0.5, 0.95, 0.99)):
    if not values:
        return {f"p{int(q * 100)}": None for q in qs}
    s = sorted(values)
    return {f"p{int(q * 100)}": round(s[min(len(s) - 1, int(q * len(s)))], 2) for q in qs}


class Timers:
    """Real-millisecond wall time of the synchronous boundaries (DB and ledger calls), per function."""

    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def wrap(self, label, fn):
        timers = self

        def timed(*a, **kw):
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                dt = (time.perf_counter() - t) * 1000
                with timers.lock:
                    r = timers.rows.setdefault(label, [])
                    r.append(dt)
        timed.__wrapped__ = fn
        return timed

    def report(self):
        out = {}
        for label, v in sorted(self.rows.items()):
            out[label] = {"n": len(v), "total_ms": round(sum(v), 1), "mean_ms": round(statistics.fmean(v), 2),
                          **{k + "_ms": x for k, x in quantiles(v, (0.5, 0.95)).items()}, "max_ms": round(max(v), 1)}
        return out


class World:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.S = cfg.scale
        self.t0 = time.monotonic()
        self.base = 0.0          # simulated second at which the scenario's own timeline starts (after start-up)
        self.sim0 = datetime.now(timezone.utc).replace(tzinfo=None)
        self.accts = [f"of-{900000001 + cfg.account_offset + i}" for i in range(cfg.accounts)]
        self.provider = {a: a[3:] for a in self.accts}
        self.fans = [FAN_BASE + i for i in range(cfg.fans)]
        self.rng = random.Random(cfg.seed)
        self.mid = 10_000_000
        self.out_id = 900_000_000
        self.lock = threading.Lock()
        # registry (the fake provider's view of the chats)
        self.inbound = {}        # (acct, fan) -> [{id, created, t, answered_t, ahead}]
        self.sent = {}           # (acct, fan) -> [message dicts]
        self.pending = {a: set() for a in self.accts}
        self.posts = []          # dict(acct, fan, t, ok, status)
        self.refusals = []       # dict(acct, fan, t, reason)
        self.events = {}
        self.event_reasons = {}
        self.in_flight = {}      # acct -> (fan, t_started)
        self.model_calls = {a: 0 for a in self.accts}
        self.model_failures = {a: 0 for a in self.accts}
        self.queue_samples = []  # (t, acct, depth, oldest_age)
        self.control = []        # dicts
        self.paused_since = {}   # (acct, fan) -> t_commit
        self.lag = []
        self.arrival_lag_s = []
        self.timers = Timers()

    # simulated time
    def sim_t(self):
        return (time.monotonic() - self.t0) * self.S

    def rel(self):
        return self.sim_t() - self.base

    def now(self):
        return self.sim0 + timedelta(seconds=self.sim_t())

    def ms(self, dt):
        return dt.replace(microsecond=dt.microsecond // 1000 * 1000)

    async def sleep_sim(self, seconds):
        await asyncio.sleep(max(0.0, seconds) / self.S)

    async def sleep_until(self, t_sim):
        while True:
            left = t_sim - self.sim_t()
            if left <= 0:
                return
            await asyncio.sleep(min(left / self.S, 0.25))

    # registry
    def new_inbound(self, acct, fan, text="neutral fan text", *, scheduled_t=None):
        with self.lock:
            self.mid += 1
            arrived = self.sim_t() if scheduled_t is None else scheduled_t
            created = self.ms(self.sim0 + timedelta(seconds=arrived))
            rec = {"id": self.mid, "created": created, "t": arrived, "answered_t": None, "text": text,
                   "ahead": len(self.pending[acct])}
            self.inbound.setdefault((acct, fan), []).append(rec)
            self.pending[acct].add(fan)
        return rec

    def answered(self, acct, fan, t):
        with self.lock:
            for rec in self.inbound.get((acct, fan), ()):
                if rec["answered_t"] is None:
                    rec["answered_t"] = t
            self.pending[acct].discard(fan)

    def emit(self, event):
        kind = event.get("event", "?")
        self.events[kind] = self.events.get(kind, 0) + 1
        reason = event.get("reason")
        if reason is not None:
            key = f"{kind}:{str(reason)[:60]}"
            self.event_reasons[key] = self.event_reasons.get(key, 0) + 1


# â”€â”€ fakes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class FakeResponse:
    def __init__(self, data, status=200, url="https://onlyfans.invalid/api2/v2/x"):
        self.status_code, self.ok, self.headers, self.url = status, status < 400, {}, url
        self.data = data
        self.text = json.dumps(data)

    def json(self):
        return copy.deepcopy(self.data)


class FakeHTTP:
    """One account socket for all its fans: history and POSTs routed by the fan in the URL."""

    def __init__(self, world: World, acct: str):
        self.w, self.acct = world, acct
        self.provider_id = int(world.provider[acct])
        self.closed = False
        self.last_post = None

    @staticmethod
    def fan_of(url):
        return int(urlsplit(url).path.split("/")[-2])

    def get(self, url, **kwargs):
        w, fan = self.w, self.fan_of(url)
        time.sleep(w.cfg.history_latency_s / w.S)
        with w.lock:
            inbound = [{"id": r["id"], "text": r["text"], "createdAt": r["created"].isoformat() + "Z", "isFromMe": False,
                        "fromUser": {"id": fan}, "withUser": {"id": fan}}
                       for r in w.inbound.get((self.acct, fan), ())]
            history = inbound + list(w.sent.get((self.acct, fan), ()))
        history.sort(key=lambda m: (m["createdAt"], m["id"]), reverse=True)
        return FakeResponse({"list": history[:60], "hasMore": False}, url=url)

    def request(self, method, url, json=None, **kwargs):
        w, fan, cfg = self.w, self.fan_of(url), self.w.cfg
        time.sleep(cfg.provider_latency_s / w.S)
        t = w.sim_t()
        storm = cfg.storm and cfg.storm[0] <= w.rel() < cfg.storm[1]
        if storm or (cfg.p_429 and w.rng.random() < cfg.p_429):
            with w.lock:
                w.posts.append({"acct": self.acct, "fan": fan, "t": t, "ok": False, "status": 429})
            return FakeResponse({"error": "rate limited"}, status=429, url=url)
        with w.lock:
            w.out_id += 1
            result = {"id": w.out_id, "text": json["text"], "isFromMe": True, "fromUser": {"id": self.provider_id},
                      "withUser": {"id": fan}, "createdAt": w.ms(w.now()).isoformat() + "Z", "price": 0, "media": []}
            w.sent.setdefault((self.acct, fan), []).append(result)
            w.posts.append({"acct": self.acct, "fan": fan, "t": t, "ok": True, "status": 200})
        w.answered(self.acct, fan, t)
        return FakeResponse(result, url=url)

    def close(self):
        self.closed = True


class FakeIntake:
    """The harness inserts the inbound rows itself; intake only has to exist."""

    def __init__(self, scope, fan, path, **kwargs):
        pass

    def start(self, client):
        pass

    async def stop(self):
        pass


# â”€â”€ database â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class Db:
    def __init__(self, admin_url):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        self.create_engine = create_engine
        u = make_url(admin_url)
        if u.host not in ("127.0.0.1", "localhost", "::1"):
            sys.exit("REFUSED: the load harness only runs against a local throwaway PostgreSQL (got host %r)" % u.host)
        self.reuse = os.environ.get("LOAD_TEST_REUSE_DB")   # dev only: a database already bootstrapped by a previous run
        self.name = self.reuse or "of_load_" + uuid.uuid4().hex[:10]
        self.fleet_token = os.environ.get("LOAD_TEST_FLEET_TOKEN")
        if self.fleet_token and (not self.reuse or not self.name.startswith("of_load_")):
            sys.exit("REFUSED: a fleet worker requires its parent-created disposable database")
        self.admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        self.sync_url = u.set(drivername="postgresql+psycopg2", database=self.name).render_as_string(hide_password=False)
        self.async_url = u.set(drivername="postgresql+asyncpg", database=self.name).render_as_string(hide_password=False)
        self.engine = None
        self.mon = None

    def create(self):
        if self.reuse:
            return
        with self.admin.connect() as c:
            c.exec_driver_sql(f'CREATE DATABASE "{self.name}" TEMPLATE template0 ENCODING \'UTF8\'')

    def bootstrap(self):
        from db.schema import bootstrap
        if not self.reuse:
            e = self.create_engine(self.sync_url, connect_args={"options": "-c timezone=UTC"})
            with e.begin() as conn:
                bootstrap(conn)
                conn.exec_driver_sql("INSERT INTO employees(id,display_name,is_active) VALUES "
                                     "(5,'Automation',false),(6,'Twin staff',false),(9,'Other',false)")
            e.dispose()
        from twin_bridge import store
        if not self.fleet_token:
            store.ensure_runtime_schema(self.sync_url)
        # the harness's own pool, labelled so it is excluded from the connection census
        self.engine = self.create_engine(self.sync_url, pool_size=4, max_overflow=4,
                                         connect_args={"options": "-c timezone=UTC -c search_path=fastt_runtime,public", "application_name": "load-harness"})
        self.mon = self.create_engine(self.sync_url, pool_size=1, max_overflow=0,
                                      connect_args={"application_name": "load-monitor"})
        if self.fleet_token:
            rows = self.run("SELECT token FROM public.load_harness_fleet WHERE id=1")
            if rows != [(self.fleet_token,)]:
                raise RuntimeError("fleet database ownership marker mismatch")

    def drop(self):
        from twin_bridge import store
        try:
            store.dispose_pools()
            for e in (self.engine, self.mon):
                if e is not None:
                    e.dispose()
            if not self.reuse:
                with self.admin.connect() as c:
                    c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        finally:
            self.admin.dispose()

    def run(self, sql, params=None):
        from sqlalchemy import text
        with self.engine.begin() as c:
            r = c.execute(text(sql), params or {})
            return r.fetchall() if r.returns_rows else None

    def many(self, sql, rows):
        from sqlalchemy import text
        if rows:
            with self.engine.begin() as c:
                c.execute(text(sql), rows)

    def reset(self):
        for t in ("twin_bridge_inputs", "twin_bridge_turns", "twin_bridge_turn_members", "twin_bridge_conversations",
                  "twin_bridge_send_attempts", "twin_bridge_writes", "native_pilots", "native_pilot_inputs",
                  "native_pilot_receipts", "native_pilot_followups", "messages", "automation_runs",
                  "of_conversation_state", "skip_list", "fans", "of_delivery_policy", "of_runtime",
                  "of_creator_bindings", "accounts"):
            try:
                self.run("DELETE FROM fastt_runtime." + t if t.startswith(("twin_bridge", "native_pilot")) else "DELETE FROM " + t)
            except Exception:
                try:
                    self.run("DELETE FROM " + t)
                except Exception:
                    pass
        for seq in ("twin_bridge_send_attempts_id_seq", "automation_runs_id_seq"):
            try:
                self.run("ALTER SEQUENCE " + seq + " RESTART WITH 1")
            except Exception:
                pass

    def seed(self, w: World):
        now = w.now()
        for a in w.accts:
            self.run("INSERT INTO accounts(id,is_active_default) VALUES(:a,false)", {"a": a})
            self.run("INSERT INTO of_creator_bindings(id,tenant_id,creator_id,creator_name,account_id,handle,channel) "
                     "VALUES(:b,'synthetic-tenant',:c,'Synthetic',:a,'synthetic','onlyfans')",
                     {"b": "binding-" + a, "c": "creator-" + a, "a": a})
            self.run("INSERT INTO of_runtime(account_id,binding_id,enabled,activated_at) "
                     "VALUES(:a,:b,false,CURRENT_TIMESTAMP)", {"a": a, "b": "binding-" + a})
            self.run("INSERT INTO of_delivery_policy(account_id,revision,enabled,transport_kind,limit_units,used_units,"
                     "queued_count,throttle_seconds,next_dispatch_at,active_intent_id,media_enabled) "
                     "VALUES(:a,1,true,'native',10000,4,0,:t,NULL,NULL,false)", {"a": a, "t": w.cfg.throttle_s})
            from db.models import AccountAiConfig, Fan
            from sqlalchemy.orm import Session
            with Session(self.engine) as session:
                session.add(AccountAiConfig(account_id=a, daily_cost_cap_cents=1_000_000))
                session.add_all([Fan(account_id=a, fan_id=f) for f in w.fans])
                session.commit()
            self.many("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state) "
                      "VALUES(:a,:f,'automated','none')", [{"a": a, "f": f} for f in w.fans])
            self.many("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,"
                      "scope_fingerprint,state,revision,reserved,send_budget_limit,send_budget_used,updated_at) "
                      "VALUES(:a,:f,'synthetic','c','f','active',4,true,3,3,:n)",
                      [{"a": a, "f": f, "n": now} for f in w.fans])

    MSG = ("INSERT INTO messages(account_id,fan_id,message_id,direction,sender_name,body,media_ids,media_count,"
           "price_cents,is_tip,is_unsent,is_promise,created_at,ingested_at,raw_json{extra}) VALUES(:a,:f,:m,:d,'synthetic',"
           ":b,'[]',0,0,false,false,false,:c,:i,'{{}}'{vals})")

    def insert_in(self, acct, fan, rec):
        self.run(self.MSG.format(extra="", vals=""), {"a": acct, "f": fan, "m": rec["id"], "d": "in", "b": rec["text"],
                                                       "c": rec["created"], "i": rec["created"]})

    def insert_out(self, acct, fan, raw, created):
        self.run(self.MSG.format(extra=",automation_kind,sent_by_employee_id", vals=",'ai_chatter',5"),
                 {"a": acct, "f": fan, "m": raw["id"], "d": "out", "b": raw["text"], "c": created, "i": created})

    def census(self):
        from sqlalchemy import text
        with self.mon.connect() as c:
            rows = c.execute(text("SELECT coalesce(application_name,''),state,count(*) FROM pg_stat_activity "
                                  "WHERE datname=:d AND pid<>pg_backend_pid() GROUP BY 1,2"), {"d": self.name}).fetchall()
            xact = c.execute(text("SELECT xact_commit+xact_rollback FROM pg_stat_database WHERE datname=:d"),
                             {"d": self.name}).scalar()
        return rows, int(xact or 0)


# â”€â”€ process / host measurements â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def rss_kb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def cpu_s():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def pg_cpu_s(container):
    if not container:
        return None
    try:
        out = subprocess.run(["docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    except Exception:
        pass
    return None


# â”€â”€ the run â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def make_schedule(w: World):
    cfg, ev = w.cfg, []
    for a in w.accts:
        if cfg.burst:
            for f in w.fans:
                t = 0.0 if cfg.prefill else w.rng.uniform(0.0, cfg.burst_window_s)
                for k in range(w.rng.choice([1, 1, 2, 3])):
                    ev.append((t + 2.5 * k * (0 if cfg.prefill else 1), a, f))
        if cfg.rate_per_min:
            t, index = 30.0, 0
            while t < cfg.sim_minutes * 60:
                if cfg.deterministic_arrivals:
                    t = 30.0 + index * 60.0 / cfg.rate_per_min
                    index += 1
                else:
                    t += w.rng.expovariate(cfg.rate_per_min / 60.0)
                if t >= cfg.sim_minutes * 60 or math.isclose(t, cfg.sim_minutes * 60, abs_tol=1e-9):
                    break
                f = w.rng.choice(w.fans)
                for k in range(1 if cfg.single_messages else w.rng.choice([1, 1, 2])):
                    ev.append((t + 2.0 * k, a, f))
    ev.sort()
    return ev


async def simulate_fans(w: World, db: Db, schedule):
    # The arrival writer must not compete for the send workers' default executor.
    # Late arrivals retain their scheduled timestamps and invalidate the benchmark.
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="load-ingress") as ingress:
        for t, acct, fan in schedule:
            due = w.base + t
            await w.sleep_until(due)
            rec = w.new_inbound(acct, fan, scheduled_t=due)
            await loop.run_in_executor(ingress, db.insert_in, acct, fan, rec)
            w.arrival_lag_s.append(max(0.0, w.sim_t() - due))


async def sampler(w: World, db: Db, stats, stop):
    next_q = 0.0
    while not stop.is_set():
        await asyncio.sleep(0.5)
        t = w.sim_t()
        stats["rss_kb_max"] = max(stats["rss_kb_max"], rss_kb())
        stats["threads_max"] = max(stats["threads_max"], threading.active_count())
        try:
            rows, _ = await asyncio.to_thread(db.census)
            n = sum(c for app, st, c in rows if not app.startswith("load-"))
            active = sum(c for app, st, c in rows if not app.startswith("load-") and st == "active")
            stats["conn_samples"].append(n)
            stats["conn_active_samples"].append(active)
        except Exception:
            pass
        if t >= next_q:
            next_q = t + 10
            with w.lock:
                for a in w.accts:
                    waiting = [r["t"] for f in w.pending[a] for r in w.inbound[(a, f)] if r["answered_t"] is None]
                    w.queue_samples.append((t, a, len(w.pending[a]), (t - min(waiting)) if waiting else 0.0))


async def lag_probe(w: World, stop):
    while not stop.is_set():
        t = time.perf_counter()
        await asyncio.sleep(0.02)
        w.lag.append((time.perf_counter() - t - 0.02) * 1000)


async def controller(w: World, db: Db):
    """Scenario d: at control_at_s pause/take over fans mid-burst, including the fan whose turn is in flight."""
    cfg = w.cfg
    await w.sleep_until(w.base + cfg.control_at_s)
    for a in w.accts:
        picked = []
        deadline = w.sim_t() + 120
        while not picked and w.sim_t() < deadline:
            inflight = w.in_flight.get(a)
            if inflight:
                picked = [inflight[0]]
            else:
                await asyncio.sleep(0.01)
        with w.lock:
            waiting = [f for f in w.pending[a] if f not in picked]
        w.rng.shuffle(waiting)
        paused = picked + waiting[:max(0, cfg.pause_n - len(picked))]
        taken = waiting[max(0, cfg.pause_n - len(picked)):max(0, cfg.pause_n - len(picked)) + cfg.takeover_n]
        for fan, mode, takeover in [(f, "paused", "none") for f in paused] + [(f, "automated", "effective") for f in taken]:
            await asyncio.to_thread(db.run, "UPDATE of_conversation_state SET mode=:m,takeover_state=:t WHERE account_id=:a AND fan_id=:f",
                                    {"m": mode, "t": takeover, "a": a, "f": fan})
            t = w.sim_t()
            w.paused_since[(a, fan)] = t
            w.control.append({"acct": a, "fan": fan, "kind": "pause" if mode == "paused" else "takeover", "t": t,
                              "in_flight": bool(picked and fan == picked[0]), "waiting_at_pause": fan in waiting})
    await w.sleep_sim(cfg.resume_after_s)
    for (a, fan), t0 in list(w.paused_since.items()):
        await asyncio.to_thread(db.run, "UPDATE of_conversation_state SET mode='automated',takeover_state='none' WHERE account_id=:a AND fan_id=:f",
                                {"a": a, "f": fan})
        w.control.append({"acct": a, "fan": fan, "kind": "resume", "t": w.sim_t()})


def install_patches(stack: ExitStack, holder: dict):
    """Everything is global (module attributes), so the World is looked up through ``holder`` at call time."""
    from native_pilot import attribution, connection as conn_mod, engine as engine_mod, runner, service
    from native_pilot.attribution import Sentinels
    from native_pilot.ledger import Ledger
    from native_pilot.scope import AccountScope
    from of_client import OFClient

    W = lambda: holder["w"]  # noqa: E731

    async def no_report(ledger, pilot, run_error=None):
        pass

    async def no_ensure(scope, fan):
        pass

    async def idle(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds / W().S)
        except asyncio.TimeoutError:
            pass

    class SyntheticEngine:
        """Stand-in for NativeEngine: one synthetic model call, then one text bubble through the REAL gateway."""

        def __init__(self, account, fan, *, chat, send, emit, content, context, own_offers_only=False, followups=None,
                     arm_unsend=None, unsent=None, card_of=None):
            self.account, self.fan, self.send, self.chat = account, fan, send, chat

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def run(self, run_id, payload=None):
            w = W()
            a, fan = self.account, self.fan
            w.in_flight[a] = (fan, w.sim_t())
            w.model_calls[a] += 1
            try:
                # The REAL NativePilotModel.chat: call reservation in the ledger, daily-budget reserve/settle rows,
                # request encoding and limits; only the HTTP transport underneath is the synthetic one.
                await self.chat(model=MODEL_NAME, messages=[{"role": "system", "content": "x" * 8000},
                                                            {"role": "user", "content": "neutral fan text"}],
                                purpose="ai_chatter", account_id=a, fan_id=fan)
            except Exception:
                w.model_failures[a] += 1
                return {"errors": 1}
            finally:
                w.in_flight.pop(a, None)
            try:
                raw = await asyncio.to_thread(self.send, fan, "synthetic reply", {})
            except Exception as exc:  # the native loop swallows send errors; the gateway holds the reason
                w.refusals.append({"acct": a, "fan": fan, "t": w.sim_t(), "reason": getattr(exc, "reason", type(exc).__name__)})
                return {}
            await asyncio.to_thread(holder["db"].insert_out, a, fan, raw, raw["createdAt"].removesuffix("Z").replace("T", " "))
            return {}

    import httpx
    from native_pilot import model as model_mod
    MODEL_NAME = model_mod.MODEL

    async def synthetic_llm(request):
        """OpenRouter stand-in: configurable latency, timeouts (as httpx.ReadTimeout), provider errors."""
        w, cfg = W(), W().cfg
        if cfg.p_model_timeout and w.rng.random() < cfg.p_model_timeout:
            await w.sleep_sim(MODEL_TIMEOUT_S)
            raise httpx.ReadTimeout("synthetic model timeout", request=request)
        if cfg.p_model_error and w.rng.random() < cfg.p_model_error:
            await w.sleep_sim(0.3)
            return httpx.Response(503, json={"error": "synthetic"})
        await w.sleep_sim(w.rng.uniform(cfg.model_lo_s, cfg.model_hi_s))
        return httpx.Response(200, json={
            "model": model_mod.MODEL, "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "synthetic reply"}}],
            "usage": {"prompt_tokens": 2500, "completion_tokens": 40}})

    class SyntheticModel(model_mod.NativePilotModel):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(synthetic_llm)
            super().__init__(**kw)

    scope_conn = {}

    def conn_for(scope):
        if scope.db_id not in scope_conn:
            scope_conn[scope.db_id] = conn_mod.PilotConnection(
                scope=scope, tenant_id="synthetic-tenant", revision=3, owner_id=int(scope.provider_id),
                expires_at=datetime.now(timezone.utc) + timedelta(days=30), capture={}, x_hash="x", proxy_url=None,
                model_id="openrouter/deepseek-v4.1-flash", model_api_key="synthetic-key", daily_cost_cap_cents=100)
        return scope_conn[scope.db_id]

    async def fake_load(scope):
        return conn_for(scope)

    def build_client(conn):
        if os.environ.get("LOAD_TEST_FLEET_TOKEN"):
            # The real connection builder binds provider access once per worker.
            # Keep that same account guard when substituting only the HTTP socket.
            from onlyfans_runtime.role_guard import allow_native_pilot_provider_access
            allow_native_pilot_provider_access(conn.scope.provider_id)
        w = W()
        acct = conn.scope.db_id
        http = FakeHTTP(w, acct)
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = conn.scope.provider_id, conn.scope.provider_id, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        client.me = lambda: {"id": int(conn.scope.provider_id), "username": "synthetic-creator"}
        return client

    async def fake_sentinels():
        return Sentinels(automation=5, human=6)

    def timed(owner, name, label=None):
        orig = getattr(owner, name)
        stack.enter_context(patch.object(owner, name, W_timer(label or f"{owner.__name__}.{name}", orig)))

    def W_timer(label, fn):
        def timed_fn(*a, **kw):
            return W().timers.wrap(label, fn)(*a, **kw)
        return timed_fn

    stack.enter_context(patch.object(runner, "emit", lambda e: W().emit(e)))
    stack.enter_context(patch.object(runner, "now", lambda: W().now()))
    stack.enter_context(patch.object(runner, "report_to_twin", no_report))
    stack.enter_context(patch.object(service, "ensure_fan_conversation", no_ensure))
    stack.enter_context(patch.object(service.Service, "idle", idle))
    stack.enter_context(patch.object(engine_mod, "NativeEngine", SyntheticEngine))
    stack.enter_context(patch.object(model_mod, "NativePilotModel", SyntheticModel))
    from native_pilot import budget as budget_mod
    class ProfileBudget(budget_mod.DailyBudget):
        async def reserve(self, **kwargs):
            try:
                return await super().reserve(**kwargs)
            except Exception as exc:
                cause = getattr(exc, "orig", None)
                state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
                code = state if isinstance(state, str) and len(state) == 5 and state.isalnum() else ""
                W().emit({"event": "synthetic-budget-error", "reason": type(exc).__name__ + (":" + code if code else "")})
                raise
        async def settle(self, *args, **kwargs):
            try:
                return await super().settle(*args, **kwargs)
            except Exception as exc:
                W().emit({"event": "synthetic-budget-settlement-error",
                          "reason": type(exc).__module__ + "." + type(exc).__name__})
                raise

        async def hold(self, *args, **kwargs):
            try:
                return await super().hold(*args, **kwargs)
            except Exception as exc:
                W().emit({"event": "synthetic-budget-hold-error",
                          "reason": type(exc).__module__ + "." + type(exc).__name__})
                raise
    stack.enter_context(patch.object(budget_mod, "DailyBudget", ProfileBudget))
    stack.enter_context(patch.object(conn_mod, "load_verified_connection", fake_load))
    stack.enter_context(patch.object(conn_mod, "build_client", build_client))
    stack.enter_context(patch.object(attribution, "require_sentinels", fake_sentinels))
    stack.enter_context(patch.object(service.Service, "_owned", lambda self, fan: True))
    # timing of the synchronous boundaries (real ms), attributed per function
    for name in ("snapshot", "claim_input", "begin_attempt", "record_sent", "finish_input", "status",
                 "allowance_status", "activate_service", "renew", "input_claimed", "service_watermark"):
        timed(Ledger, name, "ledger." + name)
    timed(service.Service, "pending_fans", "service.pending_fans")
    timed(service, "fan_hold_reason", "service.fan_hold_reason")
    timed(runner, "check_local", "runner.check_local")
    timed(runner, "read_sources", "runner.read_sources")
    timed(runner, "begin_native_run", "runner.begin_native_run")
    timed(runner, "finish_native_run", "runner.finish_native_run")
    timed(runner, "verify_attribution", "runner.verify_attribution")
    return service, AccountScope


async def run_scenario(cfg: Cfg, db: Db, holder: dict, pg_container=None):
    service, AccountScope = holder["service"], holder["AccountScope"]
    import argparse as ap
    from db.engine import engine as async_engine  # noqa: F401  (imported so dispose in shutdown is the real one)

    if db.fleet_token and (cfg.name != "target_10k" or cfg.accounts != 1):
        raise RuntimeError("fleet workers may run exactly one creator scenario")
    if not db.fleet_token:
        db.reset()
    w = World(cfg)
    w.http = {a: FakeHTTP(w, a) for a in w.accts}
    holder["w"], holder["db"] = w, db
    if not db.fleet_token:
        db.seed(w)
    schedule = make_schedule(w)

    services, tasks = [], []
    for a in w.accts:
        args = ap.Namespace(account=a, fan=None, all_fans=True, handle="synthetic-creator", enable_send=True,
                            twin_context=False, content_manifest=None, service_id=None)
        svc = service.Service(args, intake_factory=FakeIntake)
        services.append(svc)

    stats = {"rss_kb_max": rss_kb(), "threads_max": threading.active_count(), "conn_samples": [], "conn_active_samples": []}
    stop = asyncio.Event()
    stats["wait_samples"] = []

    cpu0, rss0, pg0 = cpu_s(), rss_kb(), pg_cpu_s(pg_container)
    _, xact0 = db.census()
    for svc in services:
        tasks.append(asyncio.create_task(svc.run()))
    t_wait = time.monotonic()
    while any(s.started_at is None for s in services):
        await asyncio.sleep(0.02)
        if any(t.done() for t in tasks) or time.monotonic() - t_wait > 60:
            break
    if db.fleet_token:
        if any(s.started_at is None for s in services) or any(task.done() for task in tasks):
            raise RuntimeError("creator service did not start before the fleet barrier")
        barrier = Path(os.environ["LOAD_TEST_FLEET_BARRIER"])
        ready = barrier / f"ready-{cfg.account_offset}.json"
        ready.write_text(json.dumps({"pid": os.getpid(), "account": w.accts[0]}))
        deadline = time.monotonic() + 90
        while not (barrier / "start.json").exists():
            if time.monotonic() >= deadline or any(task.done() for task in tasks):
                raise RuntimeError("fleet startup barrier failed")
            await asyncio.sleep(0.05)
        start_epoch = json.loads((barrier / "start.json").read_text())["epoch"]
        await asyncio.sleep(max(0.0, start_epoch - time.time()))
    actual_start_epoch = time.time()
    w.base = w.sim_t()   # the scenario's timeline starts here; the clock itself never jumps (watermarks are in it)
    helpers = [asyncio.create_task(sampler(w, db, stats, stop)), asyncio.create_task(lag_probe(w, stop))]
    if os.environ.get("LOAD_TEST_PROFILE") == "1":
        async def profile_waits():
            while not stop.is_set():
                await asyncio.sleep(10)
                stats["wait_samples"].append({"scenario_seconds": round(w.rel(), 2), **capture_waits()})
        helpers.append(asyncio.create_task(profile_waits()))
    if cfg.prefill:
        await w.sleep_sim(1.0)
        for t, acct, fan in schedule:
            rec = w.new_inbound(acct, fan)
            await asyncio.to_thread(db.insert_in, acct, fan, rec)
        await w.sleep_sim(9.0)   # quiet window (7 s) passes
    else:
        helpers.append(asyncio.create_task(simulate_fans(w, db, schedule)))
    if cfg.control_at_s is not None:
        helpers.append(asyncio.create_task(controller(w, db)))
    t_start_real, t_start_sim = time.monotonic(), w.sim_t()
    # Stop arrivals at the configured duration, then give the last inputs their full SLA window.
    end_sim = t_start_sim + cfg.sim_minutes * 60 + (30 if cfg.name in ("target_100k", "target_10k") else 0)
    while w.sim_t() < end_sim:
        await asyncio.sleep(0.25)
        if cfg.stop_when_drained and all(not w.pending[a] for a in w.accts) and w.sim_t() > t_start_sim + 12:
            break
        if all(t.done() for t in tasks):
            break
    elapsed_real, elapsed_sim = time.monotonic() - t_start_real, w.sim_t() - t_start_sim
    cpu1, rss1, pg1 = cpu_s(), rss_kb(), pg_cpu_s(pg_container)
    _, xact1 = db.census()
    stop.set()
    for svc in services:
        svc.stop.set()
    done, pending = await asyncio.wait(tasks, timeout=60)
    crashed = [repr(t.exception()) for t in done if t.exception()]
    for t in pending:
        t.cancel()
    for h in helpers:
        h.cancel()
    helper_results = await asyncio.gather(*helpers, return_exceptions=True)
    helper_errors = [repr(r) for r in helper_results if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)]
    result = summarize(w, cfg, services, stats, elapsed_real, elapsed_sim, t_start_sim,
                       cpu1 - cpu0, rss0, rss1, xact1 - xact0, (pg1 - pg0) if pg0 is not None and pg1 is not None else None)
    # The throttle is enforced at ADMISSION (begin_attempt), not at the wire: read the admission instants back.
    admissions = {}
    for acct, created in db.run("SELECT account_id,created_at FROM twin_bridge_send_attempts WHERE turn_id LIKE 'native-pilot-%' ORDER BY account_id,created_at") or []:
        admissions.setdefault(acct, []).append(created if isinstance(created, datetime) else datetime.fromisoformat(str(created)))
    for acct, times in admissions.items():
        gaps = [(y - x).total_seconds() for x, y in zip(times, times[1:])]
        if acct in result["accounts"]:
            result["accounts"][acct]["admissions"] = len(times)
            result["accounts"][acct]["min_admission_gap_s"] = round(min(gaps), 3) if gaps else None
            result["accounts"][acct]["throttle_respected"] = (min(gaps) >= cfg.throttle_s - 0.05) if gaps else True
            result["accounts"][acct]["min_wire_gap_s_informational"] = result["accounts"][acct].pop("min_gap_between_sends_s")
    result["offered_load"] = {
        "scheduled_messages": len(schedule),
        "generated_messages": sum(len(records) for records in w.inbound.values()),
        "persisted_messages": len(w.arrival_lag_s) if not cfg.prefill else sum(len(records) for records in w.inbound.values()),
        "max_ingress_delay_s": round(max(w.arrival_lag_s, default=0.0), 3),
        "arrival_window_s": max(0.0, cfg.sim_minutes * 60 - 30),
        "scheduled_messages_per_hour": round(len(schedule) * 3600 / (cfg.sim_minutes * 60 - 30), 3)
            if cfg.sim_minutes * 60 > 30 else 0,
    }
    result["start_epoch"] = actual_start_epoch
    result["worker_database_role"] = os.environ.get("ONLYFANS_PROCESS_ROLE")
    from native_pilot.delivery_pool import configured_delivery_concurrency
    result["delivery_concurrency"] = configured_delivery_concurrency()
    result["async_pool"] = {"size": async_engine.pool.size(), "max_overflow": async_engine.pool._max_overflow}
    result["wait_samples"] = stats["wait_samples"]
    result["service_crashed"] = crashed
    result["helper_errors"] = helper_errors
    result["runtime_errors"] = [
        f"{reason} ({count})" for reason, count in w.event_reasons.items()
        if reason.startswith(("held:service-error:", "held:shutdown-turn:",
                              "synthetic-budget-error:", "synthetic-budget-settlement-error:",
                              "synthetic-budget-hold-error:"))
    ]
    result["service_pool"] = {"sync_store_pool": "pool_size=2,max_overflow=4 per URL (twin_bridge.store)",
                              "async_engine_pool": result["async_pool"]}
    return result


def summarize(w: World, cfg: Cfg, services, stats, elapsed_real, elapsed_sim, t0, cpu, rss0, rss1, xacts, pg_cpu):
    minutes = elapsed_sim / 60.0
    out = {"scenario": cfg.name, "config": {k: v for k, v in asdict(cfg).items()},
           "sim_seconds": round(elapsed_sim, 1), "real_seconds": round(elapsed_real, 1), "accounts": {}}
    ok_posts = [p for p in w.posts if p["ok"]]
    bad_posts = [p for p in w.posts if not p["ok"]]
    for a in w.accts:
        mine = sorted(p["t"] for p in ok_posts if p["acct"] == a)
        gaps = [y - x for x, y in zip(mine, mine[1:])]
        # rolling 10 minute peak
        peak = 0
        j = 0
        for i, t in enumerate(mine):
            while mine[j] < t - 600:
                j += 1
            peak = max(peak, i - j + 1)
        lat, censored, waits = [], [], []
        never = starved = over200 = 0
        recs = [(f, r) for (acct, f), rs in w.inbound.items() if acct == a for r in rs]
        for f, r in recs:
            if r["answered_t"] is not None:
                lat.append(r["answered_t"] - r["t"])
            else:
                censored.append(elapsed_sim + t0 - r["t"])
        first_unanswered = {}
        for f, r in recs:
            first_unanswered.setdefault(f, []).append(r)
        for f, rs in first_unanswered.items():
            r = min(rs, key=lambda x: x["t"])
            wait = (r["answered_t"] - r["t"]) if r["answered_t"] is not None else (elapsed_sim + t0 - r["t"])
            expected = r["ahead"] * max(cfg.throttle_s, 1)
            if r["answered_t"] is None:
                never += 1
            if wait > 2 * expected + 180:
                starved += 1
            if r["ahead"] > 200:
                over200 += 1
        qs = [(t, d, age) for t, acct, d, age in w.queue_samples if acct == a]
        out["accounts"][a] = {
            "inbound_messages": len(recs),
            "replies_sent": len(mine),
            "replies_per_min": round(len(mine) / minutes, 2) if minutes else None,
            "peak_replies_per_10min": peak,
            "min_gap_between_sends_s": round(min(gaps), 2) if gaps else None,
            "throttle_respected": (min(gaps) >= cfg.throttle_s - 0.5) if gaps else True,
            "provider_429s": sum(1 for p in bad_posts if p["acct"] == a),
            "reply_latency_s": {**quantiles(lat), "n": len(lat), "max": round(max(lat), 1) if lat else None},
            "delivered_over_30s": sum(value > 30 for value in lat),
            "unanswered_over_30s": sum(value > 30 for value in censored),
            "unanswered_at_end": len(censored),
            "unanswered_oldest_age_s": round(max(censored), 1) if censored else 0,
            "latency_incl_unanswered_s": quantiles(lat + censored),
            "max_queue_age_s": round(max((q[2] for q in qs), default=0), 1),
            "max_queue_depth_fans": max((q[1] for q in qs), default=0),
            "fans_never_answered": never,
            "starved_fans_vs_fifo": starved,
            "fans_arriving_behind_over_200": over200,
            "model_calls": w.model_calls[a], "model_failures": w.model_failures[a],
        }
    out["totals"] = {"replies": len(ok_posts), "replies_per_real_second": round(len(ok_posts) / elapsed_real, 2) if elapsed_real else None,
                     "process_cpu_ms_per_reply": round(1000 * cpu / len(ok_posts), 1) if ok_posts else None,
                     "postgres_cpu_ms_per_reply": round(1000 * pg_cpu / len(ok_posts), 1) if ok_posts and pg_cpu is not None else None,
                     "replies_per_min_all_accounts": round(len(ok_posts) / minutes, 2) if minutes else None,
                     "sends_attempted": len(w.posts), "provider_429s": len(bad_posts)}
    # control acknowledgement
    if w.control:
        rows = []
        for c in w.control:
            if c["kind"] == "resume":
                continue
            after = [p for p in w.posts if p["acct"] == c["acct"] and p["fan"] == c["fan"] and p["t"] > c["t"]
                     and not any(r["kind"] == "resume" and r["acct"] == c["acct"] and r["fan"] == c["fan"] and r["t"] <= p["t"]
                              for r in w.control)]
            ref = [r for r in w.refusals if r["acct"] == c["acct"] and r["fan"] == c["fan"] and r["t"] >= c["t"]]
            rows.append({**c, "wire_posts_after": len(after), "first_refusal_after_s": round(ref[0]["t"] - c["t"], 2) if ref else None,
                         "refusal": ref[0]["reason"] if ref else None})
        resumed = []
        for c in w.control:
            if c["kind"] != "resume":
                continue
            nxt = [p["t"] for p in ok_posts if p["acct"] == c["acct"] and p["fan"] == c["fan"] and p["t"] > c["t"]]
            resumed.append(nxt[0] - c["t"] if nxt else None)
        inflight = [r for r in rows if r.get("in_flight")]
        out["control"] = {
            "paused_or_taken_over": len(rows),
            "sends_after_pause_or_takeover": sum(r["wire_posts_after"] for r in rows),
            "in_flight_fans": len(inflight),
            "in_flight_refused_before_wire": sum(1 for r in inflight if r["wire_posts_after"] == 0),
            "ack_latency_s_in_flight": {**quantiles([r["first_refusal_after_s"] for r in inflight if r["first_refusal_after_s"] is not None], (0.5, 1.0)),
                                        "refusal_reasons": sorted({r["refusal"] for r in inflight if r["refusal"]})},
            "resumed_fans": len(resumed),
            "resumed_answered": sum(1 for x in resumed if x is not None),
            "resume_to_reply_s": quantiles([x for x in resumed if x is not None]),
        }
    # process
    conns, active = stats["conn_samples"], stats["conn_active_samples"]
    out["process"] = {
        "cpu_seconds": round(cpu, 2),
        "cpu_pct_of_one_core_real": round(100 * cpu / elapsed_real, 1) if elapsed_real else None,
        "cpu_per_sim_second_PRODUCTION_FRACTION": round(cpu / elapsed_sim, 4) if elapsed_sim else None,
        "rss_mb_start": round(rss0 / 1024, 1), "rss_mb_end": round(rss1 / 1024, 1), "rss_mb_peak": round(stats["rss_kb_max"] / 1024, 1),
        "threads_peak": stats["threads_max"],
        "event_loop_lag_real_ms": quantiles(w.lag, (0.5, 0.95, 0.99)) | {"max": round(max(w.lag), 1) if w.lag else None},
        "host_cpus": os.cpu_count(),
    }
    out["database"] = {
        "connections_to_app_db": {"max": max(conns, default=0), "mean": round(statistics.fmean(conns), 1) if conns else 0,
                                  "active_max": max(active, default=0)},
        "transactions": xacts, "transactions_per_sim_second": round(xacts / elapsed_sim, 1) if elapsed_sim else None,
        "postgres_cpu_seconds": round(pg_cpu, 2) if pg_cpu is not None else None,
        "postgres_cpu_per_sim_second": round(pg_cpu / elapsed_sim, 4) if pg_cpu is not None and elapsed_sim else None,
    }
    out["timers"] = w.timers.report()
    out["events"] = dict(sorted(w.events.items()))
    out["event_reasons"] = dict(sorted(w.event_reasons.items(), key=lambda kv: -kv[1])[:20])
    out["fidelity_note"] = ("loop lag x scale = distortion in simulated seconds: p95 lag %s ms real = %s ms simulated"
                            % (quantiles(w.lag, (0.95,))["p95"], round((quantiles(w.lag, (0.95,))["p95"] or 0) * cfg.scale, 1)))
    return out


# â”€â”€ entry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def run_all(args, names=None):
    admin = args.pg_url or os.environ.get("ONLYFANS_TEST_PG_URL")
    if not admin:
        sys.exit("FAILED: --pg-url / ONLYFANS_TEST_PG_URL (admin URL of a disposable LOCAL PostgreSQL) is required")
    os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
    # Fleet workers use the deployed worker role and its bounded DB pool.
    # Older standalone scenarios retain their intentionally role-free fixture.
    if os.environ.get("LOAD_TEST_FLEET_TOKEN"):
        os.environ["ONLYFANS_PROCESS_ROLE"] = "worker"
    else:
        os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
    os.environ["TWIN_BRIDGE_ENABLED"] = "0"
    os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"
    os.environ["W7_WEBHOOK_DISPATCH_DISABLED"] = "1"
    os.environ.pop("ONLYFANS_FASTT_SCOPE", None)
    db = Db(admin)
    os.environ["DATABASE_URL"] = db.async_url          # before any application import
    import logging
    logging.disable(logging.CRITICAL)
    cfgs = scenarios(args)
    chosen = names or (list(cfgs) if args.scenario == "all" else args.scenario.split(","))
    for n in chosen:
        if n not in cfgs:
            sys.exit("unknown scenario " + n + "; known: " + ", ".join(cfgs))
    db.create()
    results = []
    try:
        db.bootstrap()
        holder = {}
        with ExitStack() as stack:
            holder["service"], holder["AccountScope"] = install_patches(stack, holder)

            async def go():
                for n in chosen:
                    cfg = cfgs[n]
                    print(f"[load] {n}: {cfg.accounts} account(s) x {cfg.fans} fans, {cfg.sim_minutes} sim min at x{cfg.scale}",
                          file=sys.stderr, flush=True)
                    results.append(await run_scenario(cfg, db, holder, args.pg_container))
                    r = results[-1]
                    print(f"[load] {n}: replies={r['totals']['replies']} per-min={r['totals']['replies_per_min_all_accounts']} "
                          f"real={r['real_seconds']}s", file=sys.stderr, flush=True)
            asyncio.run(go())
    finally:
        db.drop()
    return {"harness": "native_pilot.tools.load_test", "python": sys.version.split()[0], "results": results}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", default="all")
    p.add_argument("--pg-url", help="admin URL of a disposable LOCAL PostgreSQL (default ONLYFANS_TEST_PG_URL)")
    p.add_argument("--pg-container", help="docker container name of that PostgreSQL, to also report its CPU (optional)")
    p.add_argument("--accounts", type=int, default=2)
    p.add_argument("--account-offset", type=int, default=0, choices=range(200))
    p.add_argument("--fans", type=int, default=250)
    p.add_argument("--scale", type=float, default=30.0, help="simulated seconds per real second")
    p.add_argument("--throttle", type=float, default=20, help="of_delivery_policy.throttle_seconds (production: 20)")
    p.add_argument("--concurrent-fans", type=int, default=1, choices=range(1,65))
    p.add_argument("--sim-minutes", type=float, default=60.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out")
    p.add_argument("--profile", action="store_true", help="capture task/thread wait locations without message text or credentials")
    p.add_argument("--require-target-sla", action="store_true", help="fail unless unscaled target traffic and every reply meet the 30-second test SLA")
    p.add_argument("--concurrent-sends", type=int, choices=range(1, 17), default=1)
    p.add_argument("--provider-latency-seconds", type=float, default=0.2)
    p.add_argument("--history-latency-seconds", type=float, default=0.0)
    args = p.parse_args(argv)
    for value in [args.provider_latency_seconds, args.history_latency_seconds]:
        if not math.isfinite(value) or not 0 <= value <= 30:
            p.error("synthetic request latency must be finite and between zero and thirty seconds")
    os.environ["FASTT_MAX_CONCURRENT_SENDS"] = str(args.concurrent_sends)
    os.environ["FASTT_MAX_CONCURRENT_FANS"] = str(args.concurrent_fans)
    if args.profile:
        os.environ["LOAD_TEST_PROFILE"] = "1"
    result = run_all(args)
    if args.require_target_sla:
        for row in result["results"]:
            row["capacity_verdict"] = evaluate_capacity(row)
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    if args.require_target_sla and any(not row["capacity_verdict"]["passed"] for row in result["results"]):
        print("FAILED: capacity acceptance criteria were not met; see capacity_verdict in the report", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
