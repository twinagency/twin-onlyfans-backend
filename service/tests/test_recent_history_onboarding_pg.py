"""Real PostgreSQL coverage with synthetic provider/model inputs only."""
import asyncio
from dataclasses import replace
from datetime import timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from db.models import Message, Fan, AccountAiConfig, OfConversationState
from db.onlyfans_models import OfRuntime, OfWork
from onlyfans_runtime.store import Event, Refused, utcnow
from onlyfans_runtime.history_policy import OfHistoryPolicy, OfOlderHistoryJob, activate_older, memory_notice
from onlyfans_runtime.launch_history_memory.recovery import (HistoryRecovery, OfHistoryCursor,
    OfRecentHistoryCursor, OfRecentHistoryScan)
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from onlyfans_runtime.profile_learning import worker
from onlyfans_runtime.history_progress import recent_progress
from onlyfans_runtime.creator_onboarding import validate, issues, prompt

T,A,F = 'tenant-a','acc-a',101


@pytest.fixture
def ready(database, monkeypatch):
    url, engine = database
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    monkeypatch.setenv('ONLYFANS_RECENT_HISTORY_ACCOUNTS',json.dumps([A]))
    with engine.begin() as c:
        c.exec_driver_sql((Path(__file__).parents[2]/'deploy/profile-learning/schema.sql').read_text())
    store=seed(url,engine)
    with Session(engine) as s,s.begin():
        s.get(OfRuntime,A).enabled=False
    recovery=HistoryRecovery(store)
    recovery.request_account(T,A)
    with Session(engine) as s:
        cutoff=s.get(OfHistoryPolicy,A).cutoff_at
    yield store,recovery,engine,cutoff
    store.close()


def event(mid, at, fan=F, direction='in', body='No personal facts here', media=()):
    return Event(fan,mid,at.replace(tzinfo=timezone.utc).isoformat(),direction,body,media)


def scan_all(r):
    for _ in range(2):
        c=r.claim_scan(T,A)
        assert c.scope=='recent'
        r.scan_page(T,c,[F],next_cursor=None)
    # Drain the finite known-fan sweep, including an empty final page.
    r.claim_scan(T,A)


def test_cutoff_fixed_inclusive_outgoing_metadata_and_lifetime_separate(ready):
    store,r,engine,cutoff=ready
    scan_all(r)
    c=r.claim(T,A)
    rows=[event(30,cutoff+timedelta(days=1),direction='out',media=(91,)),
          event(20,cutoff),event(10,cutoff-timedelta(microseconds=1))]
    assert r.page(T,c,rows,has_more=False)=='continue'  # catch-up still owed
    c=r.claim(T,A)
    assert c.before_id is None
    assert r.page(T,c,rows,has_more=False)=='complete'
    with Session(engine) as s:
        assert s.get(OfHistoryPolicy,A).cutoff_at==cutoff
        assert s.get(Message,(A,F,20))
        assert s.get(Message,(A,F,10)) is None
        assert json.loads(s.get(Message,(A,F,30)).media_ids)==[91]
        assert s.get(OfHistoryCursor,(A,F)).status!='complete'
        view=recent_progress(s,s.get(OfRuntime,A))
        assert view['initialHistory']['state']=='recent_history_ready'
        assert view['initialHistory']['messagesSavedInWindow']==2
        assert view['memoryProcessing']['state']=='incomplete'
        assert memory_notice(s,A,'binding-'+A,F)['lifetimeHistoryComplete'] is False
    r.request_account(T,A)
    with Session(engine) as s:assert s.get(OfHistoryPolicy,A).cutoff_at==cutoff


def test_unverified_order_traverses_old_page_and_finds_later_recent_message(ready):
    _,r,engine,cutoff=ready
    c=r.claim(T,A)
    assert r.page(T,c,[event(40,cutoff-timedelta(days=2))],has_more=True)=='continue'
    c=r.claim(T,A)
    assert c.before_id==40
    r.page(T,c,[event(30,cutoff+timedelta(days=1))],has_more=False)
    with Session(engine) as s:assert s.get(Message,(A,F,30)) is not None


