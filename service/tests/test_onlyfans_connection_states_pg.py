"""Connection is not activation: runner state, default non-sending, and bounded enrollment states."""
import json
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings
from test_onlyfans_creator_enrollments_pg import submit,counts,session_payload,CREATOR
from test_onlyfans_provider_adapters import OWNER
from test_onlyfans_text_delivery_pg import TENANT
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_connection_models import OfConnectionEnrollment
from onlyfans_runtime.store import Store,utcnow
from onlyfans_runtime.connections import get_connection
from onlyfans_runtime import enrollments as module
from onlyfans_runtime.provider_connector import NativeProviderConnector,NativeProtocolError
from onlyfans_runtime.runner_state import runner_state

NOW=datetime(2026,10,24,3,0,0)
def status_file(tmp_path,accounts,*,updated=NOW,interval=30,name='supervisor-status.json'):
    path=tmp_path/name
    path.write_text(json.dumps({'updated_at':updated.isoformat(timespec='seconds')+'Z','interval_s':interval,'accounts':accounts}))
    return str(path)

# ── runner state: never claims running without proof ────────────────────────
def test_missing_or_broken_status_file_is_unknown_never_running(tmp_path):
    for path in (str(tmp_path/'absent.json'),str(tmp_path)):
        assert runner_state('of-1',path=path,now=NOW)['state']=='unknown'
    for text in ('','not json','[]','{"updated_at":"x","accounts":{}}','{"updated_at":"2026-10-24T03:00:00Z","accounts":[]}',
                 '{"updated_at":"2026-10-24T03:00:00Z","accounts":{"of-1":{"state":"flying"}}}','x'*300000):
        (tmp_path/'bad.json').write_text(text)
        got=runner_state('of-1',path=str(tmp_path/'bad.json'),now=NOW)
        assert got['state']=='unknown' and got['reason']=='status_invalid'

def test_runner_state_reports_each_supervisor_state_and_unsupervised_accounts(tmp_path):
    path=status_file(tmp_path,{'of-1':{'state':'running','reason':None,'restarts':2},'of-2':{'state':'held','reason':'policy-disabled','restarts':0},
        'of-3':{'state':'restarting','reason':'exit:1','restarts':4},'of-4':{'state':'held','reason':'bad reason\n$(x)'}})
    assert runner_state('of-1',path=path,now=NOW)=={'state':'running','reason':None,'observedAt':'2026-10-24T03:00:00Z','stale':False,'restarts':2}
    assert runner_state('of-2',path=path,now=NOW)['reason']=='policy-disabled'
    assert runner_state('of-3',path=path,now=NOW)['state']=='restarting'
    assert runner_state('of-4',path=path,now=NOW)['reason'] is None
    gone=runner_state('of-9',path=path,now=NOW)
    assert gone['state']=='not_supervised' and gone['stale'] is False

def test_stale_status_file_is_unknown_even_if_it_says_running(tmp_path):
    path=status_file(tmp_path,{'of-1':{'state':'running','reason':None,'restarts':0}},updated=NOW-timedelta(minutes=10))
    got=runner_state('of-1',path=path,now=NOW)
    assert got['state']=='unknown' and got['stale'] is True and got['reason']=='status_stale'
    assert runner_state('of-1',path=status_file(tmp_path,{'of-1':{'state':'running'}},updated=NOW-timedelta(seconds=60)),now=NOW)['state']=='running'

# ── connection API: separate connection / runner state, opt-in, defaults off ─
def api(store,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from onlyfans_commands.connection_api import router,SCHEMA,HEADER
    for key,value in {'ONLYFANS_PROCESS_ROLE':'api','ONLYFANS_CONNECTION_TOKEN':'synthetic-connect','ONLYFANS_CONNECTION_TENANT':TENANT,
        'ONLYFANS_API_TOKEN':'synthetic-read','ONLYFANS_CONTROL_TOKEN':'synthetic-control'}.items():monkeypatch.setenv(key,value)
    app=FastAPI();app.include_router(router);app.state.control_store=store
    return TestClient(app),{'Authorization':'Bearer synthetic-connect',HEADER:SCHEMA}

def test_new_connection_is_not_sending_and_api_says_runner_unknown_not_live(database,monkeypatch,tmp_path):
    monkeypatch.setenv('FASTT_SUPERVISOR_STATUS',str(tmp_path/'absent.json'))
    settings(monkeypatch);store=Store(database[0])
    submit(store);monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:None)
    assert module.process_enrollments(store,TENANT)==1
    client,headers=api(store,monkeypatch)
    account='of-'+str(OWNER)
    with Session(database[1]) as s:
        assert s.get(OfRuntime,account).enabled is False
        assert s.scalar(select(func.count()).select_from(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id==account))==0
    plain=client.get('/onlyfans/connection/v1/accounts/'+account,headers=headers).json()['connection']
    assert 'runner' not in plain and 'aiLive' not in plain, 'existing response shape must not change without opt-in'
    c=client.get('/onlyfans/connection/v1/accounts/'+account+'?runner=1',headers=headers).json()['connection']
    assert c['status']=='pending' and c['outgoingEnabled'] is False  # accepted, not yet verified by the worker, never sending
    assert c['runner']['state']=='unknown' and c['aiLive'] is False
    store.close()

