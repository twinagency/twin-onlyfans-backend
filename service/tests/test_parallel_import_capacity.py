"""Shared read admission and fencing. Set ONLYFANS_CAPACITY_TEST_URL for real PG locking tests."""
import concurrent.futures
from datetime import timedelta
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db.models import Account, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget, OfProviderReadLease
from db.onlyfans_ledger_models import OfLedgerScan
from onlyfans_runtime.store import Store, Refused, utcnow
from onlyfans_runtime import provider_read_budget as budget
from onlyfans_runtime.import_capacity import capacity, refill, requirements, Capacity
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery, OfHistoryCursor


@pytest.fixture
def store(tmp_path, monkeypatch):
    url = os.environ.get('ONLYFANS_CAPACITY_TEST_URL', 'sqlite:///'+str(tmp_path/'reads.db'))
    schema = None
    admin = None
    if url.startswith('postgresql'):
        import uuid
        schema = 'of_cap_' + uuid.uuid4().hex
        admin = create_engine(url, isolation_level='AUTOCOMMIT')
        with admin.connect() as c:
            c.exec_driver_sql('CREATE SCHEMA "'+schema+'"')
        engine = create_engine(url, pool_size=16, max_overflow=0,
            connect_args={'options':'-c search_path='+schema})
    else:
        engine = create_engine(url, pool_size=16, max_overflow=0)
    models = [Account, OfCreatorBinding, OfRuntime, OfProviderConnection, OfProviderReadBudget, OfProviderReadLease, OfLedgerScan, OfHistoryCursor]
    for m in models:
        m.__table__.create(engine, checkfirst=True)
    account = 'of-capacity-test'
    with Session(engine) as s, s.begin():
        s.add(Account(id=account));s.flush()
        s.add(OfCreatorBinding(id='capacity-binding',account_id=account,tenant_id='tenant',creator_id='creator',channel='onlyfans'));s.flush()
        s.add(OfRuntime(account_id=account,binding_id='capacity-binding',activated_at=utcnow(),enabled=False));s.flush()
        s.add(OfProviderConnection(account_id=account,binding_id='capacity-binding',owner_id=1,revision=1,
            sealed_config='not-a-credential',expires_at=utcnow()+timedelta(hours=1),status='verified',verified_at=utcnow(),updated_at=utcnow()))
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    monkeypatch.setenv('ONLYFANS_IMPORT_CAPACITY', json.dumps({account:{'rps':64,'inflight':8}}))
    value=SimpleNamespace(engine=engine,_runtime=Store._runtime,_read_connection=Store._read_connection)
    try:
        yield value,account
    finally:
        if schema is None:
            for m in reversed(models):
                m.__table__.drop(engine,checkfirst=True)
        engine.dispose()
        if admin is not None:
            with admin.connect() as c:
                c.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
            admin.dispose()


def acquire(store,account,kind='history',**kwargs):
    return budget.acquire(store,'tenant',account,connection_revision=1,kind=kind,**kwargs)


def test_independent_claims_reserve_live_and_financial_slots(store):
    s,a=store
    claims=[acquire(s,a) for _ in range(6)]
    with pytest.raises(budget.ReadDeferred,match='slots'):acquire(s,a)
    recent=acquire(s,a,'recent');ledger=acquire(s,a,'ledger')
    with pytest.raises(budget.ReadDeferred):acquire(s,a,'ledger')
    # Last-kind changes and another completion must not invalidate other reads.
    assert budget.current(s,'tenant',claims[0])
    assert budget.release(s,'tenant',claims[1])
    assert budget.current(s,'tenant',claims[0])
    replacement=acquire(s,a,'ledger')
    for c in claims+[recent,ledger,replacement]:budget.release(s,'tenant',c)
    with Session(s.engine) as db:
        assert db.get(OfProviderReadBudget,a).lease_token is None


def test_session_change_and_wrong_tenant_refuse_old_results(store):
    s,a=store;c=acquire(s,a)
    with pytest.raises(Refused):budget.current(s,'other',c)
    with Session(s.engine) as db,db.begin():db.get(OfProviderConnection,a).revision=2
    with pytest.raises(Refused):budget.current(s,'tenant',c)


def test_429_backoff_shared_even_when_another_request_succeeds(store):
    s,a=store;c1=acquire(s,a);c2=acquire(s,a)
    assert budget.release(s,'tenant',c1,retry_after=120)
    assert budget.release(s,'tenant',c2)
    with Session(s.engine) as db:
        assert db.get(OfProviderReadBudget,a).rate_ceiling==32
    for kind in budget.KINDS:
        with pytest.raises(budget.ReadDeferred,match='backoff'):acquire(s,a,kind)


