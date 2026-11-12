"""Readiness uses existing controls and budgets, without stopping already admitted work."""
from sqlalchemy.orm import Session
from commercial_worker_fixture import world,OPERATOR,FAN_A,FAN_B
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.models import OfConversationState
from onlyfans_runtime.text_delivery import TextAuthority
from plan_c_worker import _work_counts

def test_readiness_excludes_no_contact_and_spent_budget(world):
 world.product("Beach photo",[31],standard=1200,description="One photo.")
 world.launch(authorization_mode="autonomous")
 before=_work_counts(world.pilot);assert before['eligibleFans']==len(world.fans)
 with Session(world.database[1]) as session:revision=session.get(OfConversationState,(world.account,FAN_A)).revision
 world.store.control(TENANT,world.account,FAN_A,mode='no_contact',expected_revision=revision,
   command_key='readiness-stop',actor=OPERATOR,reason='Fan contact stopped',action='mark_no_contact')
 assert _work_counts(world.pilot)['eligibleFans']==len(world.fans)-1
 with Session(world.database[1]) as session:revision=session.get(OfDeliveryPolicy,world.account).revision
 TextAuthority(world.store).configure(TENANT,world.account,expected_revision=revision,enabled=True,media_enabled=True,
   limit_units=1,throttle_seconds=1,transport_kind='fansapi',actor=OPERATOR,reason='One-unit reviewed test budget')
 world.say('hello',FAN_B);world.pilot.tick()
 assert len(world.fans_mock.posts)==1
 work=_work_counts(world.pilot)
 assert work['eligibleFans']==0 and work['budgetHold']=='delivery_budget_exhausted'
 assert work['accountHold'] is False
