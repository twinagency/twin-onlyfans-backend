"""R0 acceptance: real scheduler/runtime/ledger and operator HTTP; only transports are mocked.

All data are synthetic. Historical labels refer to production-shaped fixtures, never live rows or provider proof.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from conversation_engine import operator_v2
from db.models import OfConversationState
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineModelCall, OfEngineOwner, OfEngineRequest
from onlyfans_runtime.store import Refused, utcnow
from test_plan_c_model_retry_due_pg import (make_world, database, world, configured_draft, stopped_text,
                                           post, ACCOUNT, TENANT, FAN, FAN2, ACTOR, key)


def items(w, monkeypatch):
    """Retrieve through the actual authenticated operator route, using the worker's deterministic clock."""
    from onlyfans_commands.engine_approval_api import router, HEADER, SCHEMA
    app = FastAPI()
    app.state.control_store = w.w.store
    app.include_router(router)
    with monkeypatch.context() as m:
        m.setattr(operator_v2, 'utcnow', w.clock)
        m.setenv('ONLYFANS_PROCESS_ROLE', 'api')
        m.setenv('ONLYFANS_CONTROL_TOKEN', 'r0-fixture-operator-token')
        m.setenv('ONLYFANS_CONTROL_TENANT', TENANT)
        for name in ('ONLYFANS_API_TOKEN', 'ONLYFANS_CONFIGURATION_TOKEN', 'ONLYFANS_CAMPAIGN_TOKEN',
                     'ONLYFANS_CONTENT_TOKEN', 'ONLYFANS_CONNECTION_TOKEN'):
            m.delenv(name, raising=False)
        with TestClient(app) as client:
            response = client.get(f'/onlyfans/control/v2/accounts/{ACCOUNT}/unresolved', headers={
                'Authorization': 'Bearer r0-fixture-operator-token', HEADER: SCHEMA, 'x-plan-c-actor': ACTOR})
            assert response.status_code == 200, response.text
            return response.json()['items']


def request_row(w, source):
    with Session(w.w.database[1]) as s:
        return s.scalar(select(OfEngineRequest).where(OfEngineRequest.source_message_id == source,
                                                     OfEngineRequest.account_id == ACCOUNT))


def request_item(w, source, monkeypatch):
    rid = request_row(w, source).request_id
    found = [i for i in items(w, monkeypatch) if i.get('requestId') == rid]
    assert len(found) == 1, found
    return found[0]


def test_retry_peer_preserves_active_identity_verification_lease(make_world, monkeypatch):
    """Peer selected before settlement must not hand over an owner's pending identity transaction."""
    from concurrent.futures import ThreadPoolExecutor
    w = make_world([429, 'ok'])
    post(w, 92901)
    w.ticks(1)
    w.advance(31)
    peer = w.new_pilot()
    owner_ledger = w.pilot.worker.engine.model.ledger
    peer_ledger = peer.worker.engine.model.ledger
    selected, pending, peer_done = threading.Event(), threading.Event(), threading.Event()
    real_rows, real_note = peer_ledger.rows, owner_ledger.note_identity

    def read_after_owner_settles(*args, **kwargs):
        selected.set()  # scheduler + engine selected the still-open request before the owner starts
        assert pending.wait(30), 'owner did not reach persisted identity_pending'
        return real_rows(*args, **kwargs)

    def pause_identity(*args, **kwargs):
        real_note(*args, **kwargs)
        pending.set()  # settled response exists, identity verdict is not yet persisted
        assert peer_done.wait(30), 'peer did not finish its leased-call observation'

    monkeypatch.setattr(peer_ledger, 'rows', read_after_owner_settles)
    monkeypatch.setattr(owner_ledger, 'note_identity', pause_identity)
    with ThreadPoolExecutor(max_workers=2) as pool:
        observer = pool.submit(peer.tick)
        assert selected.wait(30), 'peer did not select due retry'
        # Its previous 429 tick retained the scheduler page cursor; the first tick wraps, the second selects.
        owner = pool.submit(w.ticks, 2)
        try:
            observer.result(timeout=30)
            assert w.request(92901) == ('open', 'prepare_reply', None)
            calls = w.calls()
            assert len(calls) == 2 and calls[-1].outcome == 'identity_pending'
            assert calls[-1].lease_expires_at > w.clock()
            assert w.w.fans.posts == []
        finally:
            peer_done.set()
        owner.result(timeout=30)
    w.ticks(3)
    assert w.provider.calls == [429, 'ok']
    assert w.request(92901)[0] == 'completed'
    assert len(w.w.fans.posts) == 1 and w.out_messages() == 1
    assert w.w.fans.posts[0]['body']['text'] == 'hey you'
    assert [c.outcome for c in w.calls()] == ['http_429', 'verified_ok']


