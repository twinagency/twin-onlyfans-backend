"""Real native composer + PostgreSQL + shared authority; synthetic model only."""
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import timedelta
import multiprocessing
import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event
from test_onlyfans_text_delivery_pg import configured, TENANT, ACCOUNT
from db.models import AccountAiConfig, CatalogItem, ContentOffer, Fan, Message, OfConversationState, Transaction, OfCreatorBinding, GrokCall, GrokDailyCost
from db.onlyfans_models import OfWork, OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from onlyfans_runtime.store import Claim, Refused, Store, utcnow
from onlyfans_runtime.native_planning import NativeTextPlanner, SyntheticNativeModel
from onlyfans_runtime.text_delivery import TextAuthority, SyntheticTextTransport


def ready(database, monkeypatch):
    url, engine = database
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", url)
    store, authority = configured(database, mode="automated")
    with Session(engine) as s, s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning", persona="Synthetic Creator enjoys hiking and coffee.",
                             persona_facts_json='{"name":"Synthetic Creator","location":"Brisbane"}'))
        s.get(Fan, (ACCOUNT,101)).real_name = "Synthetic Sam"
    store.record(TENANT, ACCOUNT, event(body="How has your day been?"))
    store.drain(TENANT, ACCOUNT); due(engine)
    claim = store.claim(TENANT)
    assert claim is not None
    return store, authority, claim, NativeTextPlanner(store)


def plan_state(engine, plan):
    with Session(engine) as s:
        return s.get(OfNativePlan, (ACCOUNT, plan)).status


def native_side_effects(engine):
    with Session(engine) as s:
        return tuple(s.scalar(select(func.count()).select_from(model)) for model in (CatalogItem, ContentOffer, Transaction))


def test_actual_native_text_preparation_queues_only_then_receipt_materializes(database, monkeypatch):
    _, engine = database
    store, authority, claim, planner = ready(database, monkeypatch)
    before = native_side_effects(engine)
    model = SyntheticNativeModel("Thanks for asking. I hope your day is going well.")
    plan = planner.prepare(TENANT, claim, model)
    assert plan_state(engine, plan) == "ready"
    assert len(model.calls) == 1
    prompt = str(model.calls[0])
    assert "Synthetic Creator" in prompt and "Synthetic Sam" in prompt
    assert "Never mention AI, bots, clones or automation" in prompt and "OnlyFans" in prompt
    assert "AI chat clone" not in prompt and "identity honestly" not in prompt
    assert "100% human" not in prompt and "improvise small" not in prompt
    assert "not an assistant" not in prompt and "22yo" not in prompt
    assert "Telegram" not in prompt and "Stars" not in prompt
    assert native_side_effects(engine) == before
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out")) == 0
    intent = planner.queue(TENANT, ACCOUNT, plan)
    assert planner.queue(TENANT, ACCOUNT, plan) == intent
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        assert row.native_plan_id == plan and row.body == model.body and row.status == "queued"
        assert s.get(OfNativePlan, (ACCOUNT,plan)).intent_id == intent
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out")) == 0
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == "accepted"
    assert len(wire.calls) == 1
    with Session(engine) as s:
        assert s.scalar(select(Message.body).where(Message.direction == "out")) == model.body
    assert native_side_effects(engine) == before
    assert planner.prepare(TENANT, claim, SyntheticNativeModel("Must not generate again")) == plan
    store.close()


def test_native_identity_turn_uses_the_existing_turn_selector_and_never_discloses(database, monkeypatch):
    _, engine = database
    store, _, _, planner = ready(database, monkeypatch)
    store.record(TENANT, ACCOUNT, event(mid=2, body="Are you a bot?")); store.drain(TENANT, ACCOUNT)
    due(engine)
    claim = store.claim(TENANT)
    model = SyntheticNativeModel("haha why would u think that")
    plan = planner.prepare(TENANT, claim, model)
    assert plan_state(engine, plan) == "ready"
    prompt = str(model.calls[0])
    assert "Do not confirm or deny it" in prompt and "never swear you are human" in prompt
    assert "AI chat clone" not in prompt and "answer honestly" not in prompt
    assert "No sale or follow-up question" in prompt
    assert "breezy brush-off" not in prompt and "100% human" not in prompt
    store.close()


