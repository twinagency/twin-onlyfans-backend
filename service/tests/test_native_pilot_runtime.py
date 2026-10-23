"""Pilot runtime mapping with synthetic data only: no network, no live DB, fake keys.

Covers the account-id boundary, the role-guard exception, loading the verified
connection (fail closed), the account-scoped model credential + daily budget,
and AI/human attribution for FASTT's own rules.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

CHECKS = 0
OWNER = 7001
ACCOUNT = "of-7001"
FAN = 318022380
TENANT = "tenant-synthetic"
KEY = "42" * 32
SAVED_MODEL = "openrouter/deepseek-v4.1-flash"
FAKE_MODEL_KEY = "synthetic-model-key-never-live"
FAKE_COOKIE = "synthetic-session-cookie"
FAKE_PROXY = "http://synthetic:proxy-secret@proxy.example:8080"


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def refuses(reason, fn):
    from native_pilot.ledger import Refused
    try:
        fn()
    except Refused as exc:
        check(exc.reason == reason, (reason, exc.reason))
    else:
        raise AssertionError("missing refusal " + reason)


async def async_refuses(reason, fn):
    from native_pilot.ledger import Refused
    try:
        await fn()
    except Refused as exc:
        check(exc.reason == reason, (reason, exc.reason))
        return exc
    raise AssertionError("missing refusal " + reason)


def capture():
    return {"headers": {"user_id": str(OWNER), "x_bc": "synthetic-bc", "x_of_rev": "synthetic-rev", "user_agent": "Synthetic Test"},
            "signing": {"rules": {"static_param": "synthetic-static", "start": "fixture", "end": "offline",
                                  "checksum_indexes": list(range(32)), "checksum_constant": 17}},
            "cookies": [{"name": "sess", "value": FAKE_COOKIE, "domain": ".onlyfans.com", "path": "/"}]}


# ── A. the account-id boundary ──────────────────────────────────────────────
def scope_cases():
    from native_pilot.scope import AccountScope
    s = AccountScope.parse("of-176774528")
    check(s.db_id == "of-176774528" and s.provider_id == "176774528" and s.twin_form)
    b = AccountScope.parse("176774528")
    check(b.db_id == b.provider_id == "176774528" and not b.twin_form)
    check(s.matches("of-176774528") and s.matches("176774528") and not s.matches("176774529") and not s.matches(None))
    for bad in ("", "of-", "of-0", "of-01", "OF-1", "of-1x", "of--1", " of-1", "of-1 ", "of-1\n", "1.0", "0", "-1",
                "of-" + "9" * 17, "٣", "of-٣", None, 5, b"of-1", "of-of-1"):
        check(not AccountScope.valid(bad), bad)
        refuses("invalid-account", lambda bad=bad: AccountScope.parse(bad))
    # the pilot's engine, ledger and model all accept both forms and nothing else
    from native_pilot.engine import NativeEngine
    from native_pilot.model import NativePilotModel, NativePilotModelError
    for acct in ("of-176774528", "176774528"):
        NativeEngine(acct, FAN, chat=None, send=None)
    for acct in ("of-", "x", "of-0"):
        try:
            NativeEngine(acct, FAN, chat=None, send=None)
        except ValueError:
            check(True)
        else:
            raise AssertionError(acct)
    try:
        NativePilotModel(api_key="k", account_id="not-an-account", fan_id=FAN, deadline=time.monotonic() + 5,
                         should_stop=lambda: False, on_event=lambda e: None)
    except NativePilotModelError as exc:
        check(str(exc) == "model-account-invalid")
    NativePilotModel(api_key="k", account_id="of-176774528", fan_id=FAN, deadline=time.monotonic() + 5,
                     should_stop=lambda: False, on_event=lambda e: None)


# ── B. role guard ───────────────────────────────────────────────────────────
def role_guard_cases():
    from onlyfans_runtime import role_guard as rg
    def blocked(account=None):
        try:
            rg.require_legacy_provider_role(account)
        except RuntimeError:
            return True
        return False
    base = {k: v for k, v in os.environ.items() if k != "ONLYFANS_PROCESS_ROLE"}
    with patch.dict(os.environ, base, clear=True):
        rg._PILOT_PROVIDER_ID = None
        check(not blocked() and not blocked("1"), "unset role is the legacy app: unchanged")
        rg.allow_native_pilot_provider_access("7001")        # no-op in effect without a role
        rg._PILOT_PROVIDER_ID = None
    for role in ("api", "other", ""):
        with patch.dict(os.environ, {**base, "ONLYFANS_PROCESS_ROLE": role}, clear=True):
            rg._PILOT_PROVIDER_ID = None
            check(blocked() and blocked("7001"), role)
            try:
                rg.allow_native_pilot_provider_access("7001")
            except RuntimeError:
                check(True)
            else:
                raise AssertionError("allowance must not open the " + role + " role")
            check(blocked("7001"))
    with patch.dict(os.environ, {**base, "ONLYFANS_PROCESS_ROLE": "worker"}, clear=True):
        rg._PILOT_PROVIDER_ID = None
        check(blocked() and blocked("7001"), "worker is closed until the explicit allowance")
        for bad in ("of-7001", "", "70 01", 7001, None):
            try:
                rg.allow_native_pilot_provider_access(bad)
            except ValueError:
                check(True)
            else:
                raise AssertionError(bad)
        check(blocked())
        rg.allow_native_pilot_provider_access("7001")
        check(not blocked() and not blocked("7001") and not blocked(7001), "the one named account passes")
        check(blocked("7002") and blocked("of-7001"), "any other account stays refused")
        rg.allow_native_pilot_provider_access("7001")        # same account: idempotent
        try:
            rg.allow_native_pilot_provider_access("7002")
        except RuntimeError:
            check(True)
        else:
            raise AssertionError("allowance must not be rebindable")
    rg._PILOT_PROVIDER_ID = None


# ── C. verified connection → session, proxy, model credential ───────────────
async def seed_connection(tmp, **over):
    from sqlalchemy import select
    from db.engine import engine, get_session
    from db.models import Base, Account, AccountAiConfig, OfCreatorBinding
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_connection_models import OfProviderConnection
    from onlyfans_runtime import connections

    expires = over.get("expires") or (datetime.now(timezone.utc) + timedelta(hours=2))
    value = {"ownerId": OWNER, "capture": capture(), "xHash": "synthetic-hash", "proxyUrl": FAKE_PROXY,
             "expiresAt": expires.isoformat(), "model": {"modelId": SAVED_MODEL, "apiKey": FAKE_MODEL_KEY}}
    value.update(over.get("value", {}))
    revision = 4
    if os.environ.get("NATIVE_PILOT_TEST_SCHEMA_READY") != "1":   # PG runs bring their own 0091 schema
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
    async with get_session() as s:
        for table in (OfProviderConnection, OfRuntime, OfCreatorBinding, AccountAiConfig):
            for row in (await s.scalars(select(table))).all():
                await s.delete(row)
            await s.flush()
        if await s.get(Account, ACCOUNT) is not None:
            await s.delete(await s.get(Account, ACCOUNT))
            await s.flush()
        s.add(Account(id=ACCOUNT, nickname="Synthetic"))
        s.add(OfCreatorBinding(id="binding-1", tenant_id=TENANT, creator_id="creator-1", creator_name="Synthetic",
                               account_id=ACCOUNT, handle="synthetic", channel="onlyfans",
                               revoked_at=over.get("revoked_at")))
        if over.get("runtime_binding"):
            s.add(OfCreatorBinding(id=over["runtime_binding"], tenant_id=TENANT, creator_id="creator-2", creator_name="Other",
                                   account_id=ACCOUNT, handle="other", channel="onlyfans", revoked_at=datetime.utcnow()))
        await s.flush()
        s.add(OfRuntime(account_id=ACCOUNT, binding_id=over.get("runtime_binding", "binding-1"),
                        activated_at=datetime.utcnow(), enabled=False))
        s.add(AccountAiConfig(account_id=ACCOUNT, model=over.get("saved_model", SAVED_MODEL),
                              daily_cost_cap_cents=over.get("cap", 100)))
        await s.flush()
        s.add(OfProviderConnection(
            account_id=ACCOUNT, binding_id="binding-1", owner_id=over.get("owner_id", OWNER), revision=revision,
            sealed_config=over.get("sealed") or connections._seal(value, TENANT, ACCOUNT, "binding-1", revision),
            expires_at=expires.astimezone(timezone.utc).replace(tzinfo=None),
            status=over.get("status", "verified"), verified_at=over.get("verified_at", datetime.utcnow()),
            last_error=over.get("last_error"), updated_at=datetime.utcnow()))
    return value, revision


async def connection_cases(tmp):
    from native_pilot import connection as conn_mod
    from native_pilot.scope import AccountScope
    scope = AccountScope.parse(ACCOUNT)
    os.environ["ONLYFANS_CONNECTION_KEY"] = KEY

    await seed_connection(tmp)
    conn = await conn_mod.load_verified_connection(scope)
    check(conn.scope == scope and conn.tenant_id == TENANT and conn.revision == 4 and conn.owner_id == OWNER)
    check(conn.proxy_url == FAKE_PROXY and conn.x_hash == "synthetic-hash" and conn.capture["headers"]["user_id"] == str(OWNER))
    check(conn.model_id == SAVED_MODEL and conn.model_api_key == FAKE_MODEL_KEY and conn.daily_cost_cap_cents == 100)
    shown = repr(conn)
    check(FAKE_PROXY not in shown and FAKE_MODEL_KEY not in shown and FAKE_COOKIE not in shown and "synthetic-hash" not in shown, "no secrets in repr")

    # fail closed: every condition below is a separate fixed public code
    await async_refuses("connection-account-form", lambda: conn_mod.load_verified_connection(AccountScope.parse(str(OWNER))))
    other = AccountScope.parse("of-7002")
    await async_refuses("connection-missing", lambda: conn_mod.load_verified_connection(other))
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    cases = [
        ("connection-not-verified", dict(status="pending")), ("connection-not-verified", dict(status="failed")),
        ("connection-not-verified", dict(status="paused")), ("connection-not-verified", dict(status="expired")),
        ("connection-not-verified", dict(verified_at=None)),
        ("connection-check-failing", dict(last_error="provider_connection_check_retry")),
        ("connection-check-failing", dict(last_error="provider_authentication_required")),
        ("connection-expired", dict(expires=datetime.now(timezone.utc) - timedelta(seconds=1))),
        ("connection-owner-mismatch", dict(owner_id=OWNER + 1)),
        ("connection-scope", dict(revoked_at=datetime.utcnow())),
        ("connection-scope", dict(runtime_binding="binding-other")),
        ("connection-credentials-unavailable", dict(sealed="not-base64!")),
        ("connection-session-invalid", dict(value={"capture": {**capture(), "headers": {**capture()["headers"], "user_id": "9999"}}})),
        ("connection-session-invalid", dict(value={"capture": {**capture(), "cookies": []}})),
        ("connection-session-invalid", dict(value={"proxyUrl": "ftp://nope"})),
        ("model-credential-missing", dict(value={"model": None})),
        ("model-credential-missing", dict(value={"model": {"modelId": SAVED_MODEL}})),
        ("model-credential-mismatch", dict(value={"model": {"modelId": "deepseek-v4-flash", "apiKey": FAKE_MODEL_KEY}})),
        ("model-config-invalid", dict(saved_model="not-a-registered-model")),
        ("model-route-unsupported", dict(saved_model="deepseek-v4-flash",
                                         value={"model": {"modelId": "deepseek-v4-flash", "apiKey": FAKE_MODEL_KEY}})),
        ("model-budget-unset", dict(cap=-1)), ("model-budget-unset", dict(cap=1_000_001)),
    ]
    for code, over in cases:
        await seed_connection(tmp, **over)
        exc = await async_refuses(code, lambda: conn_mod.load_verified_connection(scope))
        check(FAKE_MODEL_KEY not in str(exc) and FAKE_PROXY not in str(exc) and FAKE_COOKIE not in str(exc))
    # key handling: absent / wrong key both look like the same unavailable credential
    await seed_connection(tmp)
    for bad_key in ("", "zz" * 32, "43" * 32):
        with patch.dict(os.environ, {"ONLYFANS_CONNECTION_KEY": bad_key}):
            await async_refuses("connection-credentials-unavailable", lambda: conn_mod.load_verified_connection(scope))
    # credentials sealed for another revision/binding cannot be moved onto this row
    value, _ = await seed_connection(tmp)
    from onlyfans_runtime import connections
    await seed_connection(tmp, sealed=connections._seal(value, TENANT, ACCOUNT, "binding-1", 99))
    await async_refuses("connection-credentials-unavailable", lambda: conn_mod.load_verified_connection(scope))

    # build the client from the verified record: no network, role guard opened for this owner only
    await seed_connection(tmp)
    conn = await conn_mod.load_verified_connection(scope)
    from onlyfans_runtime import role_guard as rg
    def deny(*a, **kw):
        raise AssertionError("network forbidden")
    with patch.dict(os.environ, {"ONLYFANS_PROCESS_ROLE": "worker"}), patch.object(socket.socket, "connect", deny):
        rg._PILOT_PROVIDER_ID = None
        client = conn_mod.build_client(conn)
        check(client.account_id == str(OWNER) and client.user_id == str(OWNER) and client._x_hash == "synthetic-hash")
        check(client.proxy_url == FAKE_PROXY and client.proxy_label == "twin-connection")
        check(client.http.proxies.get("https") == FAKE_PROXY and client.http.proxies.get("http") == FAKE_PROXY)
        check(client._signed_headers("https://onlyfans.com/api2/v2/users/me")["X-Hash"] == "synthetic-hash", "no warm-up request")
        client.http.close()
        check(rg._PILOT_PROVIDER_ID == str(OWNER))
        # the opened guard serves this owner only
        from of_client import OFClient
        try:
            rg.require_legacy_provider_role("9999")
        except RuntimeError:
            check(True)
        else:
            raise AssertionError("another account passed the pilot guard")
        rg._PILOT_PROVIDER_ID = None
    with patch.dict(os.environ, {"ONLYFANS_PROCESS_ROLE": "api"}):
        rg._PILOT_PROVIDER_ID = None
        try:
            conn_mod.build_client(conn)
        except RuntimeError:
            check(True)
        else:
            raise AssertionError("api role must stay closed")
    import dataclasses
    stale = dataclasses.replace(conn, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    try:
        conn_mod.build_client(stale)
    except Exception as exc:
        check(getattr(exc, "reason", "") == "connection-expired")
    else:
        raise AssertionError("expired connection built a client")
    # the loader wrote nothing
    from sqlalchemy import select
    from db.engine import get_session
    from db.onlyfans_connection_models import OfProviderConnection
    async with get_session() as s:
        row = await s.get(OfProviderConnection, ACCOUNT)
        check(row.revision == 4 and row.status == "verified")


# ── D. model credential + the existing daily budget ─────────────────────────
def completion(cost=0.00018, tokens=(120, 30)):
    from native_pilot.model import MODEL
    return {"model": MODEL, "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "cost": cost}}


async def budget_cases(tmp):
    from sqlalchemy import select
    from db.engine import get_session
    from db.models import GrokCall, GrokDailyCost
    from llm_client import LLMCapExceeded
    from native_pilot.budget import DailyBudget, BudgetRefused
    from native_pilot.model import ENDPOINT, NativePilotModel, NativePilotModelError
    from native_pilot.scope import AccountScope

    scope = AccountScope.parse(ACCOUNT)
    await seed_connection(tmp, cap=100)
    now = datetime(2026, 10, 24, 12, 0, 0)
    budget = DailyBudget(scope, SAVED_MODEL, endpoint=ENDPOINT, clock=lambda: now)
    check(budget.provider == "openrouter")

    async def ledger():
        async with get_session() as s:
            row = await s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id == ACCOUNT))
            return (row.cost_cents, row.call_count, row.provider, row.day) if row else None

    async def calls():
        async with get_session() as s:
            return [(c.status, c.cost_cents, c.account_id, c.fan_id, c.purpose, c.provider, c.endpoint, c.prompt_json)
                    for c in (await s.scalars(select(GrokCall).order_by(GrokCall.id))).all()]

    est = budget.estimate(40000, 400)
    check(est == int(-(-(40000 * 0.044 + 400 * 0.132) // 10)) or est > 0)
    r = await budget.reserve(fan_id=FAN, request_bytes=40000, max_output_tokens=400)
    check(r.reserved_units == est and await ledger() == (est, 1, "openrouter", "2026-10-24"))
    pending = (await calls())[0]
    check(pending[0] == "pending" and pending[2:6] == (ACCOUNT, FAN, "ai_chatter", "openrouter") and pending[6] == ENDPOINT)
    check("synthetic" not in pending[7] and json.loads(pending[7]) == {"native_pilot": True, "request_bytes": 40000}, "no prompt text stored")
    # provider-reported cost replaces the estimate (USD x 10_000 ledger units)
    check(await budget.settle(r, cost_usd=0.00234, tokens_in=120, tokens_out=30) == 23)
    check(await ledger() == (23, 1, "openrouter", "2026-10-24"))
    done = (await calls())[0]
    check(done[0] == "done" and done[1] == 23)
    try:
        await budget.settle(r, cost_usd=0.1, tokens_in=1, tokens_out=1)
    except BudgetRefused as exc:
        check(exc.reason == "budget-evidence-changed", "a call settles once")
    # no reported cost: registry rate x reported tokens
    r2 = await budget.reserve(fan_id=FAN, request_bytes=1000, max_output_tokens=400)
    check(await budget.settle(r2, cost_usd=None, tokens_in=1000, tokens_out=100) == budget._units(1000, 100))
    # nothing usable reported: reservation is retained and the call is unknown
    r3 = await budget.reserve(fan_id=FAN, request_bytes=1000, max_output_tokens=400)
    before = (await ledger())[0]
    check(await budget.settle(r3, cost_usd=None, tokens_in=None, tokens_out=None) is None)
    check((await ledger())[0] == before and (await calls())[-1][0] == "unknown")
    # hold keeps the reservation
    r4 = await budget.reserve(fan_id=FAN, request_bytes=1000, max_output_tokens=400)
    before = (await ledger())[0]
    await budget.hold(r4)
    check((await ledger())[0] == before and (await calls())[-1][0] == "unknown")
    await budget.hold(r4)  # idempotent
    # the cap is the account's saved cap (100 cents = 10_000 units), not a new number
    async with get_session() as s:
        row = await s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id == ACCOUNT))
        row.cost_cents = 10_000 - est + 1
    try:
        await budget.reserve(fan_id=FAN, request_bytes=40000, max_output_tokens=400)
    except LLMCapExceeded:
        check(True)
    else:
        raise AssertionError("reservation passed the saved cap")
    async with get_session() as s:
        row = await s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id == ACCOUNT))
        row.cost_cents = 10_000 - est
    await budget.reserve(fan_id=FAN, request_bytes=40000, max_output_tokens=400)   # exactly at the cap is allowed
    # another UTC day starts a fresh row; another account is untouched
    tomorrow = DailyBudget(scope, SAVED_MODEL, endpoint=ENDPOINT, clock=lambda: now + timedelta(days=1))
    await tomorrow.reserve(fan_id=FAN, request_bytes=1000, max_output_tokens=400)
    async with get_session() as s:
        days = sorted(r.day for r in (await s.scalars(select(GrokDailyCost))).all())
        check(days == ["2026-10-24", "2026-10-25"] and all(r.account_id == ACCOUNT for r in (await s.scalars(select(GrokDailyCost))).all()))
    for bad in (dict(fan_id=0, request_bytes=10), dict(fan_id=FAN, request_bytes=0), dict(fan_id=True, request_bytes=10)):
        try:
            await budget.reserve(max_output_tokens=400, **bad)
        except BudgetRefused as exc:
            check(exc.reason == "budget-request-invalid")
        else:
            raise AssertionError(bad)
    try:
        DailyBudget(scope, "not-a-model", endpoint=ENDPOINT)
    except BudgetRefused as exc:
        check(exc.reason == "budget-model-unknown")

    # ── through NativePilotModel: the credential is the account's, spend is held ──
    async with get_session() as s:
        for row in (await s.scalars(select(GrokDailyCost))).all():
            await s.delete(row)
        for row in (await s.scalars(select(GrokCall))).all():
            await s.delete(row)
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion())
    events = []
    def make(handler, budget_obj=budget, **kw):
        return NativePilotModel(api_key=FAKE_MODEL_KEY, account_id=ACCOUNT, fan_id=FAN, deadline=time.monotonic() + 60,
                                should_stop=lambda: False, on_event=events.append, budget=budget_obj,
                                transport=httpx.MockTransport(handler), **kw)
    async def ask(m):
        return await m.chat(model=__import__("native_pilot.model", fromlist=["MODEL"]).MODEL,
                            messages=[{"role": "user", "content": "hi"}], purpose="ai_chatter",
                            account_id=ACCOUNT, fan_id=FAN)
    m = make(handler)
    result = await ask(m)
    check(result.content == "ok" and len(seen) == 1)
    check(seen[0].headers["authorization"] == "Bearer " + FAKE_MODEL_KEY, "account-scoped credential is what is sent")
    led = await ledger()
    check(led[0] == 2 and led[1] == 1, led)        # $0.00018 -> 2 units, estimate replaced
    check((await calls())[0][0] == "done")
    # a refusal by the cap means NO request
    async with get_session() as s:
        row = await s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id == ACCOUNT))
        row.cost_cents = 10_000
    seen.clear()
    m = make(handler)
    try:
        await ask(m)
    except NativePilotModelError as exc:
        check(str(exc) == "model-budget-exhausted" and m.stopped_reason == "model-budget-exhausted")
    check(seen == [], "no HTTP when the budget refuses")
    async with get_session() as s:
        row = await s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id == ACCOUNT))
        row.cost_cents = 0
    # unproven outcomes keep the reservation (HTTP error, bad body, timeout)
    for label, bad in (("http", lambda r: httpx.Response(500, json={})),
                       ("body", lambda r: httpx.Response(200, content=b"not json")),
                       ("timeout", lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic")))):
        before = (await ledger())[0]
        m = make(bad)
        try:
            await ask(m)
        except NativePilotModelError:
            check(m.stopped_reason is not None, label)
        after = (await ledger())[0]
        check(after > before, (label, before, after))
        check((await calls())[-1][0] == "unknown", label)
    # accounting that cannot be written stops the pilot rather than hiding spend
    class BrokenSettle(DailyBudget):
        async def settle(self, *a, **k):
            raise RuntimeError("synthetic")
    m = make(handler, BrokenSettle(scope, SAVED_MODEL, endpoint=ENDPOINT, clock=lambda: now))
    try:
        await ask(m)
    except NativePilotModelError as exc:
        check(str(exc) == "model-budget-settle-failed")
    check((await calls())[-1][0] == "unknown")
    # the pilot's own limits are unchanged by the budget hook
    check(make(handler).max_calls == 80 and make(handler).max_tokens == 400)
    json_body = json.loads(seen[-1].content)
    check(json_body["max_tokens"] == 400 and json_body["model"] == "deepseek/deepseek-v4.1-flash")


# ── E. attribution FASTT can use ────────────────────────────────────────────
async def attribution_cases(tmp):
    from sqlalchemy import select
    from db.engine import get_session
    from db.models import Account, Employee, Fan, Message
    from native_pilot import attribution
    from native_pilot.ledger import Refused
    from automations import ai_chatter as fastt

    async with get_session() as s:
        for row in (await s.scalars(select(Employee))).all():
            await s.delete(row)
        for row in (await s.scalars(select(Message))).all():
            await s.delete(row)
    found = await attribution.read_sentinels()
    check(found == attribution.Sentinels(None, None))
    await async_refuses("attribution-sentinels-missing", attribution.require_sentinels)
    check(await fastt._automation_emp_id() is None, "FASTT cannot resolve its own sentinel today")
    # an active/owned/chatter row with the same name is a real person, never a sentinel
    async with get_session() as s:
        s.add(Employee(display_name="Twin staff", is_active=True))
        s.add(Employee(display_name="Twin staff", is_active=False, user_id="someone"))
        s.add(Employee(display_name="Automation", is_active=True))
    check(await attribution.read_sentinels() == attribution.Sentinels(None, None))
    check(await attribution.seed_sentinels() == ["Automation", "Twin staff"], "only the missing rows are created")
    check(await attribution.seed_sentinels() == [], "idempotent")
    found = await attribution.require_sentinels()
    check(found.automation and found.human and found.automation != found.human)
    async with get_session() as s:
        n = len((await s.scalars(select(Employee))).all())
    check(n == 5)
    s2 = await attribution.seed_sentinels()
    check(s2 == [])
    check(await fastt._automation_emp_id() == found.automation, "FASTT now resolves the very same sentinel")
    # FASTT's own rule tells the two apart (and NULL is neither a human nor the AI)
    rule = fastt._is_named_chatter_send
    check(rule(None, None, found.human, automation_emp_id=found.automation) is True)
    check(rule(None, None, found.automation, automation_emp_id=found.automation) is False)
    check(rule("ai_chatter", None, found.automation, automation_emp_id=found.automation) is False)
    check(rule("ai_upseller", None, found.human, automation_emp_id=found.automation) is False)
    check(rule(None, None, None, automation_emp_id=found.automation) is False, "unattributed is never assumed human")
    # ... and so does its last look before the wire, on real rows
    t0 = datetime.utcnow() - timedelta(minutes=5)
    async with get_session() as s:
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=1, direction="in", body="hi", created_at=t0))
    async def moved_on(**row):
        async with get_session() as s:
            await s.execute(__import__("sqlalchemy").delete(Message).where(Message.message_id == 2))
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=2, direction="out", body="x",
                          created_at=t0 + timedelta(minutes=1), **row))
        return await fastt._thread_moved_on(ACCOUNT, FAN, t0)
    check(await moved_on(sent_by_employee_id=found.human) == "already_answered", "a Twin human reply is seen")
    check(await moved_on(automation_kind="ai_chatter", sent_by_employee_id=found.automation) == "already_answered", "an AI reply is seen")
    check(await moved_on(sent_by_employee_id=None) == "", "an unattributed row is NOT assumed to be a named human")
    check(await moved_on(sent_by_employee_id=found.automation) == "", "the AI sentinel alone is not a human")

    # Twin's projection stamps the human sentinel only when it exists (sync lookup)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from onlyfans_runtime.staff_sentinels import human_employee_id
    sync_url = os.environ["DATABASE_URL"].replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")
    with Session(create_engine(sync_url)) as ss:
        check(human_employee_id(ss) == found.human)
        ss.add(Employee(display_name="Twin staff", is_active=False))
        ss.flush()
        check(human_employee_id(ss) is None, "ambiguous sentinels are never guessed")
        ss.rollback()
    async with get_session() as s:
        for row in (await s.scalars(select(Employee))).all():
            await s.delete(row)
    with Session(create_engine(sync_url)) as ss:
        check(human_employee_id(ss) is None, "absent sentinel: Twin keeps writing NULL, as before")
    # ambiguity on the pilot side refuses
    async with get_session() as s:
        s.add(Employee(display_name="Automation", is_active=False))
        s.add(Employee(display_name="Automation", is_active=False))
    await async_refuses("attribution-sentinels-ambiguous", attribution.read_sentinels)


# ── F. sender retry audit: one message POST, nothing hidden behind it ───────
def sender_cases():
    from curl_cffi.requests.exceptions import ProxyError
    from of_client import OFClient

    class Http:
        def __init__(self, fail=None):
            self.calls, self.fail = [], fail
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if self.fail:
                raise self.fail
            class R:
                ok, status_code, text, url_ = True, 200, "{}", url
                def json(self_): return {"id": 1}
            return R()
        get = request

    def client(http):
        c = OFClient.__new__(OFClient)
        c.account_id = c.user_id = str(OWNER)
        c.http, c.timeout_s, c.proxy_url, c.proxy_label = http, 1, None, None
        c._signed_headers = lambda url: {}
        return c

    # the single-attempt senders: one POST, no proxy retry, no redirect following
    for fail in (None, ProxyError("synthetic CONNECT failure")):
        http = Http(fail)
        try:
            client(http).send_text_once_body(FAN, "<p>x</p>")
        except ProxyError:
            check(fail is not None)
        check(len(http.calls) == 1 and http.calls[0][0] == "POST" and http.calls[0][2]["allow_redirects"] is False, fail)
        http = Http(fail)
        try:
            client(http).post_json("https://onlyfans.com/api2/v2/chats/1/messages", json_body={}, proxy_tries=1, allow_redirects=False)
        except ProxyError:
            pass
        check(len(http.calls) == 1)
    # the general sender keeps its documented behaviour (default proxy retry, session redirects)
    http = Http(ProxyError("synthetic"))
    with patch("time.sleep"):
        try:
            client(http).post_json("https://onlyfans.com/api2/v2/chats/1/messages", json_body={})
        except ProxyError:
            pass
    check(len(http.calls) == 3 and all("allow_redirects" not in c[2] for c in http.calls), "legacy callers unchanged")


async def suite(tmp):
    logging.disable(logging.CRITICAL)
    scope_cases()
    role_guard_cases()
    sender_cases()
    await connection_cases(tmp)
    await budget_cases(tmp)
    await attribution_cases(tmp)
    from db.engine import engine
    await engine.dispose()


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    with tempfile.TemporaryDirectory(prefix="native-pilot-runtime-") as tmp:
        os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + (Path(tmp) / "runtime.db").as_posix())
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
        asyncio.run(suite(tmp))
    print(f"all cases passed ({CHECKS} checks)")
