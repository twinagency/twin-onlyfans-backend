"""Plan C defects on the joined path: real worker, real TextAuthority, real FansApiRealTransport, PostgreSQL.

Only the provider is a fixture (httpx.MockTransport); there is no network, no model call and no spend.
Covers: fan-isolated admission, restart recovery of an interrupted attempt, the send-outcome table
(<= 3 attempts total, stable key, provider outcome fields not HTTP status alone), event handling, and the
legacy-writer fence at TextAuthority.prepare.
"""
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import (ACCOUNT, FAN, FAN2, TENANT, MockFans, World, answer, inbound,  # noqa: F401
                                         iso, posts_text, world)
from conversation_engine.engine import SyntheticProposal
from conversation_engine.real_worker import RealConversationWorker
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOwner
from db.onlyfans_fans_intake_models import OfFansEvidence, OfFansProjection
from fans_api_adapter.events import FansEventIntake
from fans_api_adapter.real_transport import FansApiRealTransport, idempotency_key
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import TextAuthority


class Crash(BaseException):
    """The process dies: not an Exception, so no handler in the send path may swallow it."""


def posts_to(world, fan):
    return [p for p in world.fans.posts if f'/chats/{fan}/messages' in p['path']]


def add_fan(world, fan):
    """A second conversation: first inbound creates it, then it is switched to automated like FAN."""
    world.transport.allowed_chats = frozenset(set(world.transport.allowed_chats) | {str(fan)})
    world.say('hi', fan=fan)
    world.automate(fan)


def intents(world, fan):
    return world.rows(OfTextIntent, OfTextIntent.fan_id == fan)


def timeout_for(world, fan):
    """Every POST to this fan's chat is lost AFTER the request may have left (no response at all)."""
    real = world.fans.transport.handler

    def handler(request):
        if request.method == 'POST' and f'/chats/{fan}/messages' in request.url.path:
            world.fans.posts.append({'path': request.url.path, 'key': request.headers['Idempotency-Key'],
                                     'body': json.loads(request.content), 'auth': None})
            raise httpx.ReadTimeout('response lost', request=request)
        return real(request)
    world.fans.transport.handler = handler


# ============================================================ 1. cross-fan stall

def test_fan_a_unknown_send_does_not_stall_fan_b_and_a_gets_no_dependent_resend(world):
    add_fan(world, FAN2)
    world.reply(lambda snap: SyntheticProposal('reply to %s' % snap['fan_id']))
    timeout_for(world, FAN)
    a, _ = world.say('hello from A')
    assert a['outcome'] == 'unknown'
    row_a = intents(world, FAN)[0]
    assert row_a.status == 'unknown' and len(posts_to(world, FAN)) == 1
    with Session(world.database[1]) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id is None      # no account-wide pointer for an engine account
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == row_a.attempt_id
    # fan B is not blocked: planned, admitted, sent, accepted
    b, _ = world.say('hello from B', fan=FAN2)
    assert b['outcome'] == 'accepted' and len(posts_to(world, FAN2)) == 1
    b2, _ = world.say('and again', fan=FAN2, seconds=5)
    assert b2['outcome'] == 'accepted' and len(posts_to(world, FAN2)) == 2
    # fan A's own dependents stay held: a new message from A never sends a second message to A
    a2, _ = world.say('anyone there?', seconds=5)
    assert a2['outcome'] != 'accepted' and len(posts_to(world, FAN)) == 1
    assert intents(world, FAN)[0].status == 'unknown'
    # restart: a fresh worker over the same database changes nothing
    world.worker = RealConversationWorker(world.engine, world.intake, world.transport,
                                          sleep=lambda sec: (world.sleeps.append(sec), world.no_throttle()))
    assert world.transport.recover_interrupted() == 0
    a3, _ = world.say('still there?', seconds=9)
    assert a3['outcome'] != 'accepted' and len(posts_to(world, FAN)) == 1
    b3, _ = world.say('B again after restart', fan=FAN2, seconds=9)
    assert b3['outcome'] == 'accepted'
    assert {p['key'] for p in posts_to(world, FAN)} == {idempotency_key(ACCOUNT, row_a.intent_id)}


def test_legacy_gap_mode_account_keeps_the_single_account_slot(database):
    """Only engine (fansapi) accounts are fan-isolated; the synthetic/native gap-mode behaviour is unchanged."""
    from onlyfans_runtime.text_delivery import _fan_isolated
    from types import SimpleNamespace
    assert _fan_isolated(SimpleNamespace(transport_kind='fansapi')) is True
    for kind in ('synthetic', 'native', None):
        assert _fan_isolated(SimpleNamespace(transport_kind=kind)) is False
    assert _fan_isolated(None) is False


