"""A provider throttle is pacing evidence, never proof that a send did not happen (handoff section 8, D429).

HTTP 429 must start the account cooldown AND leave delivery uncertain: the attempt stays `unknown`, the fan stays
held, the reservation stays held, nothing is replayed, and the existing evidence owner keeps the status and
Retry-After. Other non-2xx outcomes (404, 5xx) stay uncertain WITHOUT a cooldown. Nothing here is a provider call:
the wire is a scripted httpx.MockTransport behind the real FansApiClient/FansApiRealTransport/TextAuthority, on
real PostgreSQL.
"""
import gzip
import json
import time
from datetime import timedelta
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.models import Fan, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from fans_api_adapter.real_transport import RealFansConfig, build_client
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import TextAuthority, SyntheticTextTransport
from test_onlyfans_foundation_pg import database, seed, event  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, current
from test_real_transport_pg import build, real_flag, status, text_intent  # noqa: F401
from test_plan_c_unknown_send_evidence_pg import Provider, kept, attempt, reply, forbid, TOKEN, BODY
from fake_fans_server import ACCT, CREATOR

NOT_SENT_503 = (503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"not_sent"}', {})


def next_dispatch(database):
    with Session(database[1]) as s:
        return s.get(OfDeliveryPolicy, ACCOUNT).next_dispatch_at


def assert_uncertain_and_held(database, intent, provider, posts=1):
    row = status(database, intent)
    assert (row.status, row.reason) == ('unknown', 'invalid_receipt')
    assert row.reservation_held and row.provider_message_id is None
    with Session(database[1]) as s:
        control = s.get(OfConversationState, (ACCOUNT, FAN))
        assert control.unresolved_attempt_id == row.attempt_id
    assert attempt(database).outcome == 'unknown'
    assert len(provider.posts) == posts


# ----------------------------------------------------------------------------------------- the adapter result
def client_for(provider):
    client = build_client(RealFansConfig('fixture-token', ACCT, 'https://api.fansapi.com', 5),
                          creator_id=CREATOR, epoch=1, transport=provider.transport())
    assert client.identity().state == 'verified'
    return client


@pytest.mark.parametrize('retry,expected', [('600', 600), ('99999', 3600), (None, None), ('soon', None)])
def test_adapter_reports_429_as_rate_limited_but_still_unknown(retry, expected):
    provider = Provider((429, '{}', {'Retry-After': retry} if retry else {}))
    result = client_for(provider).send(FAN, key='k' * 32, text='Hi')
    assert result.state == 'unknown', 'a 429 never proves the send did not happen'
    assert result.http_status == 429 and result.cause == 'provider_rate_limited'
    assert result.retry_after == expected


@pytest.mark.parametrize('status_code', [404, 500, 502, 503, 504])
def test_adapter_carries_no_rate_limit_metadata_for_other_statuses(status_code):
    provider = Provider((status_code, '{}', {'Retry-After': '600'}))
    result = client_for(provider).send(FAN, key='k' * 32, text='Hi')
    assert result.state == 'unknown' and result.http_status == status_code
    assert result.cause is None and result.retry_after is None


