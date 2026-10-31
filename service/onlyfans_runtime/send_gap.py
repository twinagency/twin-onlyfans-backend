"""The per-account send gap: one small leaf module so the operator setters, the ledger and the tuning agree.

Shared ceiling (ONLYFANS-SHARED-CEILING-2026-10-31, rule 1): the gap between two sends on one OnlyFans account is
never below 10 seconds, and the default stays 20. Lowering is a deliberate per-account setting (20 -> 15 -> 10);
nothing here ever lowers it by itself. Pure functions, no database, no network.

* ``FASTT_PER_ACCOUNT_SEND_GAP=1`` lets an account's own ``of_delivery_policy.throttle_seconds`` (10..) set its
  gap. Off (the default) the engine behaves exactly as before: every account is held to at least 20 seconds.
* ``ONLYFANS_ALLOW_SEND_GAP_BELOW_FLOOR=1`` is the explicit override that lets an operator command store a
  throttle under 10 seconds (the database check still allows 1..86400). The engine itself never goes under 10.
"""
from __future__ import annotations

import os

HARD_FLOOR_S = 10.0       # the shared ceiling: never faster than one send per 10 s per account
DEFAULT_S = 20.0          # the default gap, and what a row that holds nothing usable (< 10) falls back to
OVERRIDE_ENV = "ONLYFANS_ALLOW_SEND_GAP_BELOW_FLOOR"
PER_ACCOUNT_ENV = "FASTT_PER_ACCOUNT_SEND_GAP"


def below_floor(throttle_seconds, env=None) -> bool:
    """True when an operator-supplied throttle must be refused: under the 10 s floor without the explicit override."""
    env = os.environ if env is None else env
    if env.get(OVERRIDE_ENV) == "1":
        return False
    return type(throttle_seconds) is int and throttle_seconds < HARD_FLOOR_S


def per_account_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get(PER_ACCOUNT_ENV) == "1"


def effective_gap(throttle_seconds, min_interval_s: float, per_account: bool) -> float:
    """Seconds between two sends on one account.

    Legacy (``per_account`` False): ``max(throttle_seconds, min_interval_s)`` with ``min_interval_s`` >= 20, exactly
    what the engine did before. Per-account: the row's own value, but a value under the 10 s floor is treated as "not
    set" and becomes the 20 s default (a row left at the column default of 1 must not mean one send per second), and
    the result never goes under the floor or under an explicitly raised ``min_interval_s``."""
    t = float(throttle_seconds or 0)
    if not per_account:
        return max(t, float(min_interval_s))
    if t < HARD_FLOOR_S:
        t = DEFAULT_S
    return max(t, float(min_interval_s), HARD_FLOOR_S)
