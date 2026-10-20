"""Agency isolation and staff handover against disposable PostgreSQL only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import hashlib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from test_onlyfans_foundation_pg import database, seed, event
from test_onlyfans_text_delivery_pg import configured, TENANT, ACCOUNT, FAN
from db.models import OfCreatorBinding, OfConversationState
from db.onlyfans_models import OfControlAudit
from onlyfans_runtime.store import Refused
from onlyfans_runtime.handover import takeover_owner
from onlyfans_read.agency_scope import AgencyScope, AgencyScopeMiddleware, HEADER, VERSION_HEADER, VERSION, current_scope, parse_scope

CREATOR = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
AGENCY = "33333333-3333-4333-8333-333333333333"
STAFF = f"agency-user:{AGENCY}:44444444-4444-4444-8444-444444444444"
COLLEAGUE = f"agency-user:{AGENCY}:55555555-5555-4555-8555-555555555555"


def assign(engine, account=ACCOUNT, creator=CREATOR):
    with Session(engine) as session, session.begin():
        binding = session.scalar(select(OfCreatorBinding).where(OfCreatorBinding.account_id == account))
        binding.creator_id = creator


def headers(actor=STAFF, creators=(CREATOR,), manage=True):
    return {HEADER: json.dumps({"creatorIds": list(creators), "actor": actor, "canManage": manage})}


def control(store, action, revision, actor=STAFF):
    from onlyfans_commands.settings import ACTIONS
    return store.control(TENANT, ACCOUNT, FAN, mode=ACTIONS[action], action=action, expected_revision=revision,
        command_key=hashlib.sha256(f"{actor}:{action}:{revision}".encode()).hexdigest(), actor=actor, reason="Synthetic staff test")


def test_scope_parser_and_namespace_never_fall_back():
    for raw in ["", "null", "{}", "[]", "x" * 12289, json.dumps({"creatorIds": ["wrong"], "actor": STAFF, "canManage": True})]:
        with pytest.raises((ValueError, TypeError)): parse_scope(raw)
    assert parse_scope(None) is None
    assert parse_scope(headers()[HEADER]).actor == STAFF
    app = FastAPI(); app.add_middleware(AgencyScopeMiddleware)
    @app.get("/onlyfans/api/v1/status")
    async def state():
        await asyncio.sleep(0.01)
        return {"actor": await asyncio.to_thread(lambda: current_scope.get().actor)}
    client = TestClient(app)
    prefix = "/onlyfans/agency/v1/api/v1/status"
    assert client.get(prefix).status_code == 403
    assert client.get(prefix, headers={HEADER: "{}"}).status_code == 403
    assert client.get("/onlyfans/agency/v1/api/v1/earnings", headers=headers()).status_code == 403
    assert client.post(prefix, headers=headers(manage=False)).status_code == 403
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda actor: client.get(prefix, headers=headers(actor)), [STAFF, COLLEAGUE]))
    assert [result.json()["actor"] for result in results] == [STAFF, COLLEAGUE]
    assert all(result.headers[VERSION_HEADER] == VERSION for result in results)
    assert current_scope.get() is None


def test_native_commands_recheck_live_creator_binding(database):
    store = seed(*database); assign(database[1])
    token = current_scope.set(AgencyScope((OTHER,), STAFF, True))
    try:
        with pytest.raises(Refused, match="scope"): control(store, "request_takeover", 0)
    finally: current_scope.reset(token)
    token = current_scope.set(AgencyScope((CREATOR,), STAFF, True))
    try:
        assert control(store, "request_takeover", 0) == 1
        assign(database[1], creator=OTHER)
        with pytest.raises(Refused, match="scope"): control(store, "release_takeover", 1)
    finally: current_scope.reset(token); store.close()


def test_staff_ownership_survives_relogin_and_prevents_colleague_send_or_release(database):
    store, authority = configured(database, mode="automated"); assign(database[1])
    assert control(store, "request_takeover", 0) == 1
    with pytest.raises(Refused, match="another team"): control(store, "release_takeover", 1, COLLEAGUE)
    with pytest.raises(Refused, match="another team"):
        authority.prepare(TENANT, ACCOUNT, FAN, command_key="wrong-owner", expected_revision=1,
            body="Synthetic reply", origin="manual", actor=COLLEAGUE)
    with Session(database[1]) as session:
        state = session.get(OfConversationState, (ACCOUNT, FAN))
        assert takeover_owner(session, ACCOUNT, FAN, state) == STAFF
    assert control(store, "release_takeover", 1, STAFF) == 2
    assert control(store, "request_takeover", 2, COLLEAGUE) == 3
    with pytest.raises(Refused, match="another team"):
        authority.prepare(TENANT, ACCOUNT, FAN, command_key="former-owner", expected_revision=3,
            body="Synthetic reply", origin="manual", actor=STAFF)
    assert authority.prepare(TENANT, ACCOUNT, FAN, command_key="current-owner", expected_revision=3,
        body="Synthetic reply", origin="manual", actor=COLLEAGUE)
    store.close()


def test_competing_staff_takeovers_have_one_owner(database):
    store = seed(*database); assign(database[1])
    def take(actor):
        try: return control(store, "request_takeover", 0, actor)
        except Refused: return "refused"
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(take, [STAFF, COLLEAGUE]))
    assert results.count(1) == 1 and results.count("refused") == 1
    with Session(database[1]) as session:
        events = session.scalars(select(OfControlAudit)).all()
        assert len(events) == 1
        assert takeover_owner(session, ACCOUNT, FAN, session.get(OfConversationState, (ACCOUNT, FAN))) == events[0].actor
    store.close()


def test_http_reads_controls_and_handover_are_scoped(database, monkeypatch):
    store = seed(*database); other = seed(*database, account="acc-b", tenant=TENANT)
    assign(database[1]); assign(database[1], "acc-b", OTHER)
    for key, value in {"ONLYFANS_PROCESS_ROLE": "api", "ONLYFANS_API_TOKEN": "synthetic-read", "ONLYFANS_API_TENANT": "deployment",
            "ONLYFANS_CONTROL_TOKEN": "synthetic-control", "ONLYFANS_CONTROL_TENANT": TENANT}.items(): monkeypatch.setenv(key, value)
    from onlyfans_read import api, repo
    from onlyfans_commands.api import router as controls
    import importlib
    engine_module = importlib.import_module("db.engine")
    engine = create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as session: yield session
    monkeypatch.setattr(repo, "get_session", sessions)
    monkeypatch.setattr(engine_module, "get_session", sessions)
    app = FastAPI(); app.add_middleware(AgencyScopeMiddleware); app.include_router(api.router); app.include_router(controls); app.state.control_store = store
    client = TestClient(app)
    base = "/onlyfans/agency/v1"
    read_headers = {**headers(), "Authorization": "Bearer synthetic-read", "X-OnlyFans-Schema-Version": "of-preview.v2"}
    listing = client.get(base + "/api/v1/accounts", headers=read_headers)
    assert listing.status_code == 200, listing.text
    assert [item["accountId"] for item in listing.json()["accounts"]] == [ACCOUNT]
    for suffix in ["conversations", "workspace/content", "conversations/acc-b:101/handover"]:
        assert client.get(base + "/api/v1/accounts/acc-b/" + suffix, headers=read_headers).status_code == 404
    path = base + f"/control/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}"
    body = {"action": "request_takeover", "expectedRevision": 0, "commandKey": "a" * 64, "actor": STAFF}
    command_headers = {**headers(), "Authorization": "Bearer synthetic-control", "X-OnlyFans-Control-Version": "of-controls.v1"}
    assert client.post(path, headers={**command_headers, **headers(manage=False)}, json=body).status_code == 403
    assert client.post(path, headers={**command_headers, **headers(creators=(OTHER,))}, json=body).status_code == 404
    assert client.post(path, headers=command_headers, json={**body, "actor": COLLEAGUE}).status_code == 400
    assert client.post(path, headers=command_headers, json=body).status_code == 200
    history = client.get(base + f"/api/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}/handover", headers=read_headers)
    assert history.status_code == 200, history.text
    assert history.json()["ownerActor"] == STAFF and len(history.json()["events"]) == 1
    assign(database[1], creator=OTHER)
    assert client.get(base + f"/api/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}/handover", headers=read_headers).status_code == 404
    assert client.get(base + "/api/v1/accounts", headers=read_headers).json()["accounts"] == []
    store.close(); other.close(); asyncio.run(engine.dispose())