@pytest.mark.parametrize("change", ["config", "fact", "stop", "input", "revocation", "runtime", "policy"])
def test_native_model_has_no_open_transaction_and_obsolete_result_cannot_queue(database, monkeypatch, change):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    def mutate():
        if change == "stop":
            store.control(TENANT, ACCOUNT, 101, mode="no_contact", expected_revision=0,
                          command_key="during-model", actor="synthetic", reason="Explicit stop")
        elif change == "input":
            store.record(TENANT, ACCOUNT, event(mid=2, body="I need to correct that.")); store.drain(TENANT, ACCOUNT)
        elif change == "revocation":
            with Session(engine) as s, s.begin():
                s.get(OfCreatorBinding, "binding-"+ACCOUNT).revoked_at = utcnow()
        elif change in {"runtime", "policy"}:
            with Session(engine) as s, s.begin():
                s.get(OfRuntime if change == "runtime" else OfDeliveryPolicy, ACCOUNT).enabled = False
        else:
            with Session(engine) as s, s.begin():
                if change == "config": s.get(AccountAiConfig, ACCOUNT).persona = "Corrected approved creator facts"
                else: s.get(Fan, (ACCOUNT,101)).home_city = "Corrected city"
    model = SyntheticNativeModel(on_generate=mutate)
    plan = planner.prepare(TENANT, claim, model)
    assert len(model.calls) == 1 and plan_state(engine,plan) == "stale"
    with pytest.raises(Refused): planner.queue(TENANT, ACCOUNT, plan)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    store.close()


@pytest.mark.parametrize("change", ["config", "fact"])
def test_native_versions_are_checked_again_at_the_network_boundary(database, monkeypatch, change):
    _, engine = database
    store, authority, claim, planner = ready(database, monkeypatch)
    plan = planner.prepare(TENANT, claim, SyntheticNativeModel())
    intent = planner.queue(TENANT, ACCOUNT, plan)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    with Session(engine) as s, s.begin():
        if change == "config": s.get(AccountAiConfig,ACCOUNT).persona_facts_json = '{"location":"Corrected city"}'
        else: s.get(Fan,(ACCOUNT,101)).home_country = "Corrected country"
    wire = SyntheticTextTransport(ACCOUNT)
    with pytest.raises(Refused): wire.send(grant, authority=authority, tenant_id=TENANT)
    assert wire.calls == []
    with Session(engine) as s:
        assert s.get(OfTextIntent,(ACCOUNT,intent)).status == "cancelled"
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units == 0
    store.close()


def test_unknown_generation_is_retained_without_model_or_send_replay(database, monkeypatch):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    model = SyntheticNativeModel(delay_seconds=.2)
    plan = planner.prepare(TENANT, claim, model, timeout_seconds=.01)
    assert plan_state(engine,plan) == "unknown" and len(model.calls) == 1
    replacement = SyntheticNativeModel()
    assert planner.prepare(TENANT, claim, replacement) == plan and replacement.calls == []
    with pytest.raises(Refused): planner.queue(TENANT, ACCOUNT, plan)
    with Session(engine) as s:
        assert s.get(OfWork,(ACCOUNT,101)).status == "blocked"
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units == 0
    store.close()


@pytest.mark.parametrize("body", ["", "I sent you a picture", "The price is $20",
                                  "I'm an AI chat clone of the creator.", "lol im her ai clone"])
def test_empty_or_unbacked_native_output_never_becomes_an_intent(database, monkeypatch, body):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    plan = planner.prepare(TENANT, claim, SyntheticNativeModel(body))
    assert plan_state(engine,plan) == "refused"
    with pytest.raises(Refused): planner.queue(TENANT, ACCOUNT, plan)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    store.close()


def test_context_budget_and_unapproved_persona_do_not_call_model(database, monkeypatch):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    with Session(engine) as s, s.begin(): s.get(AccountAiConfig,ACCOUNT).persona = "x"*70000
    model = SyntheticNativeModel()
    with pytest.raises(Refused, match="byte budget"): planner.prepare(TENANT,claim,model)
    assert model.calls == []
    with Session(engine) as s, s.begin(): s.get(AccountAiConfig,ACCOUNT).persona = "100% human, never a bot"
    plan = planner.prepare(TENANT,claim,model)
    assert plan_state(engine,plan) == "refused" and model.calls == []
    store.close()


