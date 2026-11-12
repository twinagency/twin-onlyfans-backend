"""Encrypted connection lifecycle against owned PG and explicit mocked HTTPS.

No real session, account, model key, external endpoint or production DB is used.
"""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func,select,text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,seed
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT
from test_onlyfans_provider_adapters import OWNER,MODEL,capture,expires
from db.models import Account,AccountAiConfig,Fan,GrokCall,Message,OfCreatorBinding,OfConversationState
from db.onlyfans_models import OfRuntime,OfScan,OfRecovery,OfIngress
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfTextIntent
from db.onlyfans_connection_models import OfProviderConnection,OfConnectionChange
from db.schema import SchemaMismatch,check_schema
from onlyfans_runtime import connections as module
from onlyfans_runtime.connections import provision_account,save_connection,change_connection,get_connection,load_worker_accounts,load_worker_cohort,_open
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.native_worker import NativeWorker
from onlyfans_runtime.configuration import get_configuration,update_configuration
from onlyfans_runtime.store import Store,Refused,utcnow
from onlyfans_commands.connection_api import router,SCHEMA,HEADER

ACTOR='op-session:'+'a'*64


def settings(monkeypatch):
    monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','42'*32)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')


def provision(store,**overrides):
    args=dict(creator_id='creator-new',creator_name='Synthetic Creator',owner_id=OWNER,handle='synthetic',command_key='a'*64,actor=ACTOR)
    args.update(overrides)
    return provision_account(store,TENANT,**args)


def data(*,model=None,owner=OWNER):
    c=capture();c['headers']['user_id']=str(owner);c['cookies'].append({'name':'auth_id','value':str(owner),'domain':'.onlyfans.com','path':'/'})
    return {'ownerId':owner,'capture':c,'xHash':'synthetic-hash','proxyUrl':'http://synthetic:proxy-secret@proxy.example:8080',
        'expiresAt':expires().isoformat(),'model':model}


def save(store,account,payload,revision=0,key='b'*64):
    return save_connection(store,TENANT,account,payload=payload,expected_revision=revision,command_key=key,actor=ACTOR)


def change(store,account,action,revision,key,payload=None):
    return change_connection(store,TENANT,account,action=action,payload={} if payload is None else payload,
        expected_revision=revision,command_key=key,actor=ACTOR)


LIMITS={'limitUnits':7,'throttleSeconds':11,'mediaEnabled':False}


def verified(database,monkeypatch,*,model=None):
    settings(monkeypatch);store=Store(database[0]);account=provision(store);payload=data(model=model)
    assert save(store,account,payload)==1
    calls=[]
    def verify(self):calls.append(self.account_id)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',verify)
    assert load_worker_accounts(store,TENANT)==[]
    assert calls==[account] and get_connection(store,TENANT,account)['status']=='verified'
    return store,account,payload


def test_provision_is_disabled_exact_audited_and_native_settings_writer_is_ready(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    assert provision(store)==account=='of-'+str(OWNER)
    for change_args in ({'creator_name':'Changed name'},{'handle':'changed'},{'actor':'op-session:'+'f'*64},{'creator_id':'other-creator'}):
        with pytest.raises(Refused):provision(store,**change_args)
    initial=get_configuration(store,TENANT,account)
    assert initial['persona']=='' and initial['defaultModel'] is None and initial['resolvedReplyModel'] is None
    with Session(database[1]) as session:
        runtime=session.get(OfRuntime,account);config=session.get(AccountAiConfig,account)
        assert runtime.enabled is False and runtime.recovery_needed is True
        assert config.persona is config.model is None and config.daily_cost_cap_cents==100
        assert session.get(OfDeliveryPolicy,account) is None
        assert session.scalar(select(func.count()).select_from(OfConnectionChange))==1
    revision,_=update_configuration(store,TENANT,account,patch={'persona':'Synthetic creator enjoys hiking.','defaultModel':MODEL},
        expected_revision=0,expected_fingerprint=initial['fingerprint'],command_key='c'*64,actor=ACTOR)
    assert revision==1 and get_configuration(store,TENANT,account)['resolvedReplyModel']==MODEL
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is False
    store.close()


def test_existing_legacy_account_is_never_adopted_and_other_tenant_cannot_reprovision(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0])
    with Session(database[1]) as session,session.begin():session.add(Account(id='of-'+str(OWNER),nickname='Retained legacy'))
    with pytest.raises(Refused):provision(store)
    with Session(database[1]) as session:
        assert session.get(AccountAiConfig,'of-'+str(OWNER)) is None
        assert session.get(OfRuntime,'of-'+str(OWNER)) is None
        assert session.scalar(select(Account.nickname))=='Retained legacy'
    account=provision(store,owner_id=OWNER+1)
    with pytest.raises(Refused):provision_account(store,'other-tenant',creator_id='creator-new',creator_name='Synthetic Creator',
        owner_id=OWNER+1,handle='synthetic',command_key='a'*64,actor=ACTOR)
    assert get_connection(store,TENANT,account)['status']=='unconfigured';store.close()


