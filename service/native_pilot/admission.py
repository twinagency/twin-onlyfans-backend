"""Pure helpers for the account provider budget and per-conversation admission.

The durable rules live beside the rows they protect (the ledger for the native pilot,
``onlyfans_runtime.concurrent_admission`` for manual/campaign text). Keeping the arithmetic
here means both paths use the same token-bucket and setting semantics without sharing a
database transaction or a process lock.

Concurrent mode is deliberately opt-in per account via ``of_delivery_policy.delivery_mode``.
The legacy mode keeps the single-holder gate and the account send gap exactly as deployed.
"""
from __future__ import annotations

from datetime import datetime, timezone


LEGACY_MODE = "gap"
CONCURRENT_MODE = "concurrent"

DEFAULT_REQUESTS_PER_SECOND = 1.0
DEFAULT_BURST = 5
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_DISPATCH_LEASE_S = 120.0


def value(row, name: str, default=None):
    """Read an optional policy column from a row, dict or ORM object."""
    if row is None:
        return default
    try:
        if isinstance(row, dict):
            got = row.get(name, default)
        else:
            try:
                got = row[name]
            except (KeyError, IndexError, TypeError):
                got = getattr(row, name, default)
    except Exception:
        got = default
    return default if got is None else got


def delivery_mode(row) -> str:
    mode = str(value(row, "delivery_mode", LEGACY_MODE) or LEGACY_MODE).strip().lower()
    return CONCURRENT_MODE if mode == CONCURRENT_MODE else LEGACY_MODE


def is_concurrent(row) -> bool:
    return delivery_mode(row) == CONCURRENT_MODE


def bounded_int(raw, default: int, low: int, high: int) -> int:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        number = default
    return min(high, max(low, number))


def bounded_float(raw, default: float, low: float, high: float) -> float:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        number = default
    if number != number:  # NaN
        number = default
    return min(high, max(low, number))


def requests_per_second(row) -> float:
    return bounded_float(value(row, "provider_requests_per_second", DEFAULT_REQUESTS_PER_SECOND),
                         DEFAULT_REQUESTS_PER_SECOND, 0.05, 100.0)


def burst(row) -> int:
    return bounded_int(value(row, "provider_burst", DEFAULT_BURST), DEFAULT_BURST, 1, 200)


def max_concurrent(row, process_limit: int) -> int:
    policy_limit = bounded_int(value(row, "provider_max_concurrent", DEFAULT_MAX_CONCURRENT),
                               DEFAULT_MAX_CONCURRENT, 1, 64)
    return max(1, min(int(process_limit), policy_limit))


def conversation_gap_seconds(row) -> float:
    return bounded_float(value(row, "conversation_min_gap_seconds", 0.0), 0.0, 0.0, 3600.0)


def cooldown_at(row):
    """Return the later of the concurrent cooldown and the legacy dispatch stamp.

    During a mode transition either column can still carry the authoritative
    account-wide wait. Choosing the later value prevents a stale, already-expired
    concurrent cooldown from hiding a still-current legacy ``next_dispatch_at``.
    """
    raw = [value(row, "provider_cooldown_until", None), value(row, "next_dispatch_at", None)]
    raw = [item for item in raw if item is not None]
    if not raw:
        return None

    def key(item):
        parsed = item if isinstance(item, datetime) else datetime.fromisoformat(
            str(item).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    return max(raw, key=key)


def refill(tokens: float, updated_at: datetime, now: datetime, rate: float, capacity: int) -> float:
    """Tokens available at ``now``; elapsed time is clamped at zero for clock skew."""
    elapsed = max(0.0, (now - updated_at).total_seconds())
    return min(float(capacity), float(tokens) + elapsed * float(rate))


def token_wait(tokens: float, rate: float) -> float:
    """Seconds until one token is available (zero when one already is)."""
    if tokens >= 1.0:
        return 0.0
    return max(0.0, (1.0 - tokens) / max(0.01, float(rate)))
