"""Retain an unanswered correction across an actually receipted partial reply."""
import json

from sqlalchemy.orm import Session

from db.onlyfans_moxy_models import MoxyEvent
from db.onlyfans_delivery_models import OfTextIntent
from moxy_replacement.runtime import snapshot
from test_moxy_integrated_pg import setup, decision, model
from test_moxy_reply_recovery_pg import add, row, send
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT, accepted


def incoming(store, mid, body):
    store.record(TENANT, ACCOUNT, event(mid=mid, body=body))
    store.drain(TENANT, ACCOUNT)


def pending(engine, identity):
    with Session(engine) as session:
        current = session.get(MoxyEvent, (ACCOUNT, identity))
        return [item['id'] for item in snapshot(session, ACCOUNT, 101, current)['pending_fan_messages']]


def test_unanswered_question_survives_a_receipted_reply_and_then_clears(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 2, 'When did I say that?')
        incoming(store, 3, 'hey')
        first = add(service, key='partial-reply', mid=3)
        parts = [{'text': 'Hey, lovely to hear from you.', 'media': False},
                 {'text': 'How has your afternoon been?', 'media': False}]
        service.prepare(TENANT, ACCOUNT, first, model(decision(
            text=parts[0]['text'], messages=parts, unanswered_input_ids=[2])))
        assert row(engine, first).status == 'ready', row(engine, first).reason
        assert '_moxy_conversation_progress' not in json.loads(row(engine, first).payload_json)
        result, _, _ = send(service, authority, engine, first, mid=9101)
        assert result == 'accepted'
        assert '_moxy_conversation_progress' not in json.loads(row(engine, first).payload_json)
        result, _, _ = send(service, authority, engine, first, mid=9102)
        assert result == 'accepted'
        annotation = json.loads(row(engine, first).payload_json)['_moxy_conversation_progress']
        assert annotation == {'version': 1, 'unanswered_input_ids': [2]}
        incoming(store, 4, 'What about the earlier question?')
        second = add(service, key='answer-earlier', mid=4)
        assert pending(engine, second) == [2, 4]
        service.prepare(TENANT, ACCOUNT, second, model(decision(
            text='You are right, I got that wrong.', unanswered_input_ids=[])))
        assert row(engine, second).status == 'ready', row(engine, second).reason
        assert send(service, authority, engine, second, mid=9103)[0] == 'accepted'
        incoming(store, 5, 'Thanks for explaining.')
        third = add(service, key='next-question', mid=5)
        assert pending(engine, third) == [5]
    finally:
        store.close()


def test_an_uncertain_send_does_not_commit_conversation_progress(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 2, 'When did I say that?')
        first = add(service, key='uncertain-reply', mid=2)
        service.prepare(TENANT, ACCOUNT, first, model(decision(
            text='I hear you.', unanswered_input_ids=[2])))
        assert row(engine, first).status == 'ready', row(engine, first).reason
        result, wire, intent_id = send(service, authority, engine, first, outcome='accepted_lost_response')
        assert result == 'unknown' and len(wire.calls) == 1
        assert '_moxy_conversation_progress' not in json.loads(row(engine, first).payload_json)
        incoming(store, 3, 'What about my question?')
        second = add(service, key='after-uncertain', mid=3)
        assert 2 in pending(engine, second) and 3 in pending(engine, second)
        next_model = model(decision(text='I hear you.', unanswered_input_ids=[]))
        service.prepare(TENANT, ACCOUNT, second, next_model)
        assert not next_model.calls
        with Session(engine) as session:
            assert session.get(OfTextIntent, (ACCOUNT, intent_id)).status == 'unknown'
            assert session.query(OfTextIntent).count() == 1
    finally:
        store.close()


