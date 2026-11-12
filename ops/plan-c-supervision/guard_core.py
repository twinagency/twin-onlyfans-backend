"""Pure fail-closed checks for the bounded pilot's external stop supervisor."""
class GuardFailure(Exception):
    pass

def _number(value):
    return type(value) in (int, float) and value >= 0 and value < 10**9

def credit_estimate(*, baseline_total, baseline_balance, team_total, team_balance,
                    usage_cost, scope_reserved, external_reserved, prior_estimate, credit_limit=300):
    values = (baseline_total, baseline_balance, team_total, team_balance,
              scope_reserved, external_reserved, prior_estimate)
    if not all(_number(v) for v in values) or usage_cost != 0 or type(usage_cost) is bool:
        raise GuardFailure('credit_monitor_malformed')
    if team_total < baseline_total or team_balance > baseline_balance:
        raise GuardFailure('credit_monitor_inconsistent')
    estimate = max(prior_estimate, external_reserved + max(
        team_total - baseline_total, baseline_balance - team_balance, scope_reserved))
    if not _number(credit_limit) or credit_limit <= 0:
        raise GuardFailure('credit_monitor_malformed')
    if estimate >= credit_limit:  # Jesse 2026-11-10: stricter early stop 250 -> 300 (within the monitored 500)
        raise GuardFailure('credit_early_stop')
    return estimate

def require_health(*, public_status, local_status, intake, scope_status, active_sender):
    if public_status != 401 or local_status != 401:
        raise GuardFailure('ingress_unavailable')
    if type(intake) is not dict or intake.get('ok') is not True:
        raise GuardFailure('receiver_unavailable')
    if scope_status not in ('stopped','configured','active'):
        raise GuardFailure('scope_unavailable')
    if scope_status != 'active':
        if intake.get('mode') != 'intake_only' or intake.get('code') != 'ready':
            raise GuardFailure('receiver_not_ready')
    else:
        if (type(active_sender) is not dict or active_sender.get('ok') is not True
                or active_sender.get('mode') != 'active' or active_sender.get('sendingReady') is not True
                or active_sender.get('code') != 'ready'):
            raise GuardFailure('sender_not_ready')

def require_fresh(*, now, last_ok, max_age=45):
    if not _number(now) or not _number(last_ok) or last_ok > now or now-last_ok > max_age:
        raise GuardFailure('monitor_stale')