# ----------------------------------------------------------------------------------------- the real dispatch path
@pytest.mark.parametrize('body', ['{}', '{"error":"RATE_LIMITED","request_outcome":"not_sent"}', 'not-json', ''])
def test_http_429_cools_account_without_settling_or_replaying_send(database, body):
    provider = Provider((429, body, {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    start = utcnow()
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert next_dispatch(database) >= start + timedelta(seconds=599)
    with Session(database[1]) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1, 'the reserved unit stays consumed'
    assert_uncertain_and_held(database, intent, provider)
    value, raw = kept(database)
    assert value['provider']['http_status'] == 429 and value['provider']['retry_after'] == 600
    forbid(raw, TOKEN, BODY, 'Authorization', 'Bearer')
    # restart: nothing is replayed and the hold persists
    assert TextAuthority(store).dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert transport.recover_interrupted() == 0
    assert len(provider.posts) == 1
    store.close()


def test_a_compressed_429_still_cools_the_account_and_is_never_decoded(database):
    packed = gzip.compress(b'{"error":"RATE_LIMITED","request_outcome":"not_sent"}')
    provider = Provider((429, packed, {'Retry-After': '600', 'Content-Encoding': 'gzip'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    start = utcnow()
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert next_dispatch(database) >= start + timedelta(seconds=599)
    assert_uncertain_and_held(database, intent, provider)
    value, raw = kept(database)
    assert value['provider']['http_status'] == 429
    assert value['provider'].get('error_code') is None, 'the encoded body is refused, not read'
    store.close()


@pytest.mark.parametrize('code', [200, 404, 409, 422, 500])
def test_a_compressed_non_429_response_stays_a_refused_unknown_without_cooldown(database, code):
    packed = gzip.compress(b'{"data":{"id":7001,"createdAt":"2026-11-07T00:00:00+00:00"}}')
    provider = Provider((code, packed, {'Retry-After': '600', 'Content-Encoding': 'gzip'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert next_dispatch(database) < utcnow() + timedelta(seconds=10)
    assert_uncertain_and_held(database, intent, provider)
    assert kept(database)[0]['provider']['transport_error'] == 'ValueError'
    store.close()


@pytest.mark.parametrize('header,low,high', [
    (None, 9, 60),                                                      # absent -> the existing 10 s default
    ('soon', 9, 60),                                                    # malformed -> default, not a crash
    ('5', 4, 60),
    ('0', 0, 60),                                                       # never a negative or zero-length wedge
    ('99999', 3590, 3601),                                              # the adapter clamps at an hour
    ('HTTP-DATE', 100, 130),             # HTTP-date form
])
def test_429_honours_retry_after_forms(database, header, low, high):
    if header == 'HTTP-DATE':
        header = formatdate(time.time() + 120, usegmt=True)
    provider = Provider((429, '{}', {'Retry-After': header} if header else {}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    start = utcnow()
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    wait = (next_dispatch(database) - start).total_seconds()
    assert low <= wait <= high, wait
    assert_uncertain_and_held(database, intent, provider)
    store.close()


@pytest.mark.parametrize('code', [404, 500, 502, 503, 504])
def test_non_throttle_unknown_does_not_add_rate_limit_cooldown(database, code):
    provider = Provider((code, '{"request_outcome":"unknown"}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert next_dispatch(database) < utcnow() + timedelta(seconds=10)
    assert_uncertain_and_held(database, intent, provider)
    assert 'retry_after' not in kept(database)[0]['provider']
    store.close()


def test_429_never_shortens_a_longer_cooldown_set_by_a_concurrent_signal(database):
    class Racing(Provider):
        """While this POST is in flight another worker records a longer pacing wait on the same account."""
        def handle(self, request):
            if request.method == 'POST':
                with database[1].begin() as c:
                    c.execute(text("UPDATE of_delivery_policy SET next_dispatch_at = CURRENT_TIMESTAMP + INTERVAL '2 hours'"))
            return super().handle(request)
    provider = Racing((429, '{}', {'Retry-After': '60'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert next_dispatch(database) >= utcnow() + timedelta(minutes=119), 'the longer wait survives a shorter 429'
    assert_uncertain_and_held(database, intent, provider)
    store.close()


def test_429_after_a_proven_not_sent_attempt_cools_and_stops_with_two_posts_total(database):
    provider = Provider(NOT_SENT_503, (429, '{}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    start = utcnow()
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert next_dispatch(database) >= start + timedelta(seconds=599)
    assert_uncertain_and_held(database, intent, provider, posts=2)
    assert provider.posts[0] == provider.posts[1], 'same idempotency key on the bounded retry'
    store.close()


def test_a_failing_cooldown_write_never_changes_the_uncertain_outcome(database, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError('policy store down')
    monkeypatch.setattr(TextAuthority, '_account_cooldown', broken)
    provider = Provider((429, '{}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert_uncertain_and_held(database, intent, provider)
    assert kept(database)[0]['provider']['http_status'] == 429
    store.close()


def test_a_crash_while_applying_the_cooldown_remains_unknown_without_replay(database, monkeypatch):
    provider = Provider((429, '{}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    real = TextAuthority._account_cooldown

    def die(self, *a, **k):
        raise KeyboardInterrupt('process died after the 429 response')
    monkeypatch.setattr(TextAuthority, '_account_cooldown', die)
    with pytest.raises(KeyboardInterrupt):
        authority.dispatch(TENANT, ACCOUNT, intent, transport)
    monkeypatch.setattr(TextAuthority, '_account_cooldown', real)
    with database[1].begin() as c:
        c.execute(text("UPDATE of_text_intent SET deadline = CURRENT_TIMESTAMP - INTERVAL '1 second'"))
    assert authority.expire_dispatches(TENANT, ACCOUNT) == 1
    assert transport.reconcile_accepted(intent) == 'unknown'
    assert len(provider.posts) == 1
    store.close()


def test_acceptance_before_crash_is_not_turned_into_a_throttle_or_a_replay(database, monkeypatch):
    """The provider accepted (200), then the process died: unknown, no cooldown, no second POST."""
    provider = Provider((200, '{"data":{"id":7001,"createdAt":"2026-11-07T00:00:00+00:00"}}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)

    def die(self, *a, **k):
        raise KeyboardInterrupt('process died after provider acceptance')
    monkeypatch.setattr(TextAuthority, 'record_accepted', die)
    with pytest.raises(KeyboardInterrupt):
        authority.dispatch(TENANT, ACCOUNT, intent, transport)
    monkeypatch.undo()
    assert next_dispatch(database) < utcnow() + timedelta(seconds=10)
    assert len(provider.posts) == 1
    store.close()


def test_429_does_not_cool_a_different_creator_with_overlapping_fan_id(database):
    provider = Provider((429, '{}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    other = 'independent-creator'
    other_store = seed(database[0], database[1], account=other, tenant=TENANT, fan=FAN)
    current(database[1], account=other, fan=FAN, mode='human_controlled')
    other_authority = TextAuthority(other_store)
    other_authority.configure(TENANT, other, expected_revision=0, enabled=True, limit_units=10,
                              throttle_seconds=1, actor='offline-operator', reason='isolated creator fixture')
    other_intent = other_authority.prepare(TENANT, other, FAN, command_key='other-reply',
        expected_revision=0, body='Hello', origin='manual', actor='offline-operator')
    wire = SyntheticTextTransport(other)
    assert other_authority.dispatch(TENANT, other, other_intent, wire) == 'accepted'
    assert len(wire.calls) == 1 and len(provider.posts) == 1
    other_store.close(); store.close()


def test_http_429_defers_another_fan_on_same_account(database):
    from approval_helpers import queue_decision
    provider = Provider((429, '{}', {'Retry-After': '600'}))
    store, authority, engine, transport, _ = build(database, provider)
    first = text_intent(engine, store)
    second_fan = FAN + 1
    with Session(database[1]) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=second_fan))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=second_fan, mode='automated', revision=0,
                                  takeover_state='none', requires_attention=False))
    engine.select_simulated(TENANT, ACCOUNT, second_fan)
    store.record(TENANT, ACCOUNT, event(mid=2, fan=second_fan, body='hello'))
    store.drain(TENANT, ACCOUNT)
    second = queue_decision(engine, TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, second_fan, 2))
    transport.allowed_chats = frozenset({str(FAN), str(second_fan)})
    assert authority.dispatch(TENANT, ACCOUNT, first, transport) == 'unknown'
    assert authority.dispatch(TENANT, ACCOUNT, second, transport) == 'queued'
    assert status(database, second).reason == 'account_throttled'
    assert len(provider.posts) == 1, 'the cooled account sends nothing else'
    store.close()


def test_late_positive_evidence_settles_a_429_attempt_without_resending(database, monkeypatch):
    """A 429 is not delivery evidence either way; only independently verified history settles it."""
    import native_pilot.recovery as recovery
    from onlyfans_runtime import send_recovery
    provider = Provider((429, '{}', {'Retry-After': '600', 'x-ofapi-trace-id': 'tr-429'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    # a complete history that does NOT contain the message never settles it (absence is not proof of non-delivery)
    from onlyfans_runtime.store import Refused
    with pytest.raises(Refused):
        send_recovery.reconcile(authority, TENANT, ACCOUNT, FAN, intent, history=[], complete=True,
                                provider_message_id=9001, command_key='d' * 64, actor='op-session:' + 'a' * 64,
                                reason='Operator looked; nothing found')
    assert status(database, intent).status == 'unknown' and status(database, intent).reservation_held
    monkeypatch.setattr(recovery, '_matches', lambda raw, snapshot: SimpleNamespace(message_id=raw['id'], created_at=raw['createdAt']))
    monkeypatch.setattr(recovery, 'proof_hash', lambda raw, snapshot: 'f' * 64)
    raw = {'id': 9001, 'createdAt': utcnow().isoformat() + 'Z'}
    result = send_recovery.reconcile(authority, TENANT, ACCOUNT, FAN, intent, history=[raw], complete=True,
                                     provider_message_id=9001, command_key='c' * 64, actor='op-session:' + 'a' * 64,
                                     reason='Operator found the exact message')
    assert result['outcome'] == 'accepted'
    row = attempt(database)
    assert row.outcome == 'accepted' and json.loads(row.evidence_json)['message_id'] == 9001
    assert len(provider.posts) == 1
    assert next_dispatch(database) >= utcnow() + timedelta(seconds=500), 'pacing is independent of how the send settles'
    store.close()