@pytest.mark.parametrize('delay', ['7200', '9' * 60, '9' * 100, '9' * 10000, '0' * 100 + '7200', 'http-date'],
                         ids=['seconds', 'large', 'overflow', 'oversized', 'leading-zeroes', 'http-date'])
def test_long_provider_delay_is_operator_work_never_an_early_retry(make_world, monkeypatch, delay):
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=7200), usegmt=True) if delay == 'http-date' else delay
    w = make_world([(429, {'Retry-After': value})])
    post(w, 92001)
    w.ticks(1)
    [call] = w.calls()
    assert call.retry_not_before - call.updated_at > timedelta(seconds=3600)
    if len(value.lstrip('0')) > 64:
        detail = json.loads(call.failure_detail_json)
        assert detail['retryAfterBeyondRange'] is True and len(call.failure_detail_json.encode()) <= 2000
        assert call.retry_not_before == datetime.max
    assert w.request(92001) == ('handover_pending', 'await_handover_acceptance', 'operator')
    shown = request_item(w, 92001, monkeypatch)
    assert shown['recoveryState'] == 'deferred_operator' and shown['operatorAlert']
    assert shown['recoveryReason'] == 'model_retry_deferred'
    for seconds in (31, 3599, 3601, 7201, 100000):
        w.advance(seconds)
        w.ticks(2, w.new_pilot())
    assert w.provider.calls == [429] and w.w.fans.posts == []
    assert len(w.calls()) == 1 and w.calls()[0].retry_not_before == call.retry_not_before


def test_http_date_deadline_survives_restart_and_two_workers(make_world):
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    w = make_world([(429, {'Retry-After': value})])
    post(w, 92002)
    w.ticks(1)
    [call] = w.calls()
    assert timedelta(seconds=115) < call.retry_not_before - call.updated_at <= timedelta(seconds=121)
    a, b = w.new_pilot(), w.new_pilot()
    w.advance(90)
    w.ticks(2, a); w.ticks(2, b)
    assert w.provider.calls == [429]
    w.advance(122)
    errors = []
    def tick(p):
        try: p.tick()
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=tick, args=(p,)) for p in (a, b)]
    for t in threads: t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive()
    assert errors == []
    w.ticks(3)
    assert w.provider.calls == [429, 'ok'] and w.request(92002)[0] == 'completed'
    assert len(w.w.fans.posts) == 1 and w.w.fans.posts[0]['body']['text'] == 'hey you'
    assert [c.seq for c in w.calls()] == [0, 1]


@pytest.mark.parametrize('script,expected', [(['invalid', 429], ['invalid', 'http_429']),
    (['invalid', 401], ['invalid', 'http_401']), ([429, 401], ['http_429', 'http_401'])])
def test_mixed_correction_and_transport_history_never_gets_a_third_call(make_world, monkeypatch, script, expected):
    w = make_world(script)
    post(w, 92003)
    w.ticks(1)
    w.advance(31)
    w.ticks(3)
    assert w.provider.calls == script
    assert [c.outcome for c in w.calls()] == expected
    assert [c.seq for c in w.calls()] == [0, 1] and {c.generation for c in w.calls()} == {0}
    assert w.request(92003) == ('handover_pending', 'await_handover_acceptance', 'operator')
    shown = request_item(w, 92003, monkeypatch)
    assert shown['operatorAlert'] and shown['attemptsRecorded'] == 2
    w.advance(100000)
    w.ticks(3, w.new_pilot())
    assert w.provider.calls == script and w.w.fans.posts == []


@pytest.mark.parametrize('mode,action', [('paused', 'pause_automation'), ('no_contact', 'mark_no_contact'),
                                       ('human_controlled', 'request_takeover')])
