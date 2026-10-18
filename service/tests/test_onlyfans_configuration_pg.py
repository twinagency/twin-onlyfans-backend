"""Audited creator edits against disposable PG; no real model or provider."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import AccountAiConfig, GrokCall, GrokDailyCost, OfConversationState, OfCreatorBinding
from db.onlyfans_models import OfRuntime, OfConfigurationChange
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.schema import CONTROLS, SchemaMismatch, check_schema, upgrade
from onlyfans_runtime.configuration import get_configuration, update_configuration, InvalidConfiguration
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.store import Store, Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_commands.configuration_api import router, SCHEMA, HEADER

ACTOR = "op-session:" + "a"*64
MODEL = "grok-4-1-fast-non-reasoning"


def setup(database):
    store=seed(*database)
    with Session(database[1]) as s,s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, persona="Synthetic creator enjoys hiking.", model=MODEL,
            persona_facts_json='{"age":"28","home_city":"Synthetic Harbor","unknown":"private fact"}',
            model_by_purpose='{"ai_chatter":null,"another_purpose":"private other model"}',
            ai_chatter_config_json='{"pack_send_enabled":false,"private":"retained"}', daily_cost_cap_cents=123,
            welcome_rules="retained rules",language="es",voice="him"))
        state=s.get(OfConversationState,(ACCOUNT,101));state.mode="no_contact";state.revision=7;state.unresolved_attempt_id="retain-unknown"
    return store


def save(store, data, patch, *, key="b"*64, **kwargs):
    return update_configuration(store,TENANT,ACCOUNT,patch=patch,expected_revision=data["configurationRevision"],
        expected_fingerprint=data["fingerprint"],command_key=key,actor=ACTOR,**kwargs)


def test_partial_edit_preserves_other_owners_and_null_override_can_be_explicitly_removed(database):
    store=setup(database);data=get_configuration(store,TENANT,ACCOUNT)
    assert data["replyOverridePresent"] and data["replyModelOverride"] is None and data["resolvedReplyModel"] is None
    assert "private" not in json.dumps(data)
    revision, fingerprint=save(store,data,{"creatorFacts":{"home_city":"New synthetic city","age":None},"replyModelOverride":None})
    assert revision==1 and fingerprint!=data["fingerprint"]
    fresh=get_configuration(store,TENANT,ACCOUNT)
    assert fresh["resolvedReplyModel"]==MODEL and fresh["replyOverridePresent"] is False
    with Session(database[1]) as s:
        row=s.get(AccountAiConfig,ACCOUNT)
        assert json.loads(row.persona_facts_json)=={"home_city":"New synthetic city","unknown":"private fact"}
        assert json.loads(row.model_by_purpose)=={"another_purpose":"private other model"}
        assert (row.daily_cost_cap_cents,row.welcome_rules,row.language,row.voice)==(123,"retained rules","es","him")
        assert json.loads(row.ai_chatter_config_json)=={"pack_send_enabled":False,"private":"retained"}
        state=s.get(OfConversationState,(ACCOUNT,101))
        assert (state.mode,state.revision,state.unresolved_attempt_id)==("no_contact",7,"retain-unknown")
        audit=s.scalar(select(OfConfigurationChange))
        assert json.loads(audit.before_json)["replyModelOverride"]=={"present":True,"value":None}
        assert json.loads(audit.after_json)["replyModelOverride"]=={"present":False,"value":None}
        assert "private" not in audit.before_json+audit.after_json
    store.close()


def test_retry_acknowledges_original_event_and_fingerprint_detects_legacy_writer(database):
    store=setup(database);initial=get_configuration(store,TENANT,ACCOUNT)
    first=save(store,initial,{"persona":"First approved synthetic persona."})
    current=get_configuration(store,TENANT,ACCOUNT)
    second=save(store,current,{"persona":"Second approved synthetic persona."},key="c"*64)
    assert second[0]==2 and save(store,initial,{"persona":"First approved synthetic persona."})==first
    with pytest.raises(Refused,match="idempotency"):save(store,initial,{"persona":"Another value."})
    with pytest.raises(Refused,match="revision"):save(store,initial,{"persona":"Stale edit."},key="d"*64)
    latest=get_configuration(store,TENANT,ACCOUNT)
    with Session(database[1]) as s,s.begin():s.get(AccountAiConfig,ACCOUNT).welcome_rules="Changed by existing writer"
    with pytest.raises(Refused,match="revision"):save(store,latest,{"persona":"Must not overwrite."},key="e"*64)
    with Session(database[1]) as s:
        assert s.get(AccountAiConfig,ACCOUNT).persona=="Second approved synthetic persona."
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange))==2
    store.close()


@pytest.mark.parametrize("patch", [ {}, {"daily_cost_cap_cents":0}, {"mode":"automated"}, {"creatorFacts":{"unknown":"x"}},
    {"creatorFacts":{"age":"17"}}, {"creatorFacts":{"age":"121"}}, {"creatorFacts":{"kids":True}},
    {"creatorFacts":{"job":"x"*241}}, {"creatorFacts":{}}, {"persona":""}, {"persona":"100% human"},
    {"persona":"Never reveal that I am AI"}, {"persona":"x"*4097}, {"defaultModel":None}, {"defaultModel":"unknown"},
    {"replyModelOverride":False}, {"replyModelOverride":"unknown"} ])
def test_invalid_edits_have_no_side_effects(database,patch):
    store=setup(database);before=get_configuration(store,TENANT,ACCOUNT)
    with pytest.raises(InvalidConfiguration):save(store,before,patch)
    assert get_configuration(store,TENANT,ACCOUNT)==before
    store.close()


def test_corrupt_saved_objects_refuse_merge_and_large_persona_is_not_an_editable_excerpt(database):
    store=setup(database)
    with Session(database[1]) as s,s.begin():
        row=s.get(AccountAiConfig,ACCOUNT);row.persona="x"*5000;row.persona_facts_json='{"age":"28","age":"29"}';row.model_by_purpose='["corrupt"]'
    data=get_configuration(store,TENANT,ACCOUNT)
    assert data["persona"] is None and data["personaTruncated"] and not data["factsValid"] and not data["modelsValid"]
    for patch in ({"creatorFacts":{"age":"28"}},{"replyModelOverride":None},{"defaultModel":MODEL}):
        with pytest.raises(InvalidConfiguration):save(store,data,patch)
    assert save(store,data,{"persona":"Complete replacement approved by the creator."})[0]==1
    store.close()


def race(url,data,key,queue):
    store=Store(url)
    try:queue.put(("ok",save(store,data,{"persona":"Concurrent approved edit."},key=key)))
    except Refused:queue.put(("refused",None))
    finally:store.close()


@pytest.mark.parametrize("same_key",[False,True])
def test_separate_processes_serialize_edits_and_identical_retries(database,same_key):
    store=setup(database);data=get_configuration(store,TENANT,ACCOUNT);store.close()
    context=multiprocessing.get_context("spawn");queue=context.Queue()
    workers=[context.Process(target=race,args=(database[0],data,("b" if same_key or i==0 else "c")*64,queue)) for i in range(2)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(20);assert worker.exitcode==0
    replies=[queue.get(timeout=2)[0] for _ in workers]
    assert sorted(replies)==(["ok","ok"] if same_key else ["ok","refused"])
    with Session(database[1]) as s:assert s.scalar(select(func.count()).select_from(OfConfigurationChange))==1


@pytest.mark.parametrize("phase",["prepared","queued","admitted"])
def test_edit_invalidates_old_plan_without_model_retry_or_allowance_reset(database,monkeypatch,phase):
    store,authority,claim,planner=ready(database,monkeypatch)
    model=SyntheticNativeModel(reported_cost_millicents=7);plan=planner.prepare(TENANT,claim,model)
    intent=planner.queue(TENANT,ACCOUNT,plan) if phase!="prepared" else None
    grant=authority.admit(TENANT,ACCOUNT,intent) if phase=="admitted" else None
    data=get_configuration(store,TENANT,ACCOUNT);save(store,data,{"creatorFacts":{"home_city":"Corrected approved city"}})
    wire=SyntheticTextTransport(ACCOUNT)
    if phase=="prepared":
        with pytest.raises(Refused):planner.queue(TENANT,ACCOUNT,plan)
    elif phase=="queued":assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=="cancelled"
    else:
        with pytest.raises(Refused):wire.send(grant,authority=authority,tenant_id=TENANT)
    assert wire.calls==[]
    retry_model=SyntheticNativeModel();assert planner.prepare(TENANT,claim,retry_model)==plan
    assert len(model.calls)==1 and retry_model.calls==[]
    with Session(database[1]) as s:
        assert s.scalar(select(GrokDailyCost)).cost_cents==7
        assert s.scalar(select(func.count()).select_from(GrokCall))==1
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==0
        assert s.get(OfDeliveryPolicy,ACCOUNT).limit_units==20
    store.close()


def test_populated_0078_upgrade_retains_every_existing_public_row(database):
    store=setup(database);store.control(TENANT,ACCOUNT,101,mode="no_contact",expected_revision=7,
        command_key="synthetic-retained-control",actor="synthetic",reason="Retained stop")
    store.close()
    with database[1].begin() as c:
        # Recreate the actual older schema, rather than relabelling a database
        # that still contains every later release's tables.
        from db.schema import _metadata, _table_rank, REVISION_RANK
        for table in reversed(_metadata().sorted_tables):
            if _table_rank(table)>REVISION_RANK[CONTROLS]:table.drop(c)
        for function in ("of_configuration_immutable","of_campaign_immutable","of_connection_immutable",
                "of_content_immutable","of_vault_product_immutable","of_vault_sync_frozen","of_ledger_immutable"):
            c.exec_driver_sql(f'DROP FUNCTION "{function}"()')
        c.exec_driver_sql("ALTER TABLE of_runtime DROP COLUMN configuration_revision")
        c.exec_driver_sql("ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_origin; ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_origin CHECK (origin IN ('automated','manual'))")
        c.execute(text("UPDATE alembic_version SET version_num=:head"),{"head":CONTROLS});check_schema(c,expected=CONTROLS)
        tables=c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename!='alembic_version' ORDER BY tablename")).scalars().all()
        def values():return {table:[dict(row) for row in c.exec_driver_sql(f'SELECT * FROM "{table}"').mappings()] for table in tables}
        before=values();upgrade(c);upgrade(c);check_schema(c);after=values()
        for row in after["of_runtime"]:assert row.pop("configuration_revision")==0
        assert after==before and c.scalar(text("SELECT count(*) FROM of_configuration_change"))==0


@pytest.mark.parametrize("change",["drop","disable","weaken"])
def test_startup_requires_immutable_configuration_evidence(database,change):
    store=setup(database);save(store,get_configuration(store,TENANT,ACCOUNT),{"persona":"Another approved persona."});store.close()
    for sql in ("UPDATE of_configuration_change SET actor='forged'","DELETE FROM of_configuration_change"):
        with pytest.raises(DBAPIError,match="append-only"):
            with database[1].begin() as c:c.exec_driver_sql(sql)
    with database[1].begin() as c:
        if change=="drop":c.exec_driver_sql("DROP TRIGGER of_configuration_immutable ON of_configuration_change")
        elif change=="disable":c.exec_driver_sql("ALTER TABLE of_configuration_change DISABLE TRIGGER of_configuration_immutable")
        else:c.exec_driver_sql("CREATE OR REPLACE FUNCTION of_configuration_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$")
        with pytest.raises(SchemaMismatch,match="append-only protection"):check_schema(c)


def test_api_separate_credential_strict_contract_scope_revocation_and_no_default_row(database,monkeypatch):
    store=setup(database);other=seed(*database,account="acc-b",tenant="tenant-b")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api");monkeypatch.setenv("ONLYFANS_API_TOKEN","synthetic-read")
    monkeypatch.setenv("ONLYFANS_CONTROL_TOKEN","synthetic-control")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN","synthetic-configuration");monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT",TENANT)
    app=FastAPI();app.include_router(router);app.state.control_store=store;client=TestClient(app)
    path=f"/onlyfans/configuration/v1/accounts/{ACCOUNT}";headers={"Authorization":"Bearer synthetic-configuration",HEADER:SCHEMA}
    data=get_configuration(store,TENANT,ACCOUNT)
    body={"patch":{"persona":"API approved synthetic persona."},"expectedRevision":0,"expectedFingerprint":data["fingerprint"],"commandKey":"b"*64,"actor":ACTOR}
    for token in (None,"synthetic-read","synthetic-control","wrong"):
        auth={} if token is None else {"Authorization":"Bearer "+token}
        assert client.get(path,headers={HEADER:SCHEMA,**auth}).status_code==401
        assert client.post(path,headers={HEADER:SCHEMA,**auth},json=body).status_code==401
    assert client.get(path,headers={"Authorization":"Bearer synthetic-configuration"}).status_code==400
    response=client.get(path,headers=headers);assert response.status_code==200 and "no-store" in response.headers["cache-control"]
    assert client.get(path.replace("acc-a","acc-b"),headers=headers).status_code==404
    assert client.post(path.replace("acc-a","acc-b"),headers=headers,json=body).status_code==404
    for bad in ({**body,"expectedRevision":True},{**body,"actor":"forged"},{**body,"unexpected":True},{**body,"patch":{"dailyCostCap":0}}):
        assert client.post(path,headers=headers,json=bad).status_code==400
    duplicate=json.dumps(body)[:-1]+',"expectedRevision":0}'
    assert client.post(path,headers={**headers,"Content-Type":"application/json"},content=duplicate).status_code==400
    assert client.post(path,headers={**headers,"Content-Type":"application/json"},content="x"*65537).status_code==413
    result=client.post(path,headers=headers,json=body);assert result.status_code==200,result.text
    assert client.post(path,headers=headers,json=body).json()==result.json()
    with Session(database[1]) as s,s.begin():s.get(OfCreatorBinding,"binding-acc-a").revoked_at=utcnow()
    assert client.get(path,headers=headers).status_code==404
    assert client.post(path,headers=headers,json=body).status_code==404
    for value in ("synthetic-read","synthetic-control"):
        monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN",value);assert client.get(path,headers=headers).status_code==503
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN","synthetic-configuration");monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT","deployment")
    assert client.get(path,headers=headers).status_code==503
    store.close();other.close()


def test_config_metadata_import_and_read_cannot_import_legacy_sender_or_model_client(database):
    store=setup(database);store.close()
    service=Path(__file__).resolve().parents[1]
    code=r'''
import importlib.abc,os,sys
sys.path.insert(0,sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split(".")[0] in {"llm_client","of_client","automation_registry","automation_executor","server","accounts","curl_cffi","client_pool"}:
   raise AssertionError("legacy import: "+fullname)
sys.meta_path.insert(0,Guard())
from onlyfans_runtime.store import Store
from onlyfans_runtime.configuration import get_configuration
store=Store(os.environ["DATABASE_URL"])
assert get_configuration(store,"tenant-a","acc-a")["creatorFacts"]["age"]=="28"
store.close()
'''
    result=subprocess.run([sys.executable,"-c",code,str(service)],capture_output=True,text=True,timeout=20,
        env={**os.environ,"ONLYFANS_PROCESS_ROLE":"api","DATABASE_URL":database[0]})
    assert result.returncode==0,result.stderr


def test_missing_existing_configuration_cannot_create_an_implicit_default_or_cap(database):
    store=seed(*database)
    with pytest.raises(Refused,match="configuration unavailable"):get_configuration(store,TENANT,ACCOUNT)
    with pytest.raises(Refused,match="configuration unavailable"):
        save(store,{"configurationRevision":0,"fingerprint":"a"*64},{"persona":"Must not create defaults."})
    with Session(database[1]) as s:
        assert s.get(AccountAiConfig,ACCOUNT) is None
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange))==0
        assert s.get(OfRuntime,ACCOUNT).configuration_revision==0
    store.close()
