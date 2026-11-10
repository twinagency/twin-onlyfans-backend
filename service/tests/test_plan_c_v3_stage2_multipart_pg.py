"""Stage 2D: multi-part replies check sequence capacity first and stay accurate when stopped, crashed or unknown."""
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from conversation_engine import approval
from conversation_engine.engine import SyntheticProposal
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfEngineRequest
from test_engine_real_worker_pg import answer, posts_text, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT

THREE = lambda snap: SyntheticProposal('part one', parts=('part two', 'part three'))  # noqa: E731


def set_units(world, used, limit):
    with world.database[1].begin() as c:
        c.execute(text('UPDATE of_delivery_policy SET used_units=:u, limit_units=:l WHERE account_id=:a'),
                  dict(u=used, l=limit, a=ACCOUNT))


def policy(world):
    with Session(world.database[1]) as s:
        p = s.get(OfDeliveryPolicy, ACCOUNT)
        return p.used_units, p.queued_count


def parts(world):
    return {d.part_index: d.status for d in world.rows(OfEngineDecision)}


@pytest.mark.parametrize('used', [8, 9])
def test_insufficient_known_capacity_sends_no_first_part_and_cancels_the_decision(world, used):
    set_units(world, used, 10)
    world.reply(THREE)
    result, _ = world.say('tell me everything')
    assert world.fans.posts == [], 'no part may start when the sequence cannot finish'
    assert parts(world) == {0: 'cancelled', 1: 'cancelled', 2: 'cancelled'}
    intents = world.rows(OfTextIntent)
    assert len(intents) == 1 and intents[0].status == 'refused' and intents[0].reason == 'sequence_capacity_insufficient'
    assert policy(world) == (used, 0), 'no unit reserved, no queue slot leaked'
    request = world.rows(OfEngineRequest)[0]
    assert request.status == 'open' and request.next_step == 'review_delivery_capacity'
    assert [a.invalidation_reason for a in world.rows(OfEngineApproval)] == ['delivery_capacity']
    # Nothing is retried: replaying the same staged send creates no intent and posts nothing.
    again = world.worker.send_approved(TENANT, ACCOUNT, world.rows(OfEngineDecision)[0].decision_id)
    assert again['outcome'].startswith('not_sent:') and world.fans.posts == [] and len(world.rows(OfTextIntent)) == 1


def test_sufficient_capacity_completes_every_part_exactly_once(world):
    set_units(world, 7, 10)
    world.reply(THREE)
    result, _ = world.say('tell me everything')
    assert result['outcome'] == 'accepted'
    assert posts_text(world) == ['part one', 'part two', 'part three']
    assert parts(world) == {0: 'completed', 1: 'completed', 2: 'completed'}
    assert policy(world) == (10, 0)
    assert world.rows(OfEngineRequest)[0].status == 'completed'
    decision = world.rows(OfEngineDecision)[0].decision_id
    world.worker.send_approved(TENANT, ACCOUNT, decision)           # a repeat/duplicate click
    assert len(world.fans.posts) == 3 and len(world.rows(OfTextIntent)) == 3


def test_a_cap_refused_single_part_is_cancelled_not_retried(world):
    set_units(world, 10, 10)
    world.say_plain('one short reply')
    world.say('hi there')
    assert world.fans.posts == []
    assert parts(world) == {0: 'cancelled'}
    intents = world.rows(OfTextIntent)
    assert len(intents) == 1 and intents[0].status == 'refused' and intents[0].reason == 'cap_exhausted'
    assert world.rows(OfEngineRequest)[0].next_step == 'review_delivery_capacity'
    decision = world.rows(OfEngineDecision)[0].decision_id
    for _ in range(2):
        world.worker.send_approved(TENANT, ACCOUNT, decision)
    assert world.fans.posts == [] and len(world.rows(OfTextIntent)) == 1


def _stop_after_first_part(world, kind):
    def hook(count):
        if count != 1:
            return
        with Session(world.database[1]) as s:
            revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
            row = s.scalar(select(OfEngineApproval))
            request, generation = row.request_id, row.generation
        if kind in ('takeover', 'no_contact'):
            world.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled' if kind == 'takeover' else 'no_contact',
                                expected_revision=revision, command_key='v3-' + kind, actor='synthetic-operator',
                                reason='Synthetic stop while part one is on the wire')
        elif kind == 'correction':
            with Session(world.database[1]) as s, s.begin():
                approval.invalidate_generation_in_session(s, ACCOUNT, request, generation, 'memory_changed')
    world.fans.hooks.append(hook)


@pytest.mark.parametrize('kind', ['takeover', 'no_contact', 'correction'])
def test_stop_or_correction_after_part_one_cancels_the_remainder_accurately(world, kind):
    world.reply(THREE)
    _stop_after_first_part(world, kind)
    result, _ = world.say('first question')
    assert posts_text(world) == ['part one'], 'a part after the stop was submitted'
    assert result['outcome'].startswith(('superseded:', 'not_authorized:', 'not_sent:')), result
    assert parts(world) == {0: 'completed', 1: 'cancelled', 2: 'cancelled'}
    intents = world.rows(OfTextIntent)
    assert [(i.status, i.projected_at is not None) for i in intents] == [('accepted', True)]   # part one is real
    with Session(world.database[1]) as s:
        assert s.scalar(select(Message.body).where(Message.direction == 'out')) == 'part one'
    assert world.rows(OfEngineRequest)[0].status != 'completed'
    world.worker.send_approved(TENANT, ACCOUNT, world.rows(OfEngineDecision)[0].decision_id)
    assert posts_text(world) == ['part one']


def _second_post(world, behaviour):
    original = world.fans.transport.handler
    state = {'n': 0}

    def handler(request):
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            state['n'] += 1
            if state['n'] == 2:
                world.fans.posts.append({'path': request.url.path, 'key': request.headers['Idempotency-Key'],
                                         'body': None, 'auth': None})
                return behaviour()
        return original(request)
    world.fans.transport.handler = handler


def test_an_unknown_part_is_never_replayed_and_later_parts_never_start(world):
    world.reply(THREE)
    _second_post(world, lambda: answer(503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'unknown'}))
    result, _ = world.say('first question')
    assert len(world.fans.posts) == 2 and result['outcome'] == 'unknown'
    assert parts(world)[0] == 'completed' and parts(world)[1] == 'unknown' and parts(world)[2] == 'ready'
    decision = world.rows(OfEngineDecision)[0].decision_id
    for _ in range(3):
        world.worker.send_approved(TENANT, ACCOUNT, decision)
        world.transport.recover_interrupted()
    assert len(world.fans.posts) == 2 and len(world.rows(OfTextIntent)) == 2


class Crash(BaseException):
    pass


def test_a_crash_after_part_two_reached_the_wire_never_duplicates_it(world):
    world.reply(THREE)

    def die():
        raise Crash()
    _second_post(world, die)
    with pytest.raises(Crash):
        world.say('first question')
    assert len(world.fans.posts) == 2
    with world.database[1].begin() as c:
        c.execute(text("UPDATE of_text_intent SET deadline = clock_timestamp() - interval '1 second' WHERE status='dispatching'"))
    assert world.transport.recover_interrupted() == 1
    statuses = {i.status for i in world.rows(OfTextIntent)}
    assert statuses == {'accepted', 'unknown'}, 'a wired attempt is unknown, never retired or retried'
    decision = world.rows(OfEngineDecision)[0].decision_id
    world.worker.send_approved(TENANT, ACCOUNT, decision)
    assert len(world.fans.posts) == 2
