"""Hosted identity, lease renewal and read-only intake on disposable PostgreSQL."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings as native_settings,change
from test_onlyfans_creator_enrollments_pg import submit,counts
from test_onlyfans_provider_adapters import OWNER,fake_curl
from test_onlyfans_text_delivery_pg import TENANT
from db.onlyfans_connection_models import OfConnectionEnrollment,OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime
from onlyfans_runtime import enrollments
from onlyfans_runtime.connections import load_worker_accounts,get_connection,get_hosted_account,_open,_seal
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.hosted_provider import HostedUnavailable
from onlyfans_runtime.store import Store,Refused,utcnow


def settings(monkeypatch):
    native_settings(monkeypatch);monkeypatch.setenv('ONLYFANS_HOSTED_CONNECT_ENABLED','1');monkeypatch.setenv('OFAPI_API_KEY','synthetic-hosted-key-private')


def payload():return {'ownerId':OWNER,'hosted':{'schemaVersion':'ofapi.v1','accountId':'acct_fixture'},'expiresAt':(datetime.now(timezone.utc)+timedelta(hours=23)).isoformat()}


def accepted(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store,payload())
    fake_curl(monkeypatch,body=('{"data":{"id":%d}}'%OWNER).encode());assert enrollments.process_enrollments(store,TENANT)==1
    assert enrollments.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted'
    return store,'of-'+str(OWNER),first


def test_enrollment_verifies_owner_then_syncs_history_with_sending_disabled(database,monkeypatch):
    store,account,first=accepted(database,monkeypatch)
    assert counts(database[1])==(1,1) and get_connection(store,TENANT,account)['outgoingEnabled'] is False
    cohort=load_worker_accounts(store,TENANT);assert len(cohort)==1 and cohort[0].account_id==account
    status=get_connection(store,TENANT,account);assert status['status']=='verified' and status['outgoingEnabled'] is False
    assert get_hosted_account(store,TENANT,account)=={'accountId':account,'providerAccountId':'acct_fixture','ownerId':str(OWNER)}
    with pytest.raises(Refused):get_hosted_account(store,'foreign-tenant',account)
    with Session(database[1]) as s:
        assert s.get(OfRuntime,account).enabled is True and s.get(OfDeliveryPolicy,account).enabled is False
        assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    store.close()


def test_wrong_owner_never_creates_canonical_account(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);submit(store,payload());fake_curl(monkeypatch,body=b'{"data":{"id":999}}')
    assert enrollments.process_enrollments(store,TENANT)==1 and counts(database[1])==(0,0)
    assert enrollments.get_enrollments(store,TENANT,'creator-a')[0]['status']=='failed';store.close()


def test_transient_identity_get_retries_only_within_bounded_claim(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store,payload());calls=fake_curl(monkeypatch,status=503)
    assert enrollments.process_enrollments(store,TENANT)==1 and len(calls)==2 and counts(database[1])==(0,0)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfConnectionEnrollment,first['id']);assert row.status=='checking' and row.sealed_config
        row.lease_until=utcnow()-timedelta(seconds=1)
    fake_curl(monkeypatch,body=('{"data":{"id":%d}}'%OWNER).encode());assert enrollments.process_enrollments(store,TENANT)==1
    assert enrollments.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted';store.close()


def test_retry_budget_clears_secrets_without_binding(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store,payload());fake_curl(monkeypatch,status=503)
    with Session(database[1]) as s,s.begin():s.get(OfConnectionEnrollment,first['id']).generation=59
    enrollments.process_enrollments(store,TENANT)
    with Session(database[1]) as s:
        row=s.get(OfConnectionEnrollment,first['id']);assert row.status=='failed' and row.sealed_config is None
    assert counts(database[1])==(0,0);store.close()


def test_health_lease_renews_without_new_login_or_revision_and_pause_stays_paused(database,monkeypatch):
    store,account,_=accepted(database,monkeypatch);load_worker_accounts(store,TENANT)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfProviderConnection,account);clear=_open(row,TENANT);old_revision=row.revision
        expiry=utcnow()-timedelta(seconds=5);clear['expiresAt']=expiry.isoformat()+'Z';row.expires_at=expiry
        row.sealed_config=_seal(clear,TENANT,account,row.binding_id,row.revision)
    assert len(load_worker_accounts(store,TENANT))==1
    with Session(database[1]) as s:
        row=s.get(OfProviderConnection,account);assert row.revision==old_revision and row.expires_at>utcnow()+timedelta(hours=22)
        assert _open(row,TENANT)['hosted']['accountId']=='acct_fixture'
        assert s.get(OfDeliveryPolicy,account).enabled is False
    change(store,account,'pause',old_revision,'f'*64)
    assert load_worker_accounts(store,TENANT)==[] and get_connection(store,TENANT,account)['status']=='paused';store.close()


def test_concurrent_pause_during_health_get_cannot_reenable_account(database,monkeypatch):
    store,account,_=accepted(database,monkeypatch);load_worker_accounts(store,TENANT)
    with Session(database[1]) as s,s.begin():s.get(OfProviderConnection,account).verified_at=utcnow()-timedelta(minutes=6)
    def pause(self):
        revision=get_connection(store,TENANT,account)['revision'];change(store,account,'pause',revision,'e'*64)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',pause)
    assert load_worker_accounts(store,TENANT)==[]
    with Session(database[1]) as s:assert s.get(OfRuntime,account).enabled is False
    assert get_connection(store,TENANT,account)['status']=='paused';store.close()


def test_hosted_worker_imports_history_without_model_or_outgoing_policy(database,monkeypatch):
    import json,sys
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from sqlalchemy import select,func
    from db.models import Message
    from onlyfans_native_protocol_fixture import native_message
    from onlyfans_runtime.native_worker import NativeWorker
    store,account,_=accepted(database,monkeypatch)
    cohort=load_worker_accounts(store,TENANT);records=[]
    class CurlSession:
        def __init__(self,**options):pass
        def __enter__(self):return self
        def __exit__(self,*_):return False
        def request(self,method,url,**options):
            records.append((method,url));assert method=='GET'
            path=urlsplit(url).path
            if path.endswith('/me'):body={'data':{'id':OWNER}}
            elif path.endswith('/chats'):body={'data':[{'withUser':{'id':101}}],'_pagination':{'next_page':None}}
            else:
                assert path.endswith('/chats/101/messages')
                body={'data':[native_message(9001,owner=OWNER)],'_pagination':{'next_page':None}}
            options['content_callback'](json.dumps(body).encode())
            return SimpleNamespace(status_code=200,headers={'content-type':'application/json'})
    monkeypatch.setitem(sys.modules,'curl_cffi',SimpleNamespace(requests=SimpleNamespace(Session=CurlSession)))
    worker=NativeWorker(store,TENANT,tuple(cohort))
    for _ in range(5):assert worker.step().accepted==0
    with Session(database[1]) as s:
        assert s.get(Message,(account,101,9001)) is not None
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=='out'))==0
        assert s.get(OfDeliveryPolicy,account).enabled is False
    assert records and all(method=='GET' for method,_ in records);store.close()


def test_hosted_send_keeps_one_use_authority_and_stable_intent_key(database,monkeypatch):
    import json,sys
    from types import SimpleNamespace
    from onlyfans_native_protocol_fixture import native_message
    from onlyfans_runtime.text_delivery import TextAuthority
    from test_onlyfans_text_delivery_pg import current
    from db.models import Fan,OfConversationState
    store,account,_=accepted(database,monkeypatch);load_worker_accounts(store,TENANT)
    change(store,account,'activate',1,'d'*64,{'limitUnits':3,'throttleSeconds':1,'mediaEnabled':False})
    cohort=load_worker_accounts(store,TENANT);wire=cohort[0].transport
    with Session(database[1]) as s,s.begin():
        s.add(Fan(account_id=account,fan_id=101));s.flush()
        s.add(OfConversationState(account_id=account,fan_id=101,mode='human_controlled',revision=0,takeover_state='none',requires_attention=False))
    current(database[1],account=account)
    authority=TextAuthority(store)
    identity=authority.prepare(TENANT,account,101,command_key='hosted-manual',expected_revision=0,body='Synthetic reply',origin='manual',actor='synthetic-operator')
    dispatch=authority.admit(TENANT,account,identity);records=[]
    class CurlSession:
        def __init__(self,**options):pass
        def __enter__(self):return self
        def __exit__(self,*_):return False
        def request(self,method,url,**options):
            records.append((method,url,options))
            body={'data':{'id':OWNER}} if method=='GET' else {'data':native_message(9999,owner=OWNER,outgoing=True,text='Synthetic reply')}
            options['content_callback'](json.dumps(body).encode())
            return SimpleNamespace(status_code=200,headers={'content-type':'application/json'})
    monkeypatch.setitem(sys.modules,'curl_cffi',SimpleNamespace(requests=SimpleNamespace(Session=CurlSession)))
    proof=wire.send(dispatch,authority=authority,tenant_id=TENANT);assert proof.message_id==9999
    posts=[options for method,_,options in records if method=='POST'];assert len(posts)==1 and posts[0]['headers']['Idempotency-Key']=='twin:'+identity
    with pytest.raises(Refused):wire.send(dispatch,authority=authority,tenant_id=TENANT)
    assert sum(method=='POST' for method,_,_ in records)==1;store.close()
