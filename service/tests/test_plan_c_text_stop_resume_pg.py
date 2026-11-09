from test_plan_c_text_activation_pg import stopped_text, start, configured_draft, database, world
from test_plan_c_scope_pg import ACTOR, key
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT
from conversation_engine import pilot_scope
from sqlalchemy.orm import Session
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy

def test_text_pilot_supported_scope_stop_then_resume(stopped_text):
    w, env, model, seen, first = stopped_text
    active = start(w, first['revision'])
    stopped = pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'stopped',
        expected_revision=active['revision'], command_key=key(99001),
        actor=ACTOR, reason='independent normal scope stop')
    with Session(w.database[1]) as s:
        runtime = s.get(OfRuntime, ACCOUNT)
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        print('STOPPED_STATE', stopped['status'], runtime.enabled, policy.enabled)
    resumed = pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'active',
        expected_revision=stopped['revision'], command_key=key(99002),
        actor=ACTOR, reason='independent scope resume after verified stop')
    assert resumed['status'] == 'active'
    assert not w.fans.posts
