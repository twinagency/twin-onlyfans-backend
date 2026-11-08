"""Grant lifecycle journeys through normal preparation and explicit stopped policy APIs."""
from dataclasses import asdict
from datetime import timedelta
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from conversation_engine import commerce, pilot_scope
from conversation_engine.catalogue import vault_coverage
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineOfferGrant
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused, utcnow
from test_offline_completion_pg import normal_pilot
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_sales_journey_pg import cards, writer
from test_plan_c_scope_pg import ACTOR, activate
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('new_request', [True, False])
@pytest.mark.parametrize('noise', [pytest.param(False,id='no_noise'),pytest.param(True,id='duplicate_config_audits')])
def test_explicit_stopped_renewal_can_issue_one_usable_successor(supervised, monkeypatch, new_request, noise):
    world, env, _, _, _ = supervised
    pilot, seen, control, model = normal_pilot(world, env, selecting=False)
    stage(pilot, 18200, 'hello')
    with Session(world.database[1]) as s:
        old = s.scalar(select(OfEngineOfferGrant))
        assert old is not None and old.used_intent_id is None
        original = {key: getattr(old, key) for key in ('grant_id', 'terms_hash', 'coverage_ref', 'expires_at', 'used_intent_id')}
    future = original['expires_at'] + timedelta(seconds=1)
    monkeypatch.setattr(commerce, 'utcnow', lambda: future)
    monkeypatch.setattr(pilot_scope, 'utcnow', lambda: future)
    control['offer'] = True
    expired = stage(pilot, 18201, 'I want to buy the photo')
    assert expired['outcome'] == 'held' and expired['reason'] == 'offer_coverage_expired', expired
    pilot.tick()
    assert cards(world) == []
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped', expected_revision=before['revision'],
        command_key='b1' * 32, actor=ACTOR, reason='offline explicit permission renewal')
    with Session(world.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
    world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False, limit_units=limit,
        throttle_seconds=throttle, actor=ACTOR, reason='offline explicit permission renewal')
    cfg = before['config']
    fresh = asdict(vault_coverage(world.store, TENANT, ACCOUNT, complete=True,
        evidence_ref='vault:offline-explicit-renewal', valid_until=future + timedelta(minutes=30)))
    fresh['valid_until'] = fresh['valid_until'].isoformat()
    cfg['offer_coverage'] = fresh
    kw = dict(expected_revision=stopped['revision'], command_key='b2' * 32, actor=ACTOR,
        reason='Administrator explicitly renewed the current photo permission with fresh evidence')
    renewed = pilot_scope.configure(world.store, TENANT, ACCOUNT, cfg, **kw)
    assert pilot_scope.configure(world.store, TENANT, ACCOUNT, cfg, **kw)['replayed']
    if noise:
        for index in range(101):
            monkeypatch.setattr(pilot_scope,'utcnow',lambda index=index:future+timedelta(milliseconds=index+1))
            duplicate=pilot_scope.configure(world.store,TENANT,ACCOUNT,cfg,
                expected_revision=renewed['revision'],command_key=f'{0xb400+index:064x}',actor=ACTOR,
                reason='Repeated unchanged explicit configuration; creates no fresh permission')
            assert duplicate['revision']==renewed['revision']
        monkeypatch.setattr(pilot_scope,'utcnow',lambda:future)
    # Receipt and activation clocks remain real; only permission/grant expiry is advanced.
    monkeypatch.setattr(pilot_scope, 'utcnow', utcnow)
    activate(world, revision=renewed['revision'], n=0xb3)
    monkeypatch.setattr(pilot_scope, 'utcnow', lambda: future)
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    if new_request:
        stage(restarted, 18202, 'I want to buy the photo')
    for _ in range(4):restarted.tick()
    assert len(cards(world)) == 1, 'fresh approved coverage did not produce usable next-sale authority'
    with Session(world.database[1]) as s:
        rows = list(s.scalars(select(OfEngineOfferGrant).order_by(OfEngineOfferGrant.created_at)))
        assert len(rows) == 2
        saved = s.get(OfEngineOfferGrant, (ACCOUNT, original['grant_id']))
        assert {key: getattr(saved, key) for key in original} == original
        assert rows[1].used_intent_id is not None
    after = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    assert after['providerRequestsUsed'] >= before['providerRequestsUsed']
    assert after['providerCreditsReserved'] >= before['providerCreditsReserved']


def queue_photo(world, env, mid=18210):
    from conversation_engine import approval
    pilot, seen, controls, model = normal_pilot(world, env)
    staged = stage(pilot, mid, 'I want to buy the photo')
    info = staged['approval']
    approval.authorize_automatic(world.store, TENANT, ACCOUNT, info['requestId'], info['generation'],
        expected_version=info['version'])
    intent = pilot.worker.engine.queue(TENANT, ACCOUNT, staged['decisionId'])
    return pilot, seen, model, staged, intent


