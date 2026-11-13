"""G02 / W2SC-02: a text-only pilot stop always commits, even when the stored policy is wider than the scope.

The stop must never be gated by the send-side scope limit check. Two ways the stored policy ends up wider:
  * drift persisted by older code (pre-#91 dashboard Activate wrote limit 10000 under an active pilot) -- simulated
    here by a direct row write, because head no longer has a writer that does it;
  * reachable at head through real commands: stop, then narrow the scope (pilot configure), then stop again.
"""
import json

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfDeliveryPolicyChange
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT
from test_plan_c_scope_pg import ACTOR, key
from test_plan_c_text_activation_pg import stopped_text, start, configured_draft, database, world  # noqa: F401


def _stop(w, revision, n):
    return pilot_scope.set_status(w.store, TENANT, ACCOUNT, 'stopped', expected_revision=revision,
                                  command_key=key(n), actor=ACTOR, reason='operator stop')


def _assert_stopped(w, result, n):
    assert result['status'] == 'stopped' and not result['replayed']
    with Session(w.database[1]) as s:
        runtime, policy = s.get(OfRuntime, ACCOUNT), s.get(OfDeliveryPolicy, ACCOUNT)
        row, config = pilot_scope.current(s, runtime)
        limits = config['limits']
        assert row.status == 'stopped' and not runtime.enabled and not policy.enabled and not policy.media_enabled
        # The stop only tightens: the stored policy is back inside the durable scope.
        assert policy.limit_units <= limits['send_units'] and policy.throttle_seconds >= limits['throttle_seconds']
        assert s.get(OfLaunchCommand, (ACCOUNT, key(n))) is not None
        change = s.scalar(select(OfDeliveryPolicyChange).where(OfDeliveryPolicyChange.account_id == ACCOUNT,
            OfDeliveryPolicyChange.revision == policy.revision))
        assert change.actor == ACTOR and json.loads(change.after_json)['enabled'] is False


def test_active_pilot_stop_commits_with_drifted_wider_policy(stopped_text):
    w, _, _, _, first = stopped_text
    active = start(w, first['revision'])
    with Session(w.database[1]) as s, s.begin():
        s.execute(update(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == ACCOUNT).values(limit_units=10000))
    with Session(w.database[1]) as s:
        assert s.get(OfRuntime, ACCOUNT).enabled and s.get(OfDeliveryPolicy, ACCOUNT).enabled   # sending is armed
    _assert_stopped(w, _stop(w, active['revision'], 92001), 92001)


@pytest.mark.parametrize('narrow', ['send_units', 'throttle_seconds'])
def test_stop_commits_after_scope_narrowed_below_policy(stopped_text, narrow):
    w, _, _, _, first = stopped_text
    active = start(w, first['revision'])
    stopped = _stop(w, active['revision'], 92002)
    config = pilot_scope.describe(w.store, TENANT, ACCOUNT)['config']
    with Session(w.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert policy.limit_units >= 2
        units = policy.limit_units - 1 if narrow == 'send_units' else policy.limit_units
        throttle = policy.throttle_seconds + (60 if narrow == 'throttle_seconds' else 0)
    config['limits'] = {**config['limits'], 'send_units': units, 'throttle_seconds': throttle,
                        'daily_send_units': min(units, config['limits']['daily_send_units'])}
    configured = pilot_scope.configure(w.store, TENANT, ACCOUNT, config, expected_revision=stopped['revision'],
                                       command_key=key(92003), actor=ACTOR, reason='narrow the scope')
    assert configured['status'] == 'configured'
    _assert_stopped(w, _stop(w, configured['revision'], 92004), 92004)
