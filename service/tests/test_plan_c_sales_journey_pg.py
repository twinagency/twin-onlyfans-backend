"""Joined sales journey through the actual runtime and ordinary signed application entry point."""
from datetime import datetime, timedelta
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine.commerce import RealCatalogueCoverage, approve_catalogue_offer
from conversation_engine import pilot_scope
from conversation_engine.catalogue import vault_coverage
from conversation_engine.offers import match
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineOfferGrant, OfEngineOwner, OfEngineRequest
from db.onlyfans_delivery_models import OfDeliveryPolicy
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused, utcnow
from plan_c_worker import create_app
from test_onlyfans_foundation_pg import database
from test_plan_c_draft_app_pg import configured_draft, signed
from test_plan_c_supervised_pg import supervised, stage
from test_engine_real_worker_pg import world, inbound, unlock
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_scope_pg import ACTOR, activate, key
from test_engine_real_worker_pg import PROVIDER, CREATOR, FAN2, answer
from fans_api_adapter.backfill import FansBackfill
from fans_api_adapter.real_transport import RealFansConfig, build_client


def install_coverage(world, *, seconds=1800):
    """Legitimate setup through public policy APIs; never seeds a per-fan grant."""
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped',
        expected_revision=before['revision'], command_key='a1' * 32, actor=ACTOR, reason='offline coverage setup')
    with Session(world.database[1]) as session:
        policy = session.get(OfDeliveryPolicy, ACCOUNT)
        revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
    world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False,
        limit_units=limit, throttle_seconds=throttle, actor=ACTOR, reason='offline coverage setup')
    config = before['config']
    coverage = pilot_scope.offer_coverage_snapshot(world.store, TENANT, ACCOUNT, product=config['product'],
        evidence_ref='vault:offline-setup', valid_until=utcnow() + timedelta(seconds=seconds))
    config['offer_coverage'] = coverage
    configured = pilot_scope.configure(world.store, TENANT, ACCOUNT, config,
        expected_revision=stopped['revision'], command_key='a2' * 32, actor=ACTOR, reason='offline standing coverage approval')
    activate(world, revision=configured['revision'], n=0xa3)


HISTORY_BASE = 'https://api.fansapi.com'


def stop_for_history(world, *, n=91001):
    """Existing stopped preparation: status stopped, delivery disabled, runtime disabled. Returns the stopped revision."""
    from db.onlyfans_models import OfRuntime
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped',
        expected_revision=before['revision'], command_key=key(n), actor=ACTOR, reason='offline history preparation')
    with Session(world.database[1]) as session:
        policy = session.get(OfDeliveryPolicy, ACCOUNT)
        revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
    world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False,
        limit_units=limit, throttle_seconds=throttle, actor=ACTOR, reason='offline history preparation')
    with Session(world.database[1]) as session, session.begin():
        session.get(OfRuntime, ACCOUNT).enabled = False
    return stopped['revision']


