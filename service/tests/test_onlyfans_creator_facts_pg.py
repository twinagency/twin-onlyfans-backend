"""Shared typed facts through the existing admin reader; no live services."""
from copy import deepcopy
from datetime import date, timedelta
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_configuration_pg import setup, save
from test_onlyfans_workspace_pg import read
from test_onlyfans_worker_pg import start, worker
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import AccountAiConfig, GrokCall
from db.onlyfans_models import OfRuntime, OfWork, OfConfigurationChange
from db.onlyfans_creator_facts_models import OfCreatorFactsSource
from db.schema import LEDGER, check_schema, upgrade
from onlyfans_runtime.creator_facts import TwinFactsReader, SOURCE_COLUMNS, map_facts, effective, refresh
from onlyfans_runtime.configuration import get_configuration, InvalidConfiguration
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.store import Refused, utcnow


def source(**changes):
    value=dict.fromkeys(SOURCE_COLUMNS)
    value.update(id="creator-acc-a",updated_at=utcnow().isoformat()+"Z",date_of_birth="2000-01-02",
        hometown="Synthetic origin",current_city="Synthetic present",cover_story="Approved cover story",
        occupation="Retired occupation",relationship_canon="single",tattoos=False,
        tattoo_details=[],turn_ons=[],hard_limits=[])
    value.update(changes)
    return value


def reader(monkeypatch, value, *, on_read=None):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    calls=[]
    def get(self,url):
        calls.append(url)
        if on_read:on_read()
        body={"ok":True,"version":"v1","table":"clones","rowCount":1,"truncated":parse_qs(urlsplit(url).query)["limit"]==["1"],"rows":[deepcopy(value)]}
        return json.dumps(body).encode()
    monkeypatch.setattr(TwinFactsReader,"_get_once",get)
    return TwinFactsReader("http://127.0.0.1:3000","synthetic-"+"x"*40), calls


def make_due(engine):
    with Session(engine) as s,s.begin():s.get(OfCreatorFactsSource,ACCOUNT).attempted_at=utcnow()-timedelta(seconds=31)


def test_typed_mapping_does_not_infer_unknowns_or_use_retired_job_over_cover_story():
    value=source(date_of_birth="2000-10-19",relationship_canon="wont_say",tattoos=None)
    facts=map_facts(value,today=date(2026,10,18))
    assert facts["age"]=="25" and map_facts(value,today=date(2026,10,19))["age"]=="26"
    assert facts["job"]=="Approved cover story"
    assert facts["home_city"]=="Hometown: Synthetic origin; Current city: Synthetic present"
    assert not {"relationship","tattoos","kids","family"}&facts.keys()
    assert map_facts(source())["tattoos"]=="No tattoos"
    with pytest.raises(Refused):map_facts(source(date_of_birth="2020-01-01"))


def test_reader_scope_role_owned_row_and_no_secret_in_repr(monkeypatch):
    value=source();r,calls=reader(monkeypatch,value)
    assert r.token not in repr(r)
    assert r.read(value["id"])[0]==value
    query=parse_qs(urlsplit(calls[0]).query)
    assert query=={"table":["clones"],"select":[",".join(SOURCE_COLUMNS)],"filter":["id:eq:creator-acc-a"],"limit":["2"]}
    for creator in ("creator-acc-a&limit=1000", "", None):
        with pytest.raises(Refused):r.read(creator)
    value["id"]="other-creator"
    with pytest.raises(Refused,match="unavailable"):r.read("creator-acc-a")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    with pytest.raises(Refused):r.read("creator-acc-a")


@pytest.mark.parametrize("url",["http://public.example","https://u:p@private.example","https://private.example/path","https://private.example/?x=1"])
def test_reader_requires_explicit_fixed_origin_and_private_plaintext(url):
    with pytest.raises(Refused):TwinFactsReader(url,"x"*40)