def test_encrypted_capture_and_model_never_appear_in_sql_audit_or_read_projection(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    payload=data(model={'modelId':MODEL,'apiKey':'synthetic-model-secret'})
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('API save must not make provider calls'))
    assert save(store,account,payload)==1 and save(store,account,payload)==1
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account)
        assert _open(row,TENANT)==payload
        record={column.name:getattr(row,column.name) for column in row.__table__.columns}
        audits=[{column.name:getattr(audit,column.name) for column in audit.__table__.columns} for audit in session.scalars(select(OfConnectionChange))]
        text_value=json.dumps([record,audits,get_connection(store,TENANT,account)],default=str)
        for secret in ['synthetic-session','synthetic-model-secret','proxy-secret','synthetic-hash','synthetic-static']:
            assert secret not in text_value
        assert session.get(AccountAiConfig,account).model is None
    assert get_connection(store,TENANT,account)['status']=='pending'
    with pytest.raises(Refused):change(store,account,'activate',1,'c'*64,LIMITS)
    store.close()


@pytest.mark.parametrize('change_kind',['ciphertext','revision','binding','account','tenant','owner','expiry','key'])
def test_authenticated_encryption_rejects_tampering_and_scope_transplants(database,monkeypatch,change_kind):
    settings(monkeypatch);store=Store(database[0]);account=provision(store);save(store,account,data())
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account)
        if change_kind=='ciphertext':row.sealed_config=row.sealed_config[:-4]+'AAAA'
        if change_kind=='revision':row.revision+=1
        if change_kind=='binding':row.binding_id='different-binding'
        if change_kind=='account':row.account_id='different-account'
        if change_kind=='owner':row.owner_id+=1
        if change_kind=='expiry':row.expires_at+=timedelta(minutes=1)
        if change_kind=='key':monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','43'*32)
        with pytest.raises(Refused,match='credentials are unavailable'):_open(row,'foreign-tenant' if change_kind=='tenant' else TENANT)
    store.close()


def test_pending_verification_is_get_only_then_activation_pause_reverify_reencrypt_every_revision(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is False
    assert change(store,account,'activate',1,'c'*64,LIMITS)==2
    accounts=load_worker_accounts(store,TENANT);assert len(accounts)==1 and accounts[0].model is None
    assert accounts[0].reader is accounts[0].transport
    with Session(database[1]) as session:
        assert _open(session.get(OfProviderConnection,account),TENANT)==payload
    assert change(store,account,'pause',2,'d'*64)==3
    assert load_worker_accounts(store,TENANT)==[]
    assert change(store,account,'verify',3,'e'*64)==4
    assert get_connection(store,TENANT,account)['verifiedAt'] is None
    assert load_worker_accounts(store,TENANT)==[]
    assert change(store,account,'activate',4,'f'*64,LIMITS)==5
    assert change(store,account,'pause',2,'d'*64)==3 # original historical acknowledgement
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account)
        assert row.revision==5 and _open(row,TENANT)==payload
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is True;store.close()


def test_expiry_and_failed_identity_never_activate_or_expose_provider_error(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store);payload=data();save(store,account,payload)
    def fail(self):raise RuntimeError('secret-session secret-proxy-url')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',fail)
    assert load_worker_accounts(store,TENANT)==[]
    public=get_connection(store,TENANT,account)
    assert public['status']=='failed' and public['lastError']=='provider_verification_failed'
    assert 'secret' not in json.dumps(public) and public['outgoingEnabled'] is False
    with Session(database[1]) as session,session.begin():session.get(OfProviderConnection,account).expires_at=utcnow()-timedelta(seconds=1)
    assert load_worker_accounts(store,TENANT)==[]
    assert get_connection(store,TENANT,account)['status']=='failed'
    with pytest.raises(Refused):change(store,account,'verify',1,'c'*64)
    with pytest.raises(Refused):change(store,account,'activate',1,'c'*64,LIMITS)
    monkeypatch.setattr(module,'session_from_capture',lambda **kw:(_ for _ in ()).throw(Refused('synthetic expired capture')))
    assert save(store,account,payload)==1 # historical success survives new validation refusal
    store.close()