def history_client(world, revision, *, n=91002, start=None, request_limit=10, rows=None):
    """Guarded read-only provider client from the existing budgeted stopped-backfill authorization (fake provider only)."""
    guard = pilot_scope.authorize_stopped_backfill(world.store, TENANT, ACCOUNT, expected_revision=revision,
        actor=ACTOR, reason='offline history preparation', command_key=key(n),
        start_date=start or datetime(2015, 1, 1), request_limit=request_limit, base=HISTORY_BASE)
    calls = []
    def provider(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/accounts':
            return answer(200, [{'id': PROVIDER, 'onlyfans_id': CREATOR, 'is_authenticated': True}])
        if request.url.path.endswith('/transactions'):
            return answer(200, {'data': {'list': [], 'hasMore': False}})
        if request.url.path.endswith('/chargebacks'):
            return answer(200, {'data': {'list': []}})
        if request.url.path.endswith('/messages'):
            fan = int(request.url.path.split('/chats/')[1].split('/')[0])
            return answer(200, {'data': rows(fan) if rows else [], '_pagination': {'next_page': None}})
        return answer(404, {})
    client = build_client(RealFansConfig('fixture-token', PROVIDER, HISTORY_BASE, 5), creator_id=CREATOR,
        epoch=1, transport=httpx.MockTransport(provider), request_guard=guard)
    return client, calls


def resume_after_history(world, revision, *, n=91003):
    from db.onlyfans_models import OfRuntime
    with Session(world.database[1]) as session, session.begin():
        session.get(OfRuntime, ACCOUNT).enabled = True
    return activate(world, revision=revision, n=n)


def prepare_scoped_history(world, *, chats=(FAN, FAN2), start=None, pages_budget=6, rows=None):
    """Legitimate complete-history preparation for the sales journeys.

    Stopped scope + disabled delivery/runtime + the existing budgeted authorization, then the normal FansBackfill
    through the guarded fake provider; finally the normal activation. No backfill row, grant or purchase is seeded.
    The authorization binds the walk to exactly the scope's configured fans, so both are walked.
    """
    revision = stop_for_history(world)
    client, calls = history_client(world, revision, start=start, rows=rows)
    summary = FansBackfill(world.intake).run(TENANT, ACCOUNT, client, reason='operator',
        start_date=start or datetime(2015, 1, 1), pages_budget=pages_budget, history_chats=tuple(chats))
    assert summary['status'] == 'complete', summary
    assert all(method == 'GET' for method, _ in calls)
    resume_after_history(world, revision)
    return summary


def writer(world, env, *, on_call=None, choose=None):
    seen = []
    def respond(request):
        data = json.loads(request.content)
        seen.append(data)
        prompt = data['messages'][-1]['content']
        approved = re.search(r'APPROVED OFFER: id ([0-9a-f-]{36}),', prompt)
        latest = prompt.split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0]
        if latest == 'no thanks':
            proposal = {'body': 'Of course, we can just chat.', 'sales_signal': 'decline'}
        elif 'I want to buy the photo' in latest and approved:
            proposal = {'body': 'Here is the photo.', 'action': 'offer', 'offer_grant_id': approved[1], 'pitch': True}
        else:
            proposal = {'body': 'I hear you.' if 'buy the photo' in latest else 'Tell me more.'}
        if choose is not None:
            proposal = choose(latest, proposal)
        if on_call is not None:
            on_call(latest, prompt)
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [
            {'message': {'content': json.dumps(proposal)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [
                {'provider': 'Fixture Provider', 'selected': True}]}}})
    transport = httpx.MockTransport(respond)
    pilot = build_supervised_pilot(world.store, env, model_transport=transport, fans_transport=world.fans.transport)
    return pilot, seen, transport


def existing_grant(world, pilot):
    pilot.worker.engine.select_simulated(TENANT, ACCOUNT, FAN)
    coverage = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config'].get('offer_coverage')
    return approve_catalogue_offer(world.store, TENANT, ACCOUNT, FAN, world.item,
        RealCatalogueCoverage(ACCOUNT, FAN, world.item, coverage['evidence_ref'] if coverage else 'vault:offline-setup',
            datetime.fromisoformat(coverage['valid_until']) if coverage else utcnow() + timedelta(minutes=30)))


def cards(world):
    return [row for row in world.fans.posts if row['body'].get('price', 0) > 0]


def requests_for(world, mid):
    with Session(world.database[1]) as session:
        return list(session.scalars(select(OfEngineRequest).where(OfEngineRequest.source_message_id == mid)))


