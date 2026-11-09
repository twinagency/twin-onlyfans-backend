'''Stopped preparation uses the normal ledger and backfill, with offline providers only.'''
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import httpx
import pytest
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from db.models import OfCreatorBinding, Message
from db.onlyfans_models import OfRuntime, OfIngress, OfWork
from fans_api_adapter.backfill import FansBackfill
from fans_api_adapter.real_transport import RealFansConfig, build_client
from onlyfans_runtime.store import Refused, utcnow
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, PROVIDER, CREATOR, FAN2, answer
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_scope_pg import config_for, install, ACTOR, OTHER, key
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_supervised_pg import supervised

START = datetime(2015, 1, 1)
BASE = 'https://api.fansapi.com'

def setup_stopped(fixture, requests=20):
    w, env, _, seen = fixture
    cfg = config_for(w, env, requests=requests, credits=requests, per_request=1)
    install(w, cfg)
    stopped = pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'stopped',
        expected_revision=1, command_key=key(2), actor=ACTOR, reason='test stop')
    with Session(w.database[1]) as s, s.begin():
        s.get(OfRuntime, ACCOUNT).enabled = False
    return w, seen, stopped['revision']

def guard(w, revision, *, actor=ACTOR, command=3, limit=10, start=START):
    return pilot_scope.authorize_stopped_backfill(w.store, TENANT, ACCOUNT,
        expected_revision=revision, actor=actor, reason='offline test', command_key=key(command),
        start_date=start, request_limit=limit, base=BASE)

def client(w, guard, callback=None):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if callback:
            callback(len(calls))
        if request.url.path == '/api/accounts':
            return answer(200, [{'id':PROVIDER, 'onlyfans_id':CREATOR, 'is_authenticated':True}])
        if request.url.path.endswith('/transactions'):
            return answer(200, {'data':{'list':[], 'hasMore':False}})
        if request.url.path.endswith('/chargebacks'):
            return answer(200, {'data':{'list':[]}})
        if request.url.path.endswith('/messages'):
            fan = int(request.url.path.split('/chats/')[1].split('/')[0])
            return answer(200, {'data':[{'id':90000+fan, 'fromUser':{'id':fan},
                'text':'historic context', 'createdAt':'2021-01-01T00:00:00Z',
                'isSentByMe':False, 'media':[], 'price':0}], '_pagination':{'next_page':None}})
        return answer(404, {})
    out = build_client(RealFansConfig('fixture-token', PROVIDER, BASE, 5),
        creator_id=CREATOR, epoch=1, transport=httpx.MockTransport(handler), request_guard=guard)
    return out, calls

def test_stopped_preparation_resumes_existing_cursor_without_send_or_model(configured_draft):
    w, seen, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, calls = client(w, g)
    backfill = FansBackfill(w.intake)
    first = backfill.run(TENANT, ACCOUNT, c, reason='operator', start_date=START,
                         pages_budget=2, history_chats=(FAN,FAN2))
    assert first['status'] == 'budget_exhausted'
    final = backfill.run(TENANT, ACCOUNT, c, reason='operator',
                         pages_budget=3, history_chats=(FAN,FAN2))
    assert final['status'] == 'complete' and final['run'] == first['run']
    from conversation_engine.journey import backfill_complete
    with Session(w.database[1]) as s:
        assert backfill_complete(s, ACCOUNT, FAN)
        assert backfill_complete(s, ACCOUNT, FAN2)
        assert not backfill_complete(s, ACCOUNT, 999)
    assert len(calls) == 5 and all(method == 'GET' for method, _ in calls)
    assert not seen and not w.fans.posts
    with Session(w.database[1]) as s:
        assert len([r for r in s.query(OfIngress).filter_by(account_id=ACCOUNT, status='pending')
                    if r.message_id in (90000+FAN, 90000+FAN2)]) == 2
    assert w.store.drain(TENANT, ACCOUNT) >= 2
    with Session(w.database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, 90000+FAN)).body == 'historic context'
        assert s.get(Message, (ACCOUNT, FAN2, 90000+FAN2)).body == 'historic context'
        assert not list(s.query(OfWork).filter_by(account_id=ACCOUNT, status='waiting'))
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    assert view['providerRequestsUsed'] == 5 and view['providerCreditsReserved'] == 5

