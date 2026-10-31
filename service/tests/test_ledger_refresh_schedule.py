"""Recent-ledger refresh timing stays anchored to provider coverage."""
from datetime import datetime, timedelta

import pytest

from onlyfans_runtime.ledger import (
    RECENT_COVERAGE_FRESHNESS,
    RECENT_REFRESH_INTERVAL,
    Refused,
    recent_refresh_at,
)


CUTOFF = datetime(2026, 10, 31, 12, 21, 31)


def test_completion_does_not_postpone_a_slow_scan():
    completed = CUTOFF + timedelta(seconds=172)
    assert recent_refresh_at(completed, CUTOFF) == completed + timedelta(seconds=1)


def test_normal_page_keeps_one_refresh_interval_without_drifting():
    completed = CUTOFF + timedelta(seconds=5)
    assert recent_refresh_at(completed, CUTOFF) == CUTOFF + RECENT_REFRESH_INTERVAL - timedelta(seconds=5)


def test_scan_at_the_provider_lease_boundary_still_refreshes_on_time():
    completed = CUTOFF + timedelta(seconds=45)
    following = recent_refresh_at(completed, CUTOFF)
    assert following == CUTOFF + RECENT_REFRESH_INTERVAL - timedelta(seconds=45)
    assert following + timedelta(seconds=45) == CUTOFF + RECENT_REFRESH_INTERVAL
    assert CUTOFF + RECENT_COVERAGE_FRESHNESS > following + timedelta(seconds=45)


def test_provider_delay_beyond_the_interval_starts_immediately():
    completed = CUTOFF + timedelta(minutes=20)
    assert recent_refresh_at(completed, CUTOFF) == completed + timedelta(seconds=1)


@pytest.mark.parametrize("completed,cutoff", [
    (CUTOFF, CUTOFF.replace(tzinfo=None) + timedelta(seconds=1)),
    (CUTOFF - timedelta(seconds=1), CUTOFF),
    ("2026-10-31T12:21:31", CUTOFF),
])
def test_invalid_refresh_times_fail_closed(completed, cutoff):
    with pytest.raises(Refused, match="invalid ledger refresh timing"):
        recent_refresh_at(completed, cutoff)
