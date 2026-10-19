"""Creator connection proof uses the existing worker, with synthetic sessions only."""
import copy,json,threading
from datetime import timedelta
from sqlalchemy import select,func
from sqlalchemy.orm import Session
import pytest
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings,data,verified,change,LIMITS,ACTOR
from test_onlyfans_provider_adapters import OWNER,MODEL
from test_onlyfans_text_delivery_pg import TENANT
from db.models import Account,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfConnectionEnrollment,OfProviderConnection,OfConnectionChange
from onlyfans_runtime.store import Store,Refused,utcnow
from onlyfans_runtime.connections import save_connection,get_connection,_open,load_worker_accounts
from onlyfans_runtime import enrollments as module
from onlyfans_runtime.provider_connector import NativeProviderConnector

CREATOR='creator-session:'+'c'*64
def session_payload():return {key:value for key,value in data().items() if key in {'ownerId','capture','xHash','expiresAt'}}
def submit(store,payload=None,key='1'*64,creator='creator-a',actor=CREATOR):
    return module.submit_enrollment(store,TENANT,creator_id=creator,creator_name='Synthetic creator',handle='synthetic',payload=payload or session_payload(),command_key=key,actor=actor)
def counts(engine):
    with Session(engine) as s:return s.scalar(select(func.count()).select_from(Account)),s.scalar(select(func.count()).select_from(OfCreatorBinding))

def test_unverified_owner_never_reserves_an_account_and_failure_has_no_secret(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);payload=session_payload();first=submit(store,payload)
    assert first['status']=='pending' and first['accountId'] is None and counts(database[1])==(0,0)
    assert submit(store,payload)['id']==first['id']
    with Session(database[1]) as s:
        row=s.get(OfConnectionEnrollment,first['id']);assert row.sealed_config and 'synthetic-hash' not in row.sealed_config
    def wrong(self):raise RuntimeError('synthetic-hash MUST NEVER be returned')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',wrong)
    assert module.process_enrollments(store,TENANT)==1
    result=module.get_enrollments(store,TENANT,'creator-a')[0]
    assert result['status']=='failed' and result['accountId'] is None and counts(database[1])==(0,0)
    assert 'synthetic-hash' not in json.dumps(result) and 'capture' not in json.dumps(result)
    with Session(database[1]) as s:assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    store.close()

def test_valid_worker_proof_attaches_exact_creator_without_enabling_messages(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store)
    calls=[];monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:calls.append(self.account_id))
    assert module.process_enrollments(store,TENANT)==1 and calls==['of-'+str(OWNER)]
    result=module.get_enrollments(store,TENANT,'creator-a')[0]
    assert result['status']=='accepted' and result['accountId']=='of-'+str(OWNER)
    assert module.get_enrollments(store,'foreign-tenant','creator-a')==[]
    assert module.get_enrollments(store,TENANT,'foreign-creator')==[]
    with Session(database[1]) as s:
        binding=s.scalar(select(OfCreatorBinding));assert binding.creator_id=='creator-a' and binding.tenant_id==TENANT
        assert s.get(OfRuntime,result['accountId']).enabled is False
        assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    assert get_connection(store,TENANT,result['accountId'])['outgoingEnabled'] is False
    assert load_worker_accounts(store,TENANT)==[]
    assert get_connection(store,TENANT,result['accountId'])['status']=='verified'
    store.close()

def test_session_refresh_keeps_private_model_and_proxy_and_cannot_set_them(database,monkeypatch):
    store,account,original=verified(database,monkeypatch,model={'modelId':MODEL,'apiKey':'synthetic-model-key'})
    assert change(store,account,'activate',1,'c'*64,LIMITS)==2
    payload=session_payload()
    assert save_connection(store,TENANT,account,payload=payload,expected_revision=2,command_key='d'*64,actor=CREATOR,session_only=True)==3
    assert save_connection(store,TENANT,account,payload=payload,expected_revision=2,command_key='d'*64,actor=CREATOR,session_only=True)==3
    with Session(database[1]) as s:
        clear=_open(s.get(OfProviderConnection,account),TENANT)
        assert clear['proxyUrl']==original['proxyUrl'] and clear['model']==original['model']
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is False
    for extra in [{'model':None},{'proxyUrl':None}]:
        with pytest.raises(Refused):save_connection(store,TENANT,account,payload={**payload,**extra},expected_revision=3,command_key='e'*64,actor=CREATOR,session_only=True)
    wrong=copy.deepcopy(payload);wrong['ownerId']+=1
    with pytest.raises(Refused):save_connection(store,TENANT,account,payload=wrong,expected_revision=3,command_key='e'*64,actor=CREATOR,session_only=True)
    store.close()

