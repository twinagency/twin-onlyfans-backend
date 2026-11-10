"""Expected-behavior challenges of the continuation base; external services are scripted."""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine.journey import signals
from db.models import VaultItem
from db.onlyfans_engine_models import OfEngineDecision, OfEngineModelCall, OfEngineOfferGrant, OfFansWebhook
from fans_api_adapter.backfill import FansBackfill
from model_transport.pilot_supervised import build_supervised_pilot
from plan_c_worker import create_app
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, answer, inbound, FAN2
from test_plan_c_draft_app_pg import configured_draft, signed
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_sales_journey_pg import install_coverage, cards, prepare_scoped_history
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT


def normal_pilot(world, env, *, selecting=True, on_call=None):
    install_coverage(world)
    # Complete history through the existing stopped, budget-authorized, guarded backfill (never a seeded row).
    prepared = prepare_scoped_history(world, chats=(FAN, FAN2), pages_budget=4)
    assert prepared['status'] == 'complete', prepared
    def provider(request):
        if request.method == 'GET':
            if '/transactions' in request.url.path:
                return answer(200, {'data': {'list': [], 'hasMore': False}})
            if '/chargebacks' in request.url.path:
                return answer(200, {'data': {'list': []}})
            if request.url.path.endswith('/messages'):
                return answer(200, {'data': [], '_pagination': {'next_page': None}})
        return world.fans.handle(request)
    seen, controls = [], {'offer': selecting}
    def model(request):
        data = json.loads(request.content);seen.append(data)
        prompt = data['messages'][-1]['content']
        latest = prompt.split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0]
        approved = re.search(r'APPROVED OFFER: id ([0-9a-f-]{36}),', prompt)
        if latest == 'no thanks':
            proposal = {'body': 'Of course, we can just chat.', 'sales_signal': 'decline'}
        elif controls['offer'] and approved:
            proposal = {'body': 'Here is the photo.', 'action': 'offer', 'offer_grant_id': approved[1], 'pitch': True}
        else:
            proposal = {'body': 'How was your day?'}
        if on_call: on_call()
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [
            {'message': {'content': json.dumps(proposal)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})
    transport = httpx.MockTransport(model)
    pilot = build_supervised_pilot(world.store, env, model_transport=transport, fans_transport=httpx.MockTransport(provider))
    return pilot, seen, controls, transport


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_http_ack_does_not_wait_for_a_blocked_model(supervised):
    world, env, _, _, _ = supervised
    entered, release = threading.Event(), threading.Event()
    def blocked_model():
        entered.set();assert release.wait(8), 'test cleanup did not release scripted external model'
    pilot, seen, _, _ = normal_pilot(world, env, selecting=False, on_call=blocked_model)
    raw, headers = signed(inbound(18100, 'hello there'), key='ack-before-model')
    delayed = False
    with TestClient(create_app(pilot, run_loop=False)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, '/onlyfans/webhooks/plan-c', content=raw, headers=headers)
        try:
            response = future.result(timeout=1.5)
        except TimeoutError:
            delayed = True
        finally:
            release.set()
        response = future.result(timeout=10)
    assert not delayed, 'HTTP acknowledgement waited for expensive model generation'
    assert response.status_code == 202 and not entered.is_set() and seen == []
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key == 'ack-before-model')) is not None
    pilot.tick()
    assert len(world.fans.posts) == 1 and len(seen) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_unrelated_catalogue_change_preserves_photo_permission(supervised):
    world, env, _, _, _ = supervised
    pilot, seen, _, _ = normal_pilot(world, env)
    world.second_item()
    stage(pilot, 18110, 'got any pics?');pilot.tick()
    assert 'APPROVED OFFER: id ' in seen[-1]['messages'][-1]['content']
    assert len(cards(world)) == 1 and cards(world)[0]['body']['price'] == 3.0


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_stale_real_media_provenance_is_not_exposed_as_an_available_grant(supervised):
    world, env, _, _, _ = supervised
    pilot, seen, control, _ = normal_pilot(world, env, selecting=False)
    stage(pilot, 18120, 'hello')
    with Session(world.database[1]) as s, s.begin():
        assert len(list(s.scalars(select(OfEngineOfferGrant)))) == 1
        s.get(VaultItem, (ACCOUNT, 11)).content_hash_kind = 'synthetic-file-v1'
    control['offer'] = True
    stage(pilot, 18121, 'Tell me something interesting')
    assert 'APPROVED OFFER: none this turn' in seen[-1]['messages'][-1]['content']
    pilot.tick()
    assert cards(world) == [] and len(world.fans.posts) == 1, 'commercial unavailability must still allow plain chat'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_unchanged_held_offer_does_not_repeat_model_generations(supervised):
    world, env, _, _, _ = supervised
    pilot, seen, control, _ = normal_pilot(world, env, selecting=False)
    stage(pilot, 18130, 'hello')
    with Session(world.database[1]) as s, s.begin():
        s.get(VaultItem, (ACCOUNT, 11)).content_hash_kind = 'synthetic-file-v1'
    control['offer'] = True
    stage(pilot, 18131, 'got any pics?')
    before = len(seen)
    for _ in range(8): pilot.tick()
    assert len(seen) == before, 'unchanged unavailable evidence repeatedly consumed model work'
    assert cards(world) == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_free_photo_request_is_not_consent_to_a_paid_card(supervised):
    world, env, _, _, _ = supervised
    pilot, seen, _, _ = normal_pilot(world, env)
    stage(pilot, 18140, 'Send me a free photo');pilot.tick()
    assert cards(world) == [], 'a free request was silently converted to the paid photo'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_price_question_can_be_answered_without_a_compulsory_sale(supervised):
    world, env, _, _, _ = supervised
    pilot, _, _, _ = normal_pilot(world, env, selecting=False)
    stage(pilot, 18141, 'How much for the photo?');pilot.tick()
    assert len(world.fans.posts) == 1 and cards(world) == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('text', ['Show me a photo', 'Can you send me the pic?', 'Actually, I want that photo', 'I want to buy the photo 🙏'])
