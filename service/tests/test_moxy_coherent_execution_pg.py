"""Complete emission/restart regressions; synthetic model and private PostgreSQL only."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db.models import GrokCall, GrokDailyCost
from db.onlyfans_moxy_models import MoxyEvent
from db.onlyfans_delivery_models import OfTextIntent
from moxy_replacement import writer
from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.runtime import MoxyService
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.native_planning import SyntheticNativeModel
from test_moxy_integrated_pg import setup, decision, model
from test_moxy_reply_recovery_pg import add, row, send
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


@pytest.mark.parametrize('field,value', [('action', []), ('action', {}),
    ('flags', [{}]), ('request_kind', [])])
def test_malformed_types_are_typed_rejections(field, value):
    raw = decision(**{field: value})
    with pytest.raises(ActionBlocked):
        writer.parse(json.dumps(raw))


def test_ordinary_heart_is_not_markup():
    assert writer.parse(json.dumps(decision(text='Lovely hearing from you <3')))['text'].endswith('<3')


@pytest.mark.parametrize('parts', [
    [('That outfit photo is $75.', False), ('Here is the photograph.', True)],
    [('Here is the photograph.', True), ('The outfit photo is $75.', False)],
    [('Want to see the outfit photo?', False), ('Here is that photograph.', True)],
])
def test_parent_binding_survives_each_actual_delivery_and_restart(database, monkeypatch, parts):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        identity = add(service)
        messages = [{'text': text, 'media': media} for text, media in parts]
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            'offer', messages[0]['text'], 'first', sequence='fixture_level', messages=messages,
            reason='The first product is the bound outfit photograph.')))
        assert row(engine, identity).status == 'ready', row(engine, identity).reason
        wires = []
        for number in range(len(parts)):
            result, wire, _ = send(MoxyService(store), authority, engine, identity, mid=9301+number)
            assert result == 'accepted'
            wires.extend(wire.calls)
        assert [w.price_cents for w in wires] == [7500 if media else 0 for _, media in parts]
        assert [w.media_ids for w in wires] == [(7101,) if media else () for _, media in parts]
        assert row(engine, identity).status == 'done'
        assert MoxyService(store).step(TENANT, ACCOUNT, model(decision())) == 0
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == len(parts)
        store.record(TENANT, ACCOUNT, event(mid=2, body='How was your afternoon?'))
        store.drain(TENANT, ACCOUNT)
        later = add(service, key='later', mid=2)
        service.prepare(TENANT, ACCOUNT, later, model(decision(text='What made yours interesting?')))
        assert row(engine, later).status == 'ready'
    finally:
        store.close()


def test_unexpected_preparation_failure_does_not_skip_admitted_delivery(database, monkeypatch):
    from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount, CycleReport
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from test_onlyfans_text_delivery_pg import unthrottle
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        identity = add(service)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(text='How was the trip?')))
        intent = service.queue(TENANT, ACCOUNT, identity)
        wire = SyntheticTextTransport(ACCOUNT)
        account = WorkerAccount(ACCOUNT, model(decision()), wire)
        worker = NativeWorker(store, TENANT, (account,))
        # Fault after admission, during unrelated preparation. The public
        # worker must still dispatch via its existing authority, not a bypass.
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic unrelated preparation failure')
        monkeypatch.setattr(MoxyService, 'step', fail)
        unthrottle(engine)
        report = CycleReport()
        with pytest.raises(RuntimeError):
            worker._moxy_turn(account, report)
        assert report.accepted == 1 and len(wire.calls) == 1
        with pytest.raises(RuntimeError):
            worker._moxy_turn(account, report)
        assert len(wire.calls) == 1
        with Session(engine) as s:
            assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'accepted'
    finally:
        store.close()


def test_deterministic_queue_rejection_is_retired_not_repeated(database, monkeypatch):
    import moxy_replacement.runtime as runtime
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        identity = add(service)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(text='How was the trip?')))
        original = runtime.validate_decision
        def reject(settings, snap, d, **kwargs):
            if not kwargs.get('planning'):
                raise ActionBlocked('synthetic_prepare_contract_rejection')
            return original(settings, snap, d, **kwargs)
        monkeypatch.setattr(runtime, 'validate_decision', reject)
        assert service.step(TENANT, ACCOUNT, model(decision())) == 0
        assert row(engine, identity).status == 'held'
        assert row(engine, identity).reason == 'synthetic_prepare_contract_rejection'
        assert service.step(TENANT, ACCOUNT, model(decision())) == 0
        monkeypatch.setattr(runtime, 'validate_decision', original)
        store.record(TENANT, ACCOUNT, event(mid=2, body='How was your afternoon?'))
        store.drain(TENANT, ACCOUNT)
        later = add(service, key='after-rejection', mid=2)
        service.prepare(TENANT, ACCOUNT, later, model(decision(text='What made yours interesting?')))
        assert row(engine, later).status == 'ready'
        assert send(service, authority, engine, later)[0] == 'accepted'
    finally:
        store.close()


@pytest.mark.parametrize('raw', ['{broken', json.dumps(decision(action=[]))])
def test_restart_after_saved_bad_output_uses_same_single_correction(database, monkeypatch, raw):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        identity = add(service)
        # Simulate a process dying after immutable provider-result recording,
        # before parse/disposition/settlement. Keep the real recorded raw proof.
        service.prepare(TENANT, ACCOUNT, identity, SyntheticNativeModel(raw_output=raw))
        with Session(engine) as s, s.begin():
            e = s.get(MoxyEvent, (ACCOUNT, identity))
            for other in list(s.scalars(select(MoxyEvent))):
                if json.loads(other.payload_json).get('correction_of') == identity:
                    s.delete(other)
            call = s.get(GrokCall, e.model_call_id)
            ledger = s.scalar(select(GrokDailyCost))
            ledger.cost_cents += e.reserved_millicents - call.cost_cents
            call.status = 'pending'; call.cost_cents = e.reserved_millicents
            e.charged_millicents = None; e.decision_json = None
            e.status = 'generating'; e.lease_until = utcnow() - timedelta(seconds=1)
        restarted = MoxyService(store)
        restarted.step(TENANT, ACCOUNT, model(decision(text='How has your afternoon been?')))
        restarted.step(TENANT, ACCOUNT, model(decision(text='This should never be used.')))
        with Session(engine) as s:
            corrections = [e for e in s.scalars(select(MoxyEvent))
                           if json.loads(e.payload_json).get('correction_of') == identity]
            assert len(corrections) == 1
            fixed = corrections[0]
            assert fixed.status == 'queued', fixed.reason
            assert s.scalar(select(func.count()).select_from(GrokCall)) == 2
            intent = fixed.intent_id
        from onlyfans_runtime.text_delivery import SyntheticTextTransport
        assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == 'accepted'
    finally:
        store.close()