def test_current_control_blocks_retry_and_is_not_an_overdue_alert(make_world, monkeypatch, mode, action):
    w = make_world([429])
    post(w, 92004)
    w.ticks(1)
    with Session(w.w.database[1]) as s: revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    w.w.store.control(TENANT, ACCOUNT, FAN, mode=mode, expected_revision=revision, command_key='r0-control',
                      actor=ACTOR, reason='synthetic pending retry control', action=action)
    w.advance(600)
    w.ticks(3)
    shown = request_item(w, 92004, monkeypatch)
    assert shown['recoveryState'] == 'intentional_control_hold' and not shown['operatorAlert']
    assert w.provider.calls == [429] and w.w.fans.posts == []


@pytest.mark.parametrize('first', [429, 'timeout'])
def test_legitimate_handback_retains_attempts_and_unknown_evidence(make_world, first):
    w = make_world([first])
    post(w, 92005)
    w.ticks(1)
    original = w.calls()[0]
    with Session(w.w.database[1]) as s: revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    w.w.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision, command_key='r0-take',
                      actor=ACTOR, reason='synthetic takeover', action='request_takeover')
    with Session(w.w.database[1]) as s: revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    w.w.store.control(TENANT, ACCOUNT, FAN, mode='automated', expected_revision=revision, command_key='r0-handback',
                      actor=ACTOR, reason='synthetic legitimate handback', action='release_takeover')
    assert w.provider.calls == [first] and w.w.fans.posts == []
    w.advance(31)
    w.ticks(3, w.new_pilot())
    if first == 429:
        assert w.provider.calls == [429, 'ok'] and w.request(92005)[0] == 'completed'
        assert [c.seq for c in w.calls()] == [0, 1] and len(w.w.fans.posts) == 1
    else:
        assert w.provider.calls == ['timeout'] and w.w.fans.posts == []
    assert w.calls()[0].call_id == original.call_id and w.calls()[0].status == original.status
    assert w.calls()[0].reserved_microusd == original.reserved_microusd


def test_operator_http_distinguishes_scheduled_due_and_overdue_with_stable_scoped_ids(make_world, monkeypatch):
    w = make_world([(429, {'Retry-After': '120'})])
    post(w, 92006)
    w.ticks(1)
    scheduled = request_item(w, 92006, monkeypatch)
    assert scheduled['recoveryState'] == 'scheduled_retry' and not scheduled['operatorAlert']
    w.advance(121)
    due = request_item(w, 92006, monkeypatch)
    assert due['recoveryState'] == 'retry_due' and not due['operatorAlert']
    w.advance(421)
    overdue = request_item(w, 92006, monkeypatch)
    assert overdue['recoveryState'] == 'overdue_eligible' and overdue['operatorAlert']
    assert overdue['id'] == scheduled['id'] == due['id']
    assert request_item(w, 92006, monkeypatch) == overdue
    post(w, 92007, fan=FAN2)
    w.ticks(4)
    assert w.request(92006)[0] == w.request(92007)[0] == 'completed'
    assert len(w.w.fans.posts) == 2 and w.provider.calls == [429, 'ok', 'ok']


def test_unknown_and_identity_holds_are_individually_retrievable(make_world, monkeypatch):
    w = make_world(['timeout', 'identity'])
    post(w, 92008)
    w.ticks(1)
    post(w, 92009, fan=FAN2)
    w.ticks(2)
    w.advance(600)
    w.ticks(3)
    assert request_item(w, 92008, monkeypatch)['recoveryState'] == 'unknown_model_outcome'
    assert request_item(w, 92009, monkeypatch)['recoveryState'] == 'identity_hold'
    assert w.provider.calls == ['timeout', 'identity'] and w.w.fans.posts == []
    unknown = [c for c in w.calls() if c.status == 'unknown'][0]
    assert unknown.reserved_microusd > 0 and unknown.actual_microusd is None


def clone_call(s, original, **overrides):
    values = {c.name: getattr(original, c.name) for c in OfEngineModelCall.__table__.columns}
    values.update(call_id=str(uuid.uuid4()), retry_not_before=None, failure_detail_json=None)
    values.update(overrides)
    row = OfEngineModelCall(**values)
    s.add(row)
    return row