# ============================================================ 5. interrupted attempt is reconciled as UNKNOWN on restart

def test_crash_after_the_request_may_have_left_is_unknown_after_restart_never_queued_or_retried(world):
    add_fan(world, FAN2)
    world.reply(lambda snap: SyntheticProposal('reply'))

    def die(*args, **kwargs):
        raise Crash()
    world.transport.client.send = die
    with pytest.raises(Crash):
        world.say('hello')
    del world.transport.client.send
    row = intents(world, FAN)[0]
    assert row.status == 'dispatching' and row.wire_started_at is not None       # the marker was committed before I/O
    # while the deadline is open another process might still be mid-flight: nothing is touched, nothing is re-queued
    assert world.transport.recover_interrupted() == 0
    assert intents(world, FAN)[0].status == 'dispatching'
    # restart after the deadline: it becomes UNKNOWN for this fan only
    with Session(world.database[1]) as s, s.begin():
        s.get(OfTextIntent, (ACCOUNT, row.intent_id)).deadline = utcnow() - timedelta(seconds=1)
    assert world.transport.recover_interrupted() == 1
    after = intents(world, FAN)[0]
    assert (after.status, after.reason) == ('unknown', 'dispatch_expired')
    assert after.status != 'queued' and len(posts_to(world, FAN)) == 0
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention is True
    # no resend to A; B is untouched
    a, _ = world.say('hello again?', seconds=5)
    assert a['outcome'] != 'accepted' and posts_to(world, FAN) == []
    b, _ = world.say('hello from B', fan=FAN2, seconds=5)
    assert b['outcome'] == 'accepted'
    # The wire marker does not prove a request left the process. Replaying
    # even the same key could create a new send after an idempotency expiry.
    assert world.transport.reconcile_accepted(row.intent_id) == 'unknown'
    assert posts_to(world, FAN) == []
    assert intents(world, FAN)[0].status == 'unknown'


def test_dispatch_itself_expires_a_dead_attempt_before_admitting_new_work(world):
    """Even with no explicit recovery call, the next dispatch on the account converts a dead attempt to unknown."""
    add_fan(world, FAN2)
    world.reply(lambda snap: SyntheticProposal('reply'))
    world.transport.client.send = lambda *a, **k: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        world.say('hello')
    row = intents(world, FAN)[0]
    with Session(world.database[1]) as s, s.begin():
        s.get(OfTextIntent, (ACCOUNT, row.intent_id)).deadline = utcnow() - timedelta(seconds=1)
    del world.transport.client.send
    b, _ = world.say('hello from B', fan=FAN2)
    assert b['outcome'] == 'accepted'
    assert intents(world, FAN)[0].status == 'unknown'


# ============================================================ 5. send submission outcome table

def scripted(world, outcomes):
    """The first POSTs get these scripted provider answers; later ones fall through to the accepting mock."""
    queue = list(outcomes)
    real = world.fans.transport.handler

    def handler(request):
        if not (request.method == 'POST' and request.url.path.endswith('/messages') and queue):
            return real(request)
        kind = queue.pop(0)
        if kind == 'accepted':
            return real(request)
        world.fans.posts.append({'path': request.url.path, 'key': request.headers['Idempotency-Key'],
                                 'body': json.loads(request.content), 'auth': None})
        if kind == 'timeout':
            raise httpx.ReadTimeout('lost', request=request)
        if kind == 'garbage':
            return httpx.Response(200, content=b'<html>gateway</html>')
        table = {
            'not_sent': (503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'not_sent'}),
            'unavailable': (503, {'error': 'IDEMPOTENCY_UNAVAILABLE'}),
            'unknown': (503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'unknown'}),
            'plain503': (503, {}),
            'missing_outcome': (503, {'error': 'SERVICE_UNAVAILABLE'}),
            'conflict': (409, {'error': 'IDEMPOTENCY_CONFLICT'}),
            'mismatch': (422, {'error': 'IDEMPOTENCY_KEY_MISMATCH'}),
            'validation': (422, {'error': 'VALIDATION_FAILED'}),
            'validation_not_sent': (422, {'error': 'VALIDATION_FAILED', 'request_outcome': 'not_sent'}),
            'rate': (429, {}),
            'no_id': (200, {'data': {}}),
            'bad_error_on_200': (200, {'error': 'SOMETHING'}),
            'not_sent_on_500': (500, {'error': 'INTERNAL', 'request_outcome': 'not_sent'}),
        }
        status, body = table[kind]
        return answer(status, body)
    world.fans.transport.handler = handler


