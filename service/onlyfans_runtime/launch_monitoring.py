"""Evaluate the established private operational projection without taking action.

This is a bounded stdin adapter for an authorized monitor's existing read. It
does not fetch credentials, contact a provider, send notifications, or alter
account state. Nonzero exit status lets the deployment's monitor route an
actionable condition through its explicitly configured notification channel.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

MAX_BYTES = 128 * 1024
CONDITIONS = {
    "commerce_evidence_conflict": ("critical", "Investigate ledger evidence; retain the commerce hold."),
    "history_quarantined": ("critical", "Investigate quarantined intake before resuming affected work."),
    "model_cost_unresolved": ("warning", "Reconcile the pending model call before retrying generation."),
    "model_budget_exhausted": ("warning", "Review model spend and the approved account budget."),
    "intake_count_mismatch": ("critical", "Inspect intake bookkeeping and worker errors."),
    "receipt_projection_pending": ("warning", "Inspect receipt projection; retain accepted transport evidence."),
    "generation_unresolved": ("warning", "Inspect the unresolved generation; do not regenerate blindly."),
    "history_not_current": ("warning", "Inspect recent intake and the current connection; archive totals are separate."),
    "history_sync_time_invalid": ("critical", "Inspect worker and database clocks before trusting freshness."),
    "runtime_not_registered": ("critical", "Inspect the scoped runtime registration."),
}
OTHER_REASONS = {"runtime_disabled", "delivery_disabled", "history_recovery_required", "cap_exhausted",
                 "generation_held", "delivery_in_progress", "delivery_unresolved",
                 "waiting_queue_older_than_two_minutes"}


def _count(value):
    if type(value) is not int or not 0 <= value <= 10**15:
        raise ValueError("invalid count")
    return value


def _stamp(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid time")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("unqualified time")
    return result.astimezone(timezone.utc)


def evaluate(snapshot, *, now=None, max_age_seconds=120):
    """Return safe fixed-text alerts; never copy an error/body from the input."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ValueError("invalid monitor clock or freshness policy")
    alerts = []

    def add(code, severity, action):
        if not any(row["code"] == code for row in alerts):
            alerts.append({"code": code, "severity": severity, "action": action})

    try:
        if type(snapshot) is not dict:
            raise ValueError("invalid snapshot")
        body = snapshot.get("operations", snapshot)
        if type(body) is not dict or body.get("schemaVersion") != "of-operations.v3":
            raise ValueError("unsupported projection")
        observed = _stamp(body["asOf"])
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise ValueError("stale projection")
        if type(body["providerConnectionVerified"]) is not bool:
            raise ValueError("invalid connection state")
        work, delivery, intake = body["work"], body["delivery"], body["intake"]
        for group, names in ((work, ("waiting", "claimed", "blocked")),
                             (delivery, ("queued", "dispatching", "unknown", "retainedUnresolvedReceipts")),
                             (intake, ("pending", "quarantined"))):
            if type(group) is not dict:
                raise ValueError("invalid counters")
            for name in names:
                _count(group[name])
        waiting_age = work["oldestWaitingAgeSeconds"]
        if waiting_age is not None:
            _count(waiting_age)
        if bool(work["waiting"]) != (waiting_age is not None):
            raise ValueError("inconsistent queue age")
        reasons = body["reasons"]
        if type(reasons) is not list or len(reasons) > 64 or any(type(reason) is not str for reason in reasons):
            raise ValueError("invalid reasons")
        if not body["providerConnectionVerified"]:
            add("connection_unavailable", "critical", "Verify the stored connection and current binding; keep outgoing work held.")
        if waiting_age is not None and waiting_age > 120:
            add("work_stalled", "warning", "Inspect worker health, current input holds and the oldest waiting work.")
        if delivery["unknown"] or delivery["retainedUnresolvedReceipts"] or "delivery_unresolved" in reasons:
            add("transport_unresolved", "critical", "Reconcile retained send evidence; do not retry or clear the unknown outcome.")
        for reason in reasons:
            if reason in CONDITIONS:
                severity, action = CONDITIONS[reason]
                add(reason, severity, action)
            elif reason not in OTHER_REASONS:
                add("unsupported_condition", "warning", "Review the operational projection and update the monitor for its new condition.")
        if intake["quarantined"]:
            severity, action = CONDITIONS["history_quarantined"]
            add("history_quarantined", severity, action)
        return {"schemaVersion": "of-launch-alerts.v1", "observedAt": observed.isoformat(),
                "state": "attention" if alerts else "clear", "alerts": alerts}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"schemaVersion": "of-launch-alerts.v1", "observedAt": None, "state": "unavailable",
                "alerts": [{"code": "monitor_input_unavailable", "severity": "critical",
                            "action": "Restore a fresh successful scoped operations read; unavailable input is not healthy."}]}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized projection")
        snapshot = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, OSError):
        snapshot = None
    result = evaluate(snapshot)
    print(json.dumps(result, separators=(",", ":")))
    return 2 if result["state"] == "unavailable" else int(result["state"] == "attention")


if __name__ == "__main__":
    raise SystemExit(main())