def test_read_health_refuses_corrupt_ciphertext_or_wrong_key_without_mutation_or_secret_leak(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    change(store,account,'activate',1,'c'*64,LIMITS)
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is True
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account);original=row.sealed_config
        original_audits=session.scalar(select(func.count()).select_from(OfConnectionChange))
    monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','43'*32)
    public=get_connection(store,TENANT,account)
    assert public['status']=='failed' and public['lastError']=='stored_credentials_unavailable' and public['outgoingEnabled'] is False
    assert 'synthetic-session' not in json.dumps(public) and 'proxy-secret' not in json.dumps(public)
    monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','42'*32)
    with Session(database[1]) as session,session.begin():session.get(OfProviderConnection,account).sealed_config='corrupt'
    public=get_connection(store,TENANT,account)
    assert public['status']=='failed' and public['outgoingEnabled'] is False
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account)
        assert row.status=='verified' and row.revision==2
        assert session.scalar(select(func.count()).select_from(OfConnectionChange))==original_audits
        assert session.get(OfRuntime,account).enabled is True
    with Session(database[1]) as session,session.begin():session.get(OfProviderConnection,account).sealed_config=original
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is True
    store.close()


def test_stale_scope_revocation_binding_and_mutated_retry_refuse_without_rewriting(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    for bad in (dict(payload,xHash='changed-hash'),dict(payload,model={'modelId':MODEL,'apiKey':'changed'})):
        with pytest.raises(Refused,match='idempotency'):save(store,account,bad)
    with pytest.raises(Refused,match='revision'):save(store,account,payload,0,'c'*64)
    with pytest.raises(Refused,match='scope'):get_connection(store,'foreign-tenant',account)
    with Session(database[1]) as session,session.begin():
        runtime=session.get(OfRuntime,account)
        session.get(OfCreatorBinding,runtime.binding_id).revoked_at=utcnow()
    assert load_worker_accounts(store,TENANT)==[]
    with pytest.raises(Refused,match='scope'):save(store,account,payload)
    with pytest.raises(Refused,match='scope'):get_connection(store,TENANT,account)
    store.close()


def test_same_provider_owner_cannot_be_armed_under_another_canonical_account(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store);save(store,account,data())
    other=seed(*database,account='other-canonical',tenant='foreign-tenant')
    with pytest.raises(Refused,match='another account'):
        save_connection(store,'foreign-tenant','other-canonical',payload=data(),expected_revision=0,command_key='b'*64,actor=ACTOR)
    with Session(database[1]) as session:
        assert session.get(OfProviderConnection,'other-canonical') is None
        assert session.get(OfRuntime,'other-canonical').enabled is True # existing scope unchanged
        assert session.scalar(select(func.count()).select_from(OfProviderConnection))==1
    store.close();other.close()


def test_first_save_refuses_capture_of_another_owner(database,monkeypatch):
    # W5BB-1 at the sink: with no existing row, another login's capture never lands under this account.
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    forged=data();forged['capture']['cookies'][-1]['value']=str(OWNER+1)
    for payload in (data(owner=OWNER+1),forged):
        with pytest.raises(Refused,match='invalid connection configuration'):save(store,account,payload)
    with Session(database[1]) as session:assert session.get(OfProviderConnection,account) is None
    save(store,account,data());store.close()


def test_concurrent_registration_and_owner_claims_serialize_into_one_record(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(lambda _:provision(store,owner_id=OWNER+1),range(2)))
    assert values==['of-'+str(OWNER+1)]*2
    first=seed(*database,account='writer-a',tenant='tenant-a')
    second=seed(*database,account='writer-b',tenant='tenant-b');payload=data()
    def claim(args):
        tenant,account=args
        try:
            save_connection(store,tenant,account,payload=payload,expected_revision=0,command_key='b'*64,actor=ACTOR)
            return 'ok'
        except Refused:return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(claim,[('tenant-a','writer-a'),('tenant-b','writer-b')]))
    assert sorted(outcomes)==['ok','refused']
    with Session(database[1]) as session:
        assert session.scalar(select(func.count()).select_from(OfProviderConnection))==1
        assert session.scalar(select(func.count()).select_from(OfConnectionChange).where(OfConnectionChange.action=='provision'))==1
        assert session.scalar(select(func.count()).select_from(AccountAiConfig))==1
    store.close();first.close();second.close()


