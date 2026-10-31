"""Engine v2 settings: one explicit, bounded place for every knob the scheduler uses.

Every value has a default, a lower and an upper bound, and comes from an environment variable.
A value outside its bounds is clamped (never rejected, never silently widened) and the clamp is
reported once at start so it is visible. Nothing here reads a database or a network.

The one setting that is deliberately NOT freely configurable is the account send interval:
``FASTT_MIN_SEND_INTERVAL_S`` defaults to 20 seconds and can be raised, but never lowered below
20 (Ronan's rule: at least 20 s between sends per OnlyFans account). A smaller value is clamped to
20 and reported. The effective interval for an admission is ``max(this, of_delivery_policy.throttle_seconds)``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

MIN_SEND_INTERVAL_FLOOR_S = 20.0

# name: (env, default, low, high)
_SPEC = {
    "concurrency": ("FASTT_PREPARE_CONCURRENCY", 4, 1, 16),
    "quiet_s": ("FASTT_BURST_QUIET_S", 7.0, 1.0, 60.0),
    "burst_max_wait_s": ("FASTT_BURST_MAX_WAIT_S", 45.0, 5.0, 300.0),
    "min_send_interval_s": ("FASTT_MIN_SEND_INTERVAL_S", None, None, 3600.0),   # default and low bound: the floor, read at call time
    "pace_wait_max_s": ("FASTT_SEND_WAIT_MAX_S", 900.0, 60.0, 3600.0),
    "queue_max": ("FASTT_QUEUE_MAX", 200, 10, 2000),
    "runner_lease_ttl_s": ("FASTT_RUNNER_LEASE_TTL_S", 60.0, 15.0, 600.0),
    "status_every_s": ("FASTT_STATUS_EVERY_S", 15.0, 2.0, 300.0),
    "throttled_retries": ("FASTT_THROTTLE_RETRIES", 40, 1, 400),
    "drain_grace_s": ("FASTT_DRAIN_GRACE_S", 90.0, 5.0, 600.0),
    "reconcile_wait_s": ("FASTT_RECONCILE_WAIT_S", 30.0, 5.0, 600.0),
    "auto_reconcile": ("FASTT_AUTO_RECONCILE", 1, 0, 1),
    "absent_after_s": ("FASTT_AUTO_RESOLVE_ABSENT_AFTER_S", 0.0, 0.0, 86400.0),   # 0 = never infer non-delivery
}


@dataclass(frozen=True)
class Tuning:
    concurrency: int = 4
    quiet_s: float = 7.0
    burst_max_wait_s: float = 45.0
    min_send_interval_s: float = MIN_SEND_INTERVAL_FLOOR_S
    pace_wait_max_s: float = 900.0
    queue_max: int = 200
    runner_lease_ttl_s: float = 60.0
    status_every_s: float = 15.0
    throttled_retries: int = 40
    drain_grace_s: float = 90.0
    reconcile_wait_s: float = 30.0
    auto_reconcile: int = 1
    absent_after_s: float = 0.0
    clamped: tuple = field(default=(), compare=False)

    @classmethod
    def from_env(cls, env=None, **override) -> "Tuning":
        env = os.environ if env is None else env
        values, clamped = {}, []
        for name, (var, default, lo, hi) in _SPEC.items():
            if name == "min_send_interval_s":
                # Read the floor at call time. It is a module constant, not a setting: no environment value,
                # config row or argument can go below it (only a test may patch the constant itself).
                default = lo = MIN_SEND_INTERVAL_FLOOR_S
            raw = override.get(name, env.get(var))
            if raw is None or raw == "":
                values[name] = default
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                clamped.append((var, "invalid"))
                values[name] = default
                continue
            if number != number:
                clamped.append((var, "invalid"))
                values[name] = default
                continue
            bounded = min(hi, max(lo, number))
            if bounded != number:
                clamped.append((var, "clamped"))
            values[name] = int(bounded) if isinstance(default, int) and not isinstance(default, bool) else float(bounded)
        if 0 < values["absent_after_s"] < 120:
            values["absent_after_s"] = 120.0
            clamped.append(("FASTT_AUTO_RESOLVE_ABSENT_AFTER_S", "raised-to-120"))
        if values["burst_max_wait_s"] < values["quiet_s"]:
            values["burst_max_wait_s"] = values["quiet_s"]
            clamped.append(("FASTT_BURST_MAX_WAIT_S", "raised-to-quiet"))
        return cls(**values, clamped=tuple(clamped))