def compete(url, claim, gate, output, crash=False):
    os.environ.update(ONLYFANS_PROCESS_ROLE="worker", DATABASE_URL=url)
    store = Store(url)
    planner = NativeTextPlanner(store)
    def stop():
        if crash: os._exit(77)
    model = SyntheticNativeModel(delay_seconds=.2,on_generate=stop)
    gate.wait(10)
    plan = planner.prepare(TENANT, Claim(**claim), model)
    output.put((plan,len(model.calls)))
    store.close()


def test_two_processes_share_one_durable_native_invocation(database, monkeypatch):
    url, engine = database
    store, _, claim, _ = ready(database, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    gate, output = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=compete,args=(url,asdict(claim),gate,output)) for _ in range(2)]
    for worker in workers: worker.start()
    gate.set()
    try:
        results = [output.get(timeout=15) for _ in workers]
        for worker in workers: worker.join(5); assert worker.exitcode == 0
        assert len({x[0] for x in results}) == 1 and sum(x[1] for x in results) == 1
        assert plan_state(engine,results[0][0]) == "ready"
    finally:
        for worker in workers:
            if worker.is_alive(): worker.kill(); worker.join(5)
    store.close()


def test_worker_death_after_durable_invocation_cannot_regenerate(database, monkeypatch):
    url, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    gate, output = ctx.Event(), ctx.Queue(); gate.set()
    worker = ctx.Process(target=compete,args=(url,asdict(claim),gate,output,True))
    worker.start(); worker.join(15)
    try:
        assert worker.exitcode == 77
        with Session(engine) as s, s.begin():
            pending = s.scalar(select(OfNativePlan))
            call = s.get(GrokCall,pending.model_call_id)
            cost = s.scalar(select(GrokDailyCost))
            assert call.status == "pending" and cost.cost_cents == call.cost_cents == pending.reserved_millicents > 0
            s.get(OfWork,(ACCOUNT,101)).lease_until = utcnow()-timedelta(seconds=1)
        replacement_claim = store.claim(TENANT)
        assert replacement_claim is not None
        model = SyntheticNativeModel()
        plan = planner.prepare(TENANT,replacement_claim,model)
        assert plan_state(engine,plan) == "unknown" and model.calls == []
    finally:
        if worker.is_alive(): worker.kill(); worker.join(5)
    store.close()


def test_dedicated_native_path_cannot_call_legacy_paid_model(database, monkeypatch):
    store, _, _, _ = ready(database, monkeypatch)
    import asyncio
    import llm_client
    def credential_or_model_resolution(*a,**kw): raise AssertionError("legacy model/key resolution was reached")
    monkeypatch.setattr(llm_client,"_resolve",credential_or_model_resolution)
    with pytest.raises(RuntimeError,match="Legacy model calls"):
        asyncio.run(llm_client.chat(model="unused",messages=[],purpose="ai_chatter",account_id=ACCOUNT))
    store.close()


def test_operations_reports_current_input_without_erasing_historical_refusals(database, monkeypatch):
    import asyncio
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from onlyfans_read import repo
    url, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    plan = planner.prepare(TENANT, claim, SyntheticNativeModel("I sent you a picture"))
    assert plan_state(engine, plan) == "refused"

    async def operations():
        async_engine = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"))
        @asynccontextmanager
        async def session():
            async with async_sessionmaker(async_engine)() as s:
                yield s
        monkeypatch.setattr(repo, "get_session", session)
        try:
            return await repo.SqlOnlyFansReadRepo().operations(tenant_id=TENANT, account_id=ACCOUNT)
        finally:
            await async_engine.dispose()

    status = asyncio.run(operations())
    assert status["planning"]["refused"] == 1 and "generation_held" in status["reasons"]
    store.record(TENANT, ACCOUNT, event(mid=2, body="Here is newer input")); store.drain(TENANT, ACCOUNT)
    status = asyncio.run(operations())
    assert status["planning"]["refused"] == 0 and "generation_held" not in status["reasons"]
    assert plan_state(engine, plan) == "refused", "superseded evidence remains in PostgreSQL"
    due(engine)
    next_claim = store.claim(TENANT)
    next_plan = planner.prepare(TENANT, next_claim, SyntheticNativeModel())
    with Session(engine) as s, s.begin():
        s.get(OfNativePlan, (ACCOUNT, next_plan)).expires_at = utcnow()-timedelta(seconds=1)
    status = asyncio.run(operations())
    assert status["planning"]["ready"] == 0 and status["planning"]["stale"] == 1
    assert "generation_held" in status["reasons"]
    assert plan_state(engine, next_plan) == "ready", "the read derives expiry without rewriting evidence"
    store.close()
