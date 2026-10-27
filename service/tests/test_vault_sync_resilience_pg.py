"""Durable checkpoint survival on an isolated PostgreSQL test server."""
from datetime import timedelta
from sqlalchemy import select,func
from sqlalchemy.orm import Session
import pytest
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified,TENANT
from test_onlyfans_vault_pg import media,page,queue
from onlyfans_runtime.vault_management import process_vault_jobs
from onlyfans_runtime.provider_connector import NativeProviderConnector,NativeReadTransientError,NativeAuthenticationError
from onlyfans_runtime.store import utcnow
from db.onlyfans_vault_models import OfVaultSyncJob
from db.models import VaultItem

def test_transient_read_keeps_same_job_cursor_and_resumes_without_duplicates(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    job=queue(store,account);calls=[]
    def first(self,**kw):
        calls.append(kw['offset'])
        return page([media(i) for i in range(1,25)],True)
    monkeypatch.setattr(NativeProviderConnector,'vault_page',first)
    assert process_vault_jobs(store,TENANT)==1
    def fail(self,**kw):
        calls.append(kw['offset'])
        raise NativeReadTransientError()
    monkeypatch.setattr(NativeProviderConnector,'vault_page',fail)
    assert process_vault_jobs(store,TENANT)==0
    with Session(store.engine) as s,s.begin():
        row=s.get(OfVaultSyncJob,(account,job['jobId']))
        assert row.status=='running' and row.page_offset==24 and row.items_seen==24 and row.pages_read==1
        assert row.completed_at is None and row.last_error=='vault_read_failed' and row.lease_until>utcnow()
    # Both the job and the account-wide provider budget must honor backoff.
    assert process_vault_jobs(store,TENANT)==0
    after_backoff=utcnow()+timedelta(seconds=6)
    monkeypatch.setattr('onlyfans_runtime.provider_read_budget.utcnow',lambda:after_backoff)
    monkeypatch.setattr('onlyfans_runtime.vault_management.utcnow',lambda:after_backoff)
    def finish(self,**kw):
        calls.append(kw['offset'])
        return page([media(25)])
    monkeypatch.setattr(NativeProviderConnector,'vault_page',finish)
    assert process_vault_jobs(store,TENANT)==1
    with Session(store.engine) as s:
        row=s.get(OfVaultSyncJob,(account,job['jobId']))
        assert row.status=='complete' and row.page_offset==25 and row.items_seen==25
        assert s.scalar(select(func.count()).select_from(OfVaultSyncJob))==1
        assert s.scalar(select(func.count()).select_from(VaultItem))==25
    assert calls==[0,24,24]
    store.close()

def test_auth_failure_does_not_become_a_read_retry(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    job=queue(store,account)
    def denied(*_,**__):raise NativeAuthenticationError('native provider rejected authentication')
    monkeypatch.setattr(NativeProviderConnector,'vault_page',denied)
    assert process_vault_jobs(store,TENANT)==0
    with Session(store.engine) as s:
        row=s.get(OfVaultSyncJob,(account,job['jobId']))
        assert row.status=='failed' and row.page_offset==0 and row.last_error=='connection_unavailable'
    store.close()

def test_malformed_page_still_stops_atomically(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    job=queue(store,account)
    monkeypatch.setattr(NativeProviderConnector,'vault_page',lambda *_,**__:b'{"list":[{"id":1}],"hasMore":false}')
    assert process_vault_jobs(store,TENANT)==0
    with Session(store.engine) as s:
        row=s.get(OfVaultSyncJob,(account,job['jobId']))
        assert row.status=='failed' and row.page_offset==0 and row.last_error=='unsupported_vault_response'
        assert s.scalar(select(func.count()).select_from(VaultItem))==0
    store.close()
