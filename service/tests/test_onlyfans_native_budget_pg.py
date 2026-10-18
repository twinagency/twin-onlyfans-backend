"""Real PostgreSQL native model-budget evidence; no paid model or provider."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import multiprocessing

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import AccountAiConfig, GrokCall, GrokDailyCost, Fan, OfConversationState
from db.onlyfans_delivery_models import OfNativePlan
from onlyfans_runtime.native_planning import NativeTextPlanner, SyntheticNativeModel
from onlyfans_runtime.store import Claim, Store, Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def evidence(engine, identity):
    with Session(engine) as s:
        plan = s.get(OfNativePlan, (ACCOUNT, identity))
        call = s.get(GrokCall, plan.model_call_id) if plan.model_call_id else None
        costs = list(s.scalars(select(GrokDailyCost).where(GrokDailyCost.account_id == ACCOUNT)))
        s.expunge_all()
        return plan, call, costs


@pytest.mark.parametrize("cap,spent", [(100, 10000), (0, 0)])
def test_exhausted_or_zero_budget_never_invokes_or_resets(database, monkeypatch, cap, spent):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    with Session(engine) as s, s.begin():
        s.get(AccountAiConfig, ACCOUNT).daily_cost_cap_cents = cap
        s.add(GrokDailyCost(day=utcnow().strftime("%Y-%m-%d"), account_id=ACCOUNT,
            provider="grok", cost_cents=spent, call_count=17, is_capped=True))
    model = SyntheticNativeModel()
    plan, call, costs = evidence(engine, planner.prepare(TENANT, claim, model))
    assert (plan.status, plan.reason) == ("refused", "model_budget_refused")
    assert model.calls == [] and call is None
    assert [(x.cost_cents, x.call_count, x.is_capped) for x in costs] == [(spent, 17, True)]
    store.close()


def test_reservation_commits_before_model_and_known_usage_settles_once(database, monkeypatch):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    with Session(engine) as s, s.begin():
        s.add(GrokDailyCost(day=utcnow().strftime("%Y-%m-%d"), account_id=ACCOUNT,
            provider="grok", cost_cents=200, call_count=3, is_capped=True))
    def observe():
        with Session(engine) as s:
            call = s.scalar(select(GrokCall).where(GrokCall.account_id == ACCOUNT))
            plan = s.scalar(select(OfNativePlan))
            cost = s.scalar(select(GrokDailyCost))
            assert call.status == "pending" and plan.model_call_id == call.id
            assert call.cost_cents == plan.reserved_millicents > 0
            assert cost.cost_cents == 200+call.cost_cents and cost.call_count == 4
    model = SyntheticNativeModel(on_generate=observe, reported_cost_millicents=7)
    identity = planner.prepare(TENANT, claim, model)
    plan, call, costs = evidence(engine, identity)
    assert plan.status == "ready" and call.status == "done" and plan.charged_millicents == call.cost_cents == 7
    assert (costs[0].cost_cents, costs[0].call_count, costs[0].is_capped) == (207, 4, True)
    assert planner.prepare(TENANT, claim, model) == identity and len(model.calls) == 1
    from llm_client import settle_scoped_synthetic_call
    with Session(engine) as s, s.begin():
        assert settle_scoped_synthetic_call(s, account_id=ACCOUNT, call_id=call.id,
            reserved_millicents=plan.reserved_millicents, actual_millicents=999, now=utcnow()) == 7
    assert evidence(engine, identity)[2][0].cost_cents == 207
    assert planner.queue(TENANT, ACCOUNT, identity)
    store.close()


@pytest.mark.parametrize("outcome", ["missing_usage", "timeout", "failure"])
def test_uncertain_usage_retains_reservation_and_never_repeats(database, monkeypatch, outcome):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    model = SyntheticNativeModel(reported_cost_millicents=None if outcome == "missing_usage" else 1,
        delay_seconds=.2 if outcome == "timeout" else 0, fail=outcome == "failure")
    identity = planner.prepare(TENANT, claim, model, timeout_seconds=.01 if outcome == "timeout" else 10)
    plan, call, costs = evidence(engine, identity)
    assert plan.status == "unknown" and call.status == "unknown"
    assert plan.cost_settled_at is None and plan.charged_millicents is None
    assert costs[0].cost_cents == call.cost_cents == plan.reserved_millicents > 0
    assert costs[0].call_count == 1
    with pytest.raises(Refused): planner.queue(TENANT, ACCOUNT, identity)
    assert planner.prepare(TENANT, claim, model) == identity and len(model.calls) == 1
    store.close()


def test_guard_refused_output_still_charges_known_model_usage(database, monkeypatch):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    model = SyntheticNativeModel("I just sent you a photo.", reported_cost_millicents=9)
    plan, call, costs = evidence(engine, planner.prepare(TENANT, claim, model))
    assert plan.status == "refused" and call.status == "done"
    assert plan.charged_millicents == costs[0].cost_cents == 9 and costs[0].call_count == 1
    store.close()


@pytest.mark.parametrize("base,purpose", [(None,None), ("retired",None),
    ("grok-4-1-fast-non-reasoning", "{"),
    ("grok-4-1-fast-non-reasoning", '{"ai_chatter":"retired"}'),
    ("grok-4-1-fast-non-reasoning", '{"ai_chatter":null}'),
    ("grok-4-1-fast-non-reasoning", '{"ai_chatter":"deepseek-v4-flash","ai_chatter":"grok-4-1-fast-non-reasoning"}')])
def test_bad_saved_choice_never_falls_back_or_spends(database, monkeypatch, base, purpose):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    with Session(engine) as s, s.begin():
        cfg = s.get(AccountAiConfig, ACCOUNT)
        cfg.model, cfg.model_by_purpose = base, purpose
    model = SyntheticNativeModel()
    plan, call, costs = evidence(engine, planner.prepare(TENANT, claim, model))
    assert plan.status == "refused" and plan.reason == "model_configuration_refused"
    assert not model.calls and call is None and not costs
    store.close()


def test_saved_purpose_and_voice_use_the_same_configuration_snapshot(database, monkeypatch):
    _, engine = database
    store, _, claim, planner = ready(database, monkeypatch)
    with Session(engine) as s, s.begin():
        cfg = s.get(AccountAiConfig, ACCOUNT)
        cfg.model_by_purpose, cfg.voice = '{"ai_chatter":"deepseek-v4-flash"}', "him"
    from automations import ai_chatter, _voice
    original = ai_chatter._build_messages
    seen = []
    def compose(*args, **kwargs):
        seen.append(kwargs["v"])
        return original(*args, **kwargs)
    monkeypatch.setattr(ai_chatter, "_build_messages", compose)
    plan, call, _ = evidence(engine, planner.prepare(TENANT, claim, SyntheticNativeModel()))
    assert plan.status == "ready" and call.model == "deepseek-v4-flash" and call.provider == "deepseek"
    assert seen == [_voice.blocks("him", sell_customs=False)]
    store.close()


@pytest.mark.parametrize("started", [datetime(2026,10,17,23,59,59),
    datetime(2026,10,18,9,59,59,tzinfo=timezone(timedelta(hours=10)))])
def test_midnight_settlement_uses_original_utc_day(database, monkeypatch, started):
    _, engine = database
    store, _, _, _ = ready(database, monkeypatch)
    from llm_client import reserve_scoped_synthetic_call, settle_scoped_synthetic_call
    with Session(engine) as s, s.begin():
        call, reserve, day = reserve_scoped_synthetic_call(s, account_id=ACCOUNT, fan_id=101,
            model="grok-4-1-fast-non-reasoning", messages=[{"role":"user","content":"hello"}], now=started)
        identity = call.id
    with Session(engine) as s, s.begin():
        assert settle_scoped_synthetic_call(s, account_id=ACCOUNT, call_id=identity,
            reserved_millicents=reserve, actual_millicents=3, now=started+timedelta(seconds=2)) == 3
    with Session(engine) as s:
        costs = list(s.scalars(select(GrokDailyCost)))
        assert day == "2026-10-17" and [(x.day,x.cost_cents,x.call_count) for x in costs] == [(day,3,1)]
    store.close()


def compete_budget(url, claim, gate, output):
    store = Store(url)
    model = SyntheticNativeModel(delay_seconds=.2, reported_cost_millicents=None)
    gate.wait(10)
    identity = NativeTextPlanner(store).prepare(TENANT, Claim(**claim), model)
    with Session(store.engine) as s:
        output.put((s.get(OfNativePlan,(ACCOUNT,identity)).status,len(model.calls)))
    store.close()


def test_two_fans_in_two_processes_share_one_account_budget(database, monkeypatch):
    url, engine = database
    store, _, claim, _ = ready(database, monkeypatch)
    with Session(engine) as s, s.begin():
        s.get(AccountAiConfig,ACCOUNT).daily_cost_cap_cents = 2
        s.add(Fan(account_id=ACCOUNT,fan_id=202)); s.flush()
        s.add(OfConversationState(account_id=ACCOUNT,fan_id=202,mode="automated",revision=0,
            takeover_state="none",requires_attention=False))
    store.record(TENANT,ACCOUNT,event(mid=2,fan=202)); store.drain(TENANT,ACCOUNT); due(engine)
    second = store.claim(TENANT)
    assert second is not None and second.fan_id == 202
    ctx = multiprocessing.get_context("spawn"); gate, output = ctx.Event(),ctx.Queue()
    workers = [ctx.Process(target=compete_budget,args=(url,asdict(c),gate,output)) for c in (claim,second)]
    for worker in workers: worker.start()
    gate.set()
    try:
        results = [output.get(timeout=25) for _ in workers]
        for worker in workers: worker.join(5); assert worker.exitcode == 0
        assert sorted(x[0] for x in results) == ["refused","unknown"]
        assert sum(x[1] for x in results) == 1
        with Session(engine) as s:
            costs = list(s.scalars(select(GrokDailyCost)))
            assert len(costs) == 1 and costs[0].cost_cents <= 200 and costs[0].call_count == 1
    finally:
        for worker in workers:
            if worker.is_alive(): worker.kill(); worker.join(5)
    store.close()


@pytest.mark.parametrize("field,value", [("cost_settled_at",None),("budget_day","1900-01-01"),
    ("charged_millicents",999),("reserved_millicents",None)])
def test_changed_cost_proof_blocks_final_wire(database, monkeypatch, field, value):
    _, engine = database
    store, authority, claim, planner = ready(database, monkeypatch)
    identity = planner.prepare(TENANT,claim,SyntheticNativeModel())
    intent = planner.queue(TENANT,ACCOUNT,identity)
    grant = authority.admit(TENANT,ACCOUNT,intent)
    with Session(engine) as s, s.begin(): setattr(s.get(OfNativePlan,(ACCOUNT,identity)),field,value)
    transport = SyntheticTextTransport(ACCOUNT)
    with pytest.raises(Refused): transport.send(grant,authority=authority,tenant_id=TENANT)
    assert transport.calls == []
    with Session(engine) as s:
        cost = s.scalar(select(GrokDailyCost))
        assert cost.cost_cents == 1 and cost.call_count == 1
    store.close()
