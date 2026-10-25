"""Fail closed when a benchmark did not offer the load or meet the reply SLA."""


def evaluate_capacity(result):
    failures = []
    cfg = result.get("config", {})
    offered = result.get("offered_load", {})
    accounts = result.get("accounts", {})
    if result.get("scenario") != "target_100k":
        failures.append("the required target scenario was not run")
    if cfg.get("scale") != 1:
        failures.append("an unscaled clock is required")
    if cfg.get("sim_minutes", 0) < 5:
        failures.append("at least five minutes of test time is required")
    if cfg.get("accounts") != 10 or len(accounts) != 10:
        failures.append("ten accounts are required")
    scheduled = offered.get("scheduled_messages", 0)
    if scheduled <= 0 or offered.get("generated_messages") != scheduled or offered.get("persisted_messages") != scheduled:
        failures.append("the full scheduled traffic was not generated and persisted")
    if abs(offered.get("scheduled_messages_per_hour", 0) - 100000) > 1:
        failures.append("scheduled traffic did not reach 100000 messages/hour")
    if offered.get("max_ingress_delay_s", float("inf")) > 1:
        failures.append("arrival persistence fell more than one second behind")
    inbound = sum(a.get("inbound_messages", 0) for a in accounts.values())
    if inbound != scheduled:
        failures.append("reported inbound does not match scheduled traffic")
    if "service_crashed" not in result or result["service_crashed"]:
        failures.append("service errors or missing service completion evidence")
    if "helper_errors" not in result or result["helper_errors"]:
        failures.append("harness errors or missing harness completion evidence")
    if "runtime_errors" not in result or result["runtime_errors"]:
        failures.append("runtime errors or missing runtime completion evidence")
    for account, row in accounts.items():
        count = row.get("inbound_messages", 0)
        latency = row.get("reply_latency_s", {})
        if count <= 0 or latency.get("n") != count:
            failures.append(f"{account}: not every inbound has a measured reply")
        if row.get("unanswered_at_end") != 0 or row.get("unanswered_over_30s") != 0:
            failures.append(f"{account}: unanswered input remains")
        if row.get("delivered_over_30s") != 0 or latency.get("max") is None or latency["max"] > 30:
            failures.append(f"{account}: the 30-second reply deadline was missed")
        if row.get("throttle_respected") is not True:
            failures.append(f"{account}: delivery spacing was violated or not measured")
    return {"passed": not failures, "failures": failures,
            "scope": "synthetic runner/ledger benchmark; live model, provider and full media engine remain unproven"}
