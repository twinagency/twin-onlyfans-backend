"""Shared read budgets fence real native adapters against disposable PostgreSQL.

Transport methods are replaced with deterministic local responses. No provider,
paid model, real credential or production database is accessed.
"""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT, ACTOR
from test_onlyfans_ledger_pg import transaction, payload
from test_onlyfans_vault_pg import media, page as vault_page, queue
from db.models import Fan, Transaction, VaultItem
from db.onlyfans_models import OfRuntime, OfScan, OfRecovery
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from db.onlyfans_ledger_models import OfLedgerScan
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime.provider_read_budget import acquire, release, current, ReadDeferred
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeRateLimitError, session_from_capture
from onlyfans_runtime.store import Event, Refused, utcnow
from onlyfans_runtime.worker import ChatPage, MessagePage, read_step
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery, OfHistoryScan
from onlyfans_runtime.ledger import process_ledger_pages
from onlyfans_runtime.vault_management import process_vault_jobs


@pytest.fixture
def native(database,monkeypatch):
    store,account,clear=verified(database,monkeypatch)
    handle=session_from_capture(account_id=account,owner_id=clear['ownerId'],policy_revision=1,
        capture=clear['capture'],x_hash=clear['xHash'],proxy_url=clear['proxyUrl'],
        expires_at=__import__('datetime').datetime.fromisoformat(clear['expiresAt']))
    reader=NativeProviderConnector(handle);reader.connection_revision=1
    yield store,account,reader
    store.close()


def expire_lease(store,account):
    with Session(store.engine) as s,s.begin():
        s.get(OfProviderReadBudget,account).lease_until=utcnow()-timedelta(seconds=1)


def test_recent_and_discovery_hold_one_shared_lease_without_transaction_during_io(native,monkeypatch):
    store,account,reader=native
    calls=[]
    def check(kind):
        assert store.engine.pool.checkedout()==0
        with pytest.raises(ReadDeferred):acquire(store,TENANT,account,connection_revision=1,kind='ledger')
        with Session(store.engine) as s:
            row=s.get(OfProviderReadBudget,account)
            assert row.lease_token and row.last_kind==kind
        calls.append(kind)
    monkeypatch.setattr(reader,'chats',lambda **kw:(check('discovery') or ChatPage([101],None)))
    monkeypatch.setattr(reader,'messages',lambda **kw:(check('recent') or MessagePage([],False)))
    result=read_step(store,TENANT,account,reader,connection_revision=1)
    assert (result.discovery_pages,result.recovery_pages)==(1,1) and calls==['discovery','recent']
    with Session(store.engine) as s:assert s.get(OfProviderReadBudget,account).lease_token is None


@pytest.mark.parametrize('lane',['discovery','recent','archive'])
def test_expired_shared_lease_cannot_commit_a_provider_page(native,monkeypatch,lane):
    store,account,reader=native
    if lane!='discovery':
        with Session(store.engine) as s,s.begin():
            s.get(OfScan,account).status='complete'
            s.add(OfRecovery(account_id=account,fan_id=101))
    def chats(**kw):
        expire_lease(store,account)
        return ChatPage([101],None)
    def messages(**kw):
        expire_lease(store,account)
        return MessagePage([],False)
    monkeypatch.setattr(reader,'chats',chats)
    monkeypatch.setattr(reader,'messages',messages)
    if lane=='archive':
        recovery=HistoryRecovery(store);recovery.request_account(TENANT,account,connection_revision=1)
        with pytest.raises(Refused):recovery.step(TENANT,account,reader,connection_revision=1)
        with Session(store.engine) as s:assert s.get(OfHistoryScan,account).page_count==0
    else:
        with pytest.raises(Refused):
            read_step(store,TENANT,account,reader,connection_revision=1,recent_cutover=lane=='recent')
        with Session(store.engine) as s:
            assert s.get(OfScan,account).page_count==0
            if lane=='recent':assert s.get(OfRecovery,(account,101)).status!='complete'


def test_rate_limit_backoff_is_shared_with_all_lanes(native,monkeypatch):
    store,account,reader=native
    monkeypatch.setattr(reader,'chats',lambda **kw:(_ for _ in ()).throw(NativeRateLimitError(120)))
    with pytest.raises(NativeRateLimitError):read_step(store,TENANT,account,reader,connection_revision=1)
    for kind in ('recent','history','ledger','vault','posts','connection_check'):
        with pytest.raises(ReadDeferred,match='backoff'):
            acquire(store,TENANT,account,connection_revision=1,kind=kind)
    with Session(store.engine) as s:
        row=s.get(OfProviderReadBudget,account)
        assert row.lease_token is None and row.backoff_until>utcnow()+timedelta(seconds=100)


