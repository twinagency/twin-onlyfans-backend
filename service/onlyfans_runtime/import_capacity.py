"""Explicit software capacity, not a claim about upstream allowances."""
from dataclasses import dataclass
import json
import os


@dataclass(frozen=True)
class Capacity:
    rps: int = 1
    inflight: int = 1

    @property
    def burst(self):
        return max(10, self.rps)


def capacity(account):
    raw = os.environ.get('ONLYFANS_IMPORT_CAPACITY', '{}')
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError()
        value = values.get(account, {})
        if not isinstance(value, dict) or set(value) - {'rps', 'inflight'}:
            raise ValueError()
        rate, slots = value.get('rps', 1), value.get('inflight', 1)
        if type(rate) is not int or not 1 <= rate <= 64:
            raise ValueError()
        if type(slots) is not int or not 1 <= slots <= 128:
            raise ValueError()
        if rate > 1 and slots < 2:
            raise ValueError()
        return Capacity(rate, slots)
    except (ValueError, TypeError):
        raise ValueError('invalid OnlyFans import capacity configuration') from None


def refill(tokens, at, now, policy):
    """Fractional-second replenishment without minting tokens on each claim."""
    from datetime import timedelta
    elapsed = max(0., (now-at).total_seconds())
    earned = int(elapsed * policy.rps)
    return min(policy.burst, tokens + earned), at + timedelta(seconds=earned / policy.rps)


def requirements(fans, pages_per_fan, *, minutes=90, headroom=1.25):
    """Conditional capacity arithmetic. Counts must come from measured workload."""
    import math
    if fans < 0 or pages_per_fan <= 0 or minutes <= 0 or headroom < 1:
        raise ValueError('invalid workload')
    reads = math.ceil(fans * pages_per_fan)
    return {'historyReads': reads, 'minimumRps': reads/(minutes*60),
            'provisionedRps': math.ceil(reads/(minutes*60)*headroom),
            'includesDiscoveryLedgerAndMemory': False}