def test_frozen_clock_charges_identity_and_page_and_refills_fractionally(store):
    s,a=store;now=utcnow()
    c=acquire(s,a,now=now);budget.release(s,'tenant',c,now=now)
    with Session(s.engine) as db:
        assert db.get(OfProviderReadBudget,a).tokens==62
    assert refill(0,now,now+timedelta(milliseconds=125),Capacity(64,8))[0]==8


def test_legacy_mode_cannot_steal_parallel_claim(store,monkeypatch):
    s,a=store;c=acquire(s,a)
    monkeypatch.setenv('ONLYFANS_IMPORT_CAPACITY','{}')
    with pytest.raises(budget.ReadDeferred,match='lease'):acquire(s,a)
    assert budget.current(s,'tenant',c)
    budget.release(s,'tenant',c)
    legacy=acquire(s,a)
    with pytest.raises(budget.ReadDeferred):acquire(s,a)
    assert budget.release(s,'tenant',legacy)


def test_expired_claim_cannot_commit_or_release_a_replacement(store):
    s,a=store;c=acquire(s,a)
    with Session(s.engine) as db,db.begin():
        db.get(OfProviderReadLease,c.token).lease_until=utcnow()-timedelta(seconds=1)
    with pytest.raises(Refused):budget.current(s,'tenant',c)
    replacement=acquire(s,a)
    assert not budget.release(s,'tenant',c)
    assert budget.current(s,'tenant',replacement)


@pytest.mark.parametrize('value',[{'rps':0},{'rps':65},{'rps':True},{'inflight':129},{'oops':5}])
def test_invalid_capacity_refuses(value,monkeypatch):
    monkeypatch.setenv('ONLYFANS_IMPORT_CAPACITY',json.dumps({'a':value}))
    with pytest.raises(ValueError):capacity('a')


def test_ninety_minute_requirements_count_work_not_assumed_messages():
    r=requirements(23897,2)
    assert r['historyPages']==47794 and r['historyReads']==95588 and r['provisionedRps']==23
    assert not r['includesDiscoveryLedgerAndMemory']


def test_postgresql_simultaneous_claims_do_not_oversubscribe(store):
    s,a=store
    if s.engine.dialect.name!='postgresql':pytest.skip('requires disposable PostgreSQL')
    def attempt(_):
        try:return acquire(s,a)
        except budget.ReadDeferred:return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as p:
        claims=[v for v in p.map(attempt,range(32)) if v]
    assert len(claims)==6 and len({v.token for v in claims})==6
    for c in claims:assert budget.current(s,'tenant',c)


def test_oversized_page_retries_same_cursor_with_smaller_limit(store):
    s,a=store
    history=HistoryRecovery(s)
    history.request_fan('tenant',a,123,connection_revision=1)
    first=history.claim('tenant',a,connection_revision=1)
    assert first.page_limit==20
    history.fail('tenant',first,code='provider_read_failed',oversized=True)
    with Session(s.engine) as db,db.begin():
        row=db.get(OfHistoryCursor,(a,123))
        assert row.before_id is None and row.page_limit==10
        row.retry_at=utcnow()-timedelta(seconds=1)
    second=history.claim('tenant',a,connection_revision=1)
    assert second.page_limit==10 and second.before_id==first.before_id
    with pytest.raises(Refused):history.fail('tenant',first)


def test_postgresql_migration_roundtrip_preserves_cursors(store):
    s,a=store
    if s.engine.dialect.name!='postgresql':pytest.skip('requires disposable PostgreSQL')
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pathlib import Path
    import importlib.util
    spec=importlib.util.spec_from_file_location('capacity_migration',Path(__file__).parents[1]/'db/migrations/versions/20261101_0200_parallel_history_reads.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with s.engine.begin() as c:
        c.exec_driver_sql('DROP TABLE of_provider_read_lease')
        c.exec_driver_sql('ALTER TABLE of_provider_read_budget DROP COLUMN rate_ceiling, DROP COLUMN rate_recover_at')
        c.exec_driver_sql('ALTER TABLE of_launch_history_cursor DROP COLUMN page_limit')
        c.exec_driver_sql('ALTER TABLE of_provider_read_budget DROP CONSTRAINT ck_of_provider_read_budget_tokens')
        c.exec_driver_sql('ALTER TABLE of_provider_read_budget ADD CONSTRAINT ck_of_provider_read_budget_tokens CHECK(tokens BETWEEN 0 AND 10)')
        with Operations.context(MigrationContext.configure(c)):
            module.upgrade()
    claim=acquire(s,a)
    assert budget.current(s,'tenant',claim)
    with s.engine.begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            with pytest.raises(RuntimeError,match='Drain'):module.downgrade()
    budget.release(s,'tenant',claim)
    with s.engine.begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            module.downgrade()