@pytest.mark.parametrize('status', ['reserved', 'calling', 'unknown', 'settled'])
def test_http_outcome_without_confirmed_release_is_not_retry_permission(make_world, monkeypatch, status):
    w = make_world([429])
    post(w, 92010)
    w.ticks(1)
    old = w.calls()[0]
    with Session(w.w.database[1]) as s, s.begin():
        clone_call(s, old, seq=1, status=status, outcome='http_429', actual_microusd=1 if status == 'settled' else None,
                   usage_known=status == 'settled', lease_expires_at=w.clock()+timedelta(hours=1))
    w.advance(31)
    w.ticks(3)
    assert w.provider.calls == [429] and w.w.fans.posts == []
    assert len(w.calls()) == 2 and w.calls()[1].status == status
    shown = request_item(w, 92010, monkeypatch)
    assert shown['recoveryState'] == ('model_in_flight' if status in ('reserved', 'calling') else 'unknown_model_outcome')


@pytest.mark.parametrize('label,fan', [('6122ac90', FAN), ('b968a156', FAN2), ('0165083a', FAN)])
def test_historical_obsolete_fixture_is_superseded_with_zero_new_calls_or_sends(make_world, label, fan):
    w = make_world([429])
    post(w, 92011, fan=fan)
    w.ticks(1)
    old = request_row(w, 92011)
    rid = str(uuid.uuid4())
    # The parametrized incident prefix labels this synthetic history, never a live DB mutation.
    before = [(c.call_id, c.seq, c.generation, c.status, c.reserved_microusd) for c in w.calls()]
    post(w, 92012, fan=fan, text_='my newer correction')
    w.w.store.drain(TENANT, ACCOUNT)
    # Represent an already-completed newer request, as in all three historical incident histories. Only the OLD
    # request's real scheduler path is eligible; it must supersede before propose()/reservation.
    with Session(w.w.database[1]) as s, s.begin():
        s.add(OfEngineRequest(account_id=ACCOUNT, request_id=rid, fan_id=fan, binding_id=old.binding_id,
            source_message_id=92012, status='completed', owner='engine', next_step='none', revision=0,
            created_at=utcnow(), updated_at=utcnow()))
    calls_before, posts_before = len(w.provider.calls), len(w.w.fans.posts)
    w.advance(31)
    w.ticks(4)
    assert w.request(92011) == ('cancelled', 'superseded_by_newer_input', None)
    assert len(w.provider.calls) - calls_before == len(w.w.fans.posts) - posts_before == 0, label
    assert [(c.call_id, c.seq, c.generation, c.status, c.reserved_microusd) for c in w.calls()] == before


def test_newer_correction_keeps_unanswered_context_and_sends_only_current_reply(make_world):
    w = make_world([429])
    post(w, 92013, text_='are you free tonight')
    w.ticks(1)
    post(w, 92014, text_='actually I meant tomorrow')
    w.ticks(3)
    w.advance(31)
    w.ticks(3)
    assert w.request(92013)[0] == 'cancelled' and w.request(92014)[0] == 'completed'
    assert w.provider.calls == [429, 'ok'] and len(w.w.fans.posts) == 1
    prompt = json.dumps(w.provider.requests[-1]['messages'])
    assert 'are you free tonight' in prompt and 'actually I meant tomorrow' in prompt


def test_same_fan_under_other_creator_cannot_be_retried_or_exposed(make_world, monkeypatch):
    from test_onlyfans_foundation_pg import seed, event
    w = make_world([429])
    post(w, 92015)
    w.ticks(1)
    original = w.calls()[0]
    other_store = seed(*w.w.database, account='acc-r0-other', tenant='tenant-r0-other', fan=FAN)
    other_store.record('tenant-r0-other', 'acc-r0-other', event(mid=92015, fan=FAN, body='other creator private input'))
    other_store.drain('tenant-r0-other', 'acc-r0-other')
    other_rid = str(uuid.uuid4())
    with Session(w.w.database[1]) as s, s.begin():
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        owner_values = {c.name: getattr(owner, c.name) for c in OfEngineOwner.__table__.columns}
        owner_values.update(account_id='acc-r0-other', binding_id='binding-acc-r0-other')
        s.add(OfEngineOwner(**owner_values))
        s.flush()
        s.add(OfEngineRequest(account_id='acc-r0-other', request_id=other_rid, fan_id=FAN,
            binding_id='binding-acc-r0-other', source_message_id=92015, status='open', owner='engine',
            next_step='prepare_reply', revision=0, created_at=utcnow(), updated_at=utcnow()))
        s.flush()
        other = clone_call(s, original, account_id='acc-r0-other', binding_id='binding-acc-r0-other', request_id=other_rid,
            status='unknown', outcome='timeout', pilot_scope_revision=None, pilot_scope_fingerprint=None)
        other_id = other.call_id
    w.advance(31)
    w.ticks(3)
    assert w.request(92015)[0] == 'completed' and w.provider.calls == [429, 'ok'] and len(w.w.fans.posts) == 1
    assert all(i.get('requestId') != other_rid and other_id not in i['id'] for i in items(w, monkeypatch))
    with Session(w.w.database[1]) as s:
        assert s.get(OfEngineModelCall, ('acc-r0-other', other_id)).status == 'unknown'
        assert s.get(OfEngineRequest, ('acc-r0-other', other_rid)).status == 'open'
    with pytest.raises(Refused): operator_v2.unresolved(w.w.store, 'tenant-r0-other', ACCOUNT, ACTOR)
    other_store.close()


