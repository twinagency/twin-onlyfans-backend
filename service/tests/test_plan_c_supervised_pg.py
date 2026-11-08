"""Normal supervised process: isolated PostgreSQL, simulated model and provider only."""
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import approval, pilot_scope
from conversation_engine.operator_v2 import list_receipts, unresolved
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfFansWebhook, OfFansWebhookConflict
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused
from plan_c_worker import create_app
from test_engine_real_worker_pg import world, inbound, FAN2, PROVIDER, answer  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_scope_pg import ACTOR, OTHER, activate, config_for, install


@pytest.fixture
def supervised(configured_draft, tmp_path, monkeypatch):
    world, env, model, seen = configured_draft
    creds = tmp_path / 'fans-creds'
    creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=' + PROVIDER + '\n')
    creds.chmod(0o600)
    env = {**env, 'TWIN_PLAN_C_DRAFT_ONLY':'0', 'TWIN_PLAN_C_SUPERVISED':'1',
           'ONLYFANS_PROCESS_ROLE':'worker', 'TWIN_ENGINE_FANSAPI_REAL':'1',
           'TWIN_ENGINE_FANSAPI_ENV_FILE':str(creds)}
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')
    install(world, config_for(world, env, fans=[FAN, FAN2], requests=100, credits=200))
    activate(world)
    pilot = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    yield world, env, model, seen, pilot


def stage(pilot, mid, body='hello', fan=FAN, key=None):
    raw, headers = signed(inbound(mid, body, fan=fan), key=key or 'event-'+str(mid))
    return pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])


def approve(world, result):
    info = result['approval']
    return approval.approve(world.store, TENANT, ACCOUNT, info['requestId'], info['generation'],
                            expected_version=info['version'], actor=ACTOR)


def test_normal_http_webhook_approval_dispatch_receipt_duplicate_and_restart(supervised):
    world, env, model, seen, pilot = supervised
    app = create_app(pilot, run_loop=False)
    raw, headers = signed(inbound(501, 'hello there'), key='normal-event')
    with TestClient(app) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers={**headers, 'Signature':'0'*64}).status_code == 401
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    with Session(world.database[1]) as s:
        rows = list(s.scalars(select(OfEngineApproval)))
        assert len(rows) == 1 and rows[0].status == 'awaiting'
        request, generation, version = rows[0].request_id, rows[0].generation, rows[0].version
    assert world.fans.posts == [] and len(seen) == 1
    with pytest.raises(Refused):
        approval.approve(world.store, TENANT, ACCOUNT, request, generation, expected_version=version, actor=OTHER)
    approval.approve(world.store, TENANT, ACCOUNT, request, generation, expected_version=version, actor=ACTOR)
    result = pilot.tick()
    assert result['dispatched'] == ['accepted']
    assert len(world.fans.posts) == 1
    receipt = list_receipts(world.store, TENANT, ACCOUNT)
    assert len(receipt) == 1 and receipt[0]['messageId'] == '7001' and receipt[0]['projected']
    assert pilot.tick()['dispatched'] == []
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    assert restarted.tick()['dispatched'] == [] and len(world.fans.posts) == 1
    scope = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    assert scope['providerRequestsUsed'] == 3 and scope['providerCreditsReserved'] == 6


def test_conflicting_duplicate_outside_scope_or_unsupported_fences_approved_draft(supervised):
    world, _, _, _, pilot = supervised
    result = stage(pilot, 502, key='collision')
    approve(world, result)
    raw, headers = signed(inbound(503, 'other fan', fan=999), key='collision')
    assert pilot.handle(raw, headers['Signature'], 'collision')['outcome'] == 'quarantined'
    raw2, headers2 = signed({'event':'unsupported', 'account_id':PROVIDER, 'payload':{}}, key='collision')
    assert pilot.handle(raw2, headers2['Signature'], 'collision')['outcome'] == 'quarantined'
    assert pilot.tick()['dispatched'] == [] and world.fans.posts == []
    with Session(world.database[1]) as s:
        assert len(list(s.scalars(select(OfFansWebhookConflict)))) == 2
        assert s.scalar(select(OfEngineApproval)).status == 'invalidated'


def test_correction_and_takeover_invalidate_approval_without_send(supervised):
    from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
    world, _, _, _, pilot = supervised
    result = stage(pilot, 504)
    approve(world, result)
    memory = HistoricalMemory(world.store)
    version = memory.read(TENANT, ACCOUNT, FAN)['version']
    kwargs = dict(field='home_city', operation='set', value='Brisbane', expected_version=version,
                  command_key='fixture-correction', reason='verified fixture')
    with pytest.raises(Refused):
        memory.correct(TENANT, ACCOUNT, FAN, actor=OTHER, **kwargs)
    memory.correct(TENANT, ACCOUNT, FAN, actor=ACTOR, **kwargs)
    pilot.tick()
    assert world.fans.posts == []
    # A new fan input creates fresh work; takeover before approval prevents dispatch.
    result2 = stage(pilot, 505, fan=FAN2)
    approve(world, result2)
    from db.models import OfConversationState
    with Session(world.database[1]) as s: revision = s.get(OfConversationState, (ACCOUNT,FAN2)).revision
    world.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision,
        command_key='fixture-takeover', actor=ACTOR, reason='supervised takeover', action='request_takeover')
    pilot.tick()
    assert world.fans.posts == []


def test_crash_after_intake_is_recovered_without_implicit_approval(supervised, monkeypatch):
    world, _, _, seen, pilot = supervised
    original = pilot.worker.engine.plan
    def crash(*args, **kwargs): raise RuntimeError('synthetic crash after intake')
    monkeypatch.setattr(pilot.worker.engine, 'plan', crash)
    raw, headers = signed(inbound(506, 'hello'), key='crash')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 503
    monkeypatch.setattr(pilot.worker.engine, 'plan', original)
    assert world.fans.posts == []
    pilot.tick()
    with Session(world.database[1]) as s:
        assert len(list(s.scalars(select(OfFansWebhook)))) == 1
        rows = list(s.scalars(select(OfEngineApproval)))
        assert len(rows) == 1 and rows[0].status == 'awaiting'
    assert len(seen) == 1 and world.fans.posts == []


def test_parallel_workers_do_not_duplicate_approved_send(supervised):
    world, env, model, _, pilot = supervised
    result = stage(pilot, 507)
    approve(world, result)
    second = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda worker: worker.tick(), (pilot, second)))
    assert len(world.fans.posts) == 1
    with Session(world.database[1]) as s:
        assert len(list(s.scalars(select(OfTextIntent)))) == 1


def test_unknown_send_is_retained_and_different_fan_can_progress(supervised):
    world, env, model, _, _ = supervised
    def handler(request):
        if request.method == 'POST' and '/chats/'+str(FAN)+'/messages' in request.url.path:
            world.fans.posts.append({'path':request.url.path})
            return answer(503, {'error':'ambiguous'})
        return world.fans.handle(request)
    fake = httpx.MockTransport(handler)
    pilot = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=fake)
    first = stage(pilot, 508, fan=FAN)
    approve(world, first)
    assert pilot.tick()['dispatched'] == ['unknown']
    second = stage(pilot, 509, fan=FAN2)
    approve(world, second)
    world.no_throttle()
    assert pilot.tick()['dispatched'] == ['accepted']
    assert len(world.fans.posts) == 2
    assert any(item['kind'] == 'unknown_send' for item in unresolved(world.store, TENANT, ACCOUNT))
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=fake)
    restarted.tick()
    assert len(world.fans.posts) == 2
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfTextIntent).where(OfTextIntent.fan_id == FAN)).status == 'unknown'