OUTCOMES = [
    # (provider answers, dispatch outcome, POSTs issued)
    (['not_sent', 'not_sent', 'not_sent'], 'not_accepted', 3),          # budget is 3 TOTAL, then the proven-not-sent stands
    (['unavailable', 'unavailable', 'unavailable'], 'not_accepted', 3),
    (['not_sent', 'accepted'], 'accepted', 2),
    (['unavailable', 'not_sent', 'accepted'], 'accepted', 3),
    (['unknown'], 'unknown', 1),                                         # may have been sent: stop, never retry
    (['plain503'], 'unknown', 1),                                        # an HTTP 503 alone proves nothing
    (['missing_outcome'], 'unknown', 1),
    (['timeout'], 'unknown', 1),
    (['garbage'], 'unknown', 1),
    (['no_id'], 'unknown', 1),
    (['bad_error_on_200'], 'unknown', 1),
    (['rate'], 'unknown', 1),
    (['not_sent_on_500'], 'unknown', 1),                                 # request_outcome only counts on the documented 503s
    (['mismatch'], 'unknown', 1),                                        # never rotate the key
    (['validation'], 'unknown', 1),                                      # a 4xx without request_outcome=not_sent is not non-delivery
    (['validation_not_sent'], 'not_accepted', 1),                        # the provider itself says not sent
    (['conflict', 'conflict', 'conflict'], 'unknown', 1),                # a 409 leaves the earlier request unresolved
    (['conflict', 'accepted'], 'unknown', 1),                             # never replay an uncertain POST
    (['conflict', 'unavailable', 'unavailable'], 'unknown', 1),
    (['conflict', 'not_sent', 'accepted'], 'unknown', 1),
]


def test_send_outcome_table_uses_provider_fields_three_attempts_total_and_one_key(world):
    fans = [1000 + i for i in range(len(OUTCOMES))]
    for fan in fans:
        add_fan(world, fan)
    world.reply(lambda snap: SyntheticProposal('reply'))
    mismatches = []
    for fan, (script, expected, posts) in zip(fans, OUTCOMES):
        scripted(world, script)
        result, _ = world.say('go', fan=fan, seconds=3)
        sent = posts_to(world, fan)
        keys = {p['key'] for p in sent}
        row = intents(world, fan)[0]
        if (result['outcome'], len(sent), len(keys)) != (expected, posts, 1) or keys != {idempotency_key(ACCOUNT, row.intent_id)}:
            mismatches.append((script, expected, posts, result['outcome'], len(sent), len(keys)))
        assert len(sent) <= 3
    assert mismatches == []


def test_same_action_retry_after_proven_not_sent_reuses_the_same_key(world):
    add_fan(world, FAN2)
    world.reply(lambda snap: SyntheticProposal('reply'))
    scripted(world, ['not_sent', 'not_sent', 'not_sent'])
    result, _ = world.say('go', fan=FAN2)
    assert result['outcome'] == 'not_accepted'
    first_keys = {p['key'] for p in posts_to(world, FAN2)}
    assert len(first_keys) == 1 and len(posts_to(world, FAN2)) == 3
    decision = result['decisionId']
    queued = world.engine.retry_not_sent(TENANT, ACCOUNT, decision, max_attempts=3)
    scripted(world, [])
    world.no_throttle()
    assert world.worker.authority.dispatch(TENANT, ACCOUNT, queued, world.transport) == 'accepted'
    assert {p['key'] for p in posts_to(world, FAN2)} == first_keys          # same logical action, same key
    assert len(posts_to(world, FAN2)) == 4                                  # 3 proven-not-sent + 1 accepted, each attempt <= 3


def test_real_transport_refuses_a_budget_above_three_attempts(world):
    with pytest.raises(Refused):
        FansApiRealTransport(world.store, TENANT, ACCOUNT, world.transport.client, allowed_chats=frozenset({str(FAN)}), max_attempts=4, enabled=True)
    FansApiRealTransport(world.store, TENANT, ACCOUNT, world.transport.client, allowed_chats=frozenset({str(FAN)}), max_attempts=3, enabled=True)


# ============================================================ 5. event handling

