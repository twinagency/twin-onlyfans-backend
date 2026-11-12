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
OPERATOR='op-session:'+'a'*64


def admin_submit(store,payload,proxy,key='9'*64,creator='creator-a'):
    return module.submit_enrollment(store,TENANT,creator_id=creator,creator_name='Verified admin creator',
        handle='synthetic',payload=payload,command_key=key,actor=OPERATOR,initial_proxy_url=proxy)


def test_admin_first_connection_seals_proxy_and_worker_proves_same_network(database,monkeypatch):
    from onlyfans_runtime.connections import capture_network_fingerprint
    settings(monkeypatch);store=Store(database[0]);p=session_payload();proxy=data()['proxyUrl']
    p['networkFingerprint']=capture_network_fingerprint(proxy)
    first=admin_submit(store,p,proxy);assert counts(database[1])==(0,0)
    with Session(database[1]) as s:
        row=s.get(OfConnectionEnrollment,first['id'])
        assert proxy not in row.sealed_config and module._clear(row)['initialProxyUrl']==proxy
    assert 'proxy' not in json.dumps(first).lower()
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    assert module.process_enrollments(store,TENANT)==1
    result=module.get_enrollments(store,TENANT,'creator-a')[0]
    assert result['status']=='accepted' and counts(database[1])==(1,1)
    with Session(database[1]) as s:
        clear=_open(s.get(OfProviderConnection,result['accountId']),TENANT)
        assert clear['proxyUrl']==proxy and clear['model'] is None
        assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    assert get_connection(store,TENANT,result['accountId'])['outgoingEnabled'] is False
    store.close()


def test_admin_proxy_capture_refuses_wrong_network_and_creator_proxy_override(database,monkeypatch):
    from onlyfans_runtime.connections import capture_network_fingerprint
    settings(monkeypatch);store=Store(database[0]);p=session_payload();proxy=data()['proxyUrl']
    p['networkFingerprint']=capture_network_fingerprint(None)
    with pytest.raises(Refused):admin_submit(store,p,proxy)
    p['networkFingerprint']=capture_network_fingerprint(proxy)
    for actor in [CREATOR,'agency-user:forged','op-session:forged']:
        with pytest.raises(Refused):module.submit_enrollment(store,TENANT,creator_id='creator-a',creator_name='Synthetic',
            handle='synthetic',payload=p,command_key='9'*64,actor=actor,initial_proxy_url=proxy)
    for bad in ['ftp://proxy.example:21','http://proxy.example:8080/private','http://proxy.example:8080?secret=bad']:
        with pytest.raises(Refused):admin_submit(store,{**p,'networkFingerprint':capture_network_fingerprint(bad)},bad)
    assert counts(database[1])==(0,0)
    store.close()


def test_admin_first_proxy_is_frozen_after_crash_without_duplicate_account(database,monkeypatch):
    from onlyfans_runtime.connections import capture_network_fingerprint
    settings(monkeypatch);store=Store(database[0]);p=session_payload();proxy=data()['proxyUrl']
    p['networkFingerprint']=capture_network_fingerprint(proxy)
    first=admin_submit(store,p,proxy)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    real=module.save_connection
    def crash(*args,**kw):real(*args,**kw);raise SystemExit('fixture worker crash')
    monkeypatch.setattr(module,'save_connection',crash)
    with pytest.raises(SystemExit):module.process_enrollments(store,TENANT)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfConnectionEnrollment,first['id']);assert row.expected_revision==0
        row.lease_until=utcnow()-timedelta(seconds=1)
    monkeypatch.setattr(module,'save_connection',real)
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted'
    assert counts(database[1])==(1,1)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfConnectionChange))==2
        assert _open(s.get(OfProviderConnection,'of-'+str(OWNER)),TENANT)['proxyUrl']==proxy
    store.close()


def test_admin_browser_capture_keeps_initial_proxy_across_normalization_replay(database,monkeypatch):
    from test_onlyfans_browser_capture import payload,assets
    from onlyfans_runtime.connections import capture_network_fingerprint
    settings(monkeypatch);store=Store(database[0]);calls=assets(monkeypatch);p=payload();proxy=data()['proxyUrl']
    p['networkFingerprint']=capture_network_fingerprint(proxy)
    first=admin_submit(store,p,proxy)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    real=module.save_connection
    def crash(*args,**kw):real(*args,**kw);raise SystemExit('fixture worker crash')
    monkeypatch.setattr(module,'save_connection',crash)
    with pytest.raises(SystemExit):module.process_enrollments(store,TENANT)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfConnectionEnrollment,first['id']);frozen=module._clear(row)
        assert 'browser' not in frozen and frozen['initialProxyUrl']==proxy and frozen['networkFingerprint']==p['networkFingerprint']
        row.lease_until=utcnow()-timedelta(seconds=1)
    before=len(calls);monkeypatch.setattr(module,'save_connection',real)
    assert module.process_enrollments(store,TENANT)==1 and len(calls)==before
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted'
    store.close()


