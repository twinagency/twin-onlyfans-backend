"""Offline launch regressions. Disposable SQLite/ASGI only, no provider or model.

These do not establish PostgreSQL locking, deployed source or live acceptance.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import os
from types import SimpleNamespace as N

os.environ["TWIN_FASTT_CONFIG_ISOLATED"] = "1"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ.pop("ONLYFANS_PROCESS_ROLE", None)

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Account, Base, Chat, Fan, OfConversationState, OfCreatorBinding, Transaction
from db.onlyfans_models import OfRuntime
from onlyfans_read.agency_scope import AgencyScopeMiddleware, actor_allowed, creator_allowed, current_scope, parse_scope
from onlyfans_read.contract import decode_cursor, encode_cursor
from onlyfans_read import repo
from onlyfans_runtime.store import Store, Refused

CREATOR = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ACTOR = "agency-user:33333333-3333-4333-8333-333333333333:44444444-4444-4444-8444-444444444444"

def grant(**overrides):
    return {"creatorIds": [CREATOR], "actor": ACTOR, "canManage": False, "canChat": True, **overrides}

def test_scope_compatibility_actor_and_action_privilege():
    legacy = parse_scope(json.dumps({"creatorIds": [CREATOR], "actor": ACTOR, "canManage": True}))
    assert legacy.version == "of-agency.v1" and not legacy.can_chat
    chatter = parse_scope(json.dumps(grant()))
    assert chatter.version == "of-agency.v2" and chatter.can_chat and not chatter.can_manage
    token = current_scope.set(chatter)
    try:
        assert actor_allowed(ACTOR, capability="chat") and not actor_allowed(ACTOR)
        assert not actor_allowed(ACTOR + "x", capability="chat")
        assert creator_allowed(CREATOR) and not creator_allowed(OTHER)
        with pytest.raises(Refused, match="permission"):
            Store.control(N(), "tenant", "a", 1, mode="automated", action="resume_automation", expected_revision=0, command_key="a", actor=ACTOR, reason="test")
    finally:
        current_scope.reset(token)
    for raw in [json.dumps(grant(canChat="true")), json.dumps(grant(extra=True)), '{"creatorIds":[],"actor":"' + ACTOR + '","canManage":false,"canChat":true,"canChat":false}']:
        with pytest.raises(ValueError): parse_scope(raw)

def test_asgi_chatter_paths_default_deny_and_version_handshake():
    app = FastAPI(); app.add_middleware(AgencyScopeMiddleware)
    @app.api_route("/onlyfans/{path:path}", methods=["GET", "POST", "PUT"])
    async def view(request: Request, path: str):
        return {"chat": actor_allowed(ACTOR, capability="chat"), "manage": actor_allowed(ACTOR)}
    client = TestClient(app)
    headers = {"X-OnlyFans-Agency-Scope": json.dumps(grant())}
    prefix = "/onlyfans/agency/v1/"
    for path in ["control/v1/accounts/a/conversations/a:1", "configuration/v1/accounts/a/history-memory/fans/1/corrections"]:
        result = client.post(prefix + path, headers=headers)
        assert result.status_code == 200 and result.json() == {"chat": True, "manage": False}
        assert result.headers["X-OnlyFans-Access-Version"] == "of-agency.v2"
    for path in ["configuration/v1/accounts/a", "control/v1/accounts/a/control", "control/v1/accounts/a/ledger/retry", "campaign/v1/accounts/a", "content/v1/accounts/a"]:
        assert client.post(prefix + path, headers=headers).status_code == 403
    assert client.post(prefix + "control/v1/accounts/a/conversations/a:1").status_code == 403

def test_runtime_binding_denies_cross_tenant_and_unassigned_creator():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Account.__table__, OfCreatorBinding.__table__, OfRuntime.__table__])
    with Session(engine) as s, s.begin():
        s.add(Account(id="a")); s.flush()
        s.add(OfCreatorBinding(id="binding", tenant_id="tenant", creator_id=CREATOR, creator_name="Synthetic", account_id="a", handle="synthetic", channel="onlyfans")); s.flush()
        s.add(OfRuntime(account_id="a", binding_id="binding", activated_at=datetime.utcnow())); s.flush()
        token = current_scope.set(parse_scope(json.dumps(grant())))
        try:
            assert Store._runtime(s, "tenant", "a").account_id == "a"
            with pytest.raises(Refused): Store._runtime(s, "another-agency", "a")
            current_scope.set(parse_scope(json.dumps(grant(creatorIds=[OTHER]))))
            with pytest.raises(Refused): Store._runtime(s, "tenant", "a")
        finally: current_scope.reset(token)
    engine.dispose()

def test_search_cursor_is_bound_to_query_and_keeps_large_provider_ids():
    cursor = encode_cursor(account_id="a", last_activity_at="2026-10-28T00:00:00Z", fan_id=9007199254740993, scope_key="one-query")
    assert decode_cursor(cursor, account_id="a", scope_key="one-query")[1] == 9007199254740993
    for account, scope in [("b", "one-query"), ("a", "another-query"), ("a", None)]:
        with pytest.raises(ValueError): decode_cursor(cursor, account_id=account, scope_key=scope)

def test_server_search_filters_before_page_limit_and_escapes_wildcards(monkeypatch):
    async def work():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[Account.__table__, Fan.__table__, Chat.__table__, OfConversationState.__table__]))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        @asynccontextmanager
        async def session():
            async with factory() as value: yield value
        monkeypatch.setattr(repo, "get_session", session)
        now = datetime.utcnow()
        async with factory() as s, s.begin():
            s.add_all([Account(id="a"), Account(id="b")]); await s.flush()
            for fan in range(1, 151):
                s.add(Fan(account_id="a", fan_id=fan, of_display_name="Target" if fan == 1 else "Other", of_username="literal%name" if fan == 1 else "normal"))
                s.add(Chat(account_id="a", fan_id=fan, last_message_at=now, unread_count=1 if fan == 1 else 0, hidden_locally=False))
                s.add(OfConversationState(account_id="a", fan_id=fan, requires_attention=fan == 1))
            s.add(Fan(account_id="b", fan_id=1, of_display_name="Target")); s.add(Chat(account_id="b", fan_id=1, last_message_at=now, unread_count=1))
        reader = repo.SqlOnlyFansReadRepo()
        first = await reader.conversation_page(account_id="a", before=None, limit=20)
        assert all(row.fan_id != 1 for row in first)
        for options in [{"query": "Target"}, {"filter": "unread"}, {"filter": "attention"}, {"query": "%"}]:
            rows = await reader.conversation_page(account_id="a", before=None, limit=20, **options)
            assert [(row.account_id, row.fan_id) for row in rows] == [("a", 1)]
        await engine.dispose()
    asyncio.run(work())


def test_historical_imports_do_not_inflate_current_binding_purchase_blockers():
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_delivery_models import OfDeliveryPolicy, OfCommerceEvidence
    from db.onlyfans_ledger_models import OfLedgerScan, OfLedgerEvidence, OfLedgerExclusion
    from onlyfans_runtime.launch_operations_models import OfLedgerResolution
    from onlyfans_runtime.launch_operations import _ledger_core
    from onlyfans_runtime.commerce import _unlinked_paid
    engine = create_engine("sqlite://")
    models = [Account, OfCreatorBinding, OfRuntime, Fan, Transaction, OfProviderConnection,
              OfDeliveryPolicy, OfCommerceEvidence, OfLedgerScan, OfLedgerEvidence, OfLedgerExclusion, OfLedgerResolution]
    Base.metadata.create_all(engine, tables=[model.__table__ for model in models])
    cutoff = datetime(2026, 10, 1)
    with Session(engine) as s, s.begin():
        s.add(Account(id="a")); s.flush()
        s.add(OfCreatorBinding(id="binding", tenant_id="tenant", creator_id=CREATOR, account_id="a", created_at=cutoff)); s.flush()
        runtime = OfRuntime(account_id="a", binding_id="binding", activated_at=cutoff)
        s.add(runtime); s.add(Fan(account_id="a", fan_id=1)); s.flush()
        for identity, occurred in [(1, cutoff-timedelta(days=3)), (2, cutoff+timedelta(days=3))]:
            s.add(Transaction(id=identity, account_id="a", fan_id=1, provider_transaction_id=str(identity),
                kind="ppv_message", amount_cents=1000, currency="USD", status="cleared", source="ledger",
                message_id=None, occurred_at=occurred, ingested_at=cutoff+timedelta(days=20)))
        s.flush()
        assert s.scalars(_unlinked_paid("a")).all() == [2]
        evidence = _ledger_core(s, runtime)
        assert evidence["unresolvedTransactions"] == 1 and evidence["commerceHold"]
        assert all(s.get(Transaction, identity).message_id is None for identity in [1, 2]), "No purchase was linked or evidence removed"
    engine.dispose()