def archived_chat(pilot, start, count=21):
    """Stored provider history through the same signed intake, without generating replies to setup chatter."""
    for mid in range(start, start + count):
        raw, headers = signed(inbound(mid, 'An intervening ordinary message'), key='archive-' + str(mid))
        accepted = pilot.worker.intake.accept(raw, headers['Signature'], b'real-worker-secret',
            headers['X-OFAPI-Idempotency-Key'], expected_tenant=TENANT, expected_account=ACCOUNT)
        assert accepted['projection'] == 'projected'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_purchase_request_prepares_approved_photo_without_seeded_fan_grant(supervised):
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    pilot, seen, model = writer(world, env)
    with Session(world.database[1]) as session:
        assert list(session.scalars(select(OfEngineOfferGrant))) == []
    raw, headers = signed(inbound(1400, 'I want to buy the photo'), key='purchase-no-seeded-grant')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    pilot.tick()
    assert len(cards(world)) == 1, {'provider_posts': world.fans.posts, 'prompt': seen[-1]['messages'][-1]['content']}
    sent = cards(world)[0]['body']
    assert sent['price'] == 3.0 and sent['mediaFiles'] == ['11'] and sent['previews'] == []
    assert 'USD 3.00' in sent['text']
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    restarted.tick()
    assert len(cards(world)) == 1
    notification = unlock(7001, amount=3)
    for n in range(2):
        raw_unlock, unlock_headers = signed(notification, key='unlock-proof-' + str(n))
        restarted.handle(raw_unlock, unlock_headers['Signature'], unlock_headers['X-OFAPI-Idempotency-Key'])
    # A duplicate notification has the same event body; no second access or financial entry.
    with Session(world.database[1]) as session:
        access = list(session.scalars(select(OfEngineAccessEvidence)))
        assert len(access) == 1 and access[0].status == 'confirmed'
    assert [(r.status, r.next_step) for r in requests_for(world, 1400)] == [('completed', 'none')]


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('has_grant', [False, True])
def test_refusal_then_current_explicit_photo_request_completes_same_turn(supervised, has_grant):
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    pilot, seen, _ = writer(world, env)
    if has_grant:
        existing_grant(world, pilot)
    stage(pilot, 1410, 'no thanks')
    pilot.tick()
    world.no_throttle()
    with Session(world.database[1]) as session:
        assert session.get(OfEngineOwner, (ACCOUNT, FAN)).sales_mode == 'paused'
    stage(pilot, 1411, 'hello again')
    pilot.tick()
    world.no_throttle()
    with Session(world.database[1]) as session:
        assert session.get(OfEngineOwner, (ACCOUNT, FAN)).sales_mode == 'paused'
    assert cards(world) == []
    stage(pilot, 1412, 'I want to buy the photo')
    pilot.tick()
    assert len(cards(world)) == 1, {'provider_posts': world.fans.posts, 'prompt': seen[-1]['messages'][-1]['content']}
    with Session(world.database[1]) as session:
        owner = session.get(OfEngineOwner, (ACCOUNT, FAN))
        assert owner.sales_mode == 'reopened_scoped' and json.loads(owner.sales_scope_json) == [str(world.item)]
        requests = list(session.scalars(select(OfEngineRequest).where(OfEngineRequest.source_message_id == 1412)))
        assert len(requests) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_unclear_reference_retains_verified_existing_card_outside_recent_history(supervised):
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    pilot, seen, _ = writer(world, env)
    existing_grant(world, pilot)
    stage(pilot, 1420, 'I want to buy the photo')
    pilot.tick()
    assert len(cards(world)) == 1
    archived_chat(pilot, 1421)
    stage(pilot, 1442, 'What about the thing from before?')
    prompt = seen[-1]['messages'][-1]['content']
    assert '7001' not in prompt.split('\n\nLATEST:', 1)[0]
    with Session(world.database[1]) as session:
        runtime = world.store._runtime(session, TENANT, ACCOUNT)
        reference = match(session, runtime, FAN, 'What about the thing from before?')
        assert reference['state'] == 'unclear' and reference['offers'] == []
        assert len(list(session.scalars(select(OfEngineOfferGrant)))) == 1
    assert len(cards(world)) == 1
    assert '7001' in prompt and '300' in prompt, prompt
    assert 'access unconfirmed' in prompt and 'reference unclear' in prompt, prompt


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('change', ['missing_coverage', 'missing_history', 'removed_media', 'wrong_media', 'price', 'version'])
def test_preparation_denials_are_durable_and_do_not_call_the_model_or_provider(supervised, change):
    from db.models import CatalogItem, VaultItem
    world, env, _, _, _ = supervised
    if change != 'missing_coverage':
        install_coverage(world)
    if change != 'missing_history':
        prepare_scoped_history(world)
    with Session(world.database[1]) as session, session.begin():
        if change == 'removed_media': session.get(VaultItem, (ACCOUNT, 11)).removed_at = utcnow()
        if change == 'wrong_media': session.get(CatalogItem, world.item).media_ids = '[12]'
        if change == 'price': session.get(CatalogItem, world.item).price_cents = 400
        if change == 'version': session.get(VaultItem, (ACCOUNT, 11)).content_hash = 'changed-provider-version'
    pilot, seen, _ = writer(world, env)
    result = stage(pilot, 1500, 'I want to buy the photo')
    assert result['outcome'] == 'held', result
    assert seen == [] and world.fans.posts == []
    with Session(world.database[1]) as session:
        assert list(session.scalars(select(OfEngineOfferGrant))) == []
    held, = requests_for(world, 1500)
    assert held.status == 'open' and held.next_step == result['reason'] and held.next_step != 'prepare_reply'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_disallowed_fan_cannot_prepare_or_send(supervised):
    world, env, _, _, _ = supervised
    install_coverage(world)
    pilot, seen, _ = writer(world, env)
    result = stage(pilot, 1501, 'I want to buy the photo', fan=999)
    assert result['outcome'] == 'outside_pilot_scope'
    assert seen == [] and world.fans.posts == []
    with Session(world.database[1]) as session:
        assert list(session.scalars(select(OfEngineOfferGrant))) == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('body', ['hello again', 'What about that one?', 'I might buy the photo',
    'If I wanted to buy the photo, what would happen?', 'I said "I want to buy the photo" yesterday.',
    'I want to buy the photo if it is free', 'I want to buy the video'])
