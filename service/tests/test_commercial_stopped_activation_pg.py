"""Commercial restart must work from a real stopped runtime, without direct enable writes."""
from sqlalchemy.orm import Session
from commercial_worker_fixture import world, OPERATOR, key
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT
from conversation_engine import pilot_scope
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy
from test_commercial_scope_lifecycle_pg import recover
import pytest
from onlyfans_runtime.store import Refused

def test_full_commercial_scope_restarts_through_normal_audited_status_command(world):
    world.product('Pilot photo', [31], standard=300, description='One photo.')
    world.launch(authorization_mode='autonomous')
    recover(world.store,world.account,world.fans)
    scope=pilot_scope.describe(world.store,TENANT,world.account)
    stopped=pilot_scope.set_status(world.store,TENANT,world.account,'stopped',expected_revision=scope['revision'],command_key=key(0x99101),actor=OPERATOR,reason='Real stopped restart boundary')
    with Session(world.database[1]) as s:
        assert not s.get(OfRuntime,world.account).enabled
        assert not s.get(OfDeliveryPolicy,world.account).enabled
    active=pilot_scope.set_status(world.store,TENANT,world.account,'active',expected_revision=stopped['revision'],command_key=key(0x99102),actor=OPERATOR,reason='Explicit restart after verified preparations')
    assert active['status']=='active'
    with Session(world.database[1]) as s:
        assert s.get(OfRuntime,world.account).enabled
        policy=s.get(OfDeliveryPolicy,world.account)
        assert policy.enabled and policy.media_enabled

    replay=pilot_scope.set_status(world.store,TENANT,world.account,'active',expected_revision=stopped['revision'],command_key=key(0x99102),actor=OPERATOR,reason='Explicit restart after verified preparations')
    assert replay['replayed'] and replay['revision']==active['revision']

@pytest.mark.parametrize('block',['wrong_operator','missing_recovery','no_contact','expired_catalogue'])
def test_paid_stopped_activation_retains_existing_holds(world,block):
    world.product('Pilot photo', [31], standard=300, description='One photo.')
    world.launch(authorization_mode='autonomous')
    if block!='missing_recovery':recover(world.store,world.account,world.fans)
    scope=pilot_scope.describe(world.store,TENANT,world.account)
    stopped=pilot_scope.set_status(world.store,TENANT,world.account,'stopped',expected_revision=scope['revision'],command_key=key(0x99201),actor=OPERATOR,reason='Stopped boundary with a current hold')
    if block=='no_contact':
        from db.models import OfConversationState
        with Session(world.database[1]) as s:revision=s.get(OfConversationState,(world.account,min(world.fans))).revision
        world.store.control(TENANT,world.account,min(world.fans),mode='no_contact',expected_revision=revision,command_key=key(0x99203),actor=OPERATOR,reason='Fan explicit stop',action='mark_no_contact')
    elif block=='expired_catalogue':
        from datetime import timedelta
        from onlyfans_runtime.store import utcnow
        from db.onlyfans_engine_models import OfEnginePilotScope
        import json
        # An operator configuration records the expired approval; no DB guard is disabled.
        cfg=pilot_scope.describe(world.store,TENANT,world.account)['config']
        cfg['catalogue']['valid_until']=(utcnow()-timedelta(seconds=1)).isoformat()
        with pytest.raises(Refused):
            pilot_scope.configure(world.store,TENANT,world.account,cfg,expected_revision=stopped['revision'],command_key=key(0x99204),actor=OPERATOR,reason='Expired approval is refused')
        return
    actor=('op-session:'+'b'*64) if block=='wrong_operator' else OPERATOR
    with pytest.raises(Refused):
        pilot_scope.set_status(world.store,TENANT,world.account,'active',expected_revision=stopped['revision'],command_key=key(0x99202),actor=actor,reason='Held restart must remain refused')
    with Session(world.database[1]) as s:
        assert not s.get(OfRuntime,world.account).enabled and not s.get(OfDeliveryPolicy,world.account).enabled
