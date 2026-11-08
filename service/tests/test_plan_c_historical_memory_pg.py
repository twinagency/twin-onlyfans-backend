"""Selected stored history reaches the real model request as evidence, never authority."""
from datetime import datetime
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event, seed
from test_engine_model_pg import with_model, ok
from test_engine_simulated_journey_pg import incoming
from test_engine_memory_pg import correct
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from conversation_engine.writer_context import CreatorProfile, PromptBudget, build_context
from conversation_engine.prompt_provider_a_original import SpecPromptProvider
from db.models import Message, OfMessageDelivery
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from onlyfans_runtime.profile_learning.evidence import merge, validate
from onlyfans_runtime.profile_learning.storage import reply_memory
from onlyfans_runtime.profile_learning.worker import fingerprint


@pytest.fixture(autouse=True)
def learning_schema(request):
    if 'database' not in request.fixturenames:
        return
    _, db_engine = request.getfixturevalue('database')
    with db_engine.begin() as connection:
        connection.exec_driver_sql((Path(__file__).parents[2] / 'deploy/profile-learning/schema.sql').read_text())
        connection.exec_driver_sql((Path(__file__).parents[2] / 'deploy/profile-learning/notifications-postgresql.sql').read_text())


def archive(database, store, mid, value, *, field='event', status=None, previous=(), operation='assert', quote=None):
    body = quote or ('Cancel ' + value if operation == 'cancel' else 'I planned ' + value + ' on 2026-01-02')
    incoming(store, mid, body)
    with Session(database[1]) as s, s.begin():
        message = s.get(Message, (ACCOUNT, FAN, mid))
        message.created_at = datetime(2026, 1, 1)
        source = f'{FAN}:{mid}'
        observations = validate({'facts': [{'field': field, 'value': value, 'key': value,
            'source': source, 'quote': body, 'operation': operation,
            'date_text': '2026-01-02' if field == 'event' and operation != 'cancel' else ''}]},
            {source: {'text': body, 'direction': 'in', 'at': message.created_at.isoformat(),
                      'digest': fingerprint(message), 'fan': FAN}})
        assert len(observations) == 1
        fact, = merge(list(previous), observations)
        if status is not None:
            # Exercise the existing archived row shape; lifecycle/retrieval policy is unchanged.
            fact['status'] = status
        runtime = store._runtime(s, TENANT, ACCOUNT)
        s.execute(text('''INSERT INTO of_profile_memories
            (tenant_id,account_id,binding_id,fan_id,memory_key,field,status,kind,document_json)
            VALUES (:tenant,:account,:binding,:fan,:key,:field,:status,:kind,:doc)
            ON CONFLICT (tenant_id,account_id,binding_id,fan_id,memory_key)
            DO UPDATE SET status=EXCLUDED.status,document_json=EXCLUDED.document_json'''),
            dict(tenant=TENANT,account=ACCOUNT,binding=runtime.binding_id,fan=FAN,
                 key=fact['memory_key'],field=field,status=fact['status'],kind=fact['kind'],doc=json.dumps(fact)))
    return fact, body


def render_old(database, store, engine, client, question):
    for mid in range(100, 120):
        incoming(store, mid, 'An intervening ordinary message')
    incoming(store, 120, question)
    engine.plan(TENANT, ACCOUNT, FAN, 120)
    return client.requests[-1].messages[1]['content']


@pytest.mark.parametrize('status', ['cancelled', 'expired', 'superseded'])
def test_selected_stored_history_reaches_actual_request(database, monkeypatch, status):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    store, _, engine, runtime, client, _ = with_model(database, [ok('I remember what you told me.')])
    try:
        field = 'location' if status == 'superseded' else 'event'
        fact, original = archive(database, store, 1, 'lagoon festival', field=field,
                                 status='superseded' if status == 'superseded' else None)
        if status == 'cancelled':
            fact, cancellation = archive(database, store, 2, 'lagoon festival', previous=(fact,), operation='cancel')
        with Session(database[1]) as s:
            selected = reply_memory(s, ACCOUNT, FAN, query='lagoon festival')['memories']
        assert len(selected) == 1 and selected[0]['status'] == status
        if status == 'expired':
            runtime.budget = PromptBudget(memory_chars=350)
        prompt = render_old(database, store, engine, client, 'Do you remember the lagoon festival?')
        history = prompt.split('\n\nLATEST:\n', 1)[0]
        assert original not in history and 'lagoon festival' not in history
        memory = prompt.split('WHAT SHE KNOWS ABOUT HIM', 1)[1].split('\n\n', 1)[0]
        assert 'lagoon festival' in memory and status in memory and 'historical' in memory
        assert selected[0]['evidence'][-1]['source'] in memory
        assert 'last_confirmed 2026-01-01' in memory
        if status == 'cancelled':
            assert cancellation in memory and 'not active or action authority' in memory
        if status == 'expired':
            assert 'event_start 2026-01-02' in memory and 'outcome unconfirmed' in memory
            assert 'omitted' in memory and 'source_quote' not in memory
        if status == 'superseded':
            assert 'not current fact' in memory
    finally:
        store.close()