def test_reserved_recent_tokens_keep_new_intake_running_when_archive_is_deferred(native,monkeypatch):
    store,account,reader=native
    with Session(store.engine) as s,s.begin():
        budget=s.get(OfProviderReadBudget,account);budget.tokens=2;budget.refilled_at=utcnow()
        s.add(OfRecovery(account_id=account,fan_id=101))
    monkeypatch.setattr(reader,'chats',lambda **kw:pytest.fail('background discovery consumed recent reserve'))
    monkeypatch.setattr(reader,'messages',lambda **kw:MessagePage([],False))
    result=read_step(store,TENANT,account,reader,connection_revision=1)
    assert result.discovery_pages==0 and result.recovery_pages==1
    with Session(store.engine) as s:
        assert s.get(OfScan,account).status=='needed'
        assert s.get(OfRecovery,(account,101)).status=='complete'


@pytest.mark.parametrize('lane',['ledger','vault'])
def test_occupied_account_defers_independent_domain_without_losing_its_work(native,monkeypatch,lane):
    store,account,_=native
    job=queue(store,account) if lane=='vault' else None
    budget=acquire(store,TENANT,account,connection_revision=1,kind='history')
    def read(*args,**kw):pytest.fail('another lane read while account lease occupied')
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',read)
    monkeypatch.setattr(NativeProviderConnector,'vault_page',read)
    work=process_ledger_pages if lane=='ledger' else process_vault_jobs
    assert work(store,TENANT)==0
    release(store,TENANT,budget)
    with Session(store.engine) as s,s.begin():
        if lane=='ledger':
            row=s.get(OfLedgerScan,account);assert row.page_offset==0
            row.next_poll_at=utcnow()-timedelta(seconds=1)
        else:
            row=s.get(OfVaultSyncJob,(account,job['jobId']));assert row.page_offset==0
            row.lease_until=utcnow()-timedelta(seconds=1)
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',lambda *a,**k:payload([]))
    monkeypatch.setattr(NativeProviderConnector,'vault_page',lambda *a,**k:vault_page([]))
    assert work(store,TENANT)==1


@pytest.mark.parametrize('lane',['ledger','vault'])
def test_expired_shared_lease_prevents_financial_and_catalogue_result_commits(native,monkeypatch,lane):
    store,account,_=native
    if lane=='vault':queue(store,account)
    def response(*args,**kw):
        assert store.engine.pool.checkedout()==0
        expire_lease(store,account)
        return payload([transaction(description='Tip from Synthetic Fan')]) if lane=='ledger' else vault_page([media()])
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',response)
    monkeypatch.setattr(NativeProviderConnector,'vault_page',response)
    work=process_ledger_pages if lane=='ledger' else process_vault_jobs
    assert work(store,TENANT)==0
    with Session(store.engine) as s:
        assert s.scalar(select(Transaction.id).where(Transaction.account_id==account)) is None
        assert s.scalar(select(VaultItem.media_id).where(VaultItem.account_id==account)) is None


def test_replaced_connection_generation_fences_old_read_result(native,monkeypatch):
    store,account,reader=native
    def chats(**kw):
        with Session(store.engine) as s,s.begin():s.get(OfProviderConnection,account).revision+=1
        return ChatPage([101],None)
    monkeypatch.setattr(reader,'chats',chats)
    with pytest.raises(Refused):read_step(store,TENANT,account,reader,connection_revision=1)
    with Session(store.engine) as s:
        assert s.get(OfScan,account).page_count==0
        assert s.get(OfRecovery,(account,101)) is None


def test_expired_lease_replacement_refuses_stale_owner_and_keeps_current_owner(native):
    store,account,_=native
    first=acquire(store,TENANT,account,connection_revision=1,kind='history')
    expire_lease(store,account)
    second=acquire(store,TENANT,account,connection_revision=1,kind='recent')
    with pytest.raises(Refused,match='stale'):current(store,TENANT,first)
    assert release(store,TENANT,first) is False
    assert current(store,TENANT,second) is True
    assert release(store,TENANT,second) is True


def test_identity_renewal_shares_the_account_lease_without_enabling_domains(native,monkeypatch):
    from onlyfans_runtime.connections import load_worker_accounts
    store,account,_=native
    with Session(store.engine) as s,s.begin():
        s.get(OfProviderConnection,account).verified_at=utcnow()-timedelta(minutes=20)
    def identity(self):
        assert store.engine.pool.checkedout()==0
        with Session(store.engine) as s:assert s.get(OfProviderReadBudget,account).last_kind=='connection_check'
        with pytest.raises(ReadDeferred):acquire(store,TENANT,account,connection_revision=1,kind='recent')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',identity)
    assert load_worker_accounts(store,TENANT)==[]
    with Session(store.engine) as s:
        assert not s.get(OfRuntime,account).enabled
        assert s.get(OfProviderConnection,account).expires_at>utcnow()+timedelta(hours=5)