def test_noncurrent_or_ambiguous_interest_does_not_reopen_paused_sales(supervised, body):
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    pilot, _, _ = writer(world, env)
    existing_grant(world, pilot)
    stage(pilot, 1510, 'no thanks')
    pilot.tick()
    world.no_throttle()
    stage(pilot, 1511, body)
    pilot.tick()
    with Session(world.database[1]) as session:
        assert session.get(OfEngineOwner, (ACCOUNT, FAN)).sales_mode == 'paused'
    assert cards(world) == []
    assert requests_for(world, 1511)[0].status == 'completed'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_model_cannot_reopen_from_a_quoted_old_request(supervised):
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    pilot, _, _ = writer(world, env)
    grant = existing_grant(world, pilot)
    stage(pilot, 1520, 'no thanks')
    pilot.tick()
    world.no_throttle()
    malicious = lambda latest, original: {'body': 'Here is the photo.', 'action': 'offer',
        'offer_grant_id': grant, 'sales_signal': 'reopen', 'item_scope': [str(world.item)], 'pitch': True}
    pilot, seen, _ = writer(world, env, choose=malicious)
    result = stage(pilot, 1521, 'I said "I want to buy the photo" yesterday.')
    assert result['outcome'] == 'refused', result
    assert len(seen) == 2 and cards(world) == []
    with Session(world.database[1]) as session:
        assert session.get(OfEngineOwner, (ACCOUNT, FAN)).sales_mode == 'paused'
    assert requests_for(world, 1521)[0].status == 'handover_pending'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_two_workers_and_restart_converge_on_one_preparation_and_card(supervised):
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    first, _, transport = writer(world, env)
    second = build_supervised_pilot(world.store, env, model_transport=transport, fans_transport=world.fans.transport)
    raw, headers = signed(inbound(1530, 'I want to buy the photo'), key='parallel-preparation')
    def handle(pilot):
        return pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(handle, (first, second)))
    first.tick()
    second.tick()
    restarted = build_supervised_pilot(world.store, env, model_transport=transport, fans_transport=world.fans.transport)
    restarted.tick()
    assert len(cards(world)) == 1
    with Session(world.database[1]) as session:
        assert len(list(session.scalars(select(OfEngineOfferGrant)))) == 1
        assert len(list(session.scalars(select(OfEngineDecision)))) == 1
    assert len(requests_for(world, 1530)) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('boundary', ['after_grant', 'after_decision'])