def test_wrong_actor_scope_or_wire_rejected_before_io(configured_draft):
    w, seen, revision = setup_stopped(configured_draft)
    with pytest.raises(Refused):
        guard(w, revision, actor=OTHER)
    with pytest.raises(Refused):
        guard(w, revision+1)
    g = guard(w, revision)
    c, calls = client(w, g)
    for method, url in [('POST', BASE+'/api/'+PROVIDER+'/chats/'+str(FAN)+'/messages'),
                        ('GET', BASE+'/api/other/transactions'),
                        ('GET', BASE+'/api/'+PROVIDER+'/chats/999/messages'),
                        ('GET', 'https://app.onlyfansapi.com/api/accounts'),
                        ('GET', BASE+'/api/'+PROVIDER+'/vault')]:
        with pytest.raises(Refused):
            g(method, url, True)
    with pytest.raises(Refused):
        FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator', start_date=START,
                                   pages_budget=2, history_chats=(FAN,999))
    assert len(calls) == 1 and not seen and not w.fans.posts

def test_noop_stop_fences_subsequent_requests(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, calls = client(w, g)
    pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'stopped', expected_revision=revision,
        command_key=key(4), actor=ACTOR, reason='stop again')
    with pytest.raises(Refused):
        c.transactions()
    assert len(calls) == 1

def test_revoked_binding_fences_subsequent_requests(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, calls = client(w, g)
    with Session(w.database[1]) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        s.get(OfCreatorBinding, runtime.binding_id).revoked_at = utcnow()
    with pytest.raises(Refused):
        c.transactions()
    assert len(calls) == 1

def test_concurrent_reservations_obey_single_run_budget(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision, limit=2)
    _, calls = client(w, g)
    def reserve(_):
        try:
            g('GET', BASE+'/api/'+PROVIDER+'/transactions', True)
            return True
        except Refused:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, range(2)))
    assert sorted(outcomes) == [False, True]
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    assert view['providerRequestsUsed'] == 2 and view['providerCreditsReserved'] == 2
    assert len(calls) == 1


def test_stop_during_page_fences_the_next_page(configured_draft):
    w, seen, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    def stop_after_identity(count):
        if count == 2:
            pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'stopped',
                expected_revision=revision, command_key=key(4), actor=ACTOR, reason='mid-run stop')
    c, calls = client(w, g, stop_after_identity)
    result = FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator', start_date=START,
                                        pages_budget=5, history_chats=(FAN,FAN2))
    assert result['status'] == 'failed' and result['pages'] == 1
    assert len(calls) == 2 and not seen and not w.fans.posts

def test_budget_exhaustion_is_recorded_without_extra_provider_io(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision, limit=2)
    c, calls = client(w, g)
    result = FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator', start_date=START,
                                        pages_budget=1, history_chats=(FAN,FAN2))
    assert result['status'] == 'budget_exhausted' and len(calls) == 2
    with pytest.raises(Refused, match='budget'):
        c.chargebacks()
    assert len(calls) == 2

def test_authorization_replay_does_not_reset_budget(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision, limit=2)
    _, calls = client(w, g)
    again = guard(w, revision, limit=2)
    again('GET', BASE+'/api/'+PROVIDER+'/transactions', True)
    with pytest.raises(Refused, match='budget'):
        g('GET', BASE+'/api/'+PROVIDER+'/transactions', True)
    with pytest.raises(Refused, match='conflict'):
        guard(w, revision, limit=3)
    assert len(calls) == 1


def test_account_emergency_stop_while_stopped_fences_requests(configured_draft):
    from onlyfans_runtime.launch_operations import account_state, control_account
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, calls = client(w, g)
    with Session(w.database[1]) as s:
        fingerprint = account_state(s, s.get(OfRuntime, ACCOUNT))['fingerprint']
    control_account(w.store, TENANT, ACCOUNT, action='emergency_stop', command_key=key(4),
        actor=ACTOR, reason='test emergency stop', expected_fingerprint=fingerprint)
    with pytest.raises(Refused):
        c.transactions()
    assert len(calls) == 1


