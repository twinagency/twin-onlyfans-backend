"""Actionable operational alerts from synthetic established API projections."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from onlyfans_runtime.launch_monitoring import evaluate

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def healthy():
    return {"schemaVersion": "of-operations.v3", "asOf": NOW.isoformat(),
        "providerConnectionVerified": True, "reasons": ["runtime_disabled", "delivery_disabled"],
        "work": {"waiting": 0, "claimed": 0, "blocked": 0, "oldestWaitingAgeSeconds": None},
        "delivery": {"queued": 0, "dispatching": 0, "unknown": 0, "retainedUnresolvedReceipts": 0},
        "intake": {"pending": 0, "quarantined": 0}}


def test_intentionally_stopped_account_is_not_a_monitor_failure():
    snapshot = healthy()
    before = copy.deepcopy(snapshot)
    result = evaluate({"operations": snapshot}, now=NOW)
    assert result["state"] == "clear" and result["alerts"] == []
    assert snapshot == before


def test_stalled_work_connection_failure_and_unknown_send_have_distinct_actions():
    snapshot = healthy()
    snapshot["providerConnectionVerified"] = False
    snapshot["work"].update(waiting=1, oldestWaitingAgeSeconds=121)
    snapshot["delivery"]["unknown"] = 1
    snapshot["reasons"] += ["delivery_unresolved", "waiting_queue_older_than_two_minutes"]
    result = evaluate(snapshot, now=NOW)
    assert result["state"] == "attention"
    alerts = {item["code"]: item for item in result["alerts"]}
    assert set(alerts) == {"work_stalled", "connection_unavailable", "transport_unresolved"}
    assert "do not retry" in alerts["transport_unresolved"]["action"]
    assert "current binding" in alerts["connection_unavailable"]["action"]


def test_retained_receipt_is_not_hidden_by_zero_current_unknown_count():
    snapshot = healthy()
    snapshot["delivery"]["retainedUnresolvedReceipts"] = 1
    snapshot["reasons"] = ["SECRET provider body token user@example.com"]
    result = evaluate(snapshot, now=NOW)
    assert {item["code"] for item in result["alerts"]} == {"transport_unresolved", "unsupported_condition"}
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("age", [-1, 121])
def test_stale_or_future_projection_is_not_healthy(age):
    snapshot = healthy()
    snapshot["asOf"] = (NOW - timedelta(seconds=age)).isoformat()
    assert evaluate(snapshot, now=NOW)["state"] == "unavailable"


@pytest.mark.parametrize("field,value", [("waiting", True), ("claimed", -1), ("blocked", "0")])
def test_malformed_counters_are_not_treated_as_zero(field, value):
    snapshot = healthy()
    snapshot["work"][field] = value
    assert evaluate(snapshot, now=NOW)["state"] == "unavailable"


def test_quarantined_intake_and_commerce_holds_remain_actionable_without_duplicates():
    snapshot = healthy()
    snapshot["intake"]["quarantined"] = 1
    snapshot["reasons"] = ["history_quarantined", "commerce_evidence_conflict"]
    result = evaluate(snapshot, now=NOW)
    assert {item["code"] for item in result["alerts"]} == {"history_quarantined", "commerce_evidence_conflict"}
    assert len(result["alerts"]) == 2


@pytest.mark.parametrize("raw", [b'{"operations":{},"operations":{}}', b"not json", b"x" * (128 * 1024 + 1)],
                         ids=["duplicate-key", "invalid-json", "oversized"])
def test_cli_fails_closed_and_does_not_echo_invalid_input(raw):
    service = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-m", "onlyfans_runtime.launch_monitoring"], input=raw,
        env={**os.environ, "PYTHONPATH": str(service)}, capture_output=True, check=False)
    assert result.returncode == 2 and result.stderr == b""
    assert json.loads(result.stdout)["alerts"][0]["code"] == "monitor_input_unavailable"