def test_activation_and_replacement_preserve_used_allowance_unknown_holds_and_throttle(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    change(store,account,'activate',1,'c'*64,LIMITS)
    future=utcnow()+timedelta(minutes=10)
    with Session(database[1]) as session,session.begin():
        policy=session.get(OfDeliveryPolicy,account);policy.used_units=3;policy.next_dispatch_at=future
        session.add(Fan(account_id=account,fan_id=101));session.flush()
        session.add(OfConversationState(account_id=account,fan_id=101,mode='no_contact',revision=7,
            unresolved_attempt_id='retain-unknown',takeover_state='none',requires_attention=True))
    change(store,account,'pause',2,'d'*64)
    change(store,account,'verify',3,'e'*64);load_worker_accounts(store,TENANT)
    with pytest.raises(Refused,match='allowance'):change(store,account,'activate',4,'f'*64,dict(LIMITS,limitUnits=2))
    change(store,account,'activate',4,'f'*64,dict(LIMITS,throttleSeconds=1200))
    with Session(database[1]) as session:
        policy=session.get(OfDeliveryPolicy,account);state=session.get(OfConversationState,(account,101))
        assert policy.used_units==3 and policy.limit_units==7 and policy.next_dispatch_at>=future
        assert policy.throttle_seconds==1200 and (state.mode,state.revision,state.unresolved_attempt_id)==('no_contact',7,'retain-unknown')
    with Session(database[1]) as session,session.begin():session.get(OfDeliveryPolicy,account).active_intent_id='retain-unknown'
    change(store,account,'pause',5,'1'*64)
    with pytest.raises(Refused,match='quiescent'):save(store,account,payload,6,'2'*64)
    change(store,account,'verify',6,'3'*64);load_worker_accounts(store,TENANT)
    with pytest.raises(Refused,match='unresolved'):change(store,account,'activate',7,'4'*64,LIMITS)
    with Session(database[1]) as session:
        policy=session.get(OfDeliveryPolicy,account)
        assert policy.active_intent_id=='retain-unknown' and policy.used_units==3 and policy.next_dispatch_at>=future
    store.close()


def test_new_conversation_autonomy_is_off_by_default_and_activation_never_arms_it(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    assert get_connection(store,TENANT,account)['automateNewConversations'] is False
    change(store,account,'activate',1,'c'*64,LIMITS)
    with Session(database[1]) as session,session.begin():
        row=session.get(OfProviderConnection,account)
        assert row.automate_new_conversations is False and row.automation_starts_at is None
        for fan,mode in [(101,'paused'),(102,'no_contact'),(103,'human_controlled')]:
            session.add(Fan(account_id=account,fan_id=fan));session.flush()
            session.add(OfConversationState(account_id=account,fan_id=fan,mode=mode,revision=7,
                unresolved_attempt_id='retain-unknown-'+str(fan),takeover_state='none',requires_attention=True))
    # Activation never arms future conversations (that is its own explicit administrator setting), so asking for
    # it here is refused with a clear message and nothing changes: no revision bump, no flag, no state touched.
    with pytest.raises(Refused,match='future conversations setting'):
        change(store,account,'activate',2,'d'*64,dict(LIMITS,automateNewConversations=True))
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account)
        assert row.revision==2 and row.automate_new_conversations is False and row.automation_starts_at is None
        for fan,mode in [(101,'paused'),(102,'no_contact'),(103,'human_controlled')]:
            state=session.get(OfConversationState,(account,fan))
            assert (state.mode,state.revision,state.unresolved_attempt_id)==(mode,7,'retain-unknown-'+str(fan))
    assert get_connection(store,TENANT,account)['automateNewConversations'] is False
    # An explicit false is still accepted, and a stale armed row is reported truthfully, then reset to off by activation.
    with Session(database[1]) as session,session.begin():
        row=session.get(OfProviderConnection,account);row.automate_new_conversations=True;row.automation_starts_at=utcnow()
    assert get_connection(store,TENANT,account)['automateNewConversations'] is True
    change(store,account,'activate',2,'e'*64,dict(LIMITS,automateNewConversations=False))
    with Session(database[1]) as session:
        row=session.get(OfProviderConnection,account)
        assert row.automate_new_conversations is False and row.automation_starts_at is None
    store.close()


def test_loader_has_explicit_worker_role_and_hard_account_bound(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0])
    for value in (0,33,True,1.5,'1'):
        with pytest.raises(Refused,match='bound'):load_worker_accounts(store,TENANT,limit=value)
    for cursor in (True,'../other',{'account':'a'}):
        with pytest.raises(Refused,match='cursor'):load_worker_cohort(store,TENANT,after_account=cursor)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    with pytest.raises(Refused,match='worker role'):load_worker_accounts(store,TENANT)
    store.close()