def test_admin_enrollment_cannot_replace_existing_network_or_foreign_binding(database,monkeypatch):
    from onlyfans_runtime.connections import capture_network_fingerprint
    store,account,original=verified(database,monkeypatch,model={'modelId':MODEL,'apiKey':'synthetic-model-key'})
    before=get_connection(store,TENANT,account);proxy='http://different:fixture@proxy.example:8080'
    p=session_payload();p['networkFingerprint']=capture_network_fingerprint(proxy)
    admin_submit(store,p,proxy,creator='creator-new')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('Changed proxy reached provider'))
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,'creator-new')[0]['status']=='failed'
    assert get_connection(store,TENANT,account)==before
    with Session(database[1]) as s:assert _open(s.get(OfProviderConnection,account),TENANT)==original
    admin_submit(store,{**p,'networkFingerprint':capture_network_fingerprint(original['proxyUrl'])},original['proxyUrl'],key='8'*64,creator='foreign')
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,'foreign')[0]['status']=='failed'
    assert get_connection(store,TENANT,account)==before
    store.close()

def test_browser_enrollment_normalizes_in_worker_and_freezes_before_replay(database,monkeypatch):
    from test_onlyfans_browser_capture import payload,assets
    settings(monkeypatch);store=Store(database[0]);calls=assets(monkeypatch);p=payload()
    from onlyfans_runtime.connections import capture_network_fingerprint
    p['networkFingerprint']=capture_network_fingerprint(None)
    first=submit(store,p);assert first['status']=='pending' and calls==[] and counts(database[1])==(0,0)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    real=module.save_connection
    def crash(*args,**kw):real(*args,**kw);raise SystemExit('synthetic worker crash')
    monkeypatch.setattr(module,'save_connection',crash)
    with pytest.raises(SystemExit):module.process_enrollments(store,TENANT)
    assert len(calls)==2
    with Session(database[1]) as s,s.begin():
        row=s.get(OfConnectionEnrollment,first['id']);assert 'browser' not in module._clear(row)
        assert module._clear(row)['networkFingerprint']==p['networkFingerprint']
        row.lease_until=utcnow()-timedelta(seconds=1)
    monkeypatch.setattr(module,'save_connection',real)
    assert module.process_enrollments(store,TENANT)==1 and len(calls)==2
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted'
    assert get_connection(store,TENANT,'of-123')['outgoingEnabled'] is False
    assert counts(database[1])==(1,1)
    store.close()


def test_hosted_capture_network_is_scoped_and_never_public_status(database,monkeypatch):
    from onlyfans_runtime.connections import get_capture_context,capture_network_fingerprint
    store,account,original=verified(database,monkeypatch)
    context=get_capture_context(store,TENANT,'creator-new',account)
    assert context['proxyUrl']==original['proxyUrl'] and context['ownerId']==OWNER
    assert context['fingerprint']==capture_network_fingerprint(original['proxyUrl'])
    assert get_capture_context(store,TENANT,'creator-new')['proxyUrl'] is None
    with pytest.raises(Refused):get_capture_context(store,TENANT,'foreign-creator',account)
    with pytest.raises(Refused):get_capture_context(store,'foreign-tenant','creator-new',account)
    assert 'proxyUrl' not in get_connection(store,TENANT,account)
    before=get_connection(store,TENANT,account)['revision']
    with pytest.raises(Refused):save_connection(store,TENANT,account,payload=session_payload(),expected_revision=before,command_key='a'*64,actor=CREATOR,session_only=True,expected_network_fingerprint=capture_network_fingerprint('http://different.invalid:8080'))
    assert get_connection(store,TENANT,account)['revision']==before
    store.close()


def test_hosted_network_change_refuses_before_assets_or_owner_probe(database,monkeypatch):
    from test_onlyfans_browser_capture import payload,assets
    settings(monkeypatch);store=Store(database[0]);calls=assets(monkeypatch);p=payload();p['networkFingerprint']='f'*64
    first=submit(store,p)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('Changed network reached provider'))
    assert module.process_enrollments(store,TENANT)==1 and calls==[] and counts(database[1])==(0,0)
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='failed'
    with Session(database[1]) as s:assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    store.close()