def test_identity_rate_limit_backoff_is_shared_with_recent_reads(native,monkeypatch):
    from onlyfans_runtime.connections import load_worker_accounts
    store,account,_=native
    with Session(store.engine) as s,s.begin():
        s.get(OfProviderConnection,account).verified_at=utcnow()-timedelta(minutes=20)
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',
        lambda self:(_ for _ in ()).throw(NativeRateLimitError(30)))
    assert load_worker_accounts(store,TENANT)==[]
    with pytest.raises(ReadDeferred,match='backoff'):
        acquire(store,TENANT,account,connection_revision=1,kind='recent')


@pytest.mark.parametrize('condition',['normal','expired','occupied','rate_limited'])
def test_held_ledger_diagnostic_uses_shared_lease_without_reconciling(native,monkeypatch,condition):
    from test_onlyfans_launch_ledger_recovery_pg import frozen_scan
    from onlyfans_runtime.launch_ledger.diagnostics import collect_diagnostic
    store,account,_=native
    start,end=frozen_scan(store.engine,account)
    occupied=acquire(store,TENANT,account,connection_revision=1,kind='history') if condition=='occupied' else None
    calls=[]
    def read(*args,**kwargs):
        assert store.engine.pool.checkedout()==0
        calls.append(1)
        with pytest.raises(ReadDeferred):acquire(store,TENANT,account,connection_revision=1,kind='recent')
        if condition=='expired':expire_lease(store,account)
        if condition=='rate_limited':raise NativeRateLimitError(45)
        return payload([transaction(description='Tip from Synthetic Fan',createdAt=(end-timedelta(seconds=1)).isoformat()+'Z')])
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',read)
    result=collect_diagnostic(store,TENANT,account)
    if occupied:
        assert not result['collection']['attempted'] and not calls
        release(store,TENANT,occupied)
    elif condition=='normal':
        assert result['valid'] and not result['applied'] and result['collection']['snapshot_unchanged']
    elif condition=='expired':
        assert result['error_code']=='diagnostic_state_changed' and result['structure'] is None
    else:
        assert result['error_code']=='diagnostic_read_failed'
        with pytest.raises(ReadDeferred,match='backoff'):
            acquire(store,TENANT,account,connection_revision=1,kind='recent')
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert (scan.status,scan.page_offset,scan.window_start,scan.window_end)==('held',40,start,end)
        assert s.scalar(select(Transaction.id).where(Transaction.account_id==account)) is None


def test_connection_retry_cannot_leak_domain_read_authority(native):
    store,account,_=native
    with Session(store.engine) as s,s.begin():
        s.get(OfProviderConnection,account).last_error='provider_connection_check_retry'
    for kind in ('recent','history','ledger','vault','posts','offer_refresh'):
        with pytest.raises(Refused,match='connection changed'):
            acquire(store,TENANT,account,connection_revision=1,kind=kind)
    claim=acquire(store,TENANT,account,connection_revision=1,kind='connection_check')
    assert current(store,TENANT,claim)
    release(store,TENANT,claim)


def test_continuous_archive_preserves_maintenance_and_recent_progress(native):
    store,account,_=native
    now=utcnow()
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account);row.tokens=10;row.refilled_at=now
    for _ in range(7):
        claim=acquire(store,TENANT,account,connection_revision=1,kind='history',now=now)
        release(store,TENANT,claim,now=now)
    for _ in range(3):
        with pytest.raises(ReadDeferred,match='budget exhausted'):
            acquire(store,TENANT,account,connection_revision=1,kind='history',now=now)
    maintenance=acquire(store,TENANT,account,connection_revision=1,kind='ledger',now=now)
    release(store,TENANT,maintenance,now=now)
    with Session(store.engine) as s:assert s.get(OfProviderReadBudget,account).tokens==2
    recent=acquire(store,TENANT,account,connection_revision=1,kind='recent',now=now)
    release(store,TENANT,recent,now=now)
    # A bounded virtual clock advances the actual durable refill calculation;
    # archive work resumes without consuming another lane's reserved capacity.
    later=now+timedelta(seconds=3)
    archive=acquire(store,TENANT,account,connection_revision=1,kind='history',now=later)
    assert current(store,TENANT,archive,now=later)
    release(store,TENANT,archive,now=later)
    with Session(store.engine) as s:assert s.get(OfProviderReadBudget,account).tokens==3
