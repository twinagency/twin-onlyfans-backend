"""Same-call fact corrections, profile retention and immutable source provenance."""
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import AccountAiConfig, Fan, FanProfile, GrokCall, GrokDailyCost, Message
from db.onlyfans_delivery_models import OfNativePlan, OfNativeMemoryChange
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import SyntheticTextTransport

LATEST = "Actually I live in Sydney now. I go hiking at weekends."
QUOTE = "I live in Sydney now"


def memory_ready(database, monkeypatch):
    store, authority, _, planner = ready(database, monkeypatch)
    _, engine = database
    with Session(engine) as s, s.begin():
        s.get(Fan,(ACCOUNT,101)).home_city = "Brisbane"
        s.add(FanProfile(account_id=ACCOUNT,fan_id=101,short_bio="Sam lives in Brisbane.",
            bullet_points="Likes hiking.",q1="Preserve this existing question",tease1="Preserve this metered opener"))
    store.record(TENANT,ACCOUNT,event(mid=2,body=LATEST));store.drain(TENANT,ACCOUNT);due(engine)
    return store,authority,store.claim(TENANT),planner


def correction(**kwargs):
    return SyntheticNativeModel("How is Sydney treating you?",fact_updates=[
        {"field":"home_city","value":"Sydney","quote":QUOTE}],
        profile_update={"short_bio":"Sam lives in Sydney.","bullet_points":"Hikes at weekends.","quote":LATEST},**kwargs)


def test_correction_and_summary_commit_with_one_call_and_survive_later_history(database, monkeypatch):
    _, engine = database
    store,authority,claim,planner = memory_ready(database,monkeypatch)
    model = correction(reported_cost_millicents=7)
    identity = planner.prepare(TENANT,claim,model)
    with Session(engine) as s:
        plan=s.get(OfNativePlan,(ACCOUNT,identity));fan=s.get(Fan,(ACCOUNT,101));profile=s.get(FanProfile,(ACCOUNT,101))
        assert plan.status == "ready" and fan.home_city == "Sydney"
        assert profile.short_bio == "Sam lives in Sydney." and profile.bullet_points == "Hikes at weekends."
        assert profile.q1 == "Preserve this existing question" and profile.tease1 == "Preserve this metered opener"
        assert profile.generated_by_grok_call_id == plan.model_call_id
        audit=list(s.scalars(select(OfNativeMemoryChange)))
        assert len(audit)==3 and {x.source_message_id for x in audit}=={2}
        assert {x.plan_id for x in audit}=={identity} and {x.input_revision for x in audit}=={claim.input_revision}
        city=next(x for x in audit if x.field=="home_city")
        assert json.loads(city.before_json)=="Brisbane" and json.loads(city.after_json)=="Sydney" and city.source_quote==QUOTE
        assert list(s.scalars(select(Message).where(Message.direction=="out")))==[]
        cost=s.scalar(select(GrokDailyCost));assert (cost.cost_cents,cost.call_count)==(7,1)
    assert planner.prepare(TENANT,claim,model)==identity and len(model.calls)==1
    intent=planner.queue(TENANT,ACCOUNT,identity)
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=="accepted"
    # The source turn falls outside the next 100-message context; stored facts
    # and the existing profile remain available without another extraction call.
    for mid in range(3,105):store.record(TENANT,ACCOUNT,event(mid=mid,body=f"Synthetic update {mid}"))
    while store.drain(TENANT,ACCOUNT): pass
    due(engine)
    later=store.claim(TENANT);assert later is not None
    next_model=SyntheticNativeModel()
    later_id=planner.prepare(TENANT,later,next_model)
    with Session(engine) as s:
        plan=s.get(OfNativePlan,(ACCOUNT,later_id));assert plan.status=="ready"
        context=json.loads(plan.context_json)
        assert len(context["messages"])==100 and 2 not in [x["message_id"] for x in context["messages"]]
        assert len(list(s.scalars(select(OfNativeMemoryChange))))==3
    prompt=str(next_model.calls[0]);assert "Sydney" in prompt and "Hikes at weekends" in prompt and "Brisbane" not in prompt
    store.close()


