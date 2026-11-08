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
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfFansWebhook, OfEnginePilotWebhookDigest
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused
from plan_c_worker import create_app
from test_engine_real_worker_pg import world, inbound, FAN2, PROVIDER, answer  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_scope_pg import ACTOR, OTHER, activate, config_for, install


@pytest.fixture
def supervised(configured_draft, tmp_path, monkeypatch, request):
    world, env, model, seen = configured_draft
    # Synthetic catalogue ID only; the actual photo must be bound separately.
    from db.models import CatalogItem, Fan, OfConversationState
    with Session(world.database[1]) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN2))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=FAN2, mode='automated', revision=0,
                                  takeover_state='none', requires_attention=False))
        item = s.get(CatalogItem, world.item)
        media = json.loads(item.media_ids)[:1]
        item.media_ids, item.preview_media_ids, item.price_cents, item.kind = json.dumps(media), '[]', 300, 'image'
    env = {**env, 'TWIN_PLAN_C_PRICE_CENTS':'300',
           'TWIN_PLAN_C_MEDIA_IDS':','.join(map(str, media)), 'TWIN_PLAN_C_PREVIEW_IDS':''}
    creds = tmp_path / 'fans-creds'
    creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=' + PROVIDER + '\n')
    creds.chmod(0o600)
    env = {**env, 'TWIN_PLAN_C_DRAFT_ONLY':'0', 'TWIN_PLAN_C_SUPERVISED':'1',
           'ONLYFANS_PROCESS_ROLE':'worker', 'TWIN_ENGINE_FANSAPI_REAL':'1',
           'TWIN_ENGINE_FANSAPI_ENV_FILE':str(creds)}
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')
    cfg = config_for(world, env, fans=[FAN, FAN2], requests=100, credits=200)
    cfg['authorization_mode'] = getattr(request, 'param', 'supervised')
    install(world, cfg)
    activate(world)
    pilot = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    yield world, env, model, seen, pilot


def stage(pilot, mid, body='hello', fan=FAN, key=None):
    raw, headers = signed(inbound(mid, body, fan=fan), key=key or 'event-'+str(mid))
    return pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])


def approve(world, result):
    assert result.get('outcome') == 'awaiting_approval', result
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
    # HTTP has committed intake only. Normal polling creates the draft.
    assert seen == [] and world.fans.posts == []
    pilot.tick()
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
    receipt = list_receipts(world.store, TENANT, ACCOUNT, ACTOR)
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
        assert len(list(s.scalars(select(OfEnginePilotWebhookDigest)))) == 2
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
    world, env, model, seen, pilot = supervised
    original = pilot.worker.engine.plan
    def crash(*args, **kwargs): raise RuntimeError('synthetic crash after intake')
    monkeypatch.setattr(pilot.worker.engine, 'plan', crash)
    raw, headers = signed(inbound(506, 'hello'), key='crash')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    with pytest.raises(RuntimeError, match='synthetic crash after intake'):
        pilot.tick()
    monkeypatch.setattr(pilot.worker.engine, 'plan', original)
    assert world.fans.posts == []
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    restarted.tick()
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
    assert any(item['kind'] == 'unknown_send' for item in unresolved(world.store, TENANT, ACCOUNT, ACTOR))
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=fake)
    restarted.tick()
    assert len(world.fans.posts) == 2
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfTextIntent).where(OfTextIntent.fan_id == FAN)).status == 'unknown'


def test_preconstructed_backfill_client_cannot_bypass_the_pilot_budget(supervised):
    from datetime import datetime
    from fans_api_adapter.backfill import FansBackfill
    world, _, _, _, _ = supervised
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed']
    with pytest.raises(Refused, match='backfill lacks current pilot request guard'):
        FansBackfill(world.intake).run(TENANT, ACCOUNT, world.transport.client, reason='operator',
                                      start_date=datetime(2025, 1, 1), history_chats=(FAN,))
    assert pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed'] == before
    assert world.fans.posts == []


def test_fixture_scope_refuses_real_provider_wire_before_identity_io(supervised):
    world, env, model, _, _ = supervised
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed']
    with pytest.raises(Refused, match='fixture scope requires a simulated provider transport'):
        build_supervised_pilot(world.store, env, model_transport=model, fans_transport=None)
    assert pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed'] == before
    assert world.fans.posts == []


def test_pending_human_send_blocks_approved_draft_then_takes_over(supervised):
    from datetime import timedelta
    from db.models import OfConversationState
    from db.onlyfans_fans_intake_models import OfFansEvidence
    from test_engine_real_worker_pg import iso
    from onlyfans_runtime.store import utcnow
    world, _, _, _, pilot = supervised
    draft = stage(pilot, 510)
    approve(world, draft)
    body = {'event':'messages.sent', 'account_id':PROVIDER, 'payload':{
        'id':5555, 'text':'human response', 'createdAt':iso(1), 'price':0, 'media':[],
        'isFromQueue':False, 'queueId':1, 'toUser':{'id':FAN}}}
    raw, headers = signed(body, key='human-sent')
    pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])
    pilot.tick()
    assert world.fans.posts == []
    with Session(world.database[1]) as s, s.begin():
        evidence = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'human_sent'))
        assert evidence.status == 'pending'
        evidence.created_at = utcnow() - timedelta(minutes=2)
    pilot.tick()
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT,FAN)).mode == 'human_controlled'
    assert world.fans.posts == []


