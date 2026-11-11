"""Durable Plan C scope and limits: isolated PostgreSQL and mock providers only."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from conversation_engine.model_config import ModelConfig
from db.models import OfCreatorBinding, CatalogItem
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEnginePilotScope
from db.schema import HEAD, check_schema
from onlyfans_runtime.store import Refused
from test_engine_real_worker_pg import world, FAN2  # noqa: F401
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT

ACTOR = 'op-session:' + 'a' * 64
OTHER = 'op-session:' + 'b' * 64
PERMISSIONS = ['configure','activate','approve','reject','edit','stop','takeover','handback','reconcile','correct','read']


def key(n):
    return f'{n:064x}'


def config_for(world, env, *, fans=None, requests=10, credits=20, per_request=2):
    model = ModelConfig.from_env(env)
    with Session(world.database[1]) as s, s.begin():
        item = s.get(CatalogItem, world.item)
        one_photo = json.loads(item.media_ids)[:1]
        item.media_ids, item.preview_media_ids, item.price_cents, item.kind = json.dumps(one_photo), '[]', 300, 'image'
        env['TWIN_PLAN_C_PRICE_CENTS'] = '300'
        env['TWIN_PLAN_C_MEDIA_IDS'] = ','.join(map(str, one_photo))
        env['TWIN_PLAN_C_PREVIEW_IDS'] = ''
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        policy.daily_limit_units = min(10, policy.limit_units)
        maximum, daily, throttle = policy.limit_units, policy.daily_limit_units, policy.throttle_seconds
    from conversation_engine.writer_context import JsonFileCreatorProfiles, profile_data
    profile = profile_data(JsonFileCreatorProfiles(env['ENGINE_MODEL_PROFILES_FILE']).get(ACCOUNT))
    return {'creator_profile':profile, 'authorization_mode':'supervised', 'qualification':{'mode':'fixture', 'reference':'offline fixture only'}, 'fans':fans or [FAN, FAN2], 'product':{'id':world.item, 'price_cents':int(env['TWIN_PLAN_C_PRICE_CENTS']),
            'media_ids':[int(i) for i in env['TWIN_PLAN_C_MEDIA_IDS'].split(',')],
            'preview_ids':[int(i) for i in env['TWIN_PLAN_C_PREVIEW_IDS'].split(',') if i]},
            'operator':{'actor':ACTOR, 'name':'Fixture operator', 'permissions':PERMISSIONS},
            'model':pilot_scope.model_spec(model),
            'limits':{'send_units':maximum, 'daily_send_units':daily, 'throttle_seconds':throttle,
                      'provider_requests':requests, 'provider_credit_budget':credits,
                      'provider_credits_per_request':per_request}}


def install(world, config, n=1):
    return pilot_scope.configure(world.store, TENANT, ACCOUNT, config, expected_revision=0, command_key=key(n),
                                 actor=ACTOR, reason='fixture configuration')


def activate(world, revision=1, n=2):
    import os
    os.environ['TWIN_PLAN_C_FIXTURE_ONLY'] = '1'
    with Session(world.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        current, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
    world.authority.configure(TENANT, ACCOUNT, expected_revision=current, enabled=True, limit_units=limit,
                              throttle_seconds=throttle, actor=ACTOR, reason='fixture only')
    return pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'active', expected_revision=revision,
                                  command_key=key(n), actor=ACTOR, reason='fixture activation')


def test_scope_schema_is_current_and_requires_complete_configuration(configured_draft):
    world, env, _, seen = configured_draft
    with world.database[1].begin() as conn:
        check_schema(conn)
        assert HEAD == '0129b_model_retry_due'
    cfg = config_for(world, env)
    for patch in ({'fans':[]}, {'operator':{'actor':OTHER, 'name':'', 'permissions':[]}},
                  {'product':{**cfg['product'], 'price_cents':1}}, {'limits':{**cfg['limits'], 'provider_requests':0}}):
        with pytest.raises(Refused):
            install(world, {**cfg, **patch})
    assert pilot_scope.describe(world.store, TENANT, ACCOUNT)['status'] == 'unconfigured'
    assert seen == [] and world.fans.posts == []


def test_scope_is_durable_idempotent_and_actor_permissions_fail_closed(configured_draft):
    world, env, _, _ = configured_draft
    cfg = config_for(world, env)
    first = install(world, cfg)
    assert (first['revision'], first['status'], first['replayed']) == (1, 'configured', False)
    assert install(world, cfg)['replayed'] is True
    with pytest.raises(Refused, match='command key conflict'):
        pilot_scope.configure(world.store, TENANT, ACCOUNT, cfg, expected_revision=0, command_key=key(1),
                              actor=ACTOR, reason='different request')
    with pytest.raises(Refused, match='operator permission'):
        pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped', expected_revision=1, command_key=key(3),
                               actor=OTHER, reason='fixture')
    actual = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    assert actual['config']['fans'] == [FAN, FAN2] and actual['config']['operator']['name'] == 'Fixture operator'
    with Session(world.database[1]) as s:
        assert s.get(OfEnginePilotScope, 1).fingerprint == first['fingerprint']


def test_active_scope_reservations_are_atomic_and_survive_stop_and_restart(configured_draft):
    world, env, _, _ = configured_draft
    cfg = config_for(world, env, requests=1, credits=2, per_request=2)
    install(world, cfg)
    active = activate(world)
    def reserve(_):
        try:
            return pilot_scope.reserve_provider_request(world.store, TENANT, ACCOUNT, expected_revision=active['revision'])
        except Refused:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sorted(results, key=lambda x: x is None) == [1, None]
    stopped = pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'stopped', expected_revision=active['revision'],
                                     command_key=key(4), actor=ACTOR, reason='fixture stop')
    with pytest.raises(Refused, match='not active'):
        pilot_scope.reserve_provider_request(world.store, TENANT, ACCOUNT, expected_revision=active['revision'])
    view = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    assert view['providerRequestsUsed'] == 1 and view['providerCreditsReserved'] == 2
    with pytest.raises(Refused, match='budget is exhausted'):
        pilot_scope.set_status(world.store, TENANT, ACCOUNT, 'active', expected_revision=stopped['revision'],
                               command_key=key(5), actor=ACTOR, reason='fixture restart')
    assert world.fans.posts == []


def test_scope_cannot_be_deleted_or_reset_by_accidental_database_update(configured_draft):
    world, env, _, _ = configured_draft
    install(world, config_for(world, env))
    active = activate(world)
    pilot_scope.reserve_provider_request(world.store, TENANT, ACCOUNT, expected_revision=active['revision'])
    with world.database[1].connect() as conn:
        trans = conn.begin()
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(text('UPDATE of_engine_pilot_scope SET provider_requests_used=0'))
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(text('DELETE FROM of_engine_pilot_scope'))
        trans.rollback()
    assert pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed'] == 1


def test_active_scope_rejects_an_existing_unguarded_worker(configured_draft):
    from fans_api_adapter.real_transport import FansApiRealTransport
    world, env, _, _ = configured_draft
    install(world, config_for(world, env))
    activate(world)
    with pytest.raises(Refused, match='durable pilot request guard'):
        FansApiRealTransport(world.store, TENANT, ACCOUNT, world.transport.client,
                             allowed_chats=frozenset({str(FAN)}), enabled=True)
    assert world.fans.posts == []
