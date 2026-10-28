import json,hashlib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import configured
from onlyfans_commands.api import router
from onlyfans_commands.settings import HEADER,SCHEMA
from db.onlyfans_models import OfIngress,OfRuntime
from onlyfans_runtime.store import Event,utcnow

def client_for(store,monkeypatch):
    for key,value in {'ONLYFANS_PROCESS_ROLE':'api','ONLYFANS_CONTROL_TOKEN':'synthetic-control','ONLYFANS_CONTROL_TENANT':'tenant-a','ONLYFANS_API_TOKEN':'synthetic-read','ONLYFANS_MANUAL_ENABLED':'1'}.items():monkeypatch.setenv(key,value)
    app=FastAPI();app.state.control_store=store;app.include_router(router)
    return TestClient(app),{'Authorization':'Bearer synthetic-control',HEADER:SCHEMA}

def test_actual_manual_refusal_carries_reason_and_revision_without_creating_intent(database,monkeypatch):
    store,_=configured(database)
    with Session(store.engine) as s,s.begin():
        payload=Event(101,501,'2026-10-28T00:00:00Z','in','Conflict').payload()
        s.add(OfIngress(account_id='acc-a',binding_id=s.get(OfRuntime,'acc-a').binding_id,event_key=hashlib.sha256(payload.encode()).hexdigest(),fan_id=101,message_id=501,payload=payload,source='import',status='quarantined',reason='conflicting_message_identity',received_at=utcnow()))
    client,headers=client_for(store,monkeypatch)
    response=client.post('/onlyfans/control/v1/accounts/acc-a/conversations/acc-a:101',headers=headers,json={'action':'manual_message','expectedRevision':0,'commandKey':'a'*64,'actor':'op-session:'+'b'*64,'body':'Synthetic manual reply'})
    assert response.status_code==409
    body=response.json();assert body['code']=='history_repair_required'
    assert body['expectedRevision']==body['currentRevision']==0
    assert body['holds']==['history_repair_required']
    assert 'conflicting_message_identity' not in response.text
    store.close()

def test_resolution_http_requires_configuration_and_exact_positive_evidence_fields(database,monkeypatch):
    store,_=configured(database);client,headers=client_for(store,monkeypatch)
    body={'action':'resolve_delivery_evidence','expectedRevision':0,'commandKey':'a'*64,'actor':'op-session:'+'b'*64,'attemptId':'fastt-pilot-1','providerMessageId':9001,'decision':'delivered'}
    path='/onlyfans/control/v1/accounts/acc-a/conversations/acc-a:101'
    assert client.post(path,headers=headers,json=body).status_code==503
    for changed in ({'decision':'not_delivered'},{'providerMessageId':True},{'providerMessageId':0},{'attemptId':'../../other'}):
        assert client.post(path,headers=headers,json={**body,**changed}).status_code==400
    from onlyfans_runtime import delivery_resolution
    called=[]
    async def verified(*args,**kwargs):called.append(kwargs);return {'recorded':True}
    monkeypatch.setattr(delivery_resolution,'operational',lambda:True)
    from onlyfans_runtime import delivery_resolution_jobs
    monkeypatch.setattr(delivery_resolution_jobs,'available_for',lambda *args:True)
    monkeypatch.setattr(delivery_resolution,'resolve',verified)
    response=client.post(path,headers=headers,json=body)
    assert response.status_code==200 and response.json()['recordedRevision']==0
    assert called[0]['attempt']=='fastt-pilot-1' and called[0]['provider_message_id']==9001
    assert client.post(path,headers={HEADER:SCHEMA},json=body).status_code==401
    store.close()