def test_genuine_new_photo_request_reopens_that_turn(supervised, text):
    world, env, _, _, _ = supervised
    pilot, seen, _, _ = normal_pilot(world, env)
    stage(pilot, 18150, 'no thanks');pilot.tick();world.no_throttle()
    stage(pilot, 18151, text);pilot.tick()
    assert len(cards(world)) == 1, (text, seen[-1]['messages'][-1]['content'])


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_http_intake_rejects_forgery_and_persistence_failure(supervised, monkeypatch):
    world, _, _, seen, pilot = supervised
    raw, headers = signed(inbound(18160, 'hello'), key='persist-failure')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw,
            headers={**headers, 'Signature': '0' * 64}).status_code == 401
        def unavailable(*args, **kwargs):
            raise RuntimeError('offline persistence failure')
        monkeypatch.setattr(pilot.worker.intake, '_resolve', unavailable)
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 503
    with Session(world.database[1]) as s:
        assert list(s.scalars(select(OfFansWebhook))) == []
    assert seen == [] and world.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('outside_first', [False, True])
def test_http_intake_preserves_cross_path_conflicts(supervised, outside_first):
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
    from conversation_engine.pilot_scope import delivery_block
    world, _, _, seen, pilot = supervised
    fans = [999, FAN] if outside_first else [FAN, 999]
    with TestClient(create_app(pilot, run_loop=False)) as client:
        for i, fan in enumerate(fans):
            raw, headers = signed(inbound(18170 + i, 'hello', fan=fan), key='cross-path')
            assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
            assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    with Session(world.database[1]) as s:
        assert len(list(s.scalars(select(OfFansWebhook)))) == 1
        assert len(list(s.scalars(select(OfEnginePilotWebhookDigest)))) == 1
        runtime = world.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert delivery_block(s, runtime, FAN)
    pilot.tick()
    assert seen == [] and world.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('kind', ['stop', 'human', 'money'])
