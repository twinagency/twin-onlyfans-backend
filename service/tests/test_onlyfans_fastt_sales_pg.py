"""Saved configuration API through the active FASTT adapter on disposable PG."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import importlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from db.models import AccountAiConfig, Employee, Message
from onlyfans_commands.configuration_api import router, HEADER, SCHEMA
from onlyfans_runtime.configuration import SALES_FLAGS
from onlyfans_runtime.store import utcnow

ACCOUNT, TENANT, FAN = "of-900001", "tenant-a", 101
ACTOR = "op-session:" + "a" * 64
ON = {key: True for key in SALES_FLAGS}


def setup(database, monkeypatch, raw):
    store = seed(*database, account=ACCOUNT)
    with Session(store.engine) as s, s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, persona="Synthetic creator enjoys hiking.",
            model="grok-4-1-fast-non-reasoning", ai_chatter_config_json=raw))
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN", "synthetic-configuration")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT", TENANT)
    app = FastAPI()
    app.include_router(router)
    app.state.control_store = store
    return store, TestClient(app)


def read(client):
    response = client.get(f"/onlyfans/configuration/v1/accounts/{ACCOUNT}",
        headers={"Authorization": "Bearer synthetic-configuration", HEADER: SCHEMA})
    assert response.status_code == 200, response.text
    return response.json()["configuration"]


async def effective(store, monkeypatch, *, reply=False):
    from automations import ai_chatter as native
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine
    engine = create_async_engine(store.engine.url.set(drivername="postgresql+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    monkeypatch.setattr(native, "get_session", session)
    monkeypatch.setattr(importlib.import_module("db.engine"), "get_session", session)
    sent = []

    async def chat(**kwargs):
        if kwargs.get("response_format"):
            return SimpleNamespace(content="{}", parsed={})
        return SimpleNamespace(content="Glad you stopped by. How are you?", parsed=None)

    def send(fan, text, kwargs):
        assert fan == FAN and not kwargs, "sales off permits only ordinary text here"
        sent.append(text)
        return {"id": 900001 + len(sent), "text": text}

    try:
        with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, content=VaultPlan(ACCOUNT, FAN)) as pilot:
            value = await native._load_config(ACCOUNT)
            assert "_native_on_request_sales_enabled" not in value
            if reply:
                result = await pilot.run(1)
                assert result["replies_sent"] == 1 and sent
            return value
    finally:
        await engine.dispose()


@pytest.mark.parametrize("raw,expected", [
    ("{}", False), (json.dumps(ON), True),
    *[(json.dumps({**ON, key: False}), False) for key in SALES_FLAGS],
    *[(json.dumps({key: value for key, value in ON.items() if key != missing}), False) for missing in SALES_FLAGS],
    ('{"pack_send_enabled":true,"pack_send_enabled":false}', False),
    (json.dumps({**ON, "pack_send_enabled": "true"}), False),
    (json.dumps({"_native_on_request_sales_enabled": True}), False),
])
def test_saved_sales_permission_matches_api_and_runtime(database, monkeypatch, raw, expected):
    store, client = setup(database, monkeypatch, raw)
    try:
        displayed = read(client)
        value = asyncio.run(effective(store, monkeypatch))
        assert displayed["onRequestSalesEnabled"] is expected
        assert value["pack_send_enabled"] is expected and value["pack_on_ask_enabled"] is expected
        assert value["qualification_gate_enabled"] is False, "permission must not enable the legacy ladder"
        assert value["enabled"] is True
        with Session(store.engine) as s:
            assert s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json == raw
    finally:
        client.close()
        store.close()


def test_api_sales_off_preserves_followup_switches_and_ordinary_reply(database, monkeypatch):
    raw = json.dumps({"fastt_post_buy_upsell": True, "fastt_discount_resend": True})
    store, client = setup(database, monkeypatch, raw)
    try:
        for enabled, key in ((True, "b"), (False, "c")):
            current = read(client)
            result = client.post(f"/onlyfans/configuration/v1/accounts/{ACCOUNT}",
                headers={"Authorization": "Bearer synthetic-configuration", HEADER: SCHEMA},
                json={"patch": {"onRequestSalesEnabled": enabled},
                    "expectedRevision": current["configurationRevision"], "expectedFingerprint": current["fingerprint"],
                    "commandKey": key * 64, "actor": ACTOR})
            assert result.status_code == 200, result.text
            assert read(client)["onRequestSalesEnabled"] is enabled
            value = asyncio.run(effective(store, monkeypatch))
            assert value["pack_send_enabled"] is enabled and value["pack_on_ask_enabled"] is enabled
            assert value["fastt_post_buy_upsell"] is True and value["fastt_discount_resend"] is True
        with Session(store.engine) as s, s.begin():
            s.add(Employee(id=1, display_name="Automation", is_active=True))
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=1, direction="in",
                body="How was your morning?", created_at=utcnow() - timedelta(minutes=1)))
        value = asyncio.run(effective(store, monkeypatch, reply=True))
        assert value["pack_send_enabled"] is False
    finally:
        client.close()
        store.close()