def test_worker_cohorts_advance_over_refused_and_pending_accounts_and_wrap_without_starvation(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);accounts=[];calls=[]
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:calls.append(self.account_id))
    for index in range(3):
        owner=OWNER+index;account=provision(store,owner_id=owner,command_key=f'{index+1:064x}')
        payload=data(owner=owner)
        save(store,account,payload);accounts.append(account)
    with Session(database[1]) as session,session.begin():
        session.get(OfProviderConnection,accounts[0]).sealed_config='tampered'
    first,cursor,count=load_worker_cohort(store,TENANT,limit=1)
    assert first==[] and cursor==accounts[0] and count==1 and calls==[]
    second,cursor,count=load_worker_cohort(store,TENANT,limit=1,after_account=cursor)
    assert second==[] and cursor==accounts[1] and count==1 and calls==[accounts[1]]
    third,cursor,count=load_worker_cohort(store,TENANT,limit=1,after_account=cursor)
    assert third==[] and cursor is None and count==1 and calls==accounts[1:]
    change(store,accounts[2],'activate',1,'f'*64,LIMITS)
    cohort,cursor,count=load_worker_cohort(store,TENANT,limit=1,after_account=accounts[1])
    assert [item.account_id for item in cohort]==accounts[2:] and count==1 and cursor is None
    wrapped,cursor,count=load_worker_cohort(store,TENANT,limit=1,after_account=accounts[2])
    assert wrapped==[] and count==1 and cursor==accounts[0]
    assert load_worker_cohort(store,'foreign-tenant',limit=1)==([],None,0)
    assert calls==accounts[1:]
    store.close()


def test_worker_cohort_stop_finishes_one_verification_and_retains_unvisited_cursor(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);accounts=[];calls=[];stop=threading.Event()
    for index in range(2):
        owner=OWNER+index;account=provision(store,owner_id=owner,command_key=f'{index+1:064x}')
        payload=data(owner=owner)
        save(store,account,payload);accounts.append(account)
    def verify(self):calls.append(self.account_id);stop.set()
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',verify)
    assert load_worker_cohort(store,TENANT,limit=2,stop=stop)==([],accounts[0],1)
    assert calls==accounts[:1]
    assert load_worker_cohort(store,TENANT,limit=2,after_account=accounts[0],stop=stop)==([],accounts[0],0)
    stop.clear()
    assert load_worker_cohort(store,TENANT,limit=2,after_account=accounts[0],stop=stop)==([],None,1)
    assert calls==accounts
    store.close()


