"""Default-off application startup and signed draft webhook; all provider/model I/O is simulated."""
import hashlib
import hmac
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import approval
from conversation_engine.writer_context import set_active_profiles
from db.models import CatalogItem
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfFansWebhook
from model_transport.pilot_draft import build_draft_pilot
from onlyfans_runtime.store import Refused
from test_engine_real_worker_pg import world, inbound, SECRET  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT


@pytest.fixture
def configured_draft(world, tmp_path):
    with Session(world.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
        item = s.get(CatalogItem, world.item)
        media, previews, price = json.loads(item.media_ids), json.loads(item.preview_media_ids), item.price_cents
    world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False,
        limit_units=limit, throttle_seconds=throttle, actor='test-operator', reason='draft-only test')
    profiles = tmp_path / 'profiles.json'
    profiles.write_text(json.dumps({ACCOUNT: {'name':'Fixture creator', 'limits':['no meetups'],
        'offers_permitted':True, 'approved_item_ids':[str(world.item)]}}))
    secret = tmp_path / 'secret'
    secret.write_bytes(SECRET); secret.chmod(0o600)
    env = {'TWIN_PLAN_C_DRAFT_ONLY':'1', 'TWIN_PLAN_C_TENANT':TENANT, 'TWIN_PLAN_C_ACCOUNT':ACCOUNT,
        'TWIN_PLAN_C_FANS':str(FAN), 'TWIN_PLAN_C_ITEM_ID':str(world.item), 'TWIN_PLAN_C_PRICE_CENTS':str(price),
        'TWIN_PLAN_C_MEDIA_IDS':','.join(map(str, media)), 'TWIN_PLAN_C_PREVIEW_IDS':','.join(map(str, previews)),
        'TWIN_PLAN_C_WEBHOOK_SECRET_FILE':str(secret), 'ENGINE_MODEL_ENABLED':'1', 'ENGINE_MODEL_PRIMARY':'m/primary',
        'ENGINE_MODEL_UPSTREAM':'fixture-provider', 'ENGINE_MODEL_UPSTREAM_REPORTED_NAME':'Fixture Provider',
        'ENGINE_MODEL_ALLOWED':'m/primary', 'ENGINE_MODEL_ACCOUNTS':ACCOUNT, 'ENGINE_MODEL_KEY_ENV':'FIXTURE_KEY',
        'FIXTURE_KEY':'fixture-not-a-real-key', 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK':'1000000',
        'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK':'2000000', 'ENGINE_MODEL_DAILY_CAP_MICROUSD':'1000000',
        'ENGINE_MODEL_CREATOR_CAP_MICROUSD':'1000000', 'ENGINE_MODEL_PROFILES_FILE':str(profiles)}
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'model':'m/primary', 'choices':[{'message':{'content':json.dumps({'body':'hey you'})},
            'finish_reason':'stop'}], 'usage':{'prompt_tokens':100, 'completion_tokens':5, 'cost':0.0001},
            'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Provider', 'selected':True}]}}})
    transport = httpx.MockTransport(respond)
    yield world, env, transport, seen
    set_active_profiles(None)


def signed(body, key='fixture-event'):
    raw = json.dumps(body, separators=(',', ':')).encode()
    signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    return raw, {'Signature':signature, 'X-OFAPI-Idempotency-Key':key, 'Content-Type':'application/json'}


def test_draft_startup_is_default_off_and_scoped_before_model_calls(configured_draft):
    world, env, transport, seen = configured_draft
    assert build_draft_pilot(world.store, {}) is None
    assert build_draft_pilot(world.store, {**env, 'TWIN_PLAN_C_DRAFT_ONLY':'0'}) is None
    for change in ({'TWIN_PLAN_C_FANS':''}, {'TWIN_PLAN_C_FANS':str(FAN)+','+str(FAN)},
                   {'ENGINE_MODEL_ACCOUNTS':ACCOUNT+',other'}, {'ENGINE_MODEL_ALLOWED':'m/primary,m/other'},
                   {'ENGINE_MODEL_UPSTREAM':'', 'ENGINE_MODEL_UPSTREAM_REPORTED_NAME':''},
                   {'TWIN_PLAN_C_PRICE_CENTS':'999'}, {'TWIN_PLAN_C_MEDIA_IDS':'999'},
                   {'TWIN_PLAN_C_PREVIEW_IDS':'999'},
                   {'ENGINE_MODEL_FALLBACK_ENABLED':'1', 'ENGINE_MODEL_FALLBACK':'m/fallback',
                    'ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK':'1000000',
                    'ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK':'2000000'}):
        with pytest.raises((Refused, ValueError)):
            build_draft_pilot(world.store, {**env, **change}, model_transport=transport)
    assert seen == [] and world.fans.posts == []
    pilot = build_draft_pilot(world.store, env, model_transport=transport)
    assert pilot.worker.draft_only and 'fixture-not-a-real-key' not in repr(pilot)
    pilot.worker.transport.allowed_chats = frozenset({str(FAN), '102'})
    with pytest.raises(Refused):
        pilot.validate()
    pilot.worker.transport.allowed_chats = pilot.allowed_chats
    from dataclasses import replace
    pilot.worker.engine.model.config = replace(pilot.model_config, primary_model='m/other', allowed_models=('m/other',))
    with pytest.raises(Refused):
        pilot.validate()
    pilot.worker.engine.model.config = pilot.model_config
    from conversation_engine.journey import JourneyConfig, configure_journey
    configure_journey(JourneyConfig(temporary_pause_reopen_hours=12))
    try:
        with pytest.raises(Refused):
            pilot.validate()
    finally:
        configure_journey(JourneyConfig())
    assert seen == [] and world.fans.posts == []