def test_verified_order_stops_at_cutoff_without_marking_lifetime_complete(ready):
    _,r,engine,cutoff=ready
    with Session(engine) as s,s.begin():
        p=s.get(OfHistoryPolicy,A);p.timestamp_order_verified=True;p.ordering_evidence='synthetic sorted fixture'
    c=r.claim(T,A)
    r.page(T,c,[event(20,cutoff),event(10,cutoff-timedelta(days=1))],has_more=True)
    c=r.claim(T,A)
    assert c.before_id is None  # head catch-up, not another old page
    r.page(T,c,[event(21,cutoff+timedelta(days=1)),event(20,cutoff)],has_more=True)
    assert r.claim(T,A) is None
    with Session(engine) as s:assert s.get(OfHistoryCursor,(A,F)).status!='complete'


def test_invalid_order_failed_request_and_restart_never_complete(ready):
    _,r,engine,cutoff=ready
    c=r.claim(T,A)
    with pytest.raises(Refused):r.page(T,c,[event(10,cutoff),event(20,cutoff)],has_more=False)
    r.fail(T,c,code='invalid_page')
    with Session(engine) as s,s.begin():
        row=s.get(OfRecentHistoryCursor,(A,F));row.retry_at=utcnow()-timedelta(seconds=1)
    resumed=HistoryRecovery(r.store).claim(T,A)
    assert resumed.before_id is None and resumed.generation>c.generation
    with Session(engine) as s:
        v=recent_progress(s,s.get(OfRuntime,A))
        assert v['initialHistory']['state']=='incomplete'
        assert s.get(OfHistoryCursor,(A,F)).before_id is None


def test_fan_readiness_waits_for_successful_account_discovery(ready):
    _,r,engine,cutoff=ready
    c=r.claim(T,A);r.page(T,c,[event(20,cutoff)],has_more=False)
    c=r.claim(T,A);r.page(T,c,[event(20,cutoff)],has_more=False)
    with Session(engine) as s:
        assert s.get(OfRecentHistoryCursor,(A,F)).status=='complete'
        view=recent_progress(s,s.get(OfRuntime,A),F)
        assert view['initialHistory']['discoveryComplete'] is False
        assert view['initialHistory']['state']=='incomplete'


def test_discovery_old_thread_does_not_stop_other_threads_and_requires_stability(ready):
    _,r,engine,_=ready
    c=r.claim_scan(T,A);r.scan_page(T,c,[F],next_cursor='1')
    c=r.claim_scan(T,A);r.scan_page(T,c,[202],next_cursor=None)
    c=r.claim_scan(T,A);r.scan_page(T,c,[202],next_cursor='1')
    c=r.claim_scan(T,A);r.scan_page(T,c,[F],next_cursor=None)
    with Session(engine) as s:assert s.get(OfRecentHistoryScan,A).status!='complete'
    # A stable second full pass completes discovery; every thread remains separately owed.
    c=r.claim_scan(T,A);r.scan_page(T,c,[202],next_cursor='1')
    c=r.claim_scan(T,A);r.scan_page(T,c,[F],next_cursor=None)
    with Session(engine) as s:
        assert s.get(OfRecentHistoryScan,A).status=='complete'
        assert s.get(OfRecentHistoryCursor,(A,202))
        assert recent_progress(s,s.get(OfRuntime,A))['initialHistory']['state']=='incomplete'


def test_arrival_during_import_is_saved_once_in_catchup(ready):
    _,r,engine,cutoff=ready
    c=r.claim(T,A);r.page(T,c,[event(20,cutoff)],has_more=False)
    c=r.claim(T,A)
    newer=event(30,cutoff+timedelta(days=2))
    r.page(T,c,[newer,event(20,cutoff)],has_more=False)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Message))==2
        assert s.get(Message,(A,F,30))