def test_invalid_payloads_and_limits_never_write_credentials_or_activate(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    original=data()
    for patch in ({'ownerId':True},{'expiresAt':'invalid'},{'expiresAt':datetime.now().isoformat()},
        {'capture':{}},{'xHash':'bad\nheader'},{'model':{'modelId':'unknown-model','apiKey':'synthetic-key'}},
        {'model':{'modelId':MODEL,'apiKey':'bad\nkey'}},{'proxyUrl':'https://proxy.example:8080/private'}):
        with pytest.raises(Refused):save(store,account,dict(original,**patch))
    with Session(database[1]) as session:
        assert session.get(OfProviderConnection,account) is None
        assert session.scalar(select(func.count()).select_from(OfConnectionChange))==1
        assert session.get(OfRuntime,account).enabled is False
    save(store,account,original)
    for patch in ({'limitUnits':True},{'limitUnits':0},{'limitUnits':10001},{'throttleSeconds':0},
        {'throttleSeconds':86401},{'mediaEnabled':1},{'automateNewConversations':1}):
        with pytest.raises(Refused):change(store,account,'activate',1,'c'*64,dict(LIMITS,**patch))
    store.close()


@pytest.mark.parametrize('configuration',['ready','mismatched','unconfigured'])
def test_actual_native_worker_uses_decrypted_selected_credential_and_one_mocked_call(database,monkeypatch,configuration):
    from test_onlyfans_worker_pg import start
    from test_onlyfans_provider_pg import fake_requests
    settings(monkeypatch);store,_=start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    payload=data(model={'modelId':MODEL,'apiKey':'synthetic-model-secret'})
    save(store,ACCOUNT,payload);assert load_worker_accounts(store,TENANT)==[]
    change(store,ACCOUNT,'activate',1,'c'*64,LIMITS)
    # The existing fixture has canonical history already. Complete only the
    # owned synthetic scan so this test isolates credential->worker delivery.
    with Session(database[1]) as session,session.begin():
        scan=session.get(OfScan,ACCOUNT);scan.status='complete';scan.repeat_requested=False
        runtime=session.get(OfRuntime,ACCOUNT);runtime.recovery_needed=False;runtime.last_sync_at=utcnow()
        for recovery in session.scalars(select(OfRecovery).where(OfRecovery.account_id==ACCOUNT)):recovery.status='complete'
        config=session.get(AccountAiConfig,ACCOUNT)
        if configuration=='mismatched':config.model='deepseek-v4-flash-non-thinking'
        elif configuration=='unconfigured':config.model=None;config.persona=None
    accounts=load_worker_accounts(store,TENANT);assert len(accounts)==1 and accounts[0].model.model_id==MODEL
    worker=NativeWorker(store,TENANT,tuple(accounts));report=worker.step()
    assert report.accepted==int(configuration=='ready') and report.errors==0,report.public()
    worker.step();assert len(calls)==int(configuration=='ready') and sum(method=='POST' for method,_,_ in requests)==int(configuration=='ready')
    with Session(database[1]) as session:
        assert session.get(AccountAiConfig,ACCOUNT).model==({'ready':MODEL,'mismatched':'deepseek-v4-flash-non-thinking','unconfigured':None}[configuration])
        assert session.scalar(select(func.count()).select_from(GrokCall))==int(configuration=='ready')
        assert session.scalar(select(func.count()).select_from(Message).where(Message.direction=='out'))==int(configuration=='ready')
    store.close()


def test_connection_api_separate_credentials_strict_json_redacted_ack_and_revocation(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    for key,value in {'ONLYFANS_PROCESS_ROLE':'api','ONLYFANS_CONNECTION_TOKEN':'synthetic-connect','ONLYFANS_CONNECTION_TENANT':TENANT,
        'ONLYFANS_API_TOKEN':'synthetic-read','ONLYFANS_CONTROL_TOKEN':'synthetic-control'}.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('API never verifies provider'))
    app=FastAPI();app.include_router(router);app.state.control_store=store;client=TestClient(app)
    path='/onlyfans/connection/v1/accounts/'+account;headers={'Authorization':'Bearer synthetic-connect',HEADER:SCHEMA}
    body={'action':'save','payload':data(),'expectedRevision':0,'commandKey':'b'*64,'actor':ACTOR}
    for token in ('wrong','synthetic-read','synthetic-control'):
        assert client.get(path,headers={HEADER:SCHEMA,'Authorization':'Bearer '+token}).status_code==401
        assert client.post(path,headers={HEADER:SCHEMA,'Authorization':'Bearer '+token},json=body).status_code==401
    assert client.get(path,headers={'Authorization':'Bearer synthetic-connect'}).status_code==400
    for raw in (json.dumps(body)[:-1]+',"expectedRevision":0}',json.dumps(body).replace('"expectedRevision": 0','"expectedRevision": NaN')):
        assert client.post(path,headers={**headers,'Content-Type':'application/json'},content=raw).status_code==400
    ack=client.post(path,headers=headers,json=body);assert ack.status_code==200,ack.text
    assert client.post(path,headers=headers,json=body).json()==ack.json()
    result=client.get(path,headers=headers);assert result.status_code==200 and 'no-store' in result.headers['cache-control']
    assert 'sync' not in result.json()['connection']
    progress=client.get(path+'?progress=1',headers=headers)
    assert progress.status_code==200 and progress.json()['connection']['sync']['lastCompleteSyncAt'] is None
    assert client.get(path+'?progress=1',headers={HEADER:SCHEMA,'Authorization':'Bearer synthetic-read'}).status_code==401
    assert 'synthetic-session' not in ack.text+result.text+progress.text
    with Session(database[1]) as session,session.begin():
        runtime=session.get(OfRuntime,account);session.get(OfCreatorBinding,runtime.binding_id).revoked_at=utcnow()
    assert client.get(path,headers=headers).status_code==404
    assert client.post(path,headers=headers,json=body).status_code==404
    monkeypatch.setenv('ONLYFANS_CONNECTION_TOKEN','synthetic-read');assert client.get(path,headers=headers).status_code==503
    store.close()


def test_connection_audit_is_immutable_and_checked_at_startup(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);provision(store);store.close()
    for sql in ("UPDATE of_connection_change SET actor='forged'","DELETE FROM of_connection_change"):
        with pytest.raises(DBAPIError,match='append-only'):
            with database[1].begin() as connection:connection.exec_driver_sql(sql)
    with database[1].begin() as connection:
        connection.exec_driver_sql('ALTER TABLE of_connection_change DISABLE TRIGGER of_connection_immutable')
        with pytest.raises(SchemaMismatch,match='append-only protection'):check_schema(connection)


def test_opt_in_sync_progress_uses_canonical_work_without_mutation_or_false_completion(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store);save(store,account,data())
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('Progress reads must not call provider'))
    assert 'sync' not in get_connection(store,TENANT,account)
    initial=get_connection(store,TENANT,account,include_sync=True)['sync']
    assert initial['pages']==0 and initial['conversationsRemaining']==0 and initial['lastCompleteSyncAt'] is None
    with Session(database[1]) as s,s.begin():
        runtime=s.get(OfRuntime,account);scan=s.get(OfScan,account)
        scan.status='complete';scan.page_count=4;scan.known_fans_complete=True
        runtime.pending_events=1;runtime.recovery_needed=True
        s.add(OfRecovery(account_id=account,fan_id=101,status='complete'))
        s.add(OfRecovery(account_id=account,fan_id=102,status='needed'))
        s.add(OfIngress(account_id=account,event_key='f'*64,binding_id=runtime.binding_id,fan_id=102,
            message_id=1001,source='history',payload='{}',status='quarantined',reason='synthetic_conflict',received_at=utcnow()))
        audits=s.scalar(select(func.count()).select_from(OfConnectionChange))
    view=get_connection(store,TENANT,account,include_sync=True)
    assert view['sync']=={'discoveryStatus':'complete','pages':4,'conversationsComplete':1,'conversationsRemaining':1,
        'pendingMessages':1,'quarantinedMessages':1,'recoveryRequired':True,'lastCompleteSyncAt':None}
    for secret in ('synthetic-session','proxy-secret','synthetic-hash','payload','event_key'):
        assert secret not in json.dumps(view)
    with pytest.raises(Refused):get_connection(store,'foreign-tenant',account,include_sync=True)
    with Session(database[1]) as s:
        assert s.get(OfRuntime,account).pending_events==1
        assert s.scalar(select(func.count()).select_from(OfConnectionChange))==audits
        assert s.get(OfProviderConnection,account).status=='pending'
    store.close()


def test_elapsed_deadline_does_not_hide_paused_or_failed_connection(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    with Session(database[1]) as s,s.begin():
        row=s.get(OfProviderConnection,account);row.status='paused';row.expires_at=utcnow()-timedelta(seconds=1)
    assert get_connection(store,TENANT,account)['status']=='paused'
    assert get_connection(store,TENANT,account,include_sync=True)['outgoingEnabled'] is False
    with Session(database[1]) as s,s.begin():
        row=s.get(OfProviderConnection,account);row.status='failed';row.last_error='provider_authentication_required'
    public=get_connection(store,TENANT,account)
    assert public['status']=='failed' and public['lastError']=='provider_authentication_required'
    store.close()
