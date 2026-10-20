"""Scoped operator controls against disposable PostgreSQL, never a provider."""
import json
import multiprocessing
from pathlib import Path
from alembic import command as alembic_command

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed, event, due
from test_onlyfans_text_delivery_pg import configured, prepare, accepted, rejected, TENANT, ACCOUNT, FAN
from db.models import Account, Fan, Message, OfConversationState, OfCreatorBinding, OfMessageDelivery
from db.onlyfans_models import OfControlAudit, OfWork
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.schema import MEDIA, SchemaMismatch, check_schema, upgrade
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_commands.settings import ACTIONS, HEADER, SCHEMA
from onlyfans_commands.api import router


def command(store, action, revision, key=None, *, fan=FAN, tenant=TENANT, account=ACCOUNT):
    return store.control(tenant, account, fan, mode=ACTIONS[action], action=action,
        expected_revision=revision, command_key=key or f"control-{action}-{revision}",
        actor="op-session:" + "a"*64, reason="Dashboard operator: " + action)


def test_controls_replay_is_historical_and_no_contact_cannot_be_reversed(database):
    url, engine = database
    store = seed(url, engine)
    assert command(store,"pause_automation",0)==1
    assert command(store,"resume_automation",1)==2
    assert command(store,"pause_automation",0)==1, "replay acknowledges the old event, not current mode"
    with pytest.raises(Refused):command(store,"pause_automation",1)
    assert command(store,"request_takeover",2)==3
    with pytest.raises(Refused):command(store,"resume_automation",3)
    assert command(store,"release_takeover",3)==4
    assert command(store,"mark_no_contact",4)==5
    with pytest.raises(Refused):command(store,"request_takeover",5)
    with pytest.raises(Refused):command(store,"resume_automation",5)
    with Session(engine) as s:
        state=s.get(OfConversationState,(ACCOUNT,FAN))
        assert (state.mode,state.revision,state.takeover_state)==("no_contact",5,"none")
        audit=s.scalars(select(OfControlAudit).order_by(OfControlAudit.revision)).all()
        assert len(audit)==5
        assert (audit[2].takeover_from,audit[2].takeover_to)==("none","effective")
        assert (audit[3].takeover_from,audit[3].takeover_to)==("effective","none")
    store.close();store=Store(url)
    assert command(store,"mark_no_contact",4)==5
    store.close()


@pytest.mark.parametrize("settlement",["accepted","not_accepted"])
def test_takeover_waits_for_inflight_proof_and_completes_once(database,settlement):
    _,engine=database
    store,authority=configured(database)
    identity=prepare(authority)
    grant=authority.admit(TENANT,ACCOUNT,identity)
    assert command(store,"request_takeover",0)==1
    with Session(engine) as s:
        state=s.get(OfConversationState,(ACCOUNT,FAN))
        assert state.takeover_state=="requested" and state.unresolved_attempt_id==grant.attempt_id
    with pytest.raises(Refused):command(store,"release_takeover",1)
    if settlement=="accepted":
        authority.record_accepted(TENANT,ACCOUNT,identity,grant.token,accepted(grant))
        authority.materialize(TENANT,ACCOUNT,identity);authority.materialize(TENANT,ACCOUNT,identity)
    else:
        proof=rejected(grant)
        authority.record_not_accepted(TENANT,ACCOUNT,identity,grant.token,proof)
        authority.record_not_accepted(TENANT,ACCOUNT,identity,grant.token,proof)
    with Session(engine) as s:
        state=s.get(OfConversationState,(ACCOUNT,FAN))
        assert (state.mode,state.revision,state.takeover_state,state.unresolved_attempt_id)==("human_controlled",2,"effective",None)
        audits=s.scalars(select(OfControlAudit).order_by(OfControlAudit.revision)).all()
        assert [x.action for x in audits]==["request_takeover","complete_takeover"]
        assert audits[-1].actor=="delivery-authority"
    assert command(store,"release_takeover",2)==3
    store.close()


def test_later_no_contact_and_retained_unknown_evidence_prevent_takeover_completion(database):
    _,engine=database
    store,authority=configured(database)
    identity=prepare(authority);grant=authority.admit(TENANT,ACCOUNT,identity)
    command(store,"request_takeover",0)
    command(store,"mark_no_contact",1)
    authority.record_accepted(TENANT,ACCOUNT,identity,grant.token,accepted(grant))
    authority.materialize(TENANT,ACCOUNT,identity)
    with Session(engine) as s:
        state=s.get(OfConversationState,(ACCOUNT,FAN))
        assert state.mode=="no_contact" and state.revision==2
        assert s.scalar(select(func.count()).select_from(OfControlAudit))==2
    store.close()