def test_existing_overrides_clear_and_inherit_one_shared_creator_owner(database,monkeypatch):
    store=setup(database);value=source();r,calls=reader(monkeypatch,value)
    def unlocked():
        with database[1].begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='500ms'")
            c.execute(text("UPDATE of_runtime SET enabled=enabled WHERE account_id=:id"),{"id":ACCOUNT})
    r,calls=reader(monkeypatch,value,on_read=unlocked)
    refresh(store,TENANT,ACCOUNT,r)
    data=get_configuration(store,TENANT,ACCOUNT)
    assert data["sharedFacts"]["state"]=="current"
    assert data["creatorFacts"]["home_city"]=="Synthetic Harbor"  # existing approved override retained
    assert data["creatorFacts"]["job"]=="Approved cover story"
    assert data["sharedFacts"]["overrideKeys"]==["age","home_city"]
    refresh(store,TENANT,ACCOUNT,r);assert len(calls)==1  # bounded repeated polling
    save(store,data,{"creatorFacts":{"job":None}},key="c"*64)
    data=get_configuration(store,TENANT,ACCOUNT)
    assert data["creatorFacts"]["job"] is None and "job" in data["sharedFacts"]["overrideKeys"]
    save(store,data,{"inheritCreatorFacts":["job","home_city"]},key="d"*64)
    data=get_configuration(store,TENANT,ACCOUNT)
    assert data["creatorFacts"]["job"]=="Approved cover story"
    assert data["creatorFacts"]["home_city"]=="Hometown: Synthetic origin; Current city: Synthetic present"
    assert data["sharedFacts"]["overrideKeys"]==["age"]
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfCreatorFactsSource))==1
        assert json.loads(s.get(OfCreatorFactsSource,ACCOUNT).overrides_json)=={"age":"28","unknown":"private fact"}
        assert s.get(OfRuntime,ACCOUNT).configuration_revision==3
    store.close()


def test_failed_or_regressed_refresh_never_freshens_cache_and_stale_ai_does_not_claim(database,monkeypatch):
    store,_=start(database,monkeypatch);value=source();r,_=reader(monkeypatch,value)
    refresh(store,TENANT,ACCOUNT,r)
    old=get_configuration(store,TENANT,ACCOUNT)["sharedFacts"]["checkedAt"]
    make_due(database[1]);value["updated_at"]="2000-01-01T00:00:00Z"
    refresh(store,TENANT,ACCOUNT,r)
    assert get_configuration(store,TENANT,ACCOUNT)["sharedFacts"]["checkedAt"]==old
    with Session(database[1]) as s,s.begin():
        cache=s.get(OfCreatorFactsSource,ACCOUNT);assert cache.last_error=="shared_creator_version_regressed"
        cache.checked_at=utcnow()-timedelta(seconds=121)
    # Existing cache remains required even when the deployment flag is later off.
    monkeypatch.delenv("ONLYFANS_TWIN_FACTS_ENABLED",raising=False)
    model=SyntheticNativeModel();result=worker(store,model=model).step()
    assert result.claims==0 and result.held==1 and not model.calls
    with Session(database[1]) as s:
        assert s.get(OfWork,(ACCOUNT,101)).status=="waiting"
        assert s.scalar(select(func.count()).select_from(GrokCall))==0
    make_due(database[1])
    def fail(self,url):raise TimeoutError("private diagnostics must not appear")
    monkeypatch.setattr(TwinFactsReader,"_get_once",fail)
    refresh(store,TENANT,ACCOUNT,r)
    with Session(database[1]) as s:
        cache=s.get(OfCreatorFactsSource,ACCOUNT)
        assert cache.last_error=="shared_creator_unavailable" and cache.checked_at<utcnow()-timedelta(seconds=120)
    store.close()