def test_existing_archives_cursors_memories_and_controls_survive_flag(ready,monkeypatch):
    store,r,engine,cutoff=ready
    old=event(5,cutoff-timedelta(days=90),body='I live in Sydney')
    store.record(T,A,old,source='import');store.drain(T,A)
    with Session(engine) as s,s.begin():
        c=s.get(OfHistoryCursor,(A,F));c.before_id=4;c.pages_completed=9
        s.get(OfConversationState,(A,F)).mode='paused'
    r.request_account(T,A)
    with Session(engine) as s:
        assert s.get(Message,(A,F,5)).body=='I live in Sydney'
        assert s.get(OfHistoryCursor,(A,F)).before_id==4
        assert s.get(OfHistoryCursor,(A,F)).pages_completed==9
        assert s.get(OfConversationState,(A,F)).mode=='paused'
        assert not s.get(OfRuntime,A).enabled
    monkeypatch.setenv('ONLYFANS_RECENT_HISTORY_ACCOUNTS','[]')
    assert r.claim(T,A).scope=='lifetime'
    with Session(engine) as s:assert s.get(Message,(A,F,5))


def test_repeated_returning_messages_activate_one_job_preserve_progress(ready):
    _,r,engine,cutoff=ready
    with Session(engine) as s,s.begin():
        runtime=s.get(OfRuntime,A)
        for mid in (31,32,33):activate_older(s,runtime,event(mid,cutoff+timedelta(days=30),body='Remember what I bought?'))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfOlderHistoryJob))==1
        j=s.get(OfOlderHistoryJob,(A,F));assert j.reason=='payment_context' and j.priority==0
    c=r.claim(T,A);assert c.scope=='lifetime'
    r.page(T,c,[event(4,cutoff-timedelta(days=90))],has_more=True)
    with Session(engine) as s,s.begin():activate_older(s,s.get(OfRuntime,A),event(40,utcnow()))
    with Session(engine) as s:assert s.get(OfHistoryCursor,(A,F)).before_id==4


def test_old_paid_extraction_queue_defers_until_activation_and_no_facts_is_valid(ready):
    store,r,engine,cutoff=ready
    for e in [event(10,cutoff-timedelta(days=90),body='I like tennis'),event(20,cutoff,body='hello'),
              event(19,cutoff-timedelta(seconds=1),direction='out',body='Do you like music?')]:
        store.record(T,A,e,source='import')
    store.drain(T,A)
    worker.seed(store,T)
    job={'tenant_id':T,'account_id':A,'binding_id':'binding-'+A,'fan_id':F}
    sources,progress,_,_=worker.source_page(store,job)
    assert [x['mid'] for x in progress]==[20]
    assert any(x.get('context_only') and x['direction']=='out' for x in sources.values())
    assert worker.source_page(store,{**job,'fan_id':0})[0]=={}
    with Session(engine) as s,s.begin():activate_older(s,s.get(OfRuntime,A),event(30,utcnow()))
    assert {x['mid'] for x in worker.source_page(store,job)[1]}=={10,20}


def test_deterministic_memory_queue_respects_scope(ready):
    store,_,engine,cutoff=ready
    for e in [event(10,cutoff-timedelta(days=90),body='My name is Jack'),event(20,cutoff,body='hello')]:
        store.record(T,A,e,source='import')
    store.drain(T,A)
    result=HistoricalMemory(store).process(T,A,F)
    assert result['processed']==1
    with Session(engine) as s,s.begin():activate_older(s,s.get(OfRuntime,A),event(30,utcnow()))
    assert HistoricalMemory(store).process(T,A,F)['processed']==1


def test_private_information_excluded_and_conflicting_permissions_require_review():
    document={k:'' for k in ['fanFacingBiography','privateInformation','writingStyle','boundaries','pricingAndOperations']}
    document.update(fanFacingBiography='Approved biography',privateInformation='secret-address',boundaries='PG-13 only. Explicit sexting permitted.')
    assert 'onboarding_content_permissions_conflict' in issues(document)
    assert 'secret-address' not in prompt(document)
    assert 'Approved biography' in prompt(document)
    document['boundaries']='Ask for approval for customs. [insert price]'
    assert 'onboarding_placeholders_require_review' in issues(document)


