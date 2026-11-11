"""Part B: release a Plan C fan held by an UNKNOWN send, only when provider history PROVES the message is absent.

Live finding (2026-11-11): two test fans were held forever by sends that ended `unknown`. The only release needed the
exact message to be found (positive reconcile), so if the message never arrived there was no way out. This pins the
conservative second path (send_recovery.release_unconfirmed_send): a complete, fresh, guarded history read after a
quiet period, no message that could be ours, a newer fan message waiting, every other hold still in force, one audited
release per command key, reservation kept, intent terminal-unconfirmed (never "not sent"), the fan answered exactly once.

Real PostgreSQL, the real Plan C text-only pilot rig (signed webhooks, ticks, fake model, scripted provider).
"""
from dataclasses import replace
from datetime import datetime, timedelta
import json
import re

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from conversation_engine import operator_v2, pilot_scope
from db.models import Fan, Message, OfConversationState, OfMessageDelivery, SkipList
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAttempt, OfEngineDecision, OfEnginePilotScope, OfEngineRequest
from fans_api_adapter.real_transport import RealFansConfig, build_client
from onlyfans_runtime import send_recovery
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from test_engine_real_worker_pg import CREATOR, FAN2, PROVIDER, inbound, iso, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig  # noqa: F401
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

CREATOR_ID = int(CREATOR)
OTHER_ACTOR = 'op-session:' + 'b' * 64
REASON = 'History shows nothing arrived; fan wrote again'


def K(n):
    return f'{n:064x}'


def provider_message(ident, sender, body, when, **extra):
    return {'id': ident, 'fromUser': {'id': sender}, 'createdAt': when, 'text': body, 'media': [], 'mediaCount': 0,
            'price': 0, 'responseType': 'message', **extra}


class Wire:
    """Wraps the rig's mock provider: scripted POST failures and a controllable newest-first chat history."""
    def __init__(self, rig):
        self.rig, self.inner = rig, rig.w.fans.transport.handler
        self.post_script = []                 # per POST: 'timeout' (lost response) or anything else = normal
        self.history = {FAN: [], FAN2: []}    # newest first
        self.pages = None                     # {fan: [page, page, ...]} to serve in pieces (continuation cursors)
        self.history_gets = 0
        rig.w.fans.transport.handler = self.handle

    def handle(self, request):
        path = request.url.path
        if request.method == 'POST' and path.endswith('/messages') and self.post_script:
            if self.post_script.pop(0) == 'timeout':
                self.rig.w.fans.posts.append({'path': path, 'key': request.headers['Idempotency-Key'],
                                              'body': json.loads(request.content), 'auth': None})
                raise httpx.ReadTimeout('lost response')
        match = re.fullmatch(rf'/api/{PROVIDER}/chats/(\d+)/messages', path)
        if request.method == 'GET' and match:
            self.history_gets += 1
            fan = int(match[1])
            if self.pages is not None:
                index = int(request.url.params.get('last_id', 0))
                page = self.pages[fan][index]
                more = (f'https://api.fansapi.com/api/{PROVIDER}/chats/{fan}/messages?limit=100&order=desc&last_id={index + 1}'
                        if index + 1 < len(self.pages[fan]) else None)
                return answer_json({'data': page, '_pagination': {'next_page': more}})
            return answer_json({'data': self.history[fan], '_pagination': {'next_page': None}})
        return self.inner(request)


def answer_json(payload):
    raw = json.dumps(payload).encode()
    return httpx.Response(200, headers={'content-type': 'application/json', 'content-length': str(len(raw))},
                          stream=httpx.ByteStream(raw))


def operator_client(rig, *, guarded=True, revision=None):
    """A fresh operator-side client over the same mock provider, carrying the pilot request guard (budget)."""
    c = rig.pilot.worker.transport.client
    guard = None
    if guarded:
        with Session(rig.w.database[1]) as s:
            runtime = rig.w.store._runtime(s, TENANT, ACCOUNT, lock=False)
            scope, _ = pilot_scope.current(s, runtime)
            guard = pilot_scope.ProviderRequestGuard(rig.w.store, TENANT, ACCOUNT, scope.revision if revision is None else revision)
    return build_client(RealFansConfig('operator-token', c.account, c.base, 5), creator_id=c.creator_id, epoch=c.epoch,
                        transport=rig.w.fans.transport, request_guard=guard)


