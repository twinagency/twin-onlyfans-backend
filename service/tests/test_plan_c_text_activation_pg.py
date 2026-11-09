"""Stopped-installation pilot activation and text-only boundaries; offline providers."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import Fan, OfConversationState
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfDeliveryPolicyChange
from db.onlyfans_engine_models import OfFansConnection
from db.onlyfans_fans_intake_models import OfFansLink
from db.onlyfans_models import OfRecovery, OfRuntime
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.launch_operations import account_state, control_account
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from test_engine_real_worker_pg import world, FAN2, PROVIDER, CREATOR
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft, signed, SECRET
from test_plan_c_scope_pg import ACTOR, OTHER, config_for, install, key
from test_plan_c_supervised_pg import stage


@pytest.fixture
def stopped_text(configured_draft, tmp_path, monkeypatch):
    w, env, model, seen = configured_draft
    cfg = config_for(w, env, fans=[FAN, FAN2], requests=100, credits=200)
    cfg['authorization_mode'] = 'autonomous'
    cfg['delivery_mode'] = 'text_only'
    with Session(w.database[1]) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN2))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=FAN2, mode='automated',
                                  revision=0, takeover_state='none', requires_attention=False))
        runtime = w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        fingerprint = account_state(s, runtime)['fingerprint']
    # Transition the fixture to the actual installation state through the real
    # stopped control, then configure its disabled, zero-limit policy.
    control_account(w.store, TENANT, ACCOUNT, action='emergency_stop', command_key=key(31001),
                    actor=ACTOR, reason='stopped installation', expected_fingerprint=fingerprint)
    with Session(w.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        revision = policy.revision
    w.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False,
        limit_units=0, throttle_seconds=cfg['limits']['throttle_seconds'], media_enabled=False,
        transport_kind='fansapi', actor=ACTOR, reason='stopped installation')
    # Signed same-account event establishes the same provider-link owner as live intake.
    raw, headers = signed({'event':'accounts.connected', 'account_id':PROVIDER, 'payload':{
        'onlyfans_id':CREATOR, 'onlyfans_username':'fixture', 'state':'authenticated'}}, key='connected-setup')
    assert w.intake.accept(raw, headers['Signature'], SECRET, 'connected-setup')['projection'] == 'projected'
    for fan_id in (FAN, FAN2):
        w.store.need_recovery(TENANT, ACCOUNT, fan_id)
        claim = w.store.claim_recovery(TENANT, ACCOUNT)
        assert claim and claim.fan_id == fan_id
        assert w.store.recovery_page(TENANT, claim, [], has_more=False) == 'complete'
    monkeypatch.setenv('ONLYFANS_FAN_CURRENT_OVERRIDE', f'{ACCOUNT}:{FAN},{ACCOUNT}:{FAN2}')
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')
    creds = tmp_path / 'fans-creds'
    creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=' + PROVIDER + '\n')
    creds.chmod(0o600)
    env = {**env, 'TWIN_PLAN_C_PRICE_CENTS':'300', 'TWIN_PLAN_C_MEDIA_IDS':str(cfg['product']['media_ids'][0]),
        'TWIN_PLAN_C_PREVIEW_IDS':'', 'TWIN_PLAN_C_DRAFT_ONLY':'0', 'TWIN_PLAN_C_SUPERVISED':'1',
        'ONLYFANS_PROCESS_ROLE':'worker', 'TWIN_ENGINE_FANSAPI_REAL':'1', 'TWIN_ENGINE_FANSAPI_ENV_FILE':str(creds)}
    first = install(w, cfg)
    with Session(w.database[1]) as s:
        runtime=s.get(OfRuntime, ACCOUNT)
        policy=s.get(OfDeliveryPolicy, ACCOUNT)
        assert not runtime.enabled and not policy.enabled and policy.limit_units == 0
        assert not policy.media_enabled and policy.transport_kind == 'fansapi'
        assert s.get(OfProviderConnection, ACCOUNT) is None
        assert all(s.get(OfRecovery, (ACCOUNT, fan)).status == 'complete' for fan in (FAN,FAN2))
    return w, env, model, seen, first


def start(w, revision=1, actor=ACTOR, command=31002):
    return pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'active', expected_revision=revision,
                                  command_key=key(command), actor=actor, reason='explicit text-only activation')


def test_stopped_real_path_activates_once_and_sends_one_mock_text(stopped_text):
    w, env, model, seen, first = stopped_text
    active = start(w, first['revision'])
    assert active['status'] == 'active'
    assert start(w, first['revision'])['replayed']
    with Session(w.database[1]) as s:
        policy=s.get(OfDeliveryPolicy, ACCOUNT)
        assert s.get(OfRuntime,ACCOUNT).enabled and policy.enabled and not policy.media_enabled
        assert policy.limit_units > 0 and policy.daily_limit_units > 0
        assert s.get(OfProviderConnection,ACCOUNT) is None
        audit=s.get(OfLaunchCommand,(ACCOUNT,key(31002)))
        proof=json.loads(audit.before_json)['textActivation']
        assert proof['connectionEpoch']==1 and proof['providerAccountId']==PROVIDER
        assert {x['fan'] for x in proof['fanRecovery']}=={FAN,FAN2}
        assert proof['policyRevisionAfter']==proof['policyRevisionBefore']+1
    pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
    stage(pilot, 31003, 'hello')
    assert pilot.tick()['dispatched'] == ['accepted']
    assert len(w.fans.posts)==1 and not w.fans.posts[0]['body'].get('price',0)


def test_text_only_blocks_commercial_at_admission_and_final_media_boundary(stopped_text):
    w, _, _, _, first = stopped_text
    start(w, first['revision'])
    with Session(w.database[1]) as s:
        runtime=s.get(OfRuntime,ACCOUNT); policy=s.get(OfDeliveryPolicy,ACCOUNT)
        state=s.get(OfConversationState,(ACCOUNT,FAN)); fan=s.get(Fan,(ACCOUNT,FAN))
        assert w.authority._eligibility(s,runtime,policy,state,fan,'automated',utcnow(),
                                        action='commercial',engine_action=True) == 'media_delivery_disabled'
        assert pilot_scope.delivery_block(s,runtime,FAN,commercial=True) == 'pilot_text_only'
        assert not w.authority._media_current(s,SimpleNamespace(account_id=ACCOUNT,catalog_item_id=w.item),policy,utcnow())
        revision=policy.revision
    with pytest.raises(Refused, match='text-only'):
        w.authority.configure(TENANT,ACCOUNT,expected_revision=revision,enabled=True,limit_units=1,
            throttle_seconds=10,media_enabled=True,transport_kind='fansapi',actor=ACTOR,reason='no media')
    with pytest.raises(Refused, match='text-only'):
        w.authority.configure(TENANT,ACCOUNT,expected_revision=revision,enabled=False,limit_units=1,
            throttle_seconds=10,media_enabled=True,transport_kind='fansapi',actor=ACTOR,reason='no media')
    assert not w.fans.posts


@pytest.mark.parametrize('problem', ['actor','revision','link','recovery','control','budget'])
def test_activation_refuses_wrong_scope_or_holds_atomically(stopped_text, problem):
    w, _, _, _, first=stopped_text
    if problem in ('link','recovery','control','budget'):
        with Session(w.database[1]) as s, s.begin():
            if problem == 'link': s.get(OfFansLink,(ACCOUNT,CREATOR)).send_blocked=True
            if problem == 'recovery': s.get(OfRecovery,(ACCOUNT,FAN2)).status='needed'
            if problem == 'control': s.get(OfConversationState,(ACCOUNT,FAN2)).mode='no_contact'
            if problem == 'budget':
                row,_=pilot_scope.require(s,s.get(OfRuntime,ACCOUNT))
                row.provider_requests_used=100
    with pytest.raises(Refused):
        start(w, first['revision']+1 if problem=='revision' else first['revision'],
              actor=OTHER if problem=='actor' else ACTOR)
    with Session(w.database[1]) as s:
        assert not s.get(OfRuntime,ACCOUNT).enabled and not s.get(OfDeliveryPolicy,ACCOUNT).enabled
    assert not w.fans.posts


def test_failed_policy_step_rolls_back_runtime_scope_and_audit(stopped_text, monkeypatch):
    w, _, _, _, first=stopped_text
    original=TextAuthority._configure_in_session
    def fail_after_policy(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise Refused('injected activation failure')
    monkeypatch.setattr(TextAuthority,'_configure_in_session',fail_after_policy)
    with Session(w.database[1]) as s:
        revision=s.get(OfDeliveryPolicy,ACCOUNT).revision
        before=len(list(s.scalars(select(OfDeliveryPolicyChange))))
    with pytest.raises(Refused,match='injected'):
        start(w,first['revision'])
    with Session(w.database[1]) as s:
        assert not s.get(OfRuntime,ACCOUNT).enabled
        policy=s.get(OfDeliveryPolicy,ACCOUNT)
        assert not policy.enabled and policy.limit_units==0 and policy.revision==revision
        assert len(list(s.scalars(select(OfDeliveryPolicyChange))))==before
    assert pilot_scope.describe(w.store,TENANT,ACCOUNT)['revision']==first['revision']


def test_takeover_and_contact_stop_prevent_later_fan_send(stopped_text):
    w, env, model, seen, first=stopped_text
    start(w, first['revision'])
    pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
    with Session(w.database[1]) as s:
        revision=s.get(OfConversationState,(ACCOUNT,FAN)).revision
    w.store.control(TENANT,ACCOUNT,FAN,mode='human_controlled',expected_revision=revision,
        command_key='text-activation-takeover',actor=ACTOR,reason='fixture takeover',action='request_takeover')
    stage(pilot, 31004, 'hello')
    pilot.tick()
    assert not w.fans.posts
    # Other fan still has normal text service, then an explicit stop and later
    # greeting must block both generation and provider wire.
    stage(pilot, 31005, 'stop messaging me', fan=FAN2)
    stage(pilot, 31006, 'hello again', fan=FAN2)
    before=len(seen)
    pilot.tick()
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN2)).mode == 'no_contact'
    assert len(seen)==before and not w.fans.posts


def test_concurrent_replay_has_one_activation_and_one_policy_audit(stopped_text):
    from concurrent.futures import ThreadPoolExecutor
    w, _, _, _, first=stopped_text
    with Session(w.database[1]) as s:
        before=len(list(s.scalars(select(OfDeliveryPolicyChange))))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: start(w,first['revision']),range(2)))
    assert sorted(item['replayed'] for item in results)==[False,True]
    assert results[0]['revision']==results[1]['revision']
    with Session(w.database[1]) as s:
        assert len(list(s.scalars(select(OfDeliveryPolicyChange))))==before+1


def test_legacy_config_is_not_rewritten_with_a_new_default(configured_draft):
    w, env, _, _=configured_draft
    cfg=config_for(w,env)
    assert 'delivery_mode' not in cfg
    first=install(w,cfg)
    read=pilot_scope.describe(w.store,TENANT,ACCOUNT)
    assert 'delivery_mode' not in read['config']
    assert read['fingerprint']==first['fingerprint']
    assert install(w,cfg)['replayed']


def test_activation_retains_lower_existing_limits_and_throttle(stopped_text):
    w, _, _, _, first=stopped_text
    with Session(w.database[1]) as s, s.begin():
        policy=s.get(OfDeliveryPolicy,ACCOUNT)
        # Fixture establishes existing stricter limits, not send authority.
        policy.limit_units=3
        policy.daily_limit_units=2
        policy.throttle_seconds=max(60,policy.throttle_seconds)
        previous_throttle=policy.throttle_seconds
    start(w,first['revision'])
    with Session(w.database[1]) as s:
        policy=s.get(OfDeliveryPolicy,ACCOUNT)
        assert (policy.limit_units,policy.daily_limit_units,policy.throttle_seconds)==(3,2,previous_throttle)


def test_zero_existing_daily_limit_remains_stopped(stopped_text):
    w, _, _, _, first=stopped_text
    with Session(w.database[1]) as s, s.begin():
        s.get(OfDeliveryPolicy,ACCOUNT).daily_limit_units=0
    with pytest.raises(Refused,match='allowance'):
        start(w,first['revision'])
    with Session(w.database[1]) as s:
        policy=s.get(OfDeliveryPolicy,ACCOUNT)
        assert not policy.enabled and policy.daily_limit_units==0