def test_sparse_output_does_not_clear_memory_or_unrelated_profile_fields(database,monkeypatch):
    _,engine=database
    store,_,claim,planner=memory_ready(database,monkeypatch)
    identity=planner.prepare(TENANT,claim,SyntheticNativeModel())
    with Session(engine) as s:
        assert s.get(OfNativePlan,(ACCOUNT,identity)).status=="ready"
        assert s.get(Fan,(ACCOUNT,101)).home_city=="Brisbane"
        assert s.get(FanProfile,(ACCOUNT,101)).short_bio=="Sam lives in Brisbane."
        assert list(s.scalars(select(OfNativeMemoryChange)))==[]
    store.close()


@pytest.mark.parametrize("bad", [
    {"facts":[{"field":"account_id","value":"other","quote":QUOTE}]},
    {"facts":[{"field":"home_city","value":True,"quote":QUOTE}]},
    {"facts":[{"field":"home_city","value":"","quote":QUOTE}]},
    {"facts":[{"field":"home_city","value":"x"*81,"quote":QUOTE}]},
    {"facts":[{"field":"home_city","value":"Sydney","quote":"a quote from another turn"}]},
    {"facts":[{"field":"home_city","value":"Sydney","quote":QUOTE}]*2},
    {"facts":[{"field":"his_age","value":"16","quote":QUOTE}]},
    {"facts":{}},
    {"profile":{"short_bio":"New bio","bullet_points":"New note"}},
    {"profile":{"short_bio":"x"*401,"bullet_points":"New note","quote":QUOTE}},
    {"profile":{"short_bio":"New bio","bullet_points":"","quote":QUOTE}},
    {"extra":"must not cross"},
    {"reply":False},
    "{\"reply\":\"hello\",\"facts\":[],\"profile\":null,\"reply\":\"duplicate\"}",
    "not-json",
])
def test_invalid_memory_refuses_atomically_but_retains_known_cost(database,monkeypatch,bad):
    _,engine=database
    store,_,claim,planner=memory_ready(database,monkeypatch)
    payload={"reply":"Thanks for telling me.","facts":[],"profile":None}
    if isinstance(bad,dict):payload.update(bad)
    raw=bad if isinstance(bad,str) else json.dumps(payload)
    model=SyntheticNativeModel(raw_output=raw,reported_cost_millicents=7)
    identity=planner.prepare(TENANT,claim,model)
    with Session(engine) as s:
        plan=s.get(OfNativePlan,(ACCOUNT,identity));assert plan.status=="refused"
        assert s.get(GrokCall,plan.model_call_id).status=="done" and plan.charged_millicents==7
        assert s.get(Fan,(ACCOUNT,101)).home_city=="Brisbane"
        assert list(s.scalars(select(OfNativeMemoryChange)))==[]
    assert planner.prepare(TENANT,claim,model)==identity and len(model.calls)==1
    store.close()


@pytest.mark.parametrize("changed",["profile","fan","input","pause","unknown_cost"])
def test_obsolete_or_uncertain_generation_never_updates_facts(database,monkeypatch,changed):
    _,engine=database
    store,_,claim,planner=memory_ready(database,monkeypatch)
    def during():
        if changed=="input":
            store.record(TENANT,ACCOUNT,event(mid=3,body="Actually I live in Perth."));store.drain(TENANT,ACCOUNT)
        elif changed=="pause":
            store.control(TENANT,ACCOUNT,101,mode="paused",expected_revision=claim.control_revision,
                command_key="memory-pause",actor="synthetic-operator",reason="Synthetic concurrency check")
        elif changed in {"profile","fan"}:
            with Session(engine) as s,s.begin():
                if changed=="profile":s.get(FanProfile,(ACCOUNT,101)).short_bio="A newer operator correction"
                else:s.get(Fan,(ACCOUNT,101)).home_city="Perth"
    identity=planner.prepare(TENANT,claim,correction(on_generate=during,
        reported_cost_millicents=None if changed=="unknown_cost" else 7))
    with Session(engine) as s:
        assert s.get(OfNativePlan,(ACCOUNT,identity)).status in {"stale","unknown"}
        assert s.get(Fan,(ACCOUNT,101)).home_city==("Perth" if changed=="fan" else "Brisbane")
        assert list(s.scalars(select(OfNativeMemoryChange)))==[]
    store.close()


