"""Synthetic PostgreSQL proof that one failing history cannot starve the queue."""
from datetime import timedelta
import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed
from db.onlyfans_models import OfRecovery
from onlyfans_runtime.store import Refused, utcnow

def test_expired_history_retry_yields_to_unattempted_conversations(database):
    url, engine = database
    store = seed(url, engine)
    with Session(engine) as s, s.begin():
        s.execute(delete(OfRecovery).where(OfRecovery.account_id == "acc-a"))
        s.add_all(OfRecovery(account_id="acc-a", fan_id=fan, status="needed",
                            generation=0, high_water=100, upper_bound=300, before_id=200)
                  for fan in [101, 102, 103])
    first = store.claim_recovery("tenant-a", "acc-a")
    assert first.fan_id == 101
    # Simulate a provider failure or worker death; no provider is contacted.
    with Session(engine) as s, s.begin():
        s.get(OfRecovery, ("acc-a", 101)).lease_until = utcnow() - timedelta(seconds=1)
    second = store.claim_recovery("tenant-a", "acc-a")
    assert second.fan_id == 102, "an expired low-ID retry must not starve untouched histories"
    store.recovery_page("tenant-a", second, [], has_more=False)
    third = store.claim_recovery("tenant-a", "acc-a")
    assert third.fan_id == 103
    store.recovery_page("tenant-a", third, [], has_more=False)
    retry = store.claim_recovery("tenant-a", "acc-a")
    assert retry.fan_id == 101
    assert (retry.high_water, retry.upper_bound, retry.before_id) == (100, 300, 200)
    assert retry.generation > first.generation and retry.token != first.token
    with pytest.raises(Refused, match="stale recovery claim"):
        store.recovery_page("tenant-a", first, [], has_more=False)
    store.recovery_page("tenant-a", retry, [], has_more=False)
    assert store.claim_recovery("tenant-a", "acc-a") is None
    store.close()
