"""Stage 2A: every wire attempt, including a retry after a provider-proven not_sent, re-checks authority.

Real FansApiRealTransport against the loopback fake Fans server; no provider or model call. The between-attempt
intervention runs in the transport's own back-off sleep, i.e. exactly between POST 1 (explicit not_sent) and POST 2.
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from conversation_engine import approval
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineApproval, OfFansConnection
from fake_fans_server import FakeFans
from onlyfans_runtime.store import Refused
from test_onlyfans_foundation_pg import database, event  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_real_transport_pg import build, real_flag, status, text_intent  # noqa: F401


def _control(store, database, mode, key):
    with Session(database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    store.control(TENANT, ACCOUNT, FAN, mode=mode, expected_revision=revision, command_key=key,
                  actor='synthetic-operator', reason='Synthetic control between proven-not-sent attempts')


def _sql(database, statement, **params):
    with database[1].begin() as c:
        c.execute(text(statement), params)


def _intervene(kind, database, store, authority, intent):
    if kind == 'takeover':
        _control(store, database, 'human_controlled', 'v3-takeover')
    elif kind == 'stop':
        _control(store, database, 'paused', 'v3-stop')
    elif kind == 'no_contact':
        _control(store, database, 'no_contact', 'v3-no-contact')
    elif kind == 'policy_stop':
        with Session(database[1]) as s:
            p = s.get(OfDeliveryPolicy, ACCOUNT)
            args = dict(expected_revision=p.revision, enabled=False, limit_units=p.limit_units, throttle_seconds=p.throttle_seconds)
        authority.configure(TENANT, ACCOUNT, actor='real-lab', reason='policy stop between attempts', **args)
    elif kind == 'newer_fan_input':
        store.record(TENANT, ACCOUNT, event(mid=2, fan=FAN, body='wait, one more thing'))
        store.drain(TENANT, ACCOUNT)
    elif kind == 'approval_withdrawn':
        with Session(database[1]) as s, s.begin():
            row = s.scalar(select(OfEngineApproval))
            approval.invalidate_generation_in_session(s, ACCOUNT, row.request_id, row.generation, 'correction')
    elif kind == 'rebind':
        _sql(database, "UPDATE of_fans_connection SET status='retired' WHERE account_id=:a", a=ACCOUNT)
    elif kind == 'freshness':
        _sql(database, "UPDATE of_text_intent SET expires_at = clock_timestamp() - interval '1 minute' WHERE intent_id=:i", i=intent)
    elif kind == 'budget':
        _sql(database, "UPDATE of_delivery_policy SET limit_units = 0 WHERE account_id=:a", a=ACCOUNT)
    else:
        raise AssertionError(kind)


KINDS = ['takeover', 'stop', 'no_contact', 'policy_stop', 'newer_fan_input', 'approval_withdrawn', 'rebind',
         'freshness', 'budget']


@pytest.mark.parametrize('kind', KINDS)
def test_authority_removed_between_attempts_sends_no_further_post(database, kind):
    fake = FakeFans(['not_sent', 'accepted'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        calls = []

        def between(_):
            calls.append(kind)
            _intervene(kind, database, store, authority, intent)
        transport.sleep = between
        outcome = authority.dispatch(TENANT, ACCOUNT, intent, transport)
        assert calls == [kind], 'the intervention must run between the two attempts'
        assert len(fake.posts) == 1 and fake.executions == {}, 'a POST left after the authority was removed'
        assert outcome == 'not_accepted'          # the first attempt is proven not sent: nothing unknown, nothing hidden
        row = status(database, intent)
        assert row.status == 'not_accepted' and row.projected_at is None
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 7001)) is None
    finally:
        fake.close()


def test_unchanged_authorized_retry_yields_exactly_one_accepted_action(database):
    fake = FakeFans(['not_sent', 'not_sent'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        rechecks = []
        transport.sleep = lambda s: rechecks.append(s)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        assert len(fake.posts) == 3 and len({p['key'] for p in fake.posts}) == 1
        assert len({repr(p['body']) for p in fake.posts}) == 1, 'payload must be immutable across retries'
        assert sum(fake.executions.values()) == 1
        row = status(database, intent)
        assert row.status == 'accepted' and row.projected_at is not None
        with Session(database[1]) as s:
            assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1, 'a retry must not consume a second unit'
            assert len(list(s.scalars(select(Message).where(Message.direction == 'out')))) == 1
    finally:
        fake.close()


@pytest.mark.parametrize('outcomes', [['unknown'], ['lost'], ['not_sent', 'unknown'], ['not_sent', 'lost'],
                                      ['conflict'], ['not_sent', 'conflict']])
def test_unknown_outcome_is_never_blindly_replayed(database, outcomes):
    fake = FakeFans(outcomes + ['accepted'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        assert len(fake.posts) == len(outcomes)
        assert status(database, intent).status == 'unknown'
    finally:
        fake.close()


def test_a_retry_authorization_is_taken_by_exactly_one_concurrent_worker(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        grant = authority.admit(TENANT, ACCOUNT, intent)
        assert authority.consume_dispatch(TENANT, grant) == grant
        with pytest.raises(Refused):           # the one-use grant itself can never be consumed twice
            authority.consume_dispatch(TENANT, grant)

        def take(posts):
            try:
                return authority.consume_dispatch(TENANT, grant, retry_after_posts=posts) == grant
            except Refused:
                return False
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(take, [1, 1, 1, 1]))
        assert results.count(True) == 1, results
        assert take(1) is False                # already taken
        assert take(2) is True                 # the next attempt number is a distinct authorization
        assert take(2) is False
        with pytest.raises(Refused):           # never beyond the 3-POST budget's counter space
            authority.consume_dispatch(TENANT, grant, retry_after_posts=3)
        assert fake.posts == []
    finally:
        fake.close()


def test_a_retry_cannot_open_a_wire_attempt_that_was_never_consumed(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        grant = authority.admit(TENANT, ACCOUNT, intent)
        with pytest.raises(Refused):
            authority.consume_dispatch(TENANT, grant, retry_after_posts=1)
        assert status(database, intent).wire_started_at is None
    finally:
        fake.close()
