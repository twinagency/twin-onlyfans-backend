"""Credential readiness uses the real encrypted authority on disposable PG.

No provider/model request, production key, live account or fallback credential
is used. Readiness never changes verification, configuration or activation.
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings, provision, data, save
from test_onlyfans_provider_adapters import MODEL
from db.models import AccountAiConfig, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_read.agency_scope import AgencyScope, current_scope
from onlyfans_runtime import connections
from onlyfans_runtime.launch_personality.policy import readiness
from onlyfans_runtime.launch_personality.workflow import PersonalityWorkflow
from onlyfans_runtime.store import Refused, Store, utcnow

SECRET = 'synthetic-only-readiness-key'
OTHER_MODEL = 'deepseek-v4-flash'


@pytest.fixture
def account(database, monkeypatch):
    settings(monkeypatch)
    # A configured house key must never substitute for missing scoped data.
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-forbidden-house-key')
    monkeypatch.setenv('GROK_API_KEY', 'synthetic-forbidden-house-key')
    store=Store(database[0])
    account_id=provision(store)
    payload=data(model={'modelId': MODEL, 'apiKey': SECRET})
    save(store,account_id,payload)
    with Session(store.engine) as s,s.begin():
        cfg=s.get(AccountAiConfig,account_id);cfg.model=MODEL
        cfg.persona='Warm and concise.';cfg.daily_cost_cap_cents=500
        row=s.get(OfProviderConnection,account_id)
        row.status='verified';row.verified_at=utcnow()
        binding=s.get(OfRuntime,account_id).binding_id
    yield store,account_id,binding,payload
    store.close()


def read(account):
    store,account_id,binding,_=account
    with Session(store.engine) as s,s.begin():
        result=connections.personality_credential_status(s,account_id,binding)
        cfg=s.get(AccountAiConfig,account_id)
        config={c.name:getattr(cfg,c.name) for c in cfg.__table__.columns}
        return result,readiness(config,result)


def rewrite(account, change):
    store,account_id,binding,payload=account
    value={**payload,**change}
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderConnection,account_id)
        row.sealed_config=connections._seal(value,'tenant-a',account_id,binding,row.revision)


def test_verified_exact_model_is_ready_without_activating_or_leaking_secrets(account,monkeypatch):
    monkeypatch.setattr(connections.NativeProviderConnector,'verify_identity',
        lambda *a,**k:pytest.fail('readiness must not call a provider'))
    metadata,checks=read(account)
    assert set(metadata)=={'accountId','modelId','configured','expiresAt'}
    assert metadata['configured'] is True and metadata['modelId']==MODEL
    assert checks['credential']=={'state':'ready','configured':True}
    text=json.dumps([metadata,checks])
    for secret in (SECRET,'synthetic-forbidden-house-key','synthetic-session','proxy-secret','synthetic-hash'):
        assert secret not in text
    store,account_id,_,_=account
    with Session(store.engine) as s:
        assert not s.get(OfRuntime,account_id).enabled
        assert s.get(OfProviderConnection,account_id).status=='verified'


@pytest.mark.parametrize('status',['pending','paused','failed','expired'])
def test_nonverified_connection_is_not_ready(account,status):
    store,account_id,_,_=account
    with Session(store.engine) as s,s.begin():s.get(OfProviderConnection,account_id).status=status
    metadata,checks=read(account)
    assert metadata['configured'] is False and checks['credential']['state']=='missing'


@pytest.mark.parametrize('change',['expired','unverified','verification_retry','wrong_key','ciphertext'])
def test_unusable_connection_never_appears_ready(account,monkeypatch,change):
    store,account_id,_,_=account
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderConnection,account_id)
        if change=='expired':row.expires_at=utcnow()-timedelta(seconds=1)
        if change=='unverified':row.verified_at=None
        if change=='verification_retry':row.last_error='provider_connection_check_retry'
        if change=='ciphertext':row.sealed_config='synthetic-corrupt-secret-ciphertext'
    if change=='wrong_key':monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','43'*32)
    metadata,checks=read(account)
    assert not metadata['configured'] and not checks['ready']
    assert SECRET not in json.dumps([metadata,checks])


@pytest.mark.parametrize('model',[None,{}, {'modelId':MODEL,'apiKey':''},
    {'modelId':MODEL,'apiKey':'bad\nsecret'}, {'modelId':'unsupported-model','apiKey':SECRET},
    {'modelId':MODEL,'apiKey':SECRET,'extra':'refused'}, ['invalid']])
def test_malformed_or_missing_scoped_model_never_uses_house_key(account,model):
    rewrite(account,{'model':model})
    metadata,checks=read(account)
    assert not metadata['configured'] and checks['credential']['state']=='missing'
    assert 'synthetic-forbidden-house-key' not in json.dumps([metadata,checks])


def test_exact_saved_reply_model_override_must_match_encrypted_credential(account):
    store,account_id,_,_=account
    with Session(store.engine) as s,s.begin():
        s.get(AccountAiConfig,account_id).model_by_purpose=json.dumps({'ai_chatter':OTHER_MODEL})
    metadata,checks=read(account)
    assert metadata['modelId']==MODEL and metadata['configured'] and not checks['ready']
    assert checks['credential']['state']=='model_mismatch'
    rewrite(account,{'model':{'modelId':OTHER_MODEL,'apiKey':SECRET}})
    metadata,checks=read(account)
    assert metadata['configured'] and checks['model']['modelId']==OTHER_MODEL
    assert checks['credential']['state']=='ready'


@pytest.mark.parametrize('model,purpose',[(None,None),('unsupported-model',None),
    (MODEL,'{"ai_chatter":"deepseek-v4-flash","ai_chatter":"grok-4-1-fast-non-reasoning"}')])
def test_missing_invalid_or_ambiguous_saved_model_is_not_ready(account,model,purpose):
    store,account_id,_,_=account
    with Session(store.engine) as s,s.begin():
        cfg=s.get(AccountAiConfig,account_id);cfg.model=model;cfg.model_by_purpose=purpose
    metadata,checks=read(account)
    assert not metadata['configured'] and not checks['ready']


def test_current_binding_and_creator_scope_are_required(account):
    store,account_id,binding,_=account
    token=current_scope.set(AgencyScope(('different-creator',),'op-session:'+'a'*64,False))
    try:
        with pytest.raises(Refused,match='scope'):read(account)
    finally:current_scope.reset(token)
    with Session(store.engine) as s,s.begin():
        with pytest.raises(Refused,match='scope'):
            connections.personality_credential_status(s,'different-account',binding)
    with Session(store.engine) as s,s.begin():
        old=s.get(OfCreatorBinding,binding);old.revoked_at=utcnow();s.flush()
        replacement=OfCreatorBinding(id='replacement-binding',account_id=account_id,
            tenant_id=old.tenant_id,creator_id='replacement-creator',channel='onlyfans')
        s.add(replacement);s.flush();s.get(OfRuntime,account_id).binding_id=replacement.id
    with pytest.raises(Refused,match='scope'):read(account)
    with Session(store.engine) as s,s.begin():
        result=connections.personality_credential_status(s,account_id,'replacement-binding')
        assert not result['configured'], 'previous creator credentials cannot follow a rebind'


def test_cross_tenant_binding_cannot_decrypt_transplanted_connection(account):
    store,account_id,binding,_=account
    with Session(store.engine) as s,s.begin():
        s.get(OfCreatorBinding,binding).tenant_id='different-tenant'
    metadata,checks=read(account)
    assert not metadata['configured'] and not checks['ready']


def test_no_connection_never_uses_environment_credential(account):
    store,account_id,_,_=account
    with Session(store.engine) as s,s.begin():
        s.delete(s.get(OfProviderConnection,account_id))
    metadata,checks=read(account)
    assert metadata['configured'] is False and checks['credential']['state']=='missing'


def test_real_personality_document_flow_consumes_authoritative_metadata(account):
    store,account_id,binding,_=account
    with Session(store.engine) as s:
        creator=s.get(OfCreatorBinding,binding).creator_id
    workflow=PersonalityWorkflow(store,credential_status_reader=connections.personality_credential_status)
    value=workflow.import_draft('tenant-a',account_id,
        profile={'accountId':account_id,'bindingId':binding,'creatorId':creator,
            'sourceId':'synthetic-source','fields':{}},messages=[],expected_revision=0,
        command_key='f'*64,actor='op-session:'+'a'*64)
    assert value['readiness']['credential']=={'state':'ready','configured':True,'metadataAvailable':True}
    assert not value['readiness']['ready'], 'valid credentials alone do not approve a draft'
    with Session(store.engine) as s,s.begin():
        s.get(OfProviderConnection,account_id).status='pending'
    value=workflow.get('tenant-a',account_id)
    assert not value['readiness']['credential']['configured']
    assert SECRET not in json.dumps(value)
