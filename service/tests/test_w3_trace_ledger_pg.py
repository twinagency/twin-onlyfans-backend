"""W3 trace-ledger probes (W3TL-01/02; were RED before G08, now promoted).

REVIEW.md #5 + proof table: "ledger path | unit: inject `unknown` -> no second call, reservation intact".
These drive ModelRuntime.propose through engine.plan with a fixture client (no network) on a throwaway PG.
"""
from datetime import timedelta

import pytest

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_engine_simulated_journey_pg import incoming
from test_engine_model_pg import BOUND, FALLBACK, Clock, config, ok, rows, with_model
from conversation_engine.model_client import ModelOutcomeUnknown
from onlyfans_runtime.store import Refused


def test_w3_unknown_timeout_is_not_called_again_on_replay(database):
    """W3TL-01 (dup-of W1-04(b)): a later plan() for the same request (webhook redelivery, resume, any caller that
    is not the scheduler tick) must not make a second paid call while the first call's outcome is unknown."""
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelOutcomeUnknown('timeout'), ok('second paid call')], clock=clock)
    incoming(store, 1, 'hello')
    with pytest.raises(Refused):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    [first] = rows(database)
    assert (first.status, first.outcome) == ('unknown', 'timeout')
    clock.offset = timedelta(seconds=45)  # past updated_at + transport_retry_seconds
    try:
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    except Refused:
        pass  # held or handed to a human: both satisfy REVIEW #5
    assert [r.model for r in client.requests] == ['m/primary'], 'unknown call was retried automatically'
    assert runtime.ledger.committed(day=first.cost_day) == BOUND  # reservation intact, nothing new reserved
    store.close()


def test_w3_unknown_timeout_does_not_unlock_a_same_pass_fallback(database):
    """W3TL-02 (dup-of W1-04(a)): a timeout is 'may have been charged', not 'provider unavailable'; it must not make
    the fallback model eligible in the same pass (second possibly-charged call for one request)."""
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelOutcomeUnknown('timeout'), ok('from the fallback')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    try:
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    except Refused:
        pass
    assert [r.model for r in client.requests] == ['m/primary'], 'unknown call was followed by a fallback call'
    store.close()