def test_exact_late_receipt_allows_next_message_without_replaying_unknown_send(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 2, 'When did I say that?')
        first = add(service, key='lost-reply', mid=2)
        service.prepare(TENANT, ACCOUNT, first, model(decision(
            text='I hear you.', unanswered_input_ids=[2])))
        result, wire, intent_id = send(service, authority, engine, first,
                                       mid=9101, outcome='accepted_lost_response')
        assert result == 'unknown' and len(wire.calls) == 1
        incoming(store, 3, 'What about my question?')
        second = add(service, key='next-after-proof', mid=3)
        waiting = model(decision(text='You did not say it; I got that wrong.', unanswered_input_ids=[]))
        service.prepare(TENANT, ACCOUNT, second, waiting)
        assert not waiting.calls
        dispatch = wire.calls[0]
        assert authority.record_accepted(TENANT, ACCOUNT, intent_id, dispatch.token,
                                         accepted(dispatch, message_id=9101)) == 'accepted'
        assert authority.repair(TENANT, ACCOUNT) == 1
        assert len(wire.calls) == 1
        assert json.loads(row(engine, first).payload_json)['_moxy_conversation_progress']['unanswered_input_ids'] == [2]
        assert pending(engine, second) == [2, 3]
        service.prepare(TENANT, ACCOUNT, second, waiting)
        assert row(engine, second).status == 'ready', row(engine, second).reason
        next_result, next_wire, _ = send(service, authority, engine, second, mid=9102)
        assert next_result == 'accepted' and len(next_wire.calls) == 1 and len(wire.calls) == 1
    finally:
        store.close()


def test_scheduler_progresses_next_input_after_late_receipt_without_replay(database, monkeypatch):
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from test_onlyfans_text_delivery_pg import unthrottle
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 2, 'When did I say that?')
        first = add(service, key='scheduler-lost-reply', mid=2)
        service.prepare(TENANT, ACCOUNT, first, model(decision(
            text='I hear you.', unanswered_input_ids=[2])))
        result, wire, intent_id = send(service, authority, engine, first,
                                       mid=9101, outcome='accepted_lost_response')
        assert result == 'unknown' and len(wire.calls) == 1
        incoming(store, 3, 'What about my question?')
        second = add(service, key='scheduler-next-after-proof', mid=3)
        next_model = model(decision(text='You did not say it; I got that wrong.', unanswered_input_ids=[]))
        assert service.step(TENANT, ACCOUNT, next_model) == 0
        assert len(wire.calls) == 1
        dispatch = wire.calls[0]
        assert authority.record_accepted(TENANT, ACCOUNT, intent_id, dispatch.token,
                                         accepted(dispatch, message_id=9101)) == 'accepted'
        assert authority.repair(TENANT, ACCOUNT) == 1
        assert pending(engine, second) == [2, 3]
        assert service.step(TENANT, ACCOUNT, next_model) == 1
        assert row(engine, second).status == 'queued'
        unthrottle(engine)
        next_wire = SyntheticTextTransport(ACCOUNT, message_id=9102)
        assert authority.dispatch(TENANT, ACCOUNT, row(engine, second).intent_id, next_wire) == 'accepted'
        assert len(next_wire.calls) == 1 and len(wire.calls) == 1
    finally:
        store.close()


def test_legacy_receipt_does_not_replay_prior_messages(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 2, 'Earlier question?')
        first = add(service, key='legacy-reply', mid=2)
        service.prepare(TENANT, ACCOUNT, first, model(decision(text='That sounds good.')))
        # A stored event from before the field existed: strip it from the saved decision (explicit legacy fixture).
        with Session(engine) as s, s.begin():
            e = s.get(MoxyEvent, (ACCOUNT, first)); d = json.loads(e.decision_json)
            d.pop('unanswered_input_ids', None); e.decision_json = json.dumps(d)
        assert send(service, authority, engine, first)[0] == 'accepted'
        assert '_moxy_conversation_progress' not in json.loads(row(engine, first).payload_json)
        incoming(store, 3, 'New question?')
        second = add(service, key='after-legacy', mid=3)
        assert pending(engine, second) == [3]
    finally:
        store.close()