def setup_unknown(rig, wire, *, fan=FAN, first=81001, second=81002, key='rel', script=('timeout',)):
    """The fan writes, the reply's response is lost (unknown, fan held), then the fan writes AGAIN (still not answered)."""
    unpace(rig)
    wire.post_script.extend(script)
    rig.post(inbound(first, 'hello, how was your day?', fan=fan), key=f'{key}-a-{fan}')
    rig.ticks(3)
    wire.post_script.clear()
    assert unknown_intent(rig, fan).status == 'unknown'
    rig.post(inbound(second, 'hey are you there??', fan=fan), key=f'{key}-b-{fan}')
    rig.ticks(3)
    return second


def unknown_intent(rig, fan):
    with Session(rig.w.database[1]) as s:
        return s.scalar(select(OfTextIntent).where(OfTextIntent.fan_id == fan, OfTextIntent.status == 'unknown'))


def age(rig, minutes=31):
    """Move the unknown send into the past (the quiet period is wall-clock time)."""
    with rig.w.database[1].begin() as c:
        for column in ('dispatched_at', 'wire_started_at', 'deadline', 'updated_at'):
            c.execute(text(f"UPDATE of_text_intent SET {column} = {column} - interval '{minutes} minutes' WHERE status = 'unknown'"))
        c.execute(text(f"UPDATE of_engine_attempt SET updated_at = updated_at - interval '{minutes} minutes' WHERE outcome = 'unknown'"))


def unpace(rig):
    with rig.w.database[1].begin() as c:
        c.execute(text('UPDATE of_delivery_policy SET next_dispatch_at = NULL'))


def at(rig, intent, seconds):
    """A provider timestamp `seconds` after the CURRENT (possibly aged) dispatch of this intent."""
    with Session(rig.w.database[1]) as s:
        dispatched = s.get(OfTextIntent, (ACCOUNT, intent.intent_id)).dispatched_at
    return (dispatched + timedelta(seconds=seconds)).isoformat() + 'Z'


def put(wire, fan, *extra):
    """Add provider messages to a fan's history, keeping it newest first as the provider serves it."""
    from fans_api_adapter.payment_evidence import stamp
    wire.history[fan] = sorted([*extra, *wire.history[fan]], key=lambda m: stamp(m['createdAt']), reverse=True)


def absent(rig, wire, intent, *, second=81002, first=81001, fan=FAN):
    """Provider history in which only the fan's two messages exist (nothing from the creator)."""
    wire.history[fan] = [provider_message(second, fan, 'hey are you there??', iso(-1)),
                         provider_message(first, fan, 'hello, how was your day?', iso(-2))]


def authority(rig):
    return rig.w.authority


def read(rig, fan, intent, **kw):
    return send_recovery.read_absence_history(authority(rig), TENANT, ACCOUNT, fan, intent.intent_id,
                                              client=kw.pop('client', None) or operator_client(rig), **kw)


def revision(rig, fan=FAN):
    with Session(rig.w.database[1]) as s:
        return s.get(OfConversationState, (ACCOUNT, fan)).revision


def release(rig, intent, history, n=1, *, fan=FAN, next_input=81002, actor=ACTOR, **patch):
    args = dict(expected_attempt_id=intent.attempt_id, expected_revision=revision(rig, fan), next_input_id=next_input,
                history=history, command_key=K(n), actor=actor, reason=REASON)
    args.update(patch)
    return send_recovery.release_unconfirmed_send(authority(rig), TENANT, ACCOUNT, fan, intent.intent_id, **args)