def test_crash_after_durable_save_replays_without_duplicate_registration(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    real=module.save_connection
    def crash(*args,**kw):real(*args,**kw);raise SystemExit('synthetic worker crash')
    monkeypatch.setattr(module,'save_connection',crash)
    with pytest.raises(SystemExit):module.process_enrollments(store,TENANT)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfConnectionEnrollment,first['id']);assert row.status=='checking' and row.expected_revision==0
        row.lease_until=utcnow()-timedelta(seconds=1)
    monkeypatch.setattr(module,'save_connection',real)
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted'
    assert counts(database[1])==(1,1)
    with Session(database[1]) as s:assert s.scalar(select(func.count()).select_from(OfConnectionChange))==2
    store.close()

def test_expired_cancelled_and_stale_claims_cannot_attach(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store)
    with Session(database[1]) as s,s.begin():s.get(OfConnectionEnrollment,first['id']).expires_at=utcnow()-timedelta(seconds=1)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('Expired request reached provider'))
    assert module.process_enrollments(store,TENANT)==1 and counts(database[1])==(0,0)
    second=submit(store,key='2'*64)
    def stale(self):
        with Session(database[1]) as s,s.begin():s.get(OfConnectionEnrollment,second['id']).generation+=1
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',stale)
    assert module.process_enrollments(store,TENANT)==0 and counts(database[1])==(0,0)
    store.close()

def test_failed_request_does_not_block_another_creator_with_valid_proof(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);submit(store)
    def wrong(self):raise Refused('wrong owner')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',wrong);module.process_enrollments(store,TENANT)
    submit(store,key='2'*64,creator='creator-b',actor='creator-session:'+'b'*64)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None);module.process_enrollments(store,TENANT)
    assert module.get_enrollments(store,TENANT,'creator-b')[0]['status']=='accepted'
    with Session(database[1]) as s:assert s.scalar(select(OfCreatorBinding)).creator_id=='creator-b'
    store.close()

def test_enrollment_is_worker_only_and_pending_submissions_are_bounded(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);payload=session_payload()
    for n in range(6):submit(store,payload,key=str(n+1)*64)
    with pytest.raises(Refused):submit(store,payload,key='7'*64)
    with pytest.raises(Refused):submit(store,payload,key='1'*64,creator='creator-foreign')
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    with pytest.raises(Refused):module.process_enrollments(store,TENANT)
    assert counts(database[1])==(0,0)
    store.close()

def test_existing_account_refuses_foreign_creator_and_reconnect_keeps_operator_settings(database,monkeypatch):
    store,account,original=verified(database,monkeypatch,model={'modelId':MODEL,'apiKey':'synthetic-model-key'})
    denied=submit(store,creator='foreign-creator')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('Foreign account reached provider'))
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,'foreign-creator')[0]['status']=='failed'
    assert get_connection(store,TENANT,account)['revision']==1
    own=submit(store,key='2'*64,creator='creator-new')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,'creator-new')[0]['status']=='accepted'
    with Session(database[1]) as s:
        clear=_open(s.get(OfProviderConnection,account),TENANT)
        assert clear['proxyUrl']==original['proxyUrl'] and clear['model']==original['model']
    assert get_connection(store,TENANT,account)['revision']==2 and counts(database[1])==(1,1)
    store.close()


def test_enrollment_http_uses_separate_auth_strict_json_and_never_calls_provider(database,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from onlyfans_commands.connection_api import router,SCHEMA,HEADER
    settings(monkeypatch);store=Store(database[0])
    for key,value in {'ONLYFANS_PROCESS_ROLE':'api','ONLYFANS_CONNECTION_TOKEN':'synthetic-connect','ONLYFANS_CONNECTION_TENANT':TENANT,
        'ONLYFANS_API_TOKEN':'synthetic-read','ONLYFANS_CONTROL_TOKEN':'synthetic-control'}.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('API never verifies provider'))
    app=FastAPI();app.include_router(router);app.state.control_store=store;client=TestClient(app)
    path='/onlyfans/connection/v1/enrollments';headers={'Authorization':'Bearer synthetic-connect',HEADER:SCHEMA}
    body={'creatorId':'creator-a','creatorName':'Synthetic creator','handle':'synthetic','session':session_payload(),'actor':CREATOR,'commandKey':'1'*64}
    for token in ('wrong','synthetic-read','synthetic-control'):
        assert client.post(path,headers={HEADER:SCHEMA,'Authorization':'Bearer '+token},json=body).status_code==401
    assert client.post(path,headers={'Authorization':'Bearer synthetic-connect'},json=body).status_code==400
    assert client.post(path,headers=headers,json={**body,'proxyUrl':None}).status_code==400
    duplicate=json.dumps(body)[:-1]+',"creatorId":"foreign"}'
    assert client.post(path,headers={**headers,'Content-Type':'application/json'},content=duplicate).status_code==400
    ack=client.post(path,headers=headers,json=body);assert ack.status_code==200,ack.text
    assert client.post(path,headers=headers,json=body).json()==ack.json()
    rows=client.get(path+'?creator=creator-a',headers=headers);assert rows.status_code==200 and len(rows.json()['enrollments'])==1
    assert client.get(path+'?creator=foreign',headers=headers).json()['enrollments']==[]
    assert 'synthetic-session' not in ack.text+rows.text and counts(database[1])==(0,0)
    store.close()