def test_auth_failure_after_projection_blocks_wire_even_if_callback_crashes(supervised, monkeypatch):
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_fans_intake_models import OfFansLink
    world, _, _, _, pilot = supervised
    draft = stage(pilot, 511)
    approve(world, draft)
    def crash(*args): raise RuntimeError('synthetic callback crash')
    monkeypatch.setattr(pilot.worker.intake, '_disable_sends', crash)
    raw, headers = signed({'event':'accounts.authentication_failed','account_id':PROVIDER,'payload':{}},
                          key='auth-failed')
    with pytest.raises(RuntimeError):
        pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansLink)).send_blocked is True
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is True
    pilot.tick()
    assert world.fans.posts == []


def test_reserved_model_call_cannot_cross_pilot_stop_and_reactivation(supervised):
    from db.onlyfans_engine_models import OfEngineModelCall, OfEngineRequest
    world, _, _, _, pilot = supervised
    draft = stage(pilot, 512)
    with Session(world.database[1]) as s:
        prior = s.scalar(select(OfEngineModelCall))
        request = s.get(OfEngineRequest, (ACCOUNT, prior.request_id))
        args = dict(account=ACCOUNT, request_id=prior.request_id, generation=prior.generation,
                    fan_id=prior.fan_id, binding_id=prior.binding_id, owner_epoch=prior.owner_epoch,
                    chain_pos=1, chain_step='primary', model=prior.model, prefixed=False,
                    snapshot_sha256='a'*64, prompt_sha256='b'*64, worker_id='fixture-worker', provider=prior.provider)
    ledger = pilot.worker.engine.model.ledger
    reservation = ledger.reserve(**args)
    status = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped', expected_revision=status['revision'],
        command_key='3'*64, actor=ACTOR, reason='fixture stop')
    pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'active', expected_revision=stopped['revision'],
        command_key='4'*64, actor=ACTOR, reason='fixture restart')
    with pytest.raises(Refused, match='model worker pilot revision changed'):
        ledger.mark_calling(ACCOUNT, reservation.call_id, 'fixture-worker')
    with Session(world.database[1]) as s:
        assert s.get(OfEngineModelCall, (ACCOUNT, reservation.call_id)).status == 'reserved'
    assert world.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_autonomous_policy_records_machine_authority_and_sends_once(supervised):
    world, env, model, _, pilot = supervised
    draft = stage(pilot, 520)
    assert draft['outcome'] == 'awaiting_approval' and world.fans.posts == []
    result = pilot.tick()
    assert result['dispatched'] == ['accepted']
    with Session(world.database[1]) as s:
        row = s.scalar(select(OfEngineApproval))
        assert row.authorization_mode == 'autonomous' and row.actor.startswith('autonomous-policy:')
        assert row.status == 'consumed'
    pilot.tick()
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    restarted.tick()
    assert len(world.fans.posts) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_autonomous_mode_still_invalidates_changed_memory_and_takeover(supervised):
    from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
    from db.models import OfConversationState
    world, _, _, seen, pilot = supervised
    original = stage(pilot, 521)
    memory = HistoricalMemory(world.store)
    view = memory.read(TENANT, ACCOUNT, FAN)
    memory.correct(TENANT, ACCOUNT, FAN, field='home_city', operation='set', value='Brisbane',
        expected_version=view['version'], command_key='auto-correction', actor=ACTOR, reason='fixture correction')
    for _ in range(4):pilot.tick()
    first = [row for row in world.fans.posts if row['path'].endswith('/chats/'+str(FAN)+'/messages')]
    assert len(first) == 1 and 'Brisbane' in str(seen[-1])
    with Session(world.database[1]) as s:
        assert s.get(OfEngineDecision,(ACCOUNT,original['decisionId'])).status == 'cancelled'
    stage(pilot, 522, fan=FAN2)
    with Session(world.database[1]) as s: revision = s.get(OfConversationState, (ACCOUNT,FAN2)).revision
    world.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision,
        command_key='auto-takeover', actor=ACTOR, reason='fixture stop', action='request_takeover')
    for _ in range(4):pilot.tick()
    assert len(world.fans.posts) == 1
    assert not any(row['path'].endswith('/chats/'+str(FAN2)+'/messages') for row in world.fans.posts)
    with Session(world.database[1]) as s:
        assert not list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.fan_id==FAN2,
            OfEngineDecision.status=='completed')))