def test_bad_browser_proof_never_provisions_and_clears_pending_secrets(database,monkeypatch):
    from test_onlyfans_browser_capture import payload,assets
    settings(monkeypatch);store=Store(database[0]);assets(monkeypatch);p=payload();p['browser']['staticParam']='wrong-static'
    first=submit(store,p)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('Bad capture reached provider identity'))
    assert module.process_enrollments(store,TENANT)==1 and counts(database[1])==(0,0)
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='failed'
    with Session(database[1]) as s:assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    store.close()
def session_payload(owner=OWNER):return {key:value for key,value in data(owner=owner).items() if key in {'ownerId','capture','xHash','expiresAt'}}
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
    login_path='/onlyfans/connection/v1/capture-context'
    assert client.post(login_path,json={'creatorId':'creator-a','accountId':None}).status_code==401
    context=client.post(login_path,headers=headers,json={'creatorId':'creator-a','accountId':None})
    assert context.status_code==200 and context.json()['context']['proxyUrl'] is None
    assert client.post(login_path,headers=headers,json={'creatorId':'creator-a','accountId':None,'proxyUrl':'http://forged.invalid:8080'}).status_code==400
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


def test_temporary_first_enrollment_retries_durably_without_blocking_other_creators(database,monkeypatch):
    from onlyfans_runtime.provider_connector import NativeProtocolError
    settings(monkeypatch);store=Store(database[0]);first=submit(store)
    calls=[]
    def unavailable(self):
        calls.append(self.account_id)
        raise NativeProtocolError("synthetic maintenance response")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",unavailable)
    assert module.process_enrollments(store,TENANT)==1
    with Session(database[1]) as s:
        row=s.get(OfConnectionEnrollment,first["id"])
        assert row.status=="pending" and row.sealed_config is not None
        frozen=(row.sealed_config,row.expires_at)
        assert row.last_error=="provider_connection_check_retry" and row.lease_until>utcnow()
    assert module.process_enrollments(store,TENANT)==0 and calls==["of-"+str(OWNER)]
    assert counts(database[1])==(0,0)
    other=session_payload(OWNER+1)
    submit(store,other,key="2"*64,creator="creator-b")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",lambda self:calls.append(self.account_id))
    assert module.process_enrollments(store,TENANT)==1
    assert module.get_enrollments(store,TENANT,"creator-b")[0]["status"]=="accepted"
    assert counts(database[1])==(1,1)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfConnectionEnrollment,first["id"])
        assert (row.sealed_config,row.expires_at)==frozen
        row.lease_until=utcnow()-timedelta(seconds=1)
    # A new Store represents a worker restart; retry state is not process-local.
    store.close();store=Store(database[0])
    assert module.process_enrollments(store,TENANT)==1
    result=module.get_enrollments(store,TENANT,"creator-a")[0]
    assert result["status"]=="accepted" and result["accountId"]=="of-"+str(OWNER)
    assert counts(database[1])==(2,2)
    with Session(database[1]) as s:
        row=s.get(OfConnectionEnrollment,first["id"])
        assert row.sealed_config is None and row.last_error is None
        assert not s.get(OfRuntime,result["accountId"]).enabled
    store.close()


@pytest.mark.parametrize("outcome",["authentication","expired","stale"])
def test_enrollment_retry_keeps_auth_expiry_and_generation_fences(database,monkeypatch,outcome):
    from onlyfans_runtime.provider_connector import NativeProtocolError,NativeAuthenticationError
    settings(monkeypatch);store=Store(database[0]);first=submit(store)
    def failure(self):
        if outcome in {"expired","stale"}:
            with Session(database[1]) as s,s.begin():
                row=s.get(OfConnectionEnrollment,first["id"])
                if outcome=="expired":row.expires_at=utcnow()-timedelta(seconds=1)
                else:row.generation+=1
        if outcome=="authentication":raise NativeAuthenticationError("synthetic rejected identity")
        raise NativeProtocolError("synthetic temporary failure")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",failure)
    module.process_enrollments(store,TENANT)
    assert counts(database[1])==(0,0)
    with Session(database[1]) as s:
        row=s.get(OfConnectionEnrollment,first["id"])
        assert row.status=={"authentication":"failed","expired":"expired","stale":"checking"}[outcome]
        assert (row.sealed_config is None)==(outcome!="stale")
    store.close()
