"""Bulk UI controls use the canonical transactional stop, with synthetic fans."""
import json
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed
from db.models import Fan, OfConversationState, SkipList
from db.onlyfans_models import OfControlAudit
from onlyfans_runtime.bulk_controls import apply_batch, validate_items
from onlyfans_runtime.store import Refused
from onlyfans_commands.api import router
from onlyfans_commands.settings import HEADER, SCHEMA
from onlyfans_read.agency_scope import AgencyScope, current_scope

ACTOR='op-session:'+'a'*64
ACCOUNT='acc-a'
def items(ids, revision=0):
    return [{'conversationId':f'{ACCOUNT}:{fan}', 'expectedRevision':revision} for fan in ids]
def apply(store, action, rows, key='b'*64):
    return apply_batch(store,'tenant-a',ACCOUNT,action=action,items=rows,command_key=key,actor=ACTOR)
def add_fans(engine, rows):
    with Session(engine) as s,s.begin():
        for fan, mode, revision, takeover, attention, muted in rows:
            s.add(Fan(account_id=ACCOUNT,fan_id=fan,is_muted=muted));s.flush()
            s.add(OfConversationState(account_id=ACCOUNT,fan_id=fan,mode=mode,revision=revision,takeover_state=takeover,requires_attention=attention))


def test_fifty_round_trip_replay_and_runtime_stop(database):
    store=seed(*database);engine=database[1]
    ids=list(range(201,251))
    add_fans(engine,[(fan,'automated',6,'none',False,False) for fan in ids])
    started=time.monotonic()
    paused=apply(store,'pause_automation',items(ids,6))
    assert all(r['recordedRevision']==7 for r in paused)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(SkipList))==50
        assert s.scalar(select(func.count()).select_from(OfControlAudit))==50
    resumed=apply(store,'resume_automation',items(ids,7),'c'*64)
    assert all(r['recordedRevision']==8 for r in resumed)
    assert apply(store,'pause_automation',items(ids,6))==paused
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(SkipList))==0
        assert s.scalar(select(func.count()).select_from(OfControlAudit))==100
        assert s.get(OfConversationState,(ACCOUNT,201)).mode=='automated'
        # New fan never included in the explicit snapshot remains off.
        s.add(Fan(account_id=ACCOUNT,fan_id=999));s.commit()
    assert not store.admit_new_conversation('tenant-a',ACCOUNT,999)
    print('100 canonical controls seconds:',round(time.monotonic()-started,3))
    store.close()


def test_races_and_protected_fans_skip_without_aborting_following_fans(database):
    store=seed(*database);engine=database[1]
    add_fans(engine,[(201,'paused',4,'none',False,False),(202,'paused',3,'effective',False,False),
        (203,'no_contact',3,'none',False,False),(204,'paused',3,'none',True,False),
        (205,'paused',3,'none',False,True),(206,'paused',3,'none',False,False),
        (207,'paused',3,'none',False,False),(208,'paused',3,'none',False,False)])
    with Session(engine) as s,s.begin():
        s.add(SkipList(account_id=ACCOUNT,fan_id=206,reason='of_restricted'))
        s.get(OfConversationState,(ACCOUNT,207)).unresolved_attempt_id='synthetic-unresolved'
    result=apply(store,'resume_automation',items(range(201,209),3))
    assert [r.get('reason',r['outcome']) for r in result]==['revision_changed','human_control','contact_blocked','human_review','contact_blocked','contact_blocked','delivery_unresolved','recorded']
    assert apply(store,'resume_automation',items([888],0))[0]['reason']=='conversation_unavailable'
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfControlAudit))==1
        assert s.get(SkipList,(ACCOUNT,206)).reason=='of_restricted'
        assert s.get(OfConversationState,(ACCOUNT,207)).unresolved_attempt_id=='synthetic-unresolved'
    store.close()


def test_http_strict_validation_scope_and_auth(database,monkeypatch):
    store=seed(*database)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api');monkeypatch.setenv('ONLYFANS_API_TOKEN','synthetic-read')
    monkeypatch.setenv('ONLYFANS_CONTROL_TOKEN','synthetic-control');monkeypatch.setenv('ONLYFANS_CONTROL_TENANT','tenant-a')
    app=FastAPI();app.include_router(router);app.state.control_store=store;client=TestClient(app)
    path=f'/onlyfans/control/v1/accounts/{ACCOUNT}/bulk-conversations'
    headers={'Authorization':'Bearer synthetic-control',HEADER:SCHEMA}
    body={'action':'pause_automation','items':items([101]),'commandKey':'b'*64,'actor':ACTOR}
    assert client.post(path,json=body).status_code==401
    assert client.post(path,json=body,headers={HEADER:SCHEMA,'Authorization':'Bearer synthetic-read'}).status_code==401
    for bad in [{**body,'action':[]},{**body,'actor':'invalid'},{**body,'items':[]},{**body,'items':items(range(1,52))},
        {**body,'items':items([101,101])},{**body,'items':[{'conversationId':'other:101','expectedRevision':0}]},
        {**body,'items':[{'conversationId':'acc-a:101','expectedRevision':True}]},{**body,'override':True}]:
        assert client.post(path,json=bad,headers=headers).status_code==400
    assert client.post(path,headers={**headers,'Content-Type':'application/json'},content=json.dumps(body)[:-1]+',"actor":"duplicate"}').status_code==400
    assert client.post(path,headers={**headers,'Content-Type':'application/json'},content=' '*65537).status_code==413
    with Session(database[1]) as s:assert s.scalar(select(func.count()).select_from(OfControlAudit))==0
    scope=current_scope.set(AgencyScope(('creator-acc-a',),ACTOR,False,True))
    try:
        with pytest.raises(Refused,match='management'):apply(store,'pause_automation',items([101]))
    finally:current_scope.reset(scope)
    scope=current_scope.set(AgencyScope(('foreign-creator',),ACTOR,True,True))
    try:
        with pytest.raises(Refused):apply(store,'pause_automation',items([101]))
    finally:current_scope.reset(scope)
    result=client.post(path,json=body,headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['results'][0]['recordedRevision']==1
    assert client.post(path,json=body,headers=headers).json()==result.json()
    assert result.headers['cache-control']=='private, no-store'
    store.close()