def test_outside_first_concurrent_key_collision_cannot_send(supervised, monkeypatch):
    from threading import Event
    world, _, _, _, pilot = supervised
    arrived, release = Event(), Event()
    original = pilot.worker.intake.accept
    def paused(*args, **kwargs):
        arrived.set()
        assert release.wait(8)
        return original(*args, **kwargs)
    monkeypatch.setattr(pilot.worker.intake, 'accept', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(stage, pilot, 523, 'allowed', FAN, 'racing-key')
        assert arrived.wait(8)
        raw, headers = signed(inbound(524, 'outside', fan=999), key='racing-key')
        assert pilot.handle(raw, headers['Signature'], 'racing-key')['outcome'] == 'outside_pilot_scope'
        release.set()
        assert future.result(timeout=15)['outcome'] == 'quarantined'
    pilot.tick()
    assert world.fans.posts == []
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansWebhook)).status == 'quarantined'
        assert s.scalar(select(OfEnginePilotWebhookDigest)) is not None


def test_fixture_scope_refuses_real_model_before_wire(supervised, monkeypatch):
    from model_transport.openrouter import OpenRouterClient
    world, env, _, seen, _ = supervised
    def unexpected(*args, **kwargs): raise AssertionError('real model wire must not be reached')
    monkeypatch.setattr(OpenRouterClient, 'chat', unexpected)
    pilot = build_supervised_pilot(world.store, env, model_transport=None, fans_transport=world.fans.transport)
    result = stage(pilot, 525)
    assert result['outcome'] == 'not_ready' and 'simulated model' in result['detail']
    assert seen == [] and world.fans.posts == []


def test_pilot_private_reads_require_the_named_administrator(supervised):
    from conversation_engine.operator_v2 import list_drafts
    world, _, _, _, pilot = supervised
    stage(pilot, 526)
    assert len(list_drafts(world.store, TENANT, ACCOUNT, ACTOR)) == 1
    with pytest.raises(Refused): list_drafts(world.store, TENANT, ACCOUNT, OTHER)
    with pytest.raises(Refused): list_receipts(world.store, TENANT, ACCOUNT, None)
    with pytest.raises(Refused): unresolved(world.store, TENANT, ACCOUNT, OTHER)


def test_worker_cannot_start_model_after_scope_changes_between_validation_and_plan(supervised, monkeypatch):
    world, _, _, seen, pilot = supervised
    original = pilot.worker.engine.plan
    def changed(*args, **kwargs):
        scope = pilot_scope.describe(world.store, TENANT, ACCOUNT)
        stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped', expected_revision=scope['revision'],
            command_key='5'*64, actor=ACTOR, reason='fixture stop')
        pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'active', expected_revision=stopped['revision'],
            command_key='6'*64, actor=ACTOR, reason='fixture restart')
        return original(*args, **kwargs)
    monkeypatch.setattr(pilot.worker.engine, 'plan', changed)
    try: stage(pilot, 527)
    except Refused: pass
    assert seen == [] and world.fans.posts == []


def test_old_unprojected_inbound_is_visible_after_reactivation_without_a_send(supervised, monkeypatch):
    world, env, model, _, pilot = supervised
    original = pilot.worker.intake._project
    def crash(*args, **kwargs): raise RuntimeError('synthetic crash before projection')
    monkeypatch.setattr(pilot.worker.intake, '_project', crash)
    with pytest.raises(RuntimeError): stage(pilot, 528, key='old-inbound')
    monkeypatch.setattr(pilot.worker.intake, '_project', original)
    state = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped', expected_revision=state['revision'],
        command_key='7'*64, actor=ACTOR, reason='fixture stop')
    pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'active', expected_revision=stopped['revision'],
        command_key='8'*64, actor=ACTOR, reason='fixture restart')
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    restarted.tick()
    assert any(item['kind'] == 'pre_activation_webhook' for item in unresolved(world.store, TENANT, ACCOUNT, ACTOR))
    assert world.fans.posts == []


def test_recovery_cursor_does_not_starve_a_different_fan(supervised, monkeypatch):
    world, env, model, _, _ = supervised
    def fake(request):
        if request.method == 'POST' and '/chats/'+str(FAN)+'/messages' in request.url.path:
            world.fans.posts.append({'path':request.url.path})
            return answer(503, {'error':'ambiguous'})
        return world.fans.handle(request)
    pilot = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=httpx.MockTransport(fake))
    draft = stage(pilot, 530, fan=FAN); approve(world, draft)
    assert pilot.tick()['dispatched'] == ['unknown']
    for mid in range(531, 537): stage(pilot, mid, fan=FAN)
    original = pilot.worker.engine.plan
    def crash(*args, **kwargs): raise RuntimeError('synthetic crash before fan-two plan')
    monkeypatch.setattr(pilot.worker.engine, 'plan', crash)
    with pytest.raises(RuntimeError): stage(pilot, 540, fan=FAN2)
    monkeypatch.setattr(pilot.worker.engine, 'plan', original)
    for _ in range(6): pilot.tick(limit=2)
    with Session(world.database[1]) as s:
        pending = list(s.scalars(select(OfEngineApproval).where(OfEngineApproval.fan_id == FAN2)))
        assert len(pending) == 1 and pending[0].status == 'awaiting'
    assert len(world.fans.posts) == 1