def test_invalid_receipt_holds_prerequisite_and_both_later_parts_even_after_restart(make_world, monkeypatch):
    from test_engine_real_worker_pg import answer
    w = make_world(['multipart'])
    original = w.w.fans.transport.handler
    def invalid_receipt(request):
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            w.w.fans.posts.append({'path': request.url.path})
            return answer(200, {'data': {'unrecognised': 'no valid receipt'}})
        return original(request)
    w.w.fans.transport.handler = invalid_receipt
    post(w, 92016)
    w.ticks(1)
    with Session(w.w.database[1]) as s:
        parts = list(s.scalars(select(OfEngineDecision).order_by(OfEngineDecision.part_index)))
        assert [p.status for p in parts] == ['unknown', 'ready', 'ready']
        intent = s.get(OfTextIntent, (ACCOUNT, parts[0].intent_id))
        assert (intent.status, intent.reason) == ('unknown', 'malformed_success')
        first_id, intent_id = parts[0].decision_id, intent.intent_id
    w.advance(100000)
    restarted = w.new_pilot()
    w.ticks(4, restarted)
    for p in parts:
        restarted.worker.send_approved(TENANT, ACCOUNT, p.decision_id)
    assert len(w.w.fans.posts) == 1 and w.provider.calls == ['multipart']
    with Session(w.w.database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent_id)).status == 'unknown'
        assert [s.get(OfEngineDecision, (ACCOUNT, p.decision_id)).status for p in parts] == ['unknown', 'ready', 'ready']
    assert any(i['kind'] == 'unknown_send' for i in items(w, monkeypatch))


@pytest.mark.parametrize('body', [b'not json', b'{' * 70000, b'{"error":{"message":"private chat text"}}'])
def test_malformed_or_oversized_errors_retain_only_bounded_allowlisted_data(body):
    from model_transport.openrouter import _failure_detail
    detail = _failure_detail(httpx.Response(429, headers={'Authorization': 'Bearer private', 'x-request-id': 'sk-secretabcdef',
        'x-provider-request-id': 'fixture-provider-id', 'retry-after': '120'}, content=body), secret='private')
    raw = json.dumps(detail)
    assert len(raw.encode()) <= 2000 and 'private' not in raw and 'sk-secret' not in raw
    assert detail['providerRequestId'] == 'fixture-provider-id' and detail['retryAfterSeconds'] == '120'
    assert 'message' not in raw.lower() and 'body' not in raw.lower()


def test_credentials_or_private_content_in_allowlisted_identifiers_are_omitted():
    from model_transport.openrouter import _failure_detail
    private = 'private fan text'
    response = httpx.Response(401, headers={'x-request-id': private}, json={'error': {'code': 'fixture-secret',
        'type': 'sk-secretabcdef', 'message': private, 'metadata': {'provider_name': private, 'raw': private}}})
    raw = json.dumps(_failure_detail(response, secret='fixture-secret', messages=({'content': private},)))
    assert private not in raw and 'fixture-secret' not in raw and 'sk-secret' not in raw


def test_oversized_stream_is_bounded_before_error_parsing_and_persistence(make_world):
    emitted = []
    class ErrorStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                emitted.append(32768)
                yield b'private-error-body' * 2048
    w = make_world([])
    def response(request):
        w.provider.calls.append(429)
        return httpx.Response(429, headers={'Retry-After': '45'}, stream=ErrorStream())
    w.provider.transport.handler = response
    post(w, 92017)
    w.ticks(1)
    [call] = w.calls()
    assert call.status == 'released' and call.outcome == 'http_429'
    assert len(emitted) <= 3 and len(call.failure_detail_json.encode()) <= 2000
    assert 'private' not in call.failure_detail_json