def facts(rig):
    with Session(rig.w.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        return dict(
            intents=[(i.fan_id, i.status, i.reason, i.reservation_held, i.wire_started_at, i.dispatch_token, i.receipt_json)
                     for i in s.scalars(select(OfTextIntent).order_by(OfTextIntent.created_at, OfTextIntent.intent_id))],
            decisions=[(d.fan_id, d.status) for d in s.scalars(select(OfEngineDecision).order_by(OfEngineDecision.created_at, OfEngineDecision.decision_id))],
            requests=[(r.fan_id, r.status, r.next_step) for r in s.scalars(select(OfEngineRequest).order_by(OfEngineRequest.created_at, OfEngineRequest.request_id))],
            attempts=[(a.outcome, a.evidence_json) for a in s.scalars(select(OfEngineAttempt).order_by(OfEngineAttempt.started_at))],
            states={f: (st.unresolved_attempt_id, st.mode, st.takeover_state, st.revision, st.requires_attention)
                    for f in (FAN, FAN2) if (st := s.get(OfConversationState, (ACCOUNT, f))) is not None},
            used=policy.used_units, queued=policy.queued_count, active=policy.active_intent_id,
            commands=s.scalar(select(func.count()).select_from(OfLaunchCommand).where(OfLaunchCommand.kind == 'release_unconfirmed_send')),
            posts=len(rig.w.fans.posts))


def counters(rig):
    with Session(rig.w.database[1]) as s:
        scope = s.scalar(select(OfEnginePilotScope))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        return policy.used_units, scope.provider_requests_used, scope.provider_credits_reserved


def refused(rig, intent, history, match, n=1, **kw):
    before = facts(rig)
    with pytest.raises(Refused, match=match):
        release(rig, intent, history, n, **kw)
    assert facts(rig) == before, 'a refused release changes nothing'


@pytest.fixture
def scene(rig):
    wire = Wire(rig)
    setup_unknown(rig, wire)
    intent = unknown_intent(rig, FAN)
    absent(rig, wire, intent)
    return rig, wire, intent


# ------------------------------------------------------------------------------------------------ the release itself
def test_release_after_the_quiet_period_with_complete_absence_history_frees_the_fan_and_it_is_answered_exactly_once(scene):
    rig, wire, intent = scene
    age(rig)
    before = facts(rig)
    assert before['posts'] == 1 and before['states'][FAN][0] == intent.attempt_id
    used, requests_before, credits_before = counters(rig)
    history = read(rig, FAN, intent)
    assert history.complete and history.reached_start and history.pages == 1 and wire.history_gets == 1
    assert counters(rig)[1] > requests_before, 'the history read went through the pilot request budget'
    result = release(rig, intent, history)
    assert result['released'] and result['deliveryOutcome'] == 'unconfirmed' and result['intentId'] == intent.intent_id
    after = facts(rig)
    row = after['intents'][0]
    assert row[:4] == (FAN, 'cancelled', send_recovery.RELEASED_UNCONFIRMED_SEND, True), 'terminal, never "not sent", reservation kept'
    assert row[4:] == before['intents'][0][4:], 'wire marker, dispatch token and (absent) receipt are retained untouched'
    assert after['decisions'][0] == (FAN, 'held') and after['requests'][0] == (FAN, 'cancelled', 'released_unconfirmed_send')
    assert after['attempts'][0][0] == 'unknown', 'the attempt record stays unknown evidence'
    evidence = json.loads(after['attempts'][0][1])
    assert evidence['release']['outcome'] == 'unconfirmed' and evidence['provider']['transport_error'] == 'ReadTimeout'
    assert after['states'][FAN][0] is None and after['active'] is None
    assert after['used'] == before['used'] and after['queued'] == before['queued'], 'delivery units are never refunded'
    assert counters(rig)[0] >= used and counters(rig)[2] >= credits_before
    assert after['commands'] == 1 and after['posts'] == 1, 'releasing sends nothing'
    with Session(rig.w.database[1]) as s:
        command = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'release_unconfirmed_send'))
        audit = json.loads(command.before_json)
        assert command.actor == ACTOR and command.command_key == K(1)
        assert audit['providerHistory']['digest'] == history.digest and audit['providerHistory']['messages'] == 2
        assert audit['sendEvidence']['provider']['transport_error'] == 'ReadTimeout'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention is True, 'attention is a separate human concern'
    # the fan's waiting message is now eligible through the ordinary scheduler and is answered once
    unpace(rig)
    rig.ticks(5)
    final = facts(rig)
    assert final['posts'] == 2, 'exactly one new POST'
    first, second = rig.w.fans.posts
    assert first['key'] != second['key'], 'a fresh logical action, not a replay of the unconfirmed key'
    assert [i[:3] for i in final['intents']] == [(FAN, 'cancelled', send_recovery.RELEASED_UNCONFIRMED_SEND), (FAN, 'accepted', None)]
    assert final['requests'][-1][:2] == (FAN, 'completed') and final['states'][FAN][0] is None
    assert final['used'] == before['used'] + 1
    rig.ticks(3)
    assert facts(rig)['posts'] == 2, 'no duplicate, nothing re-planned'
    assert 'unconfirmed' not in json.dumps(rig.seen[0]) and 'unconfirmed' in json.dumps(rig.seen[-1]), \
        'the next reply is planned knowing the earlier one is unconfirmed (not "cancelled unsent")'
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == 'out', Message.fan_id == FAN)) == 1