def test_ai_live_needs_sending_enabled_and_a_running_runner(database,monkeypatch,tmp_path):
    from test_onlyfans_connections_pg import verified,change,LIMITS
    store,account,_=verified(database,monkeypatch)
    running=status_file(tmp_path,{account:{'state':'running','reason':None,'restarts':0}},updated=utcnow())
    held=status_file(tmp_path,{account:{'state':'held','reason':'policy-disabled','restarts':0}},updated=utcnow(),name='held.json')
    # Verified + a running runner process, but sending still off: connected, not live.
    got=get_connection(store,TENANT,account,include_runner=True,runner_status_path=running)
    assert got['status']=='verified' and got['runner']['state']=='running' and got['outgoingEnabled'] is False and got['aiLive'] is False
    change(store,account,'activate',1,'c'*64,LIMITS)
    live=get_connection(store,TENANT,account,include_runner=True,runner_status_path=running)
    assert live['outgoingEnabled'] is True and live['aiLive'] is True
    # Sending is on but the runner is held, absent or unreadable: still not live.
    assert get_connection(store,TENANT,account,include_runner=True,runner_status_path=held)['aiLive'] is False
    assert get_connection(store,TENANT,account,include_runner=True,runner_status_path=str(tmp_path/'none.json'))['aiLive'] is False
    assert 'runner' not in get_connection(store,TENANT,account)
    store.close()

# ── enrollment states: no endless spinner, no duplicate work or accounts ────
def test_provider_outage_ends_in_a_visible_failure_not_an_endless_pending(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);first=submit(store)
    def down(self):raise NativeProtocolError('synthetic maintenance response')
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',down)
    for attempt in range(module.MAX_CHECKS+3):
        with Session(database[1]) as s,s.begin():
            row=s.get(OfConnectionEnrollment,first['id'])
            if row.status!='pending':break
            row.lease_until=utcnow()-timedelta(seconds=1)
        module.process_enrollments(store,TENANT)
    assert attempt<=module.MAX_CHECKS
    row=module.get_enrollments(store,TENANT,'creator-a')[0]
    assert row['status']=='failed' and row['error']=='verification_unavailable' and row['accountId'] is None
    with Session(database[1]) as s:
        assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    assert counts(database[1])==(0,0)
    store.close()

def test_repeated_submissions_supersede_waiting_ones_and_create_one_account(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0])
    first=submit(store,key='1'*64);second=submit(store,key='2'*64);third=submit(store,key='3'*64)
    rows={r['id']:r['status'] for r in module.get_enrollments(store,TENANT,'creator-a')}
    assert rows[first['id']]=='expired' and rows[second['id']]=='expired' and rows[third['id']]=='pending'
    with Session(database[1]) as s:
        assert s.get(OfConnectionEnrollment,first['id']).sealed_config is None
    calls=[]
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:calls.append(self.account_id))
    module.process_enrollments(store,TENANT,limit=4)
    assert calls==['of-'+str(OWNER)] and counts(database[1])==(1,1)
    assert module.get_enrollments(store,TENANT,'creator-a')[0]['status']=='accepted'
    store.close()

def test_exact_replay_returns_the_same_enrollment_and_does_not_supersede_it(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);payload=session_payload()
    first=submit(store,payload,key='4'*64)
    assert submit(store,payload,key='4'*64)['id']==first['id']
    assert [r['status'] for r in module.get_enrollments(store,TENANT,'creator-a')]==['pending']
    store.close()

def test_another_creator_never_supersedes_or_takes_over_an_owner(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0])
    mine=submit(store,key='1'*64,creator='creator-a')
    theirs=submit(store,key='2'*64,creator='creator-b')
    rows={r['id']:r['status'] for r in module.get_enrollments(store,TENANT,'creator-a')}
    assert rows[mine['id']]=='pending'
    calls=[]
    monkeypatch.setattr(NativeProviderConnector,'verify_identity',lambda self:calls.append(1))
    module.process_enrollments(store,TENANT,limit=2)
    # The first claimed owner keeps the account; the other creator is refused without leaking it.
    a=module.get_enrollments(store,TENANT,'creator-a')[0];b=module.get_enrollments(store,TENANT,'creator-b')[0]
    assert (a['status'],b['status'])==('accepted','failed') and b['accountId'] is None and b['error']=='verification_unavailable'
    assert len(calls)==1 and counts(database[1])==(1,1)
    store.close()