def test_controls_preserve_unknown_receipts_caps_and_block_old_generation(database):
    _,engine=database
    store,authority=configured(database,mode="automated")
    store.record(TENANT,ACCOUNT,event());store.drain(TENANT,ACCOUNT);due(engine)
    claim=store.claim(TENANT)
    identity=prepare(authority,origin="automated",work=claim)
    command(store,"pause_automation",0)
    assert authority.admit(TENANT,ACCOUNT,identity)=="cancelled"
    with pytest.raises(Refused):store.complete(TENANT,claim)
    with Session(engine) as s,s.begin():
        # Historical unknown evidence must hold resumption even without a pointer.
        s.add(Message(account_id=ACCOUNT,fan_id=FAN,message_id=777,direction="out",body="Synthetic old message",created_at=utcnow()))
        s.flush();s.add(OfMessageDelivery(account_id=ACCOUNT,fan_id=FAN,message_id=777,outcome="unknown",attempt_id="old-unknown"))
    with pytest.raises(Refused):command(store,"resume_automation",1)
    assert command(store,"request_takeover",1)==2
    with Session(engine) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).takeover_state=="requested"
        assert s.get(OfDeliveryPolicy,ACCOUNT).limit_units==20
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==0
        assert s.get(OfWork,(ACCOUNT,FAN)).status=="blocked"
        assert s.get(OfMessageDelivery,(ACCOUNT,FAN,777)).outcome=="unknown"
    store.close()


def _race_control(url, action, key, queue):
    store=Store(url)
    try:
        try: queue.put(("ok",command(store,action,0,key)))
        except Refused: queue.put(("refused",None))
    finally:store.close()


@pytest.mark.parametrize("same_key",[True,False])
def test_two_process_controls_serialize_and_replay_without_duplicate_evidence(database,same_key):
    url,engine=database
    store=seed(url,engine);store.close()
    context=multiprocessing.get_context("spawn");queue=context.Queue()
    workers=[context.Process(target=_race_control,args=(url,"pause_automation" if same_key or i==0 else "mark_no_contact",
        "same" if same_key else f"key-{i}",queue)) for i in range(2)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(20);assert worker.exitcode==0
    replies=[queue.get(timeout=2) for _ in workers]
    assert sorted(x[0] for x in replies)==(["ok","ok"] if same_key else ["ok","refused"])
    with Session(engine) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).revision==1
        assert s.scalar(select(func.count()).select_from(OfControlAudit))==1


def test_populated_media_upgrade_retains_control_evidence_and_append_only_enforcement(database):
    _,engine=database
    # Rehearse the actual published revision. Removing a few columns from HEAD
    # leaves later campaign/media tables behind and is not a 0077 database.
    from db.schema import BASELINE, _config
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.exec_driver_sql((Path(__file__).resolve().parents[1]/"db/snapshots/0070_postgresql.sql").read_text())
        alembic_command.stamp(_config(c), BASELINE)
        alembic_command.upgrade(_config(c), MEDIA)
        c.execute(Account.__table__.insert(), {"id":"acc-a", "nickname":"Synthetic"})
        c.execute(OfCreatorBinding.__table__.insert(), {"id":"binding-acc-a", "tenant_id":"tenant-a", "creator_id":"creator-a", "creator_name":"Synthetic", "account_id":"acc-a", "handle":"synthetic", "channel":"onlyfans"})
        c.execute(Fan.__table__.insert(), {"account_id":"acc-a", "fan_id":101})
        c.execute(text("INSERT INTO of_runtime(account_id,binding_id,activated_at) VALUES ('acc-a','binding-acc-a',CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO of_control_audit(account_id,command_key,fan_id,mode,previous_revision,revision,actor,reason,created_at) VALUES ('acc-a','old-control',101,'paused',0,1,'synthetic','Retained control',CURRENT_TIMESTAMP)"))
        check_schema(c,expected=MEDIA)
        before=[dict(x) for x in c.execute(text("SELECT * FROM of_control_audit")).mappings()]
        upgrade(c);upgrade(c);check_schema(c)
        after=[dict(x) for x in c.execute(text("SELECT * FROM of_control_audit")).mappings()]
        for row in after:
            for column in ("action","takeover_from","takeover_to"):assert row.pop(column) is None
        assert after==before
    for sql in ("UPDATE of_control_audit SET reason='mutated'", "DELETE FROM of_control_audit"):
        with pytest.raises(DBAPIError,match="append-only"):
            with engine.begin() as c:c.exec_driver_sql(sql)