def test_a_released_unconfirmed_send_is_never_replayed_or_reopened_by_the_engine(scene):
    rig, wire, intent = scene
    age(rig)
    release(rig, intent, read(rig, FAN, intent))
    unpace(rig)
    rig.ticks(5)
    frozen = facts(rig)
    worker = rig.pilot.worker
    for _ in range(2):    # even asking the engine to turn the ORIGINAL message again must not send it
        worker.turn(TENANT, ACCOUNT, FAN, 81001)
        worker.send_approved(TENANT, ACCOUNT, rig.w.rows(OfEngineDecision)[0].decision_id)
    assert facts(rig)['posts'] == frozen['posts'] == 2
    with pytest.raises(Refused, match='already settled|unavailable|stale'):
        rig.w.authority.record_accepted(TENANT, ACCOUNT, intent.intent_id, intent.dispatch_token, None)
    assert [i['kind'] for i in operator_v2.unresolved(rig.w.store, TENANT, ACCOUNT, ACTOR) if i['kind'] == 'unknown_send'] == []


def test_replay_of_the_same_command_key_is_idempotent_and_a_different_request_is_a_conflict(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    first = release(rig, intent, history)
    done = facts(rig)
    assert release(rig, intent, history) == first, 'replay returns the first result'
    assert release(rig, intent, read(rig, FAN, intent)) == first, 'a replay does not need (or trust) a new history read'
    assert facts(rig) == done
    with pytest.raises(Refused, match='conflict'):
        release(rig, intent, history, next_input=81003)
    with pytest.raises(Refused, match='unconfirmed plain text send required'):
        release(rig, intent, history, n=2)      # a second key cannot release it again: it is no longer unknown
    assert facts(rig) == done


# ------------------------------------------------------------------------------------------------ time and completeness
def test_refused_before_the_quiet_period_has_elapsed(scene):
    rig, wire, intent = scene
    age(rig, minutes=10)
    refused(rig, intent, read(rig, FAN, intent), 'quiet period')
    age(rig, minutes=20)       # now 30 minutes and a few seconds: still the same fresh intent
    release(rig, intent, read(rig, FAN, intent))


def test_the_quiet_period_cannot_be_shortened_only_lengthened(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    for bad in (0, 60, send_recovery.QUIET_SECONDS - 1, True, 'x', 8 * 86400):
        before = facts(rig)
        with pytest.raises(Refused, match='invalid release command'):
            release(rig, intent, history, quiet_seconds=bad)
        assert facts(rig) == before
    refused(rig, intent, history, 'quiet period', quiet_seconds=7200)


def test_history_read_before_the_quiet_period_ended_does_not_count(scene):
    rig, wire, intent = scene
    early = read(rig, FAN, intent)           # read while the send is still seconds old
    age(rig)
    refused(rig, intent, early, 'read before the quiet period')


def test_stale_history_is_refused(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    stale = replace(history, started_at=history.started_at - timedelta(minutes=10), ended_at=history.ended_at - timedelta(minutes=10))
    refused(rig, intent, stale, 'fresh provider history')
    skewed = replace(history, ended_at=utcnow() + timedelta(minutes=10))
    refused(rig, intent, skewed, 'fresh provider history')


def test_incomplete_or_truncated_history_is_refused(scene):
    rig, wire, intent = scene
    age(rig)
    # every page is newer than the send and the provider keeps saying "more": the read cannot cover the dispatch
    recent = [provider_message(81900 + n, FAN, f'x{n}', iso(-1)) for n in range(3)]
    wire.pages = {FAN: [recent for _ in range(4)], FAN2: [[]]}
    history = read(rig, FAN, intent, max_pages=2)
    assert history.complete is False and history.pages == 2 and not history.reached_start
    refused(rig, intent, history, 'complete provider history required')
    # a forged "complete" flag does not help: the messages themselves must reach back past the dispatch
    forged = replace(history, complete=True)
    refused(rig, intent, forged, 'does not cover the send')
    # an unreadable page is not "no messages"
    wire.pages = None
    wire.history[FAN] = [{'id': 5}]
    with pytest.raises(Refused, match='unreadable'):
        read(rig, FAN, intent)


def test_history_covers_the_send_when_an_older_message_is_seen_on_a_later_page(scene):
    rig, wire, intent = scene
    age(rig)
    newest = [provider_message(81002, FAN, 'hey are you there??', iso(-1))]
    older = [provider_message(81001, FAN, 'hello, how was your day?', at(rig, intent, -300))]
    wire.pages = {FAN: [newest, older, newest], FAN2: [[]]}
    history = read(rig, FAN, intent)
    assert history.complete and not history.reached_start and history.pages == 2
    release(rig, intent, history)


def test_history_read_needs_the_current_pilot_request_guard(scene):
    rig, wire, intent = scene
    age(rig)
    with pytest.raises(Refused, match='request guard'):
        read(rig, FAN, intent, client=operator_client(rig, guarded=False))
    with pytest.raises(Refused, match='request guard'):
        read(rig, FAN, intent, client=operator_client(rig, revision=10_000))
    other = operator_client(rig)
    with pytest.raises(Refused):
        send_recovery.read_absence_history(authority(rig), TENANT, ACCOUNT, FAN2, intent.intent_id, client=other)
    with Session(rig.w.database[1]) as s, s.begin():       # the budget guard is not a suggestion
        scope = s.scalar(select(OfEnginePilotScope))
        scope.provider_requests_used = json.loads(scope.config_json)['limits']['provider_requests']
    with pytest.raises(Refused, match='budget'):
        read(rig, FAN, intent)


# ------------------------------------------------------------------------------------------------ the message may be there
def test_a_message_that_could_be_ours_in_history_refuses_and_points_to_positive_reconciliation(scene):
    rig, wire, intent = scene
    age(rig)
    base = list(wire.history[FAN])
    ours = provider_message(91001, CREATOR_ID, intent.body, at(rig, intent, 3))
    for label, extra in (('same text in the send window', ours),
                         ('another text in the send window', provider_message(91002, CREATOR_ID, 'a human typed this', at(rig, intent, 20))),
                         ('same text, hours later', provider_message(91003, CREATOR_ID, intent.body, iso(-5))),
                         ('almost the same text later', provider_message(91004, CREATOR_ID, intent.body.upper() + ' ', iso(-5)))):
        wire.history[FAN] = list(base)
        put(wire, FAN, extra)
        refused(rig, intent, read(rig, FAN, intent), 'absence is not proven')
    wire.history[FAN] = list(base)
    put(wire, FAN, ours)
    with pytest.raises(Refused, match='positive reconciliation'):
        release(rig, intent, read(rig, FAN, intent))


def test_unrelated_history_does_not_block_a_release(scene):
    rig, wire, intent = scene
    age(rig)
    wire.history[FAN] = [provider_message(81002, FAN, 'hey are you there??', iso(-1)),
                         provider_message(90001, CREATOR_ID, 'a different message hours later', iso(-5)),
                         provider_message(81001, FAN, 'hello, how was your day?', iso(-2)),
                         provider_message(90000, CREATOR_ID, intent.body, at(rig, intent, -3600))]       # same words, long BEFORE the send
    release(rig, intent, read(rig, FAN, intent))


def test_unreadable_creator_messages_and_clock_skew_are_ambiguity(scene):
    rig, wire, intent = scene
    age(rig)
    base = list(wire.history[FAN])
    put(wire, FAN, provider_message(92001, CREATOR_ID, intent.body, at(rig, intent, 5), responseType='mass'))
    refused(rig, intent, read(rig, FAN, intent), 'absence is not proven')
    wire.history[FAN] = list(base)
    put(wire, FAN, provider_message(92002, FAN, 'from the future', (utcnow() + timedelta(hours=2)).isoformat() + 'Z'))
    refused(rig, intent, read(rig, FAN, intent), 'clock_skew')
    wire.history[FAN] = [*base, base[-1]]            # the oldest message twice
    refused(rig, intent, read(rig, FAN, intent), 'history_duplicate_id')


def test_a_canonical_outbound_message_blocks_the_release(scene):
    rig, wire, intent = scene
    age(rig)
    with Session(rig.w.database[1]) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=93001, direction='out', body='sent by someone',
                      created_at=intent.dispatched_at + timedelta(seconds=4), media_ids='[]', media_count=0, price_cents=0))
    refused(rig, intent, read(rig, FAN, intent), 'canonical outbound message')


# ------------------------------------------------------------------------------------------------ the next fan message
def test_a_fresh_fan_message_after_the_send_must_exist_and_be_the_one_named(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    refused(rig, intent, history, 'fresh fan message', next_input=81001)          # the message that was being answered
    refused(rig, intent, history, 'next_input_not_in_history', next_input=99999)  # nothing like it anywhere
    wire.history[FAN] = [m for m in wire.history[FAN] if m['id'] != 81002]
    refused(rig, intent, read(rig, FAN, intent), 'next_input_not_in_history')     # we have it, the provider does not


def test_without_any_new_fan_message_the_fan_stays_held(rig):
    wire = Wire(rig)
    wire.post_script.append('timeout')
    rig.post(inbound(81501, 'hello, how was your day?', fan=FAN), key='nofresh')
    rig.ticks(3)
    intent = unknown_intent(rig, FAN)
    wire.history[FAN] = [provider_message(81501, FAN, 'hello, how was your day?', iso(-2))]
    age(rig)
    refused(rig, intent, read(rig, FAN, intent), 'fresh fan message', next_input=81501)


# ------------------------------------------------------------------------------------------------ authorization and scope
def test_the_actor_must_hold_the_reconcile_permission_and_the_command_must_be_exact(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    refused(rig, intent, history, 'permission', actor=OTHER_ACTOR)
    for bad_key in ('short', 'G' * 64, K(1).upper(), None):
        before = facts(rig)
        with pytest.raises(Refused, match='invalid launch command'):
            release(rig, intent, history, command_key=bad_key)
        assert facts(rig) == before
    before = facts(rig)
    with pytest.raises(Refused):
        release(rig, intent, history, actor='anyone')
    with pytest.raises(Refused):
        release(rig, intent, history, reason='   ')
    with pytest.raises(Refused, match='invalid release command'):
        release(rig, intent, history, next_input=0)
    with pytest.raises(Refused, match='invalid release command'):
        release(rig, intent, history, expected_revision='3')
    assert facts(rig) == before


def test_history_for_another_account_fan_or_creator_is_not_accepted(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    for forged in (replace(history, fan_id=FAN2), replace(history, account_id='another'), replace(history, creator_id=1),
                   replace(history, pages=0), replace(history, messages=list(history.messages)), 'history', None):
        refused(rig, intent, forged, 'complete provider history required')


def test_the_intent_must_be_the_unchanged_unknown_plain_text_send_of_that_fan(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    refused(rig, intent, history, 'unconfirmed plain text send required', expected_attempt_id='other-attempt')
    refused(rig, intent, history, 'control revision conflict', expected_revision=revision(rig) + 1)
    with pytest.raises(Refused):
        send_recovery.release_unconfirmed_send(authority(rig), TENANT, ACCOUNT, FAN2, intent.intent_id,
            expected_attempt_id=intent.attempt_id, expected_revision=revision(rig, FAN2), next_input_id=81002,
            history=history, command_key=K(7), actor=ACTOR, reason=REASON)
    for column, value in (('reason', 'provider_identity_conflict'), ('origin', 'manual')):
        with rig.w.database[1].begin() as c:
            old = c.execute(text(f"SELECT {column} FROM of_text_intent WHERE status='unknown'")).scalar()
            c.execute(text(f"UPDATE of_text_intent SET {column} = :new WHERE status='unknown'"), {'new': value})
        refused(rig, intent, history, 'unconfirmed plain text send required')
        with rig.w.database[1].begin() as c:
            c.execute(text(f"UPDATE of_text_intent SET {column} = :old WHERE status='unknown'"), {'old': old})
    release(rig, intent, history)


# ------------------------------------------------------------------------------------------------ other holds still win
def _sql(statement):
    def change(rig):
        with rig.w.database[1].begin() as c:
            c.execute(text(statement.format(fan=FAN)))
    return change


def _restrict(rig):
    with Session(rig.w.database[1]) as s, s.begin():
        s.add(SkipList(account_id=ACCOUNT, fan_id=FAN, reason='manual_restrict'))


def _unresolved_delivery(rig):
    with Session(rig.w.database[1]) as s, s.begin():
        s.add(OfMessageDelivery(account_id=ACCOUNT, fan_id=FAN, message_id=94001, outcome='unknown'))


@pytest.mark.parametrize('label,change,match', [
    ('no_contact', _sql("UPDATE of_conversation_state SET mode='no_contact' WHERE fan_id={fan}"), 'contact_blocked'),
    ('paused', _sql("UPDATE of_conversation_state SET mode='paused' WHERE fan_id={fan}"), 'contact_blocked'),
    ('human_controlled', _sql("UPDATE of_conversation_state SET mode='human_controlled', takeover_state='effective' WHERE fan_id={fan}"), 'hold remains'),
    ('takeover_requested', _sql("UPDATE of_conversation_state SET takeover_state='requested' WHERE fan_id={fan}"), 'takeover_pending'),
    ('muted', _sql("UPDATE fans SET is_muted=true WHERE fan_id={fan}"), 'contact_blocked'),
    ('restricted', _restrict, 'fan_restricted'),
    ('other_unresolved_delivery', _unresolved_delivery, 'another delivery hold'),
])
def test_stop_takeover_and_other_holds_still_win(scene, label, change, match):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    change(rig)
    refused(rig, intent, history, match)


def test_a_pending_human_message_is_pilot_evidence_that_still_holds(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    from db.onlyfans_fans_intake_models import OfFansEvidence
    columns = {c.name: c for c in OfFansEvidence.__table__.columns}
    row = dict(account_id=ACCOUNT, evidence_id='human-1', kind='human_sent', fan_id=FAN, status='held', reason='unclassified',
               created_at=utcnow(), updated_at=utcnow())
    with Session(rig.w.database[1]) as s, s.begin():
        s.add(OfFansEvidence(**{k: v for k, v in row.items() if k in columns}))
    refused(rig, intent, history, 'evidence hold remains')


# ------------------------------------------------------------------------------------------------ two fans, independently
def test_two_fans_are_released_independently_and_each_is_answered_once(rig):
    wire = Wire(rig)
    setup_unknown(rig, wire, fan=FAN, first=82001, second=82002, key='two')
    setup_unknown(rig, wire, fan=FAN2, first=82101, second=82102, key='two')
    one, two = unknown_intent(rig, FAN), unknown_intent(rig, FAN2)
    assert one is not None and two is not None and one.intent_id != two.intent_id
    for fan, first, second in ((FAN, 82001, 82002), (FAN2, 82101, 82102)):
        wire.history[fan] = [provider_message(second, fan, 'hey are you there??', iso(-1)),
                             provider_message(first, fan, 'hello, how was your day?', iso(-2))]
    age(rig)
    posts = facts(rig)['posts']
    history_one = read(rig, FAN, one)
    # FAN is released; FAN2 is untouched and still held
    release(rig, one, history_one, n=1, fan=FAN, next_input=82002)
    mid = facts(rig)
    assert mid['states'][FAN][0] is None and mid['states'][FAN2][0] == two.attempt_id
    assert [i[1] for i in mid['intents'] if i[0] == FAN2] == ['unknown']
    # FAN2 cannot be released with FAN's history, nor FAN's intent with FAN2's scope
    refused(rig, two, history_one, 'complete provider history required', n=2, fan=FAN2, next_input=82102)
    with pytest.raises(Refused):
        release(rig, one, read(rig, FAN2, two), n=3, fan=FAN2, next_input=82102)
    unpace(rig)
    rig.ticks(5)
    after = facts(rig)
    assert after['posts'] == posts + 1, 'only FAN was answered; FAN2 stays held'
    assert after['states'][FAN2][0] == two.attempt_id and unknown_intent(rig, FAN2) is not None
    release(rig, two, read(rig, FAN2, two), n=2, fan=FAN2, next_input=82102)
    unpace(rig)
    rig.ticks(5)
    final = facts(rig)
    assert final['posts'] == posts + 2 and final['commands'] == 2
    assert all(state[0] is None for state in final['states'].values())
    assert sorted(r[1] for r in final['requests']) == ['cancelled', 'cancelled', 'completed', 'completed']
    assert final['used'] == after['used'] + 1 and final['used'] >= 4