def test_full_typed_limits_in_existing_composer_and_settings_without_changing_legacy_default(database,monkeypatch):
    store,authority,claim,planner=ready(database,monkeypatch)
    limits=["Approved limitation "+str(i)+" "+"x"*30 for i in range(12)]
    r,_=reader(monkeypatch,source(hard_limits=limits))
    refresh(store,TENANT,ACCOUNT,r)
    model=SyntheticNativeModel();plan=planner.prepare(TENANT,claim,model)
    assert limits[-1] in str(model.calls[0])
    data=get_configuration(store,TENANT,ACCOUNT)
    assert data["creatorFacts"]["limits"]=="; ".join(limits) and "limits" in data["readOnlyFacts"]
    assert any(row["value"]=="; ".join(limits) for row in read(database,"settings")["configuration"]["creatorFacts"])
    from automations._persona import compose_persona
    with Session(database[1]) as s:
        row=s.get(AccountAiConfig,ACCOUNT)
        assert limits[-1] not in compose_persona(row,fallback="")  # unchanged legacy clipping
    # A freshness-only renewal must not invalidate the prepared context.
    make_due(database[1]);refresh(store,TENANT,ACCOUNT,r)
    assert planner.queue(TENANT,ACCOUNT,plan)
    store.close()


def test_source_change_or_staleness_invalidates_existing_plan_before_egress(database,monkeypatch):
    store,authority,claim,planner=ready(database,monkeypatch)
    value=source();r,_=reader(monkeypatch,value);refresh(store,TENANT,ACCOUNT,r)
    model=SyntheticNativeModel();plan=planner.prepare(TENANT,claim,model)
    make_due(database[1]);value["cover_story"]="Corrected shared story";value["updated_at"]=utcnow().isoformat()+"Z"
    refresh(store,TENANT,ACCOUNT,r)
    with pytest.raises(Refused):planner.queue(TENANT,ACCOUNT,plan)
    assert len(model.calls)==1
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange))==2
    store.close()


def test_missing_source_holds_before_model_and_inherit_requires_cache(database,monkeypatch):
    store,_,claim,planner=ready(database,monkeypatch)
    data=get_configuration(store,TENANT,ACCOUNT)
    with pytest.raises(InvalidConfiguration,match="Synchronize"):save(store,data,{"inheritCreatorFacts":["job"]})
    monkeypatch.setenv("ONLYFANS_TWIN_FACTS_ENABLED","1")
    model=SyntheticNativeModel()
    with pytest.raises(Refused,match="facts"):planner.prepare(TENANT,claim,model)
    assert not model.calls
    store.close()


def test_populated_ledger_upgrade_preserves_rows_and_registers_fact_projection(database):
    store=setup(database);store.close()
    with database[1].begin() as c:
        from test_onlyfans_offer_import_upgrade_pg import historical_schema
        historical_schema(c,LEDGER)
        before=c.exec_driver_sql("SELECT row_to_json(t) FROM account_ai_config t").scalars().all()
        upgrade(c);upgrade(c);check_schema(c)
        assert before==c.exec_driver_sql("SELECT row_to_json(t) FROM account_ai_config t").scalars().all()
        assert c.scalar(text("SELECT count(*) FROM of_creator_facts_source"))==0


def test_identical_shared_facts_preserve_legacy_json_and_do_not_create_an_edit(database,monkeypatch):
    store=setup(database)
    with Session(database[1]) as s:
        before=s.get(AccountAiConfig,ACCOUNT).persona_facts_json
    r,_=reader(monkeypatch,source(date_of_birth=None,hometown=None,current_city=None,cover_story=None,occupation=None,relationship_canon=None,tattoos=None))
    refresh(store,TENANT,ACCOUNT,r)
    with Session(database[1]) as s:
        assert s.get(AccountAiConfig,ACCOUNT).persona_facts_json==before
        assert s.get(OfRuntime,ACCOUNT).configuration_revision==0
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange))==0
    assert get_configuration(store,TENANT,ACCOUNT)["sharedFacts"]["state"]=="current"
    store.close()