def test_http_receipt_barrier_survives_deferred_projection(supervised, monkeypatch, kind):
    from test_engine_real_worker_pg import tx, iso
    from conversation_engine.pilot_scope import delivery_block
    from db.onlyfans_fans_intake_models import OfFansProjection
    world, _, _, seen, pilot = supervised
    before = stage(pilot, 18180, 'hello')
    original = pilot.worker.intake._project
    monkeypatch.setattr(pilot.worker.intake, '_project', lambda *args: 'pending')
    event = inbound(18181, 'stop messaging me') if kind == 'stop' else (
        {'event': 'messages.sent', 'account_id': 'acct_real_worker', 'payload': {
            'id': 18181, 'toUser': {'id': FAN}, 'text': 'operator reply', 'createdAt': iso(),
            'isSentByMe': True, 'media': [], 'price': 0}} if kind == 'human' else tx('pending-money', amount=3))
    raw, headers = signed(event, key='receipt-barrier')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansProjection).where(OfFansProjection.event_key == 'receipt-barrier')).status == 'pending'
        runtime = world.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert delivery_block(s, runtime, FAN, commercial=(kind == 'money')) == 'pilot_evidence_pending'
    if kind != 'money':
        pilot.worker.send_approved(TENANT, ACCOUNT, before['decisionId'])
        assert world.fans.posts == []
    assert len(seen) == 1
    monkeypatch.setattr(pilot.worker.intake, '_project', original)


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_http_stop_then_greeting_after_dispatch_grant_prevents_wire(supervised, monkeypatch):
    from onlyfans_runtime.text_delivery import TextAuthority
    from onlyfans_runtime.store import Refused
    world, _, _, _, pilot = supervised
    staged = stage(pilot, 18190, 'hello')
    from conversation_engine import approval
    info = staged['approval']
    approval.authorize_automatic(world.store, TENANT, ACCOUNT, info['requestId'], info['generation'],
        expected_version=info['version'])
    original = TextAuthority.consume_dispatch
    reached = []
    with TestClient(create_app(pilot, run_loop=False)) as client:
        def interleaved(self, tenant, grant, **kwargs):
            if not reached:
                reached.append(grant)
                for mid, body in [(18191, 'stop messaging me'), (18192, 'hello again')]:
                    raw, headers = signed(inbound(mid, body), key='stop-at-wire-' + str(mid))
                    assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
            return original(self, tenant, grant, **kwargs)
        monkeypatch.setattr(TextAuthority, 'consume_dispatch', interleaved)
        try:
            pilot.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
        except Refused:
            pass
    assert reached and world.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_coverage_expiring_during_generation_does_not_regenerate_unchanged_hold(supervised, monkeypatch):
    from datetime import timedelta
    from conversation_engine import commerce, pilot_scope
    world, env, _, _, _ = supervised
    expiry = {}
    def expire_after_prompt():
        monkeypatch.setattr(commerce, 'utcnow', lambda: expiry['value'])
        monkeypatch.setattr(pilot_scope, 'utcnow', lambda: expiry['value'])
    pilot, seen, _, _ = normal_pilot(world, env, on_call=expire_after_prompt)
    raw = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']['offer_coverage']
    expiry['value'] = datetime.fromisoformat(raw['valid_until']) + timedelta(seconds=1)
    outcome = stage(pilot, 18240, 'got any pics?')
    assert outcome['outcome'] == 'held', outcome
    assert len(seen) == 1
    for _ in range(8):pilot.tick()
    assert len(seen) == 1, 'the exact unchanged expired-evidence hold consumed repeated generations'
    assert cards(world) == []