def test_restart_recovers_original_purchase_request_without_new_authority(supervised, monkeypatch, boundary):
    from conversation_engine import approval
    world, env, _, _, _ = supervised
    install_coverage(world)
    prepare_scoped_history(world)
    pilot, seen, transport = writer(world, env)
    target, name = (pilot.worker.engine, 'plan') if boundary == 'after_grant' else (approval, 'create_draft_approval')
    original = getattr(target, name)
    def interrupted(*args, **kwargs): raise RuntimeError('offline crash at ' + boundary)
    monkeypatch.setattr(target, name, interrupted)
    raw, headers = signed(inbound(1540, 'I want to buy the photo'), key='interrupted-purchase')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    with pytest.raises(RuntimeError, match='offline crash at ' + boundary):
        pilot.tick()
    assert world.fans.posts == []
    with Session(world.database[1]) as session:
        assert len(list(session.scalars(select(OfEngineOfferGrant)))) == 1
        assert len(list(session.scalars(select(OfEngineDecision)))) == (0 if boundary == 'after_grant' else 1)
    monkeypatch.setattr(target, name, original)
    restarted = build_supervised_pilot(world.store, env, model_transport=transport, fans_transport=world.fans.transport)
    restarted.tick()
    restarted.tick()
    assert len(cards(world)) == 1 and len(seen) == 1
    with Session(world.database[1]) as session:
        assert len(list(session.scalars(select(OfEngineOfferGrant)))) == 1
        assert len(list(session.scalars(select(OfEngineDecision)))) == 1
    assert len(requests_for(world, 1540)) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('change', ['authorized', 'recent_start_only', 'binding_changed', 'epoch_retired'])
def test_prepared_history_opens_the_sales_gate_only_for_the_authorized_scope(supervised, change):
    """Negative controls for the shared preparation: it is real coverage or it opens nothing."""
    from conversation_engine.journey import backfill_complete
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_fans_intake_models import OfFansBackfill
    world, env, _, _, _ = supervised
    install_coverage(world)
    summary = prepare_scoped_history(world, start=datetime(2022, 1, 1) if change == 'recent_start_only' else None)
    assert summary['status'] == 'complete'
    with Session(world.database[1]) as session:
        run, = session.scalars(select(OfFansBackfill).where(OfFansBackfill.account_id == ACCOUNT))
        assert json.loads(run.marker)['pilot_coverage']['kind'] == 'provider_history_end.v1'
        assert not list(session.scalars(select(OfEngineOfferGrant)))
    with Session(world.database[1]) as session, session.begin():
        if change == 'binding_changed':
            from db.models import OfCreatorBinding
            from db.onlyfans_models import OfRuntime
            runtime = session.get(OfRuntime, ACCOUNT)
            session.get(OfCreatorBinding, runtime.binding_id).revoked_at = utcnow()
            session.flush()
            replacement = OfCreatorBinding(id='replacement-binding', tenant_id='new-tenant', creator_id='replacement-creator',
                creator_name='Replacement Creator', account_id=ACCOUNT, handle='replacement', channel='onlyfans')
            session.add(replacement); session.flush(); runtime.binding_id = replacement.id
        if change == 'epoch_retired': session.get(OfFansConnection, (ACCOUNT, 1)).status = 'retired'
    with Session(world.database[1]) as session:
        assert backfill_complete(session, ACCOUNT, FAN) is (change == 'authorized')
        assert backfill_complete(session, ACCOUNT, 999) is False