def test_event_is_persisted_before_acknowledgement_and_a_failed_handler_is_not_complete(world, monkeypatch):
    world.say_plain('one reply')
    original = FansEventIntake._dispatch
    calls = {'n': 0}

    def flaky(self, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('handler crashed after the raw event was stored')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FansEventIntake, '_dispatch', flaky)
    body = inbound(701, 'hello there')
    with pytest.raises(OSError):                                     # the provider sees a failure: it will redeliver
        world.send(body, key='evt-persist')
    from db.onlyfans_engine_models import OfFansWebhook
    assert len(world.rows(OfFansWebhook)) == 1                       # raw bytes were durable BEFORE the handler ran
    proj = world.rows(OfFansProjection)[0]
    assert proj.status == 'pending'                                  # a failed handler is not complete
    assert world.rows(OfEngineDecision) == [] and world.fans.posts == []
    redelivered = world.send(body, key='evt-persist')                # same event key: deduplicated, handler retried
    assert redelivered['intake'] == 'duplicate' and redelivered['outcome'] == 'accepted'
    assert len(world.rows(OfFansWebhook)) == 1 and world.rows(OfFansProjection)[0].status == 'projected'
    assert len(world.fans.posts) == 1 and len(world.rows(OfEngineDecision)) == 1
    again = world.send(body, key='evt-persist')                      # a third delivery changes nothing
    assert again['intake'] == 'duplicate' and len(world.fans.posts) == 1


def test_a_malformed_event_is_held_visibly_never_marked_complete(world):
    bad = {'event': 'messages.ppv.unlocked', 'account_id': 'acct_real_worker', 'payload': {'message_id': 'not a number', 'chat_id': FAN}}
    result = world.send(bad, key='evt-malformed')
    assert result['outcome'] == 'projection_held'
    proj = world.rows(OfFansProjection)[0]
    assert proj.status == 'held' and proj.detail.startswith('invalid:')
    assert world.rows(OfFansEvidence) == []                          # nothing was recorded from it
    assert world.rows(OfFansProjection, OfFansProjection.status == 'projected') == []


def test_duplicate_and_out_of_order_delivery_is_one_effect(world):
    world.say_plain('one reply only')
    newer = inbound(802, 'second message', seconds=2)
    older = inbound(801, 'first message', seconds=0)
    first = world.send(newer, key='evt-newer')
    assert first['outcome'] == 'accepted'
    late = world.send(older, key='evt-older')                        # arrives after the newer one
    assert late['intake'] == 'accepted'
    world.send(newer, key='evt-newer')                               # redelivery of the newer one
    assert len(world.rows(Message, Message.direction == 'out')) == 1 and len(posts_to(world, FAN)) == 1
    assert len(world.rows(OfEngineDecision)) == 1


# ============================================================ 4. writer fence at the delivery authority

def prepare(world, origin, **extra):
    with Session(world.database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    return world.worker.authority.prepare(TENANT, ACCOUNT, FAN, command_key=uuid.uuid4().hex[:32], expected_revision=revision,
                                          body='competing writer', origin=origin, actor='legacy', **extra)


@pytest.mark.parametrize('origin,extra', [
    ('automated', {}),
    ('campaign', {'campaign_id': 'camp-1'}),
    ('engagement', {'engagement_action_id': str(uuid.uuid4())}),
])
def test_legacy_mass_scheduled_writers_cannot_prepare_for_a_fansapi_account(world, origin, extra):
    with pytest.raises(Refused, match='owned by the new engine'):
        prepare(world, origin, **extra)           # before any owner row exists for the fan
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    with pytest.raises(Refused, match='owned by the new engine'):
        prepare(world, origin, **extra)           # and after the engine owns the fan
    assert world.rows(OfTextIntent) == []


def test_a_human_message_for_an_engine_owned_fan_needs_a_verified_takeover(world):
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    with pytest.raises(Refused):
        prepare(world, 'manual')                  # engine owns the fan: no manual write without takeover
    assert world.rows(OfTextIntent) == []


# ============================================================ 4. provider-queued (mass / scheduled) sends are surfaced

def sent_event(mid, fan, queued):
    return {'event': 'messages.sent', 'account_id': 'acct_real_worker', 'payload': {
        'id': mid, 'toUser': {'id': fan}, 'isFromQueue': queued, 'createdAt': iso(), 'text': 'queued blast'}}


def test_provider_queued_send_to_an_engine_owned_fan_is_surfaced_not_silently_ignored(world):
    world.say_plain('hi')
    world.say('hello')                                                 # the engine now owns FAN
    world.say('hi', fan=FAN2)                                          # FAN2 exists but the engine does not own it
    with Session(world.database[1]) as s:
        fan2_before = s.get(OfConversationState, (ACCOUNT, FAN2)).requires_attention
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention is False
    world.send(sent_event(9001, FAN, True), key='evt-q1')
    world.send(sent_event(9002, FAN2, True), key='evt-q2')
    details = {p.event_key: p.detail for p in world.rows(OfFansProjection)}
    assert details['evt-q1'] == 'queued_send_to_engine_owned_fan' and details['evt-q2'] == 'queued_broadcast'
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention is True
        assert s.get(OfConversationState, (ACCOUNT, FAN2)).requires_attention is fan2_before   # not an owned fan: unchanged
