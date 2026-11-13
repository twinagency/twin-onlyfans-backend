"""W3 trace ledger (W3TL-01/02): an unresolved model outcome may have been charged, so the request goes to a human at
once, in the live pass and on any replay. Exactly one handover case, no second model call, the reservation kept.
Fixture client only (no network) on a throwaway PG.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, unthrottle
from test_engine_simulated_journey_pg import incoming
from test_engine_model_pg import BOUND, FALLBACK, Clock, _cases, _make_request, _reserve, config, ok, rows, with_model
from conversation_engine.model_client import ModelBudgetExhausted, ModelIncompleteResponse, ModelOutcomeUnknown
from db.onlyfans_engine_models import OfEngineRequest
from onlyfans_runtime.store import Refused

HANDED = 'outcome is unknown; request handed to a human'


def _handed_over_once(database, outcomes):
    [(category, _, _, reason, evidence)] = _cases(database)
    assert (category, reason, evidence['outcomes']) == ('model_handover', 'model_outcome_unknown', outcomes)
    with Session(database[1]) as s:
        assert s.scalar(select(OfEngineRequest.status)) == 'handover_pending'


@pytest.mark.parametrize('failure,outcome', [
    (ModelOutcomeUnknown('timeout'), 'timeout'), (ModelIncompleteResponse('partial'), 'provider_incomplete'),
    (RuntimeError('boom'), 'client_error')])
def test_w3_unknown_outcome_hands_over_in_the_live_pass_and_never_unlocks_the_fallback(database, failure, outcome):
    store, authority, engine, runtime, client, _ = with_model(
        database, [failure, ok('from the fallback')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match=HANDED):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary']  # W3TL-02: no same-pass fallback
    [row] = rows(database)
    assert (row.status, row.outcome) == ('unknown', outcome)
    assert runtime.ledger.committed(day=row.cost_day) == BOUND  # the handover keeps the full reservation
    _handed_over_once(database, [outcome])
    store.close()


def test_w3_replay_after_an_unknown_outcome_makes_no_second_call_and_opens_no_second_case(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelOutcomeUnknown('timeout'), ok('second paid call')], clock=clock)
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match=HANDED):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    clock.offset = timedelta(seconds=3600)  # long past every cooldown
    with pytest.raises(Refused):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # W3TL-01: redelivery / resume of a handed-over request
    assert [r.model for r in client.requests] == ['m/primary']
    [row] = rows(database)
    assert runtime.ledger.committed(day=row.cost_day) == BOUND
    _handed_over_once(database, ['timeout'])
    store.close()


def test_w3_an_unknown_call_of_another_generation_also_hands_over(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('must not be asked')])
    incoming(store, 1, 'hello')
    request_id = _make_request(database, engine, runtime, client)
    r = _reserve(runtime, database, request_id, calling=True, worker='w-old', generation=1)
    runtime.ledger.mark_unknown(ACCOUNT, r.call_id, 'w-old', outcome='timeout', error_class='timeout')
    with pytest.raises(Refused, match=HANDED):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # this pass plans generation 0
    assert client.requests == []
    _handed_over_once(database, ['timeout'])
    store.close()


def test_w3_unknown_reservation_still_counts_against_the_cap_for_a_later_request(database):
    # each new request needs 2 x BOUND (call + retry allowance); the earlier unknown keeps BOUND reserved after handover
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelOutcomeUnknown('timeout'), ok('never')], cfg=config(daily_cap_microusd=3 * BOUND - 1))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match=HANDED):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    unthrottle(database[1]); incoming(store, 2, 'next')
    with pytest.raises(ModelBudgetExhausted, match='daily'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    assert len(client.requests) == 1
    store.close()