def correct_memory(world, key):
    from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
    memory = HistoricalMemory(world.store)
    memory.correct(TENANT, ACCOUNT, FAN, field='home_city', operation='set', value='Brisbane',
        expected_version=memory.read(TENANT, ACCOUNT, FAN)['version'], command_key=key,
        actor=ACTOR, reason='offline verified correction after paid card queue')


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_successor_persisted_before_planning_is_reused_by_parallel_workers_after_restart(supervised):
    from concurrent.futures import ThreadPoolExecutor
    from db.onlyfans_delivery_models import OfTextIntent
    world, env, _, _, _ = supervised
    pilot, seen, model, staged, intent = queue_photo(world, env)
    with Session(world.database[1]) as s:
        old = s.scalar(select(OfEngineOfferGrant))
        original = {key: getattr(old, key) for key in ('grant_id','terms_hash','coverage_ref','expires_at','used_intent_id','authorization_json')}
    # A queued action is not a retirement proof, despite not having a receipt.
    result = commerce.prepare_pilot_offer(world.store, TENANT, ACCOUNT, FAN, 18210, pilot.worker.engine)
    assert result['state'] == 'held'
    with Session(world.database[1]) as s:assert len(list(s.scalars(select(OfEngineOfferGrant)))) == 1
    correct_memory(world, 'lineage-correct-before-cancel')
    assert pilot.worker._cancel_queued(TENANT, ACCOUNT, intent, 'memory_changed')
    def prepare(_):
        return commerce.prepare_pilot_offer(world.store, TENANT, ACCOUNT, FAN, 18210, pilot.worker.engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared = list(pool.map(prepare, range(2)))
    assert all(row['state'] == 'prepared' for row in prepared), prepared
    assert len({row['grantId'] for row in prepared}) == 1
    # No model planning after successor persistence: recover only from disk.
    first = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    second = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda worker: worker.tick(), (first, second)))
    for _ in range(3):first.tick()
    assert len(cards(world)) == 1 and len(seen) == 2
    with Session(world.database[1]) as s:
        grants = list(s.scalars(select(OfEngineOfferGrant).order_by(OfEngineOfferGrant.issuance)))
        assert len(grants) == 2 and [g.issuance for g in grants] == [0,1]
        assert {key: getattr(grants[0], key) for key in original} == original
        assert grants[1].predecessor_grant_id == grants[0].grant_id
        assert grants[1].issuance_reason == 'cancelled_unsent'
        proof = __import__('json').loads(grants[1].authorization_json)
        assert proof['neverDispatched']['intentId'] == intent and proof['neverDispatched']['wireStartedAt'] is None
        assert grants[1].used_intent_id != intent
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'cancelled'
    assert world.authority.admit(TENANT, ACCOUNT, intent) == 'cancelled'


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_pre_wire_claim_is_fenced_before_successor_and_old_token_stays_dead(supervised):
    world, env, _, _, _ = supervised
    pilot, seen, model, staged, intent = queue_photo(world, env, 18220)
    grant = world.authority.admit(TENANT, ACCOUNT, intent)
    assert not isinstance(grant, str)
    # No successor while a claim can still reach wire.
    result = commerce.prepare_pilot_offer(world.store, TENANT, ACCOUNT, FAN, 18220, pilot.worker.engine)
    assert result['state'] == 'held'
    correct_memory(world, 'lineage-correct-after-claim')
    assert world.authority.consume_dispatch(TENANT, grant) is None
    result = commerce.prepare_pilot_offer(world.store, TENANT, ACCOUNT, FAN, 18220, pilot.worker.engine)
    assert result['state'] == 'prepared', result
    with pytest.raises(Refused):world.authority.consume_dispatch(TENANT, grant)
    with pytest.raises(Refused, match='attempt is already settled'):
        world.authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, None)
    assert world.authority.admit(TENANT, ACCOUNT, intent) == 'cancelled'
    assert world.fans.posts == []
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    for _ in range(4):restarted.tick()
    assert len(cards(world)) == 1 and len(seen) == 2


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('state', ['wire', 'unknown', 'accepted'])
def test_dispatched_or_uncertain_authority_never_gets_a_successor(supervised, state):
    world, env, _, _, _ = supervised
    pilot, seen, model, staged, intent = queue_photo(world, env, 18230)
    if state == 'accepted':
        assert pilot.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] == 'accepted'
    else:
        grant = world.authority.admit(TENANT, ACCOUNT, intent)
        wire = world.authority.consume_dispatch(TENANT, grant)
        assert wire is not None
        if state == 'unknown':world.authority.mark_unknown(TENANT, ACCOUNT, intent, grant.token)
    cfg = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']['offer_coverage']
    with pytest.raises(Refused):
        commerce.approve_catalogue_offer(world.store, TENANT, ACCOUNT, FAN, world.item,
            commerce.RealCatalogueCoverage(ACCOUNT, FAN, world.item, cfg['evidence_ref'],
                __import__('datetime').datetime.fromisoformat(cfg['valid_until'])))
    with Session(world.database[1]) as s:
        rows = list(s.scalars(select(OfEngineOfferGrant)))
        assert len(rows) == 1 and rows[0].used_intent_id == intent
