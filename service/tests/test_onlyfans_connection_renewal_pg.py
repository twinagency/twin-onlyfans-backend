"""Renew verified connections using owned PostgreSQL and synthetic provider I/O."""
from datetime import timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, change, LIMITS, TENANT, settings, provision, save, data
from db.onlyfans_connection_models import OfProviderConnection, OfConnectionChange
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime
from db.models import OfCreatorBinding
from onlyfans_runtime import connections as module
from onlyfans_runtime.connections import _open, _seal, get_connection, load_worker_accounts
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeAuthenticationError
from onlyfans_runtime.store import Store, utcnow


def due(store, account, *, elapsed=False, status='verified'):
    with Session(store.engine) as s, s.begin():
        row=s.get(OfProviderConnection,account)
        clear=_open(row,TENANT)
        expiry=utcnow()+timedelta(minutes=-5 if elapsed else 5)
        clear['expiresAt']=expiry.replace(tzinfo=timezone.utc).isoformat()
        row.expires_at=expiry
        row.sealed_config=_seal(clear,TENANT,account,row.binding_id,row.revision)
        row.verified_at=utcnow()-timedelta(minutes=20)
        row.status=status
        row.last_error=None
        return clear


@pytest.mark.parametrize('elapsed,status,enabled',[(False,'verified',True),(True,'verified',True),(True,'expired',False)])
def test_valid_connection_renews_without_activation_revision_or_allowance_reset(database,monkeypatch,elapsed,status,enabled):
    store,account,_=verified(database,monkeypatch)
    change(store,account,'activate',1,'c'*64,LIMITS)
    with Session(store.engine) as s,s.begin():
        s.get(OfRuntime,account).enabled=enabled
        s.get(OfDeliveryPolicy,account).used_units=3
    original=due(store,account,elapsed=elapsed,status=status)
    calls=[]
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:calls.append(self.account_id))
    with Session(store.engine) as s:
        audits=s.scalar(select(func.count()).select_from(OfConnectionChange))
        policy_revision=s.get(OfDeliveryPolicy,account).revision
    accounts=load_worker_accounts(store,TENANT)
    assert calls==[account] and len(accounts)==int(enabled)
    with Session(store.engine) as s:
        row=s.get(OfProviderConnection,account);clear=_open(row,TENANT)
        assert row.status=='verified' and row.revision==2 and row.last_error is None
        assert utcnow()+timedelta(hours=5)<row.expires_at<=utcnow()+timedelta(hours=6)
        assert {k:v for k,v in clear.items() if k!='expiresAt'}=={k:v for k,v in original.items() if k!='expiresAt'}
        assert s.get(OfRuntime,account).enabled is enabled
        policy=s.get(OfDeliveryPolicy,account)
        assert policy.revision==policy_revision and policy.used_units==3 and policy.enabled is True
        assert s.scalar(select(func.count()).select_from(OfConnectionChange))==audits
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is enabled
    assert len(load_worker_accounts(store,TENANT))==int(enabled) and calls==[account]
    if accounts:
        assert accounts[0].transport._handle.expires_at.replace(tzinfo=None)>utcnow()+timedelta(hours=5)
    store.close()


def test_repeated_successful_renewal_has_no_connection_age_limit(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    for _ in range(10):
        due(store,account,elapsed=True)
        assert load_worker_accounts(store,TENANT)==[]
        assert get_connection(store,TENANT,account)['status']=='verified'
    with Session(store.engine) as s:
        assert s.get(OfRuntime,account).enabled is False
        assert s.get(OfProviderConnection,account).revision==1
    store.close()


def test_rejected_authentication_stops_connection_without_repeated_renewal(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    change(store,account,'activate',1,'c'*64,LIMITS)
    due(store,account)
    calls=[]
    def reject(self):
        calls.append(self.account_id)
        raise NativeAuthenticationError('synthetic secret provider response')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',reject)
    assert load_worker_accounts(store,TENANT)==[]
    public=get_connection(store,TENANT,account)
    assert public['status']=='failed' and public['lastError']=='provider_authentication_required'
    assert not public['outgoingEnabled'] and 'secret' not in str(public)
    assert load_worker_accounts(store,TENANT)==[] and calls==[account]
    store.close()


@pytest.mark.parametrize('elapsed',[False,True])
def test_temporary_failure_retries_without_disabling_saved_intent_or_extending_unverified_lease(database,monkeypatch,elapsed):
    store,account,_=verified(database,monkeypatch)
    change(store,account,'activate',1,'c'*64,LIMITS)
    original=due(store,account,elapsed=elapsed)
    calls=[]
    def unavailable(self):
        calls.append(self.account_id)
        raise RuntimeError('synthetic secret timeout')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',unavailable)
    assert load_worker_accounts(store,TENANT)==[]
    with Session(store.engine) as s:
        row=s.get(OfProviderConnection,account)
        assert _open(row,TENANT)==original and row.last_error=='provider_connection_check_retry'
        assert s.get(OfRuntime,account).enabled is True
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is (not elapsed)
    load_worker_accounts(store,TENANT)
    assert calls==[account], 'retry cooldown must bound provider calls'
    with Session(store.engine) as s,s.begin():
        s.get(OfProviderConnection,account).updated_at=utcnow()-timedelta(minutes=2)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:calls.append(self.account_id))
    assert len(load_worker_accounts(store,TENANT))==1 and calls==[account,account]
    assert get_connection(store,TENANT,account)['outgoingEnabled'] is True
    store.close()


@pytest.mark.parametrize('mutation',['pause','replacement','revoke'])
def test_inflight_check_cannot_overwrite_pause_replacement_or_revocation(database,monkeypatch,mutation):
    store,account,_=verified(database,monkeypatch)
    change(store,account,'activate',1,'c'*64,LIMITS)
    due(store,account)
    def concurrent(self):
        if mutation=='pause':change(store,account,'pause',2,'d'*64)
        elif mutation=='replacement':save(store,account,data(),revision=2,key='d'*64)
        else:
            with Session(store.engine) as s,s.begin():
                row=s.get(OfProviderConnection,account)
                s.get(OfCreatorBinding,row.binding_id).revoked_at=utcnow()
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',concurrent)
    assert load_worker_accounts(store,TENANT)==[]
    with Session(store.engine) as s:
        row=s.get(OfProviderConnection,account)
        if mutation=='pause':assert row.status=='paused' and row.revision==3
        elif mutation=='replacement':assert row.status=='pending' and row.revision==3 and row.verified_at is None
        else:assert row.expires_at<utcnow()+timedelta(minutes=6)
    store.close()


def test_never_verified_expired_capture_and_corrupted_verified_capture_do_not_renew(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    save(store,account,data())
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:pytest.fail('unverified expiry must not be renewed'))
    with Session(store.engine) as s,s.begin():s.get(OfProviderConnection,account).expires_at=utcnow()-timedelta(seconds=1)
    assert load_worker_accounts(store,TENANT)==[]
    assert get_connection(store,TENANT,account)['status']=='expired'
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderConnection,account)
        row.status='verified';row.verified_at=utcnow()-timedelta(minutes=20);row.sealed_config='corrupt'
    assert load_worker_accounts(store,TENANT)==[]
    with Session(store.engine) as s:assert s.get(OfProviderConnection,account).expires_at<utcnow()
    store.close()