def test_reconfigured_scope_fences_old_guard(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, calls = client(w, g)
    cfg = pilot_scope.describe(w.store, TENANT, ACCOUNT)['config']
    cfg['limits']['provider_requests'] -= 1
    pilot_scope.configure(w.store, TENANT, ACCOUNT, cfg, expected_revision=revision,
        command_key=key(4), actor=ACTOR, reason='change scoped limit')
    with pytest.raises(Refused):
        c.transactions()
    assert len(calls) == 1

def test_wrong_creator_and_start_are_rejected(configured_draft):
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, calls = client(w, g)
    c.creator_id = '123456'
    with pytest.raises(Refused):
        FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator',
            start_date=START, pages_budget=2, history_chats=(FAN,FAN2))
    c.creator_id = CREATOR
    with pytest.raises(Refused):
        FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator',
            start_date=datetime(2022,1,1), pages_budget=2, history_chats=(FAN,FAN2))
    assert len(calls) == 1


def test_recent_completed_window_cannot_authorize_offer_and_old_history_is_retained(configured_draft):
    from conversation_engine.journey import backfill_complete
    w, seen, revision = setup_stopped(configured_draft)
    recent = datetime(2022, 1, 1)
    g = guard(w, revision, start=recent)
    c, calls = client(w, g)
    result = FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator',
        start_date=recent, pages_budget=5, history_chats=(FAN,FAN2))
    assert result['status'] == 'complete'
    with Session(w.database[1]) as s:
        assert not backfill_complete(s, ACCOUNT, FAN)
        assert s.query(OfIngress).filter_by(account_id=ACCOUNT, message_id=90000+FAN).count() == 1
    assert not seen and not w.fans.posts and all(method == 'GET' for method, _ in calls)

def test_legacy_short_completed_run_cannot_authorize_pilot_offer(configured_draft):
    from conversation_engine.journey import backfill_complete
    w, _, revision = setup_stopped(configured_draft)
    w.backfilled(FAN)
    with Session(w.database[1]) as s:
        assert not backfill_complete(s, ACCOUNT, FAN)

