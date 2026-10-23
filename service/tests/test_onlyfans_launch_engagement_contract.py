import pytest
from onlyfans_runtime.launch_engagement.contracts import Policy, load_object, identity
from onlyfans_runtime.store import Refused


def test_default_off_and_roundtrip():
    policy = Policy.parse({})
    assert not any((policy.enabled, policy.offers, policy.follow_ups, policy.reengagement))
    assert Policy.parse(load_object(policy.json())) == policy


@pytest.mark.parametrize("value", [None, [], {"enabled": 1}, {"offers": "true"}, {"unknown": 1},
    {"fan_daily_limit": True}, {"fan_daily_limit": 0}, {"account_hourly_limit": 21},
    {"offer_delay_hours": 1}, {"fan_daily_limit": 4, "fan_weekly_limit": 3},
    {"reengage_text": ""}, {"follow_up_text": "a\x00b"}, {"reengage_text": "a" * 1001}])
def test_settings_reject_invalid(value):
    with pytest.raises(Refused):
        Policy.parse(value)


@pytest.mark.parametrize("raw", ['{"enabled":true,"enabled":false}', '{"limit":NaN}', 'not json'])
def test_ambiguous_json_refused(raw):
    with pytest.raises(Refused):
        load_object(raw)


def test_agency_management_scope_is_enforced_at_service_boundary():
    from onlyfans_read.agency_scope import current_scope, AgencyScope
    token = current_scope.set(AgencyScope(("creator",), "manager", False))
    try:
        with pytest.raises(Refused, match="management permission"):
            identity("a" * 64, "manager")
        current_scope.set(AgencyScope(("creator",), "manager", True))
        with pytest.raises(Refused, match="management permission"):
            identity("a" * 64, "someone-else")
        identity("a" * 64, "manager")
    finally:
        current_scope.reset(token)
