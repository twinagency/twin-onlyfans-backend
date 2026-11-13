"""G02-01: a provider safety stop (fans-webhook `_disable_sends`) always disables sending, even when the stored
delivery policy has drifted wider than the durable pilot scope. The stop clamps the policy back inside the scope;
it is never refused (sibling of test_g02_pilot_stop_never_refused_pg)."""
from sqlalchemy import update
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_fans_intake_models import OfFansLink
from test_engine_real_worker_pg import PROVIDER
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT
from test_plan_c_draft_app_pg import signed
from test_plan_c_supervised_pg import supervised, configured_draft, database, world  # noqa: F401


# Throttle cannot drift wider here: the scope throttle sits at the DB floor (>= 1).
def test_provider_auth_failure_disables_sends_with_drifted_wider_policy(supervised):
    world, _, _, _, pilot = supervised
    limits = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']['limits']
    with Session(world.database[1]) as s, s.begin():
        s.execute(update(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == ACCOUNT)
                  .values(limit_units=limits['send_units'] + 1))
    with Session(world.database[1]) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is True   # sending is armed
    raw, headers = signed({'event': 'accounts.authentication_failed', 'account_id': PROVIDER, 'payload': {}},
                          key='g02-01-auth-failed')
    pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])
    with Session(world.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert s.query(OfFansLink).one().send_blocked is True
        assert policy.enabled is False
        # The stop only tightens: the stored policy is back inside the durable scope.
        assert policy.limit_units <= limits['send_units'] and policy.throttle_seconds >= limits['throttle_seconds']