@pytest.mark.parametrize("change",["drop","disable","weaken"])
def test_startup_checks_control_immutability(database,change):
    _,engine=database
    with engine.begin() as c:
        if change=="drop":c.exec_driver_sql("DROP TRIGGER of_control_immutable ON of_control_audit")
        elif change=="disable":c.exec_driver_sql("ALTER TABLE of_control_audit DISABLE TRIGGER of_control_immutable")
        else:c.exec_driver_sql("CREATE OR REPLACE FUNCTION of_control_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$")
        with pytest.raises(SchemaMismatch,match="append-only protection"):check_schema(c)


def test_http_credential_contract_scope_and_strict_body(database,monkeypatch):
    store=seed(*database);other=seed(*database,account="acc-b",tenant="tenant-b")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    monkeypatch.setenv("ONLYFANS_API_TOKEN","synthetic-read")
    monkeypatch.setenv("ONLYFANS_CONTROL_TOKEN","synthetic-control")
    monkeypatch.setenv("ONLYFANS_CONTROL_TENANT",TENANT)
    app=FastAPI();app.include_router(router);app.state.control_store=store
    from onlyfans_read.projections import capabilities
    from onlyfans_read.repo import BindingRow
    binding=BindingRow("binding-acc-a",TENANT,"creator-a","Synthetic Creator",ACCOUNT,"synthetic","onlyfans")
    assert capabilities(binding)["pauseAutomation"]=="available"
    from dataclasses import replace
    assert capabilities(replace(binding,tenant_id="tenant-b"))["pauseAutomation"]=="not_configured"
    assert capabilities(binding)["sendManualMessage"]=="not_configured"
    assert capabilities(binding)["resolveDeliveryEvidence"]=="not_configured"
    client=TestClient(app)
    path=f"/onlyfans/control/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}"
    headers={"Authorization":"Bearer synthetic-control",HEADER:SCHEMA}
    body={"action":"pause_automation","expectedRevision":0,"commandKey":"a"*64,"actor":"op-session:"+"b"*64}
    for token in (None,"synthetic-read","wrong"):
        auth={} if token is None else {"Authorization":"Bearer "+token}
        assert client.post(path,headers={HEADER:SCHEMA,**auth},json=body).status_code==401
    assert client.post(path,headers={"Cookie":"allie_admin_session=anything",HEADER:SCHEMA},json=body).status_code==401
    assert client.post(path,headers={"Authorization":"Bearer synthetic-control"},json=body).status_code==400
    invalid=[{**body,"actor":"named-staff"},{**body,"expectedRevision":True},{**body,"expectedRevision":-1},
        {**body,"expectedRevision":2**31-1},{**body,"action":"send_manual_message"},{**body,"action":[]},
        {**body,"commandKey":"unhashed"},{**body,"consent_ref":"override"},{**body,"mode":"automated"},[],None]
    for bad in invalid:assert client.post(path,headers=headers,json=bad).status_code==400
    duplicate=json.dumps(body)[:-1]+',"expectedRevision":0}'
    assert client.post(path,headers={**headers,"Content-Type":"application/json"},content=duplicate).status_code==400
    assert client.post(path,headers={**headers,"Content-Type":"application/json"},content=json.dumps(body)+" "*4097).status_code==413
    assert client.post(path.replace("acc-a","acc-b"),headers=headers,json=body).status_code==404
    result=client.post(path,headers=headers,json=body)
    assert result.status_code==200,result.text
    assert result.json()["recordedRevision"]==1
    assert result.headers["cache-control"]=="private, no-store"
    assert client.post(path,headers=headers,json=body).json()==result.json()
    assert client.post(path,headers=headers,json={**body,"actor":"op-session:"+"c"*64}).status_code==409
    with Session(database[1]) as s,s.begin():s.get(OfCreatorBinding,"binding-acc-a").revoked_at=utcnow()
    assert client.post(path,headers=headers,json=body).status_code==404, "revoked binding cannot replay private evidence"
    monkeypatch.setenv("ONLYFANS_CONTROL_TENANT","deployment")
    assert client.post(path,headers=headers,json=body).status_code==503
    assert capabilities(binding)["pauseAutomation"]=="not_configured"
    monkeypatch.setenv("ONLYFANS_CONTROL_TENANT",TENANT);monkeypatch.setenv("ONLYFANS_CONTROL_TOKEN","synthetic-read")
    assert client.post(path,headers=headers,json=body).status_code==503
    monkeypatch.delenv("ONLYFANS_CONTROL_TOKEN")
    assert client.post(path,headers=headers,json=body).status_code==503
    store.close();other.close()