@pytest.mark.parametrize('base',['0108_commercial_progress','0106_parallel_history_reads'])
def test_upgrade_from_both_release_lines_preserves_existing_messages(database,base):
    from sqlalchemy import create_engine
    from alembic import command
    from db.schema import _config, BASELINE, check_schema, upgrade, HERE
    from db.models import Account, OfCreatorBinding
    import uuid
    admin=create_engine(database[1].url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    name='of_test_history_upgrade_'+uuid.uuid4().hex
    with admin.connect() as c:c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0')
    engine=create_engine(admin.url.set(database=name))
    try:
        with engine.begin() as c:
            c.exec_driver_sql((HERE/'snapshots/0070_postgresql.sql').read_text())
            command.stamp(_config(c),BASELINE)
            command.upgrade(_config(c),base)
            check_schema(c,expected=base)
        with Session(engine) as s,s.begin():
            s.add(Account(id='old-account'));s.flush()
            s.add(OfCreatorBinding(id='old-binding',account_id='old-account',tenant_id=T,creator_id='old-creator'));s.flush()
            s.add(OfRuntime(account_id='old-account',binding_id='old-binding',activated_at=utcnow(),enabled=False));s.flush()
            s.add(Fan(account_id='old-account',fan_id=F));s.flush()
            s.add(Message(account_id='old-account',fan_id=F,message_id=1,direction='out',body='Retained old commitment',created_at=utcnow()-timedelta(days=400)))
        with engine.begin() as c:
            upgrade(c)
            check_schema(c)
        with Session(engine) as s:
            assert s.get(Message,('old-account',F,1)).body=='Retained old commitment'
            assert not s.get(OfRuntime,'old-account').enabled
    finally:
        engine.dispose()
        with admin.connect() as c:c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def test_stored_old_extraction_precedes_more_old_provider_reads(ready,monkeypatch):
    store,r,engine,cutoff=ready
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED','1')
    store.record(T,A,event(10,cutoff-timedelta(days=90)),source='import');store.drain(T,A)
    with Session(engine) as s,s.begin():activate_older(s,s.get(OfRuntime,A),event(30,utcnow()))
    assert r.claim(T,A).scope=='recent'  # no old provider work until stored sources are processed


def test_transport_telemetry_is_per_attempt_and_reports_failures(ready):
    store,r,engine,_=ready
    from onlyfans_runtime.history_telemetry import measure, dispatched, OfHistoryReadAttempt
    c=r.claim(T,A)
    with pytest.raises(RuntimeError):
        with measure(store,c,discovery=False):
            dispatched();dispatched()
            raise RuntimeError('synthetic provider failure')
    with Session(engine) as s:
        row=s.scalar(select(OfHistoryReadAttempt))
        assert row.http_attempts==2 and row.outcome=='failed' and row.finished_at
        view=recent_progress(s,s.get(OfRuntime,A))
        assert view['initialHistory']['providerRequests']==2
        assert view['initialHistory']['failedReadAttempts']==1
        assert view['initialHistory']['state']=='incomplete'


def test_fact_free_paid_processing_receipted_once_and_older_queue_preserved(ready):
    store,_,engine,cutoff=ready
    for e in [event(10,cutoff-timedelta(days=90)),event(20,cutoff)]:
        store.record(T,A,e,source='import')
    store.drain(T,A);worker.seed(store,T)
    seen=[]
    async def extractor(*args,**kwargs):
        seen.extend(x['message'] for x in args[5].values() if not x.get('context_only'))
        return {'facts':[]},None
    with engine.begin() as c:
        c.execute(text("UPDATE of_profile_learning SET next_at=CURRENT_TIMESTAMP WHERE fan_id=:f"),{'f':F})
    assert asyncio.run(worker.step(store,T,threading.Event(),extractor=extractor))
    assert seen==[20]
    with engine.begin() as c:
        c.execute(text("UPDATE of_profile_learning SET next_at=CURRENT_TIMESTAMP WHERE fan_id=:f"),{'f':F})
    assert asyncio.run(worker.step(store,T,threading.Event(),extractor=extractor))
    assert seen==[20]
    with engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM of_profile_receipts WHERE message_id=10'))==0
        assert c.scalar(text('SELECT count(*) FROM messages WHERE message_id=10'))==1
