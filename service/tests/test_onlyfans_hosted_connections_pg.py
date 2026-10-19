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
