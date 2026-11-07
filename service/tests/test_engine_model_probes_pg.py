"""Jesse's five review probes (REVIEW-EVIDENCE.md) still behave, with fixture models and through the real-model path."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, unthrottle
from test_engine_memory_pg import setup, correct
from test_engine_simulated_journey_pg import incoming
from test_engine_offer_pg import setup_offer
from test_engine_model_pg import PROFILE, _grant_from_prompt, _offer_setup, config, ok, rows, with_model
from conversation_engine.engine import SyntheticModel, SyntheticProposal
from db.models import Fan, OfConversationState
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOwner, OfEngineRequest
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused


def wire(store, provider):
    return FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)


def test_probe1_cancelled_replan_gets_a_fresh_generation(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, first)
    correct(memory, value='Perth', key='audit-correction')
    provider = SyntheticFansProvider(message_id=9001)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire(store, provider)) == 'cancelled'
    again = engine.plan(TENANT, ACCOUNT, FAN, 1)
    again_intent = engine.queue(TENANT, ACCOUNT, again)
    assert again != first and again_intent != intent and len(model.calls) == 2
    assert authority.dispatch(TENANT, ACCOUNT, again_intent, wire(store, provider)) == 'accepted'
    store.close()


def test_probe1_through_the_real_model_path_new_generation_new_invocation(database):
    store, authority, engine, runtime, client, memory = with_model(database, [ok('first draft'), ok('second draft after the correction')])
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, first)
    correct(memory, value='Perth', key='audit-correction')
    provider = SyntheticFansProvider(message_id=9001)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire(store, provider)) == 'cancelled' and provider.calls == []
    second = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert second != first and [(r.generation, r.seq) for r in rows(database)] == [(0, 0), (1, 0)]
    assert 'his city: Perth' in client.requests[1].messages[1]['content']  # the replan sees the corrected memory
    assert authority.dispatch(TENANT, ACCOUNT, engine.queue(TENANT, ACCOUNT, second), wire(store, provider)) == 'accepted'
    assert provider.calls[0]['body']['text'] == 'second draft after the correction'
    store.close()


def test_probe2_other_fan_after_unknown_is_unchanged_by_this_branch(database):
    """Cross-fan gap blocking lives in onlyfans_runtime/text_delivery.py, outside this branch's scope."""
    store, authority, engine, model, memory = setup(database)
    with Session(database[1]) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=202))
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=202, mode='automated', revision=0, takeover_state='none', requires_attention=False))
    engine.select_simulated(TENANT, ACCOUNT, 202)
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    provider = SyntheticFansProvider(('unknown',))
    assert authority.dispatch(TENANT, ACCOUNT, engine.queue(TENANT, ACCOUNT, first), wire(store, provider)) == 'unknown'
    store.record(TENANT, ACCOUNT, event(mid=1, fan=202, body='hello')); store.drain(TENANT, ACCOUNT)
    second = engine.plan(TENANT, ACCOUNT, 202, 1)
    with pytest.raises(Refused, match='account delivery unresolved'):
        engine.queue(TENANT, ACCOUNT, second)
    store.close()


def test_probe3_paused_reply_claims_blocked_for_fixture_and_real_model(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = SyntheticModel({'no thanks': SyntheticProposal('Understood.', sales_signal='decline'),
        'hello': SyntheticProposal('Buy the set for USD 1.00. You already purchased it.')})
    incoming(store, 1, 'no thanks')
    provider = SyntheticFansProvider(('accepted', 'accepted'), message_id=(9001, 9002))
    dec = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert authority.dispatch(TENANT, ACCOUNT, engine.queue(TENANT, ACCOUNT, dec), wire(store, provider)) == 'accepted'
    unthrottle(database[1]); incoming(store, 2, 'hello')
    with pytest.raises(Refused):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    assert len(provider.calls) == 1
    store.close()


def test_probe4_offer_keeps_the_models_answer_and_the_engine_price(database):
    store, authority, engine, decision, item, grant = setup_offer(database)
    with Session(database[1]) as s:
        row = s.get(OfEngineDecision, (ACCOUNT, decision))
        assert row.body.startswith('untrusted model text') and row.body.endswith('USD 10.00.') and row.action == 'offer'
    store.close()


def test_probe5_renewed_buying_scope_plans_every_time_fixture_model(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = SyntheticModel({'decline': SyntheticProposal('Understood.', sales_signal='decline'),
        'buy one': SyntheticProposal('Item one.', sales_signal='reopen', item_scope=('1',)),
        'buy two': SyntheticProposal('Item two.', sales_signal='reopen', item_scope=('2',))})
    outcomes = []
    for mid, body in [(1, 'decline'), (2, 'buy one'), (3, 'buy two')]:
        incoming(store, mid, body)
        try:
            engine.plan(TENANT, ACCOUNT, FAN, mid); outcomes.append('planned')
        except Refused as exc:
            outcomes.append(str(exc))
    assert outcomes == ['planned', 'planned', 'planned']
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        assert (owner.sales_mode, owner.sales_scope_json) == ('reopened_scoped', '["2"]')
    store.close()


def test_probe5_through_the_real_model_path_decline_reopen_then_offer_within_scope(database):
    script = [lambda r: ok('understood, no pressure', sales_signal='decline'),
              lambda r: ok('oh that one, sure', sales_signal='reopen', item_scope=[str(ITEM['id'])]),
              lambda r: ok('a short scenic video with stills', action='offer', offer_grant_id=_grant_from_prompt(r), pitch=True)]
    ITEM = {}
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(database, script)
    ITEM['id'] = item_id
    for mid, body in [(1, 'no thanks'), (2, 'actually show me the scenic video'), (3, 'the scenic video please')]:
        if mid > 1:
            unthrottle(database[1])
        incoming(store, mid, body)
        decision = engine.plan(TENANT, ACCOUNT, FAN, mid)
        if mid == 2:
            assert 'SALES STATE: PAUSED' in client.requests[1].messages[1]['content']
            assert 'APPROVED OFFER: none' in client.requests[1].messages[1]['content']  # paused: the grant is hidden
            with Session(database[1]) as s:
                owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
                assert (owner.sales_mode, owner.sales_scope_json) == ('reopened_scoped', json.dumps([str(item_id)]))
        provider = SyntheticFansProvider(message_id=9000 + mid)
        assert authority.dispatch(TENANT, ACCOUNT, engine.queue(TENANT, ACCOUNT, decision), wire(store, provider)) == 'accepted'
    assert f'APPROVED OFFER: id {grant}' in client.requests[2].messages[1]['content']
    assert provider.calls[0]['body']['text'].endswith('USD 10.00.')
    store.close()