def test_irrelevant_invalidated_and_withdrawn_history_stays_excluded(database, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    store, _, engine, _, client, _ = with_model(database, [ok('Hello.')])
    try:
        archive(database, store, 1, 'orchard picnic', status='cancelled')
        archive(database, store, 2, 'lagoon secret', status='invalidated')
        archive(database, store, 3, 'lagoon withdrawn', status='superseded')
        with Session(database[1]) as s, s.begin():
            s.get(Message, (ACCOUNT, FAN, 3)).is_unsent = True
        prompt = render_old(database, store, engine, client, 'Do you remember the lagoon?')
        assert all(value not in prompt for value in ('orchard picnic', 'lagoon secret', 'lagoon withdrawn'))
    finally:
        store.close()


@pytest.mark.parametrize('value', ['Brisbane', None])
def test_stored_historical_location_obeys_operator_override(database, monkeypatch, value):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    store, _, engine, _, client, memory = with_model(database, [ok('Hello.')])
    try:
        archive(database, store, 1, 'Oldtown', field='location', status='superseded')
        correct(memory, value=value, key='historical-location-override')
        prompt = render_old(database, store, engine, client, 'Do you remember Oldtown?')
        section = prompt.split('WHAT SHE KNOWS ABOUT HIM', 1)[1].split('\n\n', 1)[0]
        assert 'Oldtown' not in section
        assert ('his city: Brisbane' if value else 'Operator-cleared fields') in section
        with Session(database[1]) as s:
            assert reply_memory(s, ACCOUNT, FAN, query='Oldtown')['memories'] == []
    finally:
        store.close()


def test_stored_historical_memory_remains_creator_and_fan_scoped(database, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    from test_onlyfans_text_delivery_pg import current
    from fans_api_adapter.intake import FansWebhookAdapter
    from conversation_engine.writer_context import StaticCreatorProfiles
    from db.models import Fan, OfConversationState
    from test_engine_model_pg import config, PROFILE
    store, authority, engine, runtime, client, _ = with_model(database, [ok('Hello.'), ok('Hello.')])
    other = None
    try:
        archive(database, store, 1, 'lagoon festival', status='cancelled')
        other = seed(database[0], database[1], account='acc-b', tenant=TENANT, fan=FAN)
        current(database[1], mode='automated', account='acc-b', fan=FAN)
        authority.configure(TENANT, 'acc-b', expected_revision=0, enabled=True, limit_units=20,
                            throttle_seconds=1, actor='synthetic-operator', reason='Offline test')
        FansWebhookAdapter(store).bind_simulated(TENANT, 'acc-b', 'acct_other_history', 'creator-other')
        engine.select_simulated(TENANT, 'acc-b', FAN)
        with Session(database[1]) as s, s.begin():
            s.add(Fan(account_id=ACCOUNT, fan_id=202))
            s.add(OfConversationState(account_id=ACCOUNT, fan_id=202, mode='automated', revision=0,
                                     takeover_state='none', requires_attention=False))
        engine.select_simulated(TENANT, ACCOUNT, 202)
        runtime.config = config(enabled_accounts=frozenset({ACCOUNT, 'acc-b'}))
        runtime.profiles = StaticCreatorProfiles({ACCOUNT: PROFILE, 'acc-b': PROFILE})
        for account, fan in [('acc-b', FAN), (ACCOUNT, 202)]:
            with Session(database[1]) as s:
                assert reply_memory(s, account, fan, query='lagoon festival')['memories'] == []
            store.record(TENANT, account, event(mid=100, fan=fan, body='What about the lagoon festival?'))
            store.drain(TENANT, account)
            engine.plan(TENANT, account, fan, 100)
            prompt = client.requests[-1].messages[1]['content']
            assert 'cancelled, historical' not in prompt and f'source {FAN}:1' not in prompt
            assert 'I planned lagoon festival' not in prompt
    finally:
        if other: other.close()
        store.close()


def test_cancelled_historical_request_does_not_reopen_work_or_send(database, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    store, _, engine, _, client, _ = with_model(database, [ok('I remember the cancellation.')])
    try:
        fact, _ = archive(database, store, 1, 'lagoon photo', field='request', quote='Please send lagoon photo')
        archive(database, store, 2, 'lagoon photo', field='request', previous=(fact,), operation='cancel')
        prompt = render_old(database, store, engine, client, 'Do you remember the lagoon photo request?')
        assert 'cancelled, historical; not active or action authority' in prompt
        with Session(database[1]) as s:
            requests = list(s.scalars(select(OfEngineRequest)))
            assert [r.source_message_id for r in requests] == [120]
            decisions = list(s.scalars(select(OfEngineDecision)))
            assert len(decisions) == 1 and decisions[0].action == 'reply'
            assert s.scalar(select(func.count()).select_from(OfMessageDelivery)) == 0
    finally:
        store.close()


@pytest.mark.parametrize('status', ['cancelled', 'expired', 'superseded'])
def test_historical_status_and_uncertainty_survive_prompt_compaction(status):
    item = dict(field='event',value='lagoon festival',subject='fan',status=status,
        potentially_stale=True,last_confirmed='2026-01-01',event_start='2026-01-02',event_end='2026-01-02',
        review_at='2026-01-08',active_until='2026-01-16',
        evidence=[dict(source='101:1',at='2026-01-01',quote='evidence '*80)],
        contextEvidence=[dict(quote='question '*80)])
    budget = PromptBudget(memory_chars=420)
    context = build_context(dict(current_message=dict(id=99,body='Remember the lagoon festival?'),history=[],
        sales_mode='open',memory=dict(state='current',memory=dict(learnedProfile=dict(memories=[item])))),
        CreatorProfile(name='Fixture',limits=('No meetups',)),budget=budget)
    prompt = SpecPromptProvider().render(context).messages[1]['content']
    assert status in prompt and 'historical' in prompt and 'potentially stale' in prompt and 'source 101:1' in prompt
    assert '2026-01-02' in prompt and '2026-01-08' in prompt and '2026-01-16' in prompt
    assert 'source_quote' not in prompt and 'omitted' in prompt
    assert len('; '.join(context.memory_lines)) <= budget.memory_chars
    if status == 'expired': assert 'outcome unconfirmed' in prompt
