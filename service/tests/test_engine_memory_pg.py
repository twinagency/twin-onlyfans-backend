"""Shared fan-memory projection, corrections and final-send freshness."""
from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import Message
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from onlyfans_runtime.store import Refused, utcnow


def setup(database):
    store, authority = configured(database, mode='automated')
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_memory_fixture', 'creator-fixture')
    model = SyntheticModel({'hello':SyntheticProposal('Hello.'), 'next':SyntheticProposal('Understood.')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    return store, authority, engine, model, HistoricalMemory(store)


def correct(memory, *, value, key):
    before = memory.read(TENANT, ACCOUNT, FAN)
    return memory.correct(TENANT, ACCOUNT, FAN, field='home_city', operation='set' if value is not None else 'clear',
        value=value, expected_version=before['version'], command_key=key, actor='op-session:' + 'a'*64, reason='Verified correction')


def test_engine_and_sidebar_use_same_correction_and_clear(database):
    store, authority, engine, model, memory = setup(database)
    correct(memory, value='Brisbane', key='set-city')
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    sidebar = memory.read(TENANT, ACCOUNT, FAN, query='hello')
    engine_memory = model.calls[-1]['memory']
    assert engine_memory['state'] == 'current'
    assert engine_memory['memory']['fingerprint'] == sidebar['fingerprint']
    assert engine_memory['memory']['facts']['home_city']['value'] == 'Brisbane'
    correct(memory, value=None, key='clear-city')
    incoming(store, 2, 'next')
    engine.plan(TENANT, ACCOUNT, FAN, 2)
    assert 'home_city' not in model.calls[-1]['memory']['memory']['facts']
    assert model.calls[-1]['memory']['memory']['overrides']['home_city']['operation'] == 'clear'
    store.close()


def test_correction_after_queue_cancels_before_wire(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    correct(memory, value='Perth', key='late-correction')
    provider = SyntheticFansProvider()
    result = authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider))
    assert result == 'cancelled' and provider.calls == []
    store.close()


def test_memory_change_during_generation_prevents_decision(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    original = model.generate
    def generate(snapshot):
        answer = original(snapshot)
        correct(memory, value='Darwin', key='during-generation')
        return answer
    model.generate = generate
    with pytest.raises(Refused, match='memory changed'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    store.close()


def test_unscoped_history_is_not_exposed_as_memory_or_transcript(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    with Session(database[1]) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=7000, direction='out',
                      body='private prior binding', created_at=utcnow()-timedelta(days=2)))
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    snapshot = model.calls[-1]
    assert snapshot['memory'] == {'state':'unavailable', 'reason':'unscoped_history_requires_review'}
    assert snapshot['history_scope_incomplete'] is True
    assert all(row['body'] != 'private prior binding' for row in snapshot['history'])
    store.close()


def test_memory_and_history_do_not_cross_creator_or_fan_scope(database):
    from test_onlyfans_foundation_pg import seed, event
    from test_onlyfans_text_delivery_pg import current
    from db.models import Fan, OfConversationState
    store, authority, engine, model, memory = setup(database)
    other = seed(database[0], database[1], account='acc-b', tenant=TENANT, fan=FAN)
    current(database[1], mode='automated', account='acc-b', fan=FAN)
    authority.configure(TENANT, 'acc-b', expected_revision=0, enabled=True,
        limit_units=20, throttle_seconds=1, actor='synthetic-operator', reason='Offline test')
    FansWebhookAdapter(store).bind_simulated(TENANT, 'acc-b', 'acct_other_memory', 'creator-other')
    engine.select_simulated(TENANT, 'acc-b', FAN)
    with Session(database[1]) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=202))
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=202, mode='automated', revision=0,
            takeover_state='none', requires_attention=False))
    engine.select_simulated(TENANT, ACCOUNT, 202)
    correct(memory, value='Brisbane', key='scope-city')
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert model.calls[-1]['memory']['memory']['facts']['home_city']['value'] == 'Brisbane'
    for account, fan in [('acc-b', FAN), (ACCOUNT, 202)]:
        assert store.record(TENANT, account, event(mid=1, fan=fan, body='hello')) == 'accepted'
        store.drain(TENANT, account)
        engine.plan(TENANT, account, fan, 1)
        snapshot = model.calls[-1]
        assert snapshot['account_id'] == account and snapshot['fan_id'] == fan
        assert 'home_city' not in snapshot['memory']['memory']['facts']
        assert all('Brisbane' not in row['body'] for row in snapshot['history'])
    other.close(); store.close()
