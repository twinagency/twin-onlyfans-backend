"""Saved model controls match the active transport and its existing sealed credential."""
import asyncio
from contextlib import asynccontextmanager
from importlib import import_module
import json
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from test_fastt_controls_pg import database,template
from test_onlyfans_connections_pg import verified,TENANT,ACTOR
from db.models import AccountAiConfig,GrokCall
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_models import OfConfigurationChange
from onlyfans_runtime import configuration,connections
from native_pilot.connection import supports_reply_model,load_verified_connection
from native_pilot.scope import AccountScope

SUPPORTED="openrouter/deepseek-v4.1-flash"
OTHER="grok-4-1-fast-non-reasoning"


def setup(database,monkeypatch,*,credential=SUPPORTED):
    store,account,_=verified(database,monkeypatch,model={"modelId":credential,"apiKey":"synthetic-model-secret"})
    with Session(store.engine) as s,s.begin():
        row=s.get(AccountAiConfig,account)
        row.model=SUPPORTED;row.model_by_purpose=None;row.reasoning_effort=None
        row.persona="Synthetic creator enjoys hiking."
    monkeypatch.setenv("ONLYFANS_FASTT_SCOPE","db:*")
    return store,account


def save(store,account,data,patch):
    return configuration.update_configuration(store,TENANT,account,patch=patch,
        expected_revision=data["configurationRevision"],expected_fingerprint=data["fingerprint"],
        command_key="d"*64,actor=ACTOR)


async def load(store,account,monkeypatch):
    module=import_module("db.engine")
    engine=create_async_engine(store.engine.url.set(drivername="postgresql+asyncpg"))
    factory=async_sessionmaker(engine,expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with factory() as s:yield s
    monkeypatch.setattr(module,"get_session",session)
    try:return await load_verified_connection(AccountScope.parse(account))
    finally:await engine.dispose()


def test_offered_model_and_reasoning_match_the_actual_connection_loader(database,monkeypatch):
    store,account=setup(database,monkeypatch)
    try:
        data=configuration.get_configuration(store,TENANT,account)
        assert data["modelsValid"] and data["resolvedReplyModel"]==SUPPORTED
        assert data["modelOptions"]==[SUPPORTED] and data["reasoningOptions"]=={SUPPORTED:[]}
        assert supports_reply_model(SUPPORTED) and not supports_reply_model(OTHER)
        assert "synthetic-model-secret" not in json.dumps(data)
        assert asyncio.run(load(store,account,monkeypatch)).model_id==SUPPORTED
    finally:store.close()


@pytest.mark.parametrize("patch",[{"defaultModel":OTHER},{"replyModelOverride":OTHER},{"reasoningEffort":"high"}])
def test_unloadable_model_or_reasoning_edit_rolls_back_without_touching_credential(database,monkeypatch,patch):
    store,account=setup(database,monkeypatch)
    try:
        data=configuration.get_configuration(store,TENANT,account)
        with Session(store.engine) as s:sealed=s.get(OfProviderConnection,account).sealed_config
        with pytest.raises(configuration.InvalidConfiguration):save(store,account,data,patch)
        assert configuration.get_configuration(store,TENANT,account)==data
        with Session(store.engine) as s:
            assert s.get(OfProviderConnection,account).sealed_config==sealed
            assert s.scalar(select(func.count()).select_from(OfConfigurationChange))==0
            assert s.scalar(select(func.count()).select_from(GrokCall))==0
    finally:store.close()


def test_credential_mismatch_is_visible_and_cannot_be_saved_as_ready(database,monkeypatch):
    store,account=setup(database,monkeypatch,credential=OTHER)
    try:
        data=configuration.get_configuration(store,TENANT,account)
        assert not data["modelsValid"] and data["resolvedReplyModel"] is None and data["defaultModel"]==SUPPORTED
        with pytest.raises(configuration.InvalidConfiguration,match="credential"):
            save(store,account,data,{"replyModelOverride":SUPPORTED})
        assert configuration.get_configuration(store,TENANT,account)==data
    finally:store.close()


def test_supported_override_preserves_the_bound_credential_and_loads_after_save(database,monkeypatch):
    store,account=setup(database,monkeypatch)
    try:
        data=configuration.get_configuration(store,TENANT,account)
        with Session(store.engine) as s:sealed=s.get(OfProviderConnection,account).sealed_config
        assert save(store,account,data,{"replyModelOverride":SUPPORTED})[0]==1
        assert asyncio.run(load(store,account,monkeypatch)).model_id==SUPPORTED
        with Session(store.engine) as s:assert s.get(OfProviderConnection,account).sealed_config==sealed
    finally:store.close()


def test_existing_unsupported_selection_is_reported_and_persona_edit_does_not_rewrite_it(database,monkeypatch):
    store,account=setup(database,monkeypatch)
    try:
        with Session(store.engine) as s,s.begin():s.get(AccountAiConfig,account).model=OTHER
        data=configuration.get_configuration(store,TENANT,account)
        assert not data["modelsValid"] and data["resolvedReplyModel"] is None and data["defaultModel"]==OTHER
        save(store,account,data,{"persona":"Updated approved synthetic creator persona."})
        with Session(store.engine) as s:assert s.get(AccountAiConfig,account).model==OTHER
    finally:store.close()


def test_generic_native_owner_keeps_its_existing_model_catalogue(database,monkeypatch):
    store,account=setup(database,monkeypatch)
    try:
        monkeypatch.setenv("ONLYFANS_FASTT_SCOPE","")
        from onlyfans_runtime import fastt_scope
        monkeypatch.setattr(fastt_scope,"owned_scope",lambda:(set(),set()))
        data=configuration.get_configuration(store,TENANT,account)
        assert OTHER in data["modelOptions"] and len(data["modelOptions"])>1
    finally:store.close()