def test_mixed_versions_fail_closed_and_rollback_never_drops_retry_evidence(make_world):
    import os, subprocess, sys
    from pathlib import Path
    from db.schema import _config, check_schema, upgrade, SchemaMismatch
    from alembic import command
    w = make_world([429])
    post(w, 92018)
    w.ticks(1)
    before = [(c.call_id, c.status, c.outcome, c.retry_not_before, c.failure_detail_json) for c in w.calls()]
    baseline = Path('/opt/r0-baseline/service')
    assert baseline.exists(), 'This check runs only on the exact immutable candidate image with its baseline reader'
    env = {**os.environ, 'PYTHONPATH': str(baseline), 'R0_TEST_DB': w.w.database[0],
           'DATABASE_URL': w.w.database[0], 'ONLYFANS_PROCESS_ROLE': 'worker', 'TWIN_FASTT_CONFIG_ISOLATED': '1'}
    result = subprocess.run([sys.executable, '-c',
        'import os; from sqlalchemy import create_engine; from db.schema import check_schema; '
        'e=create_engine(os.environ["R0_TEST_DB"]); c=e.connect(); check_schema(c)'],
        cwd='/tmp', env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'Database revision is not supported by this process' in result.stderr
    with pytest.raises(RuntimeError, match='Preserve model retry evidence'):
        with w.w.database[1].begin() as c: command.downgrade(_config(c), '0129_reconnect_receipt')
    assert [(c.call_id, c.status, c.outcome, c.retry_not_before, c.failure_detail_json) for c in w.calls()] == before


def test_exhausted_real_ledger_budget_prevents_retry_and_retains_unrelated_unknown_cost(make_world, monkeypatch):
    w = make_world([429])
    post(w, 92019)
    w.ticks(1)
    original = w.calls()[0]
    rid = str(uuid.uuid4())
    post(w, 999991, fan=FAN2)
    w.w.store.drain(TENANT, ACCOUNT)
    with Session(w.w.database[1]) as s, s.begin():
        if s.get(OfEngineOwner, (ACCOUNT, FAN2)) is None:
            owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
            owner_values = {c.name: getattr(owner, c.name) for c in OfEngineOwner.__table__.columns}
            owner_values['fan_id'] = FAN2
            s.add(OfEngineOwner(**owner_values))
            s.flush()
        s.add(OfEngineRequest(account_id=ACCOUNT, request_id=rid, fan_id=FAN2, binding_id=original.binding_id,
            source_message_id=999991, status='open', owner='engine', next_step='prepare_reply', revision=0,
            created_at=utcnow(), updated_at=utcnow()))
        s.flush()
        held = clone_call(s, original, request_id=rid, fan_id=FAN2, seq=0, status='unknown', outcome='timeout',
            reserved_microusd=w.pilot.worker.engine.model.config.daily_cap_microusd)
        held_id = held.call_id
    w.advance(600)
    w.ticks(3)
    assert w.provider.calls == [429] and w.w.fans.posts == []
    assert request_item(w, 92019, monkeypatch)['operatorAlert']
    with Session(w.w.database[1]) as s:
        held = s.get(OfEngineModelCall, (ACCOUNT, held_id))
        assert held.status == 'unknown' and held.actual_microusd is None
        assert held.reserved_microusd == w.pilot.worker.engine.model.config.daily_cap_microusd


def test_stop_while_retry_is_in_flight_prevents_any_send(make_world):
    w = make_world([429])
    post(w, 92020)
    w.ticks(1)
    original = w.provider.transport.handler
    def stop_after_model(request):
        response = original(request)
        with Session(w.w.database[1]) as s: revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
        w.w.store.control(TENANT, ACCOUNT, FAN, mode='paused', expected_revision=revision, command_key='r0-stop-send',
            actor=ACTOR, reason='synthetic stop during authorized model call', action='pause_automation')
        return response
    w.provider.transport.handler = stop_after_model
    w.advance(31)
    w.ticks(3)
    assert w.provider.calls == [429, 'ok'] and w.w.fans.posts == []
    assert w.calls()[-1].outcome == 'identity_verified'  # usage/identity retained, stale draft never authorized


@pytest.mark.parametrize('retry_after', [None, '120'])
def test_fresh_os_process_uses_the_same_persisted_deadline(make_world, retry_after):
    import os, subprocess, sys
    w = make_world([429 if retry_after is None else (429, {'Retry-After': retry_after})])
    post(w, 92021)
    w.ticks(1)
    [original] = w.calls()
    child = '''
import json, os, sys
from datetime import datetime
from onlyfans_runtime.store import Store
from model_transport.pilot_supervised import build_supervised_pilot
from test_plan_c_model_retry_due_pg import Provider
from test_engine_real_worker_pg import MockFans
p = json.load(sys.stdin)
store, model, fans = Store(p['database']), Provider([]), MockFans()
try:
    pilot = build_supervised_pilot(store, p['config'], model_transport=model.transport, fans_transport=fans.transport)
    clock = lambda: datetime.fromisoformat(p['now'])
    pilot.worker.engine.model._clock = pilot.worker.engine.model.ledger.clock = clock
    for _ in range(4): pilot.tick()
    print('R0_RESTART_RESULT ' + json.dumps({'pid': os.getpid(), 'calls': model.calls, 'posts': fans.posts}))
finally:
    store.close()
'''
    env = {**os.environ, 'DATABASE_URL': w.w.database[0], 'ONLYFANS_PROCESS_ROLE': 'worker',
           'TWIN_FASTT_CONFIG_ISOLATED': '1'}
    def run(at):
        p = subprocess.run([sys.executable, '-c', child], env=env, input=json.dumps({
            'database': w.w.database[0], 'config': w.rig.env, 'now': at.isoformat()}),
            capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr[-2000:]
        [result] = [json.loads(line.removeprefix('R0_RESTART_RESULT ')) for line in p.stdout.splitlines()
                    if line.startswith('R0_RESTART_RESULT ')]
        assert result['pid'] != os.getpid()
        return result
    before = run(original.retry_not_before - timedelta(seconds=1))
    assert before['calls'] == [] and before['posts'] == []
    after = run(original.retry_not_before + timedelta(seconds=1))
    assert after['calls'] == ['ok'] and len(after['posts']) == 1
    assert after['posts'][0]['body']['text'] == 'hey you'
    assert w.request(92021)[0] == 'completed' and [c.seq for c in w.calls()] == [0, 1]
    assert w.calls()[0].retry_not_before == original.retry_not_before


def test_real_delivery_gap_can_elapse_during_work_without_calling_pacing_sleep(world):
    """A nonempty sleep list is not the pacing contract: real elapsed control/DB work can satisfy the gap."""
    import hashlib, hmac, time
    from conversation_engine import approval
    from conversation_engine.engine import SyntheticProposal
    from test_engine_real_worker_pg import SECRET, inbound
    world.fans.hooks.clear()  # remove the generic fixture's SQL pacing reset; this proof uses the real gap
    sleeps = []
    world.worker.sleep = lambda seconds: (sleeps.append(seconds), time.sleep(seconds))
    world.reply(lambda snapshot: SyntheticProposal('hey you', parts=('how was your day',)))
    raw = json.dumps(inbound(92022, 'hello there', fan=FAN)).encode()
    staged = world.worker.handle(TENANT, ACCOUNT, raw_body=raw,
        signature=hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), secret=SECRET, event_key='r0-elapsed-gap')
    assert staged['outcome'] == 'awaiting_approval'
    draft = staged['approval']
    approval.approve(world.store, TENANT, ACCOUNT, draft['requestId'], draft['generation'],
                     expected_version=draft['version'], actor='test-operator')
    sequence = world.engine.sequence(TENANT, ACCOUNT, staged['decisionId'])
    assert len(sequence) == 2
    original = world.worker._part_state
    delayed = []
    def control_work(tenant, account, part):
        result = original(tenant, account, part)
        if part == sequence[1] and not delayed:
            delayed.append(part)
            time.sleep(1.2)  # real elapsed work after the predecessor settles; no fake clock or policy write
        return result
    world.worker._part_state = control_work
    result = world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert result['outcome'] == 'accepted' and len(world.fans.posts) == 2 and sleeps == []
    assert [p['body']['text'] for p in world.fans.posts] == ['hey you', 'how was your day']
    with Session(world.database[1]) as s:
        sends = list(s.scalars(select(OfTextIntent).order_by(OfTextIntent.wire_started_at)))
        assert len(sends) == 2 and all(i.status == 'accepted' for i in sends)
        assert sends[1].wire_started_at - sends[0].wire_started_at >= timedelta(seconds=1)
