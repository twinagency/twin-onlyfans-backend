"""Hosted wire adaptation uses synthetic responses only; no provider access."""
import json
from dataclasses import replace
from urllib.parse import urlsplit, parse_qs
from datetime import datetime, timedelta, timezone
import pytest
from test_onlyfans_provider_adapters import fake_curl, OWNER
from onlyfans_runtime.hosted_provider import HostedProviderSession, HostedUnavailable, call_once, session_from_saved
from onlyfans_runtime.native_protocol import NativeProtocolError, load
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused


def settings(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    monkeypatch.setenv('ONLYFANS_HOSTED_CONNECT_ENABLED','1')
    monkeypatch.setenv('OFAPI_API_KEY','synthetic-hosted-key-private')


def session():
    return HostedProviderSession('of-'+str(OWNER),OWNER,1,datetime.now(timezone.utc)+timedelta(hours=23),'acct_fixture')


def test_account_bound_feature_gate_and_fixed_origin(monkeypatch):
    settings(monkeypatch);handle=session();calls=fake_curl(monkeypatch,body=json.dumps({'data':{'id':OWNER}}).encode())
    NativeProviderConnector(handle).verify_identity()
    assert calls[0]['constructor']=={'trust_env':False,'retry':0}
    row=calls[1];assert row['url']=='https://app.onlyfansapi.com/api/acct_fixture/me'
    assert row['options']['headers']['Authorization']=='Bearer synthetic-hosted-key-private'
    assert 'Cookie' not in row['options']['headers'] and 'proxy' not in row['options']
    assert row['options']['allow_redirects'] is False and row['options']['verify'] is True
    for changes in [{'account_id':'of-999'},{'provider_account_id':'../acct_foreign'},{'proxy_url':'http://foreign.example:8080'},{'owner_id':True}]:
        with pytest.raises(Refused):replace(handle,**changes)
    monkeypatch.setenv('ONLYFANS_HOSTED_CONNECT_ENABLED','0')
    with pytest.raises(Refused):NativeProviderConnector(handle).verify_identity()


def test_actual_me_owner_must_match_signed_enrollment(monkeypatch):
    settings(monkeypatch);fake_curl(monkeypatch,body=b'{"data":{"id":999}}')
    with pytest.raises(NativeProtocolError):NativeProviderConnector(session()).verify_identity()


@pytest.mark.parametrize('status',[401,408,425,429,500,502,503,504])
def test_transient_get_is_bounded_and_never_retried_inline(monkeypatch,status):
    settings(monkeypatch);calls=fake_curl(monkeypatch,status=status)
    with pytest.raises(HostedUnavailable):call_once(session(),'GET','/api2/v2/users/me')
    assert len(calls)==2


@pytest.mark.parametrize('raw', [b'{"data":{},"data":{}}',b'{"data":NaN}',b'{"data":[]}',b'x'*65537])
def test_malformed_identity_is_terminal(monkeypatch,raw):
    settings(monkeypatch);fake_curl(monkeypatch,body=raw)
    with pytest.raises(NativeProtocolError) as error:NativeProviderConnector(session()).verify_identity()
    assert not isinstance(error.value,HostedUnavailable)


def test_descending_message_cursor_uses_first_id_and_validates_continuation(monkeypatch):
    settings(monkeypatch)
    envelope={'data':[{'id':98,'price':0.25}], '_pagination':{'next_page':'https://app.onlyfansapi.com/api/acct_fixture/chats/101/messages?first_id=98&order=desc'}}
    calls=fake_curl(monkeypatch,body=json.dumps(envelope).encode())
    result=load(call_once(session(),'GET','/api2/v2/chats/101/messages?limit=20&order=desc&id=100&skip_users=all'))
    assert result=={'list':[{'id':98,'price':'0.25'}],'hasMore':True}
    assert parse_qs(urlsplit(calls[1]['url']).query)['first_id']==['100']
    for url in ['https://foreign.example/api/acct_fixture/chats/101/messages?first_id=98','https://app.onlyfansapi.com/api/acct_other/chats/101/messages?first_id=98','https://app.onlyfansapi.com/api/acct_fixture/chats/101/messages?first_id=99']:
        envelope['_pagination']['next_page']=url;fake_curl(monkeypatch,body=json.dumps(envelope).encode())
        with pytest.raises(NativeProtocolError):call_once(session(),'GET','/api2/v2/chats/101/messages?limit=20&order=desc')


def test_vault_mapping_and_explicit_final_page(monkeypatch):
    settings(monkeypatch);calls=fake_curl(monkeypatch,body=b'{"data":[],"_pagination":{"next_page":null}}')
    assert load(call_once(session(),'GET','/api2/v2/vault/media?limit=20&offset=0'))=={'list':[],'hasMore':False}
    assert '/api/acct_fixture/media/vault?' in calls[1]['url']
    fake_curl(monkeypatch,body=b'{"data":[]}')
    with pytest.raises(NativeProtocolError):call_once(session(),'GET','/api2/v2/chats?limit=20')


def test_post_requires_stable_intent_key_and_never_retries_uncertainty(monkeypatch):
    settings(monkeypatch);calls=fake_curl(monkeypatch,error=TimeoutError('synthetic uncertainty'))
    with pytest.raises(Refused):call_once(session(),'POST','/api2/v2/chats/101/messages',b'{}')
    with pytest.raises(NativeProtocolError):call_once(session(),'POST','/api2/v2/chats/101/messages',b'{}',idempotency_key='twin:recorded-intent')
    assert len(calls)==2 and calls[1]['options']['headers']['Idempotency-Key']=='twin:recorded-intent'


def test_hosted_payload_refuses_extra_reference_fields_and_proxy(monkeypatch):
    settings(monkeypatch)
    payload={'ownerId':OWNER,'expiresAt':session().expires_at.isoformat(),'hosted':{'schemaVersion':'ofapi.v1','accountId':'acct_fixture'}}
    assert session_from_saved(payload=payload,account_id='of-'+str(OWNER),policy_revision=1).owner_id==OWNER
    for extra in [{'proxyUrl':'http://proxy.example:8080'},{'hosted':{**payload['hosted'],'url':'https://foreign.example'}}]:
        with pytest.raises(Refused):session_from_saved(payload={**payload,**extra},account_id='of-'+str(OWNER),policy_revision=1)