def test_incomplete_coverage_and_changed_epoch_do_not_authorize_offer(configured_draft):
    from conversation_engine.journey import backfill_complete
    from db.onlyfans_engine_models import OfFansConnection
    w, _, revision = setup_stopped(configured_draft)
    g = guard(w, revision)
    c, _ = client(w, g)
    partial = FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator',
        start_date=START, pages_budget=2, history_chats=(FAN,FAN2))
    assert partial['status'] == 'budget_exhausted'
    with Session(w.database[1]) as s:
        assert not backfill_complete(s, ACCOUNT, FAN)
    full = FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator',
        pages_budget=3, history_chats=(FAN,FAN2))
    assert full['status'] == 'complete'
    with Session(w.database[1]) as s, s.begin():
        assert backfill_complete(s, ACCOUNT, FAN)
        s.get(OfFansConnection, (ACCOUNT, 1)).status = 'retired'
    with Session(w.database[1]) as s:
        assert not backfill_complete(s, ACCOUNT, FAN)


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('scenario', ['unowned', 'recent_only', 'older_purchased'])
def test_real_scoped_backfill_then_normal_photo_journey(supervised, scenario):
    from sqlalchemy import select
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_fans_intake_models import OfFansEvidence
    from test_plan_c_sales_journey_pg import install_coverage, writer, cards
    from test_plan_c_supervised_pg import stage
    from test_plan_c_scope_pg import activate
    w, env, _, initial_model_calls, _ = supervised
    install_coverage(w)
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'stopped',
        expected_revision=before['revision'], command_key=key(1001), actor=ACTOR, reason='offline history preparation')
    with Session(w.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        policy_revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
    w.authority.configure(TENANT, ACCOUNT, expected_revision=policy_revision, enabled=False,
        limit_units=limit, throttle_seconds=throttle, actor=ACTOR, reason='offline history preparation')
    with Session(w.database[1]) as s, s.begin():
        s.get(OfRuntime, ACCOUNT).enabled = False
    start = datetime(2022, 1, 1) if scenario == 'recent_only' else START
    g = guard(w, stopped['revision'], command=1002, start=start)
    provider_calls = []
    def provider(request):
        provider_calls.append((request.method, request.url.path))
        if request.url.path == '/api/accounts':
            return answer(200, [{'id':PROVIDER, 'onlyfans_id':CREATOR, 'is_authenticated':True}])
        if request.url.path.endswith('/transactions'):
            return answer(200, {'data':{'list':[], 'hasMore':False}})
        if request.url.path.endswith('/chargebacks'):
            return answer(200, {'data':{'list':[]}})
        if request.url.path.endswith('/messages'):
            fan = int(request.url.path.split('/chats/')[1].split('/')[0])
            rows = []
            if scenario == 'older_purchased' and fan == FAN:
                rows = [{'id':88001, 'fromUser':{'id':int(CREATOR)}, 'isSentByMe':True,
                    'text':'Old paid photograph', 'createdAt':'2019-01-01T00:00:00Z',
                    'price':3, 'media':[{'id':11,'type':'photo'}], 'isOpened':True}]
            return answer(200, {'data':rows, '_pagination':{'next_page':None}})
        return answer(404, {})
    c = build_client(RealFansConfig('fixture-token', PROVIDER, BASE, 5), creator_id=CREATOR,
        epoch=1, transport=httpx.MockTransport(provider), request_guard=g)
    summary = FansBackfill(w.intake).run(TENANT, ACCOUNT, c, reason='operator',
        start_date=start, pages_budget=5, history_chats=(FAN,FAN2))
    assert summary['status'] == 'complete'
    if scenario == 'older_purchased':
        with Session(w.database[1]) as s:
            evidence = list(s.scalars(select(OfFansEvidence).where(
                OfFansEvidence.account_id == ACCOUNT, OfFansEvidence.fan_id == FAN,
                OfFansEvidence.message_id == 88001, OfFansEvidence.kind == 'unlock')))
            assert len(evidence) == 1 and evidence[0].status in ('pending','held')
    w.store.drain(TENANT, ACCOUNT)
    assert not initial_model_calls and not w.fans.posts
    with Session(w.database[1]) as s, s.begin():
        s.get(OfRuntime, ACCOUNT).enabled = True
    activate(w, revision=stopped['revision'], n=1003)
    pilot, seen, _ = writer(w, env)
    stage(pilot, 20100, 'I want to buy the photo')
    pilot.tick()
    if scenario == 'unowned':
        assert len(cards(w)) == 1
        assert cards(w)[0]['body']['price'] == 3.0
        assert cards(w)[0]['body']['mediaFiles'] == ['11']
    else:
        assert not cards(w)
    assert all(method == 'GET' for method, _ in provider_calls)


def test_pagination_continues_past_cutoff_until_actual_end(configured_draft):
    w, seen, revision = setup_stopped(configured_draft)
    start = datetime(2022, 1, 1)
    g = guard(w, revision, start=start)
    requests = []
    def provider(request):
        requests.append(request.url)
        if request.url.path == '/api/accounts':
            return answer(200, [{'id':PROVIDER, 'onlyfans_id':CREATOR, 'is_authenticated':True}])
        if request.url.path.endswith('/transactions'):
            return answer(200, {'data':{'list':[], 'hasMore':False}})
        if request.url.path.endswith('/chargebacks'):
            return answer(200, {'data':{'list':[]}})
        fan = int(request.url.path.split('/chats/')[1].split('/')[0])
        older = 'id' in request.url.params
        next_page = None
        if fan == FAN and not older:
            next_page = BASE + request.url.path + '?limit=100&order=desc&id=90001'
        mid = 90002 if fan == FAN and not older else 90001 if fan == FAN else 90003
        item = {'id':mid, 'fromUser':{'id':fan}, 'text':'older history',
                'createdAt':'2021-01-01T00:00:00Z', 'isSentByMe':False, 'media':[], 'price':0}
        return answer(200, {'data':[item], '_pagination':{'next_page':next_page}})
    c = build_client(RealFansConfig('fixture-token', PROVIDER, BASE, 5), creator_id=CREATOR,
        epoch=1, transport=httpx.MockTransport(provider), request_guard=g)
    backfill = FansBackfill(w.intake)
    first = backfill.run(TENANT, ACCOUNT, c, reason='operator', start_date=start,
                         pages_budget=3, history_chats=(FAN,FAN2))
    assert first['status'] == 'budget_exhausted'
    final = backfill.run(TENANT, ACCOUNT, c, reason='operator', pages_budget=3, history_chats=(FAN,FAN2))
    assert final['status'] == 'complete'
    assert any('id=90001' in str(url) for url in requests)
    with Session(w.database[1]) as s:
        assert s.query(OfIngress).filter_by(account_id=ACCOUNT, message_id=90001).count() == 1
    assert not seen and not w.fans.posts
