"""Explicit production delivery settings, synthetic PostgreSQL only."""
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings
from onlyfans_runtime.store import Refused

def test_fractional_delivery_policy_requires_explicit_configuration(database,monkeypatch):
    from test_onlyfans_foundation_pg import seed
    from onlyfans_runtime.text_delivery import TextAuthority
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    settings(monkeypatch)
    store=seed(*database)
    authority=TextAuthority(store)
    authority.configure("tenant-a","acc-a",expected_revision=0,enabled=False,
        limit_units=100000,throttle_seconds=0.25,actor="synthetic-operator",reason="Synthetic capacity check")
    with Session(database[1]) as s:
        policy=s.get(OfDeliveryPolicy,"acc-a")
        assert policy.enabled is False and policy.throttle_seconds==0.25
        assert policy.limit_units==100000 and policy.used_units==0
    for value in [True,0,0.01,float("nan"),float("inf")]:
        with pytest.raises(Refused):
            authority.configure("tenant-a","acc-a",expected_revision=1,enabled=False,
                limit_units=100000,throttle_seconds=value,actor="synthetic-operator",reason="Invalid interval")
    store.close()
