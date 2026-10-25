"""Baseline transport capacity evidence; 90-second maximum, no unanswered inputs."""
def evaluate_capacity(result):
    failures = []
    cfg, offered, accounts = [result.get(k, {}) for k in ["config", "offered_load", "accounts"]]
    n = cfg.get("accounts", 0)
    if result.get("scenario") != "baseline_fleet" or n not in [2, 10] or len(accounts) != n:
        failures.append("incorrect baseline topology")
    if cfg.get("scale") != 1 or cfg.get("sim_minutes", 0) < 5:
        failures.append("unscaled test duration missing")
    scheduled = offered.get("scheduled_messages", 0)
    if scheduled <= 0 or offered.get("generated_messages") != scheduled or offered.get("persisted_messages") != scheduled:
        failures.append("scheduled traffic missing")
    expected_rate = n * cfg.get("rate_per_account_hour", 0)
    if abs(offered.get("scheduled_messages_per_hour", 0) - expected_rate) > 1:
        failures.append("offered rate differs from requested rate")
    if offered.get("max_ingress_delay_s", float("inf")) > 1:
        failures.append("arrival persistence more than one second late")
    if sum(a.get("inbound_messages", 0) for a in accounts.values()) != scheduled:
        failures.append("inbound count mismatch")
    for key in ["service_crashed", "helper_errors", "runtime_errors"]:
        if key not in result or result[key]:
            failures.append(key)
    for account, row in accounts.items():
        count, latency = row.get("inbound_messages", 0), row.get("reply_latency_s", {})
        if count <= 0 or latency.get("n") != count or row.get("unanswered_at_end") != 0:
            failures.append(account + ": unanswered input")
        if row.get("delivered_over_90s") != 0 or latency.get("max") is None or latency["max"] > 90:
            failures.append(account + ": 90-second maximum missed")
        if row.get("model_failures") != 0 or row.get("provider_429s") != 0:
            failures.append(account + ": model/provider failures")
        if row.get("throttle_respected") is not True:
            failures.append(account + ": send spacing failed")
    return {"passed": not failures, "failures": failures,
            "scope": "Unchanged production runner/ledger; synthetic 6s model, 1.6s history and POST; one text reply. Full media/AI engine and live provider are not measured."}