def test_profile_change_after_admission_blocks_wire(database,monkeypatch):
    _,engine=database
    store,authority,claim,planner=memory_ready(database,monkeypatch)
    identity=planner.prepare(TENANT,claim,SyntheticNativeModel())
    intent=planner.queue(TENANT,ACCOUNT,identity);grant=authority.admit(TENANT,ACCOUNT,intent)
    with Session(engine) as s,s.begin():s.get(FanProfile,(ACCOUNT,101)).bullet_points="New operator note"
    wire=SyntheticTextTransport(ACCOUNT)
    with pytest.raises(Refused):wire.send(grant,authority=authority,tenant_id=TENANT)
    assert wire.calls==[]
    store.close()


def test_memory_evidence_is_append_only_in_postgresql(database,monkeypatch):
    _,engine=database
    store,_,claim,planner=memory_ready(database,monkeypatch)
    planner.prepare(TENANT,claim,correction())
    for sql in ["UPDATE of_native_memory_change SET after_json='\"corrupted\"'","DELETE FROM of_native_memory_change"]:
        with pytest.raises(DBAPIError,match="append-only"):
            with engine.begin() as c:c.exec_driver_sql(sql)
    with Session(engine) as s:assert len(list(s.scalars(select(OfNativeMemoryChange))))==3
    store.close()


def test_manual_fan_language_uses_native_language_and_identity_owner(database,monkeypatch):
    _,engine=database
    store,_,_,planner=memory_ready(database,monkeypatch)
    with Session(engine) as s,s.begin():
        fan=s.get(Fan,(ACCOUNT,101));fan.language,fan.language_source="es","manual"
        s.get(AccountAiConfig,ACCOUNT).language="en"
    store.record(TENANT,ACCOUNT,event(mid=3,body="eres un bot"));store.drain(TENANT,ACCOUNT);due(engine)
    model=SyntheticNativeModel("Soy un clon de chat con IA.")
    identity=planner.prepare(TENANT,store.claim(TENANT),model)
    with Session(engine) as s:assert s.get(OfNativePlan,(ACCOUNT,identity)).status=="ready"
    prompt=str(model.calls[0]);assert "OUTPUT LANGUAGE: Español (es)" in prompt and "answer honestly and directly" in prompt
    store.close()


def test_retracted_source_after_model_does_not_persist_memory_but_settles_cost(database,monkeypatch):
    _,engine=database
    store,_,claim,planner=memory_ready(database,monkeypatch)
    def retract():
        with Session(engine) as s,s.begin():s.get(Message,(ACCOUNT,101,2)).is_unsent=True
    identity=planner.prepare(TENANT,claim,correction(on_generate=retract,reported_cost_millicents=7))
    with Session(engine) as s:
        plan=s.get(OfNativePlan,(ACCOUNT,identity))
        assert (plan.status,plan.reason)==("refused","memory_source_changed")
        assert s.get(Fan,(ACCOUNT,101)).home_city=="Brisbane"
        assert list(s.scalars(select(OfNativeMemoryChange)))==[]
        assert s.scalar(select(GrokDailyCost)).cost_cents==7
        assert s.get(GrokCall,plan.model_call_id).status=="done"
    store.close()


def test_encrypted_restore_preserves_memory_cost_and_append_only_provenance(database,monkeypatch,tmp_path):
    import os
    from test_onlyfans_recovery_pg import empty_database, rows, KEY
    from db.schema import SchemaMismatch,check_schema
    from onlyfans_runtime import operations as ops
    url,engine=database
    store,_,claim,planner=memory_ready(database,monkeypatch)
    planner.prepare(TENANT,claim,correction(reported_cost_millicents=7))
    before=rows(engine);before.pop("alembic_version")
    archive=tmp_path/"native-memory.twinof"
    ops.backup(url,archive,key=KEY,pg_bin=os.environ["ONLYFANS_TEST_PG_BIN"])
    assert LATEST.encode() not in archive.read_bytes()
    with empty_database() as (target,restored):
        result=ops.restore(target,archive,key=KEY,pg_bin=os.environ["ONLYFANS_TEST_PG_BIN"])
        assert result["quarantined"] and not result["releasePermitted"]
        after=rows(restored);after.pop("alembic_version");assert after==before
        with restored.connect() as c:
            with pytest.raises(SchemaMismatch,match="quarantined"):check_schema(c)
        with pytest.raises(DBAPIError,match="append-only"):
            with restored.begin() as c:c.exec_driver_sql("DELETE FROM of_native_memory_change")
    store.close()