def test_actual_app_signed_webhook_stages_only_and_survives_redelivery(configured_draft, monkeypatch):
    world, env, transport, seen = configured_draft
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('DATABASE_URL', world.database[0])
    module = importlib.import_module('onlyfans_app')
    import onlyfans_runtime.store as store_module
    import model_transport.pilot_draft as draft_module
    async def no_init(): pass
    async def no_dispose(): pass
    monkeypatch.setattr(module, 'init_db', no_init)
    from types import SimpleNamespace
    monkeypatch.setattr(module, 'engine', SimpleNamespace(dispose=no_dispose))
    class FixtureStore(store_module.Store):
        def __new__(cls, unused):
            return world.store
    monkeypatch.setattr(store_module, 'Store', FixtureStore)
    monkeypatch.setattr(draft_module, 'build_draft_pilot', lambda store: build_draft_pilot(store, env, model_transport=transport))
    import traceback
    failures = []
    original_handle = draft_module.DraftPilot.handle
    def capturing_handle(self, *args):
        try:
            return original_handle(self, *args)
        except Exception:
            failures.append(traceback.format_exc())
            raise
    monkeypatch.setattr(draft_module.DraftPilot, 'handle', capturing_handle)
    with TestClient(module.app) as client:
        raw, headers = signed(inbound(501, 'hello there'))
        pilot = module.app.state.plan_c_draft
        module.app.state.plan_c_draft = None
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw, headers=headers).status_code == 404
        module.app.state.plan_c_draft = pilot
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw, headers={**headers, 'Signature':'0'*64}).status_code == 401
        original_plan = pilot.worker.engine.plan
        def interrupted(*args, **kwargs):
            raise RuntimeError('simulated interruption after committed intake')
        monkeypatch.setattr(pilot.worker.engine, 'plan', interrupted)
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw, headers=headers).status_code == 503
        with Session(world.database[1]) as s:
            assert len(list(s.scalars(select(OfFansWebhook)))) == 1
            assert list(s.scalars(select(OfEngineDecision))) == []
        monkeypatch.setattr(pilot.worker.engine, 'plan', original_plan)
        failures.clear()
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw, headers=headers).status_code == 202, failures
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw, headers=headers).status_code == 202, failures
        with Session(world.database[1]) as s:
            decisions = list(s.scalars(select(OfEngineDecision)))
            staged = list(s.scalars(select(OfEngineApproval)))
            assert len(decisions) == len(staged) == 1 and staged[0].status == 'awaiting'
            assert len(list(s.scalars(select(OfFansWebhook)))) == 1
            assert list(s.scalars(select(OfTextIntent))) == []
            request, generation, version = staged[0].request_id, staged[0].generation, staged[0].version
            decision_id = decisions[0].decision_id
        assert len(seen) == 1 and seen[0]['model'] == 'm/primary' and world.fans.posts == []
        assert seen[0]['provider'] == {'only':['fixture-provider'], 'allow_fallbacks':False,
            'require_parameters':True, 'max_price':{'prompt':1.0, 'completion':2.0}}
        approval.approve(world.store, TENANT, ACCOUNT, request, generation,
                         expected_version=version, actor='op-session:'+'a'*64)
        assert module.app.state.plan_c_draft.worker.send_approved(TENANT, ACCOUNT, decision_id)['outcome'] == 'draft_only'
        # Another fresh worker remains no-send while the shared policy is disabled.
        restarted = build_draft_pilot(world.store, env, model_transport=transport)
        assert restarted.worker.send_approved(TENANT, ACCOUNT, decision_id)['outcome'] == 'draft_only'
        raw_scope, headers_scope = signed(inbound(502, 'outside the pilot', fan=102), key='outside-pilot')
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw_scope, headers=headers_scope).status_code == 202
        with Session(world.database[1]) as s:
            assert len(list(s.scalars(select(OfEngineDecision)))) == 1
        with Session(world.database[1]) as s, s.begin():
            s.get(CatalogItem, world.item).price_cents += 1
        raw_changed, headers_changed = signed(inbound(503, 'changed product'), key='changed-product')
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw_changed, headers=headers_changed).status_code == 409
        with Session(world.database[1]) as s, s.begin():
            s.get(CatalogItem, world.item).price_cents -= 1
        with Session(world.database[1]) as s:
            policy = s.get(OfDeliveryPolicy, ACCOUNT)
            revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
        world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=True,
            limit_units=limit, throttle_seconds=throttle, actor='test-operator', reason='simulate policy change')
        raw2, headers2 = signed(inbound(504, 'another message'), key='fixture-event-2')
        assert client.post('/onlyfans/webhooks/plan-c-draft', content=raw2, headers=headers2).status_code == 409
        with pytest.raises(Refused):
            build_draft_pilot(world.store, env, model_transport=transport)
        assert len(seen) == 1 and world.fans.posts == []
