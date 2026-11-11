"""Part A: an unknown / ambiguous Plan C send keeps bounded, sanitized provider evidence on its attempt row.

Live finding (2026-11-11): two sends ended `unknown` with reason `invalid_receipt` and `of_engine_attempt.evidence_json`
was NULL, so nobody could tell WHY. Every transport result that is neither accepted nor proven not-sent reaches the
authority as "no receipt" (`invalid_receipt`); the provider response was thrown away. These tests pin what must now be
retained (HTTP status, provider error code, id presence, trace ids, a short redacted message, the failing proof field,
timestamps) and what must never be retained (tokens, the message body, fan text, raw provider bodies).

Real PostgreSQL, real FansApiRealTransport + FansApiClient + TextAuthority; the provider is a scripted httpx.MockTransport.
"""
from dataclasses import replace
from datetime import timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAttempt
from fake_fans_server import ACCT, CREATOR
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import Accepted, NotAccepted
from fans_api_adapter.real_transport import FansApiRealTransport
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_real_transport_pg import build, real_flag, status, text_intent  # noqa: F401

BODY = 'Hi there'
TOKEN = 'fixture-token'          # the credential build() gives the client
MAX_EVIDENCE_BYTES = 4096


def reply(code, raw, headers=None):
    if isinstance(raw, str):
        raw = raw.encode()
    return httpx.Response(code, headers={'content-length': str(len(raw)), **(headers or {})}, stream=httpx.ByteStream(raw))


class Provider:
    """Identity GET always works; each POST gets the next scripted reply (a tuple or an exception to raise)."""
    def __init__(self, *replies):
        self.replies, self.posts = list(replies), []
        self.mock = httpx.MockTransport(self.handle)

    def transport(self):
        return self.mock

    def handle(self, request):
        if request.method == 'GET' and request.url.path == '/api/accounts':
            return reply(200, json.dumps([{'id': ACCT, 'onlyfans_id': CREATOR, 'is_authenticated': True}]))
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            self.posts.append(request.headers['Idempotency-Key'])
            item = self.replies.pop(0)
            if isinstance(item, BaseException):
                raise item
            return reply(item[0], item[1], item[2] if len(item) > 2 else None)
        return reply(404, b'{}')

    def close(self):
        pass


def attempt(database):
    with Session(database[1]) as s:
        return s.scalar(select(OfEngineAttempt))


def kept(database):
    row = attempt(database)
    assert row is not None and row.evidence_json is not None, 'the attempt row must carry evidence'
    assert len(row.evidence_json.encode()) <= MAX_EVIDENCE_BYTES
    value = json.loads(row.evidence_json)
    assert value['v'] == 1
    return value, row.evidence_json


def dispatch_unknown(database, provider, body=BODY):
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store, body=body)
    outcome = authority.dispatch(TENANT, ACCOUNT, intent, transport)
    return store, authority, transport, intent, outcome


def forbid(raw_evidence, *needles):
    for needle in needles:
        assert needle not in raw_evidence, f'{needle!r} must never be retained'


# ------------------------------------------------------------------------------------------------ provider responses
CASES = [
    ('http503_unknown',
     (503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"unknown","message":"upstream hiccup"}', {'x-ofapi-trace-id': 'tr-503'}),
     dict(http_status=503, error_code='SERVICE_UNAVAILABLE', request_outcome='unknown', trace_id='tr-503',
          outcome='unknown', message='upstream hiccup', body_kind='json_object', message_id_present=False)),
    ('html_gateway',
     (502, '<html><body>bad gateway</body></html>', {}),
     dict(http_status=502, body_kind='unparseable', error_code=None, outcome='unknown')),
    ('idempotency_conflict',
     (409, '{"error":"IDEMPOTENCY_CONFLICT"}', {'x-ofapi-trace-id': 'tr-c', 'x-ofapi-idempotency-original-trace-id': 'tr-orig'}),
     dict(http_status=409, error_code='IDEMPOTENCY_CONFLICT', outcome='conflict', trace_id='tr-c', original_trace_id='tr-orig')),
    ('key_mismatch',
     (422, '{"error":"IDEMPOTENCY_KEY_MISMATCH"}', {}),
     dict(http_status=422, error_code='IDEMPOTENCY_KEY_MISMATCH', outcome='key_mismatch')),
    ('4xx_without_request_outcome',
     (422, '{"error":"BANNED_WORDS","message":"banned word"}', {}),
     dict(http_status=422, error_code='BANNED_WORDS', request_outcome=None, outcome='unknown', message='banned word')),
    ('ok_without_message_id',
     (200, '{"data":{"createdAt":"2026-11-07T00:00:00+00:00"}}', {}),
     dict(http_status=200, message_id_present=False, failed_field='data.id', outcome='unknown')),
    ('ok_without_timestamp',
     (200, '{"data":{"id":7001}}', {}),
     dict(http_status=200, message_id_present=True, failed_field='data.createdAt', created_at_state='absent')),
    ('ok_naive_timestamp',
     (200, '{"data":{"id":7001,"createdAt":"2026-11-07T00:00:00"}}', {}),
     dict(http_status=200, message_id_present=True, failed_field='data.createdAt', created_at_state='no_timezone')),
    ('ok_not_an_object',
     (200, '[]', {}),
     dict(http_status=200, body_kind='json_array', failed_field='data')),
    ('lost_response_timeout', httpx.ReadTimeout('slow upstream at 10.0.0.9'),
     dict(http_status=None, transport_error='ReadTimeout', outcome='unknown')),
    ('oversized_body',
     (200, b'x' * 1_000_001, {}),
     dict(http_status=None, transport_error='ValueError', outcome='unknown')),
]


@pytest.mark.parametrize('name,scripted,expected', CASES, ids=[c[0] for c in CASES])
def test_every_unknown_outcome_keeps_bounded_provider_evidence(database, name, scripted, expected):
    provider = Provider(scripted)
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown' and len(provider.posts) == 1, 'no retry, no second POST'
    row = status(database, intent)
    assert (row.status, row.reason) == ('unknown', 'invalid_receipt'), 'the hold reason is unchanged'
    value, raw = kept(database)
    assert value['kind'] == 'unknown_send'
    seen = value['provider']
    for key, want in expected.items():
        assert seen.get(key) == want, (key, seen)
    assert value['hold']['reason'] == 'invalid_receipt'
    assert value['hold']['proof'] == {'field': 'proof', 'detail': 'absent'}, 'the transport handed back no receipt at all'
    for stamp in ('held_at', 'dispatched_at', 'wire_started_at'):
        assert 'T' in value['hold'][stamp]
    assert seen['posts'] == 1
    forbid(raw, TOKEN, BODY, 'Authorization', 'Bearer')
    assert attempt(database).outcome == 'unknown'


def test_evidence_survives_the_hold_and_the_fan_stays_held(database):
    provider = Provider((503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"unknown"}', {'x-ofapi-trace-id': 'tr-keep'}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    first = kept(database)[1]
    # a later no-op hold (replay of the same unknown) neither erases nor rewrites the retained evidence
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
    assert kept(database)[1] == first
    assert len(provider.posts) == 1


# ------------------------------------------------------------------------------------------------ hostile bodies
def test_hostile_provider_message_is_redacted_truncated_and_never_echoes_secrets_or_the_text(database):
    hostile = ('Invalid text: ' + BODY + ' Bearer abcdefghijklmnopqrstuvwxyz0123456789 token=' + TOKEN
               + ' mail me at victim@example.com or https://evil.example/x?k=1 id 123456789012345 \x00\x1b[31m '
               + '‮evil‬ ' + 'A' * 100_000)
    provider = Provider((422, json.dumps({'error': 'BANNED_WORDS', 'message': hostile}), {'x-ofapi-trace-id': 'tr-h'}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown'
    value, raw = kept(database)
    message = value['provider']['message']
    assert isinstance(message, str) and 0 < len(message) <= 300
    forbid(raw, TOKEN, BODY, 'abcdefghijklmnopqrstuvwxyz', 'victim@example.com', 'evil.example', '123456789012345',
           '\x00', '\x1b', '‮', 'A' * 40)
    assert value['provider']['error_code'] == 'BANNED_WORDS'


def test_a_provider_message_that_only_echoes_the_outgoing_text_is_dropped(database):
    echo = 'Message rejected, content was: ' + BODY
    provider = Provider((422, json.dumps({'error': 'BANNED_WORDS', 'message': echo}), {}))
    _, _, _, intent, _ = dispatch_unknown(database, provider)
    value, raw = kept(database)
    forbid(raw, BODY)
    assert value['provider']['error_code'] == 'BANNED_WORDS'


@pytest.mark.parametrize('payload', [
    b'{"error":"' + b'E' * 5000 + b'"}',                                   # absurd error code
    b'{"error":{"code":"' + b'C' * 5000 + b'","message":"nested"}}',       # error as an object, oversize code
    b'{"error":"A","error":"B"}',                                          # duplicate keys
    b'[' * 20000 + b']' * 20000,                                           # recursion bomb
    b'{"error":123456789,"request_outcome":{"x":1},"message":["a"],"type":[1]}',   # wrong types
    b'\xff\xfe\x00garbage',                                                # not UTF-8
    b'',                                                                   # empty
    b'null',
    ('{"error":"' + 'x' * 70 + '","data":{"id":"' + '9' * 400 + '"}}').encode(),   # hostile id
])
def test_malformed_or_oversized_provider_bodies_are_bounded_and_never_raise(database, payload):
    provider = Provider((503, payload, {'x-ofapi-trace-id': 'T' * 300}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown'
    assert status(database, intent).reason == 'invalid_receipt'
    value, raw = kept(database)
    seen = value['provider']
    assert seen['http_status'] == 503 and seen.get('trace_id') is None, 'an oversize trace id is not kept'
    for field in ('error_code', 'error_type'):
        assert seen.get(field) is None or len(seen[field]) <= 64
    assert len(raw.encode()) <= MAX_EVIDENCE_BYTES
    forbid(raw, 'x' * 40, 'E' * 40, 'C' * 40, '9' * 30)


# ------------------------------------------------------------------------------------------------ the failing proof field
def tamper(monkeypatch, change):
    original = FansApiRealTransport.send

    def send(self, dispatch, *, authority, tenant_id):
        proof = original(self, dispatch, authority=authority, tenant_id=tenant_id)
        return change(proof)
    monkeypatch.setattr(FansApiRealTransport, 'send', send)


@pytest.mark.parametrize('name,change,field,detail', [
    ('payload', lambda p: replace(p, payload_hash='0' * 64), 'payload_hash', 'scope_mismatch'),
    ('attempt', lambda p: replace(p, attempt_id='another-attempt'), 'attempt_id', 'scope_mismatch'),
    ('fan', lambda p: replace(p, fan_id=p.fan_id + 1), 'fan_id', 'scope_mismatch'),
    ('evidence_ref_long', lambda p: replace(p, evidence_ref='x' * 300), 'evidence_ref', 'invalid'),
    ('evidence_ref_type', lambda p: replace(p, evidence_ref=5), 'evidence_ref', 'invalid'),
    ('stale_clock', lambda p: replace(p, occurred_at=(utcnow() - timedelta(minutes=30)).replace(tzinfo=timezone.utc).isoformat()),
     'occurred_at', 'predates_attempt'),
    ('future_clock', lambda p: replace(p, occurred_at=(utcnow() + timedelta(hours=2)).replace(tzinfo=timezone.utc).isoformat()),
     'occurred_at', 'future'),
    ('naive_clock', lambda p: replace(p, occurred_at='2026-11-07T00:00:00'), 'occurred_at', 'no_timezone'),
    ('garbled_clock', lambda p: replace(p, occurred_at='yesterday-ish'), 'occurred_at', 'invalid'),
    ('bad_message_id', lambda p: replace(p, message_id=-5), 'message_id', 'invalid'),
    ('wrong_type', lambda p: {'not': 'a receipt'}, 'proof', 'type:dict'),
])
def test_an_accepted_response_with_an_invalid_receipt_names_the_field_that_failed(database, monkeypatch, name, change, field, detail):
    tamper(monkeypatch, change)
    provider = Provider((200, '{"data":{"id":7001,"createdAt":"2026-11-07T00:00:00+00:00"}}', {'x-ofapi-trace-id': 'tr-ok'}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown' and len(provider.posts) == 1
    row = status(database, intent)
    assert (row.status, row.reason) == ('unknown', 'invalid_receipt')
    value, raw = kept(database)
    assert value['hold']['reason'] == 'invalid_receipt'
    assert value['hold']['proof'] == {'field': field, 'detail': detail}
    assert value['hold']['message_id_present'] is (name != 'wrong_type')
    if name not in ('wrong_type', 'bad_message_id'):
        assert value['hold']['message_id'] == 7001, 'the provider did answer with a message: its id is kept'
    forbid(raw, TOKEN, BODY, 'another-attempt', 'yesterday-ish', 'x' * 40)


def test_an_invalid_not_accepted_receipt_also_names_its_field(database, monkeypatch):
    tamper(monkeypatch, lambda p: NotAccepted(p.account_id, p.fan_id, p.intent_id, p.attempt_id, '1' * 64,
                                             utcnow().replace(tzinfo=timezone.utc).isoformat(), 'fansapi-not-sent:x'))
    provider = Provider((503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"not_sent"}', {}),
                        (503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"not_sent"}', {}),
                        (503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"not_sent"}', {}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown'
    value, raw = kept(database)
    assert value['hold']['proof'] == {'field': 'payload_hash', 'detail': 'scope_mismatch'}


# ------------------------------------------------------------------------------------------------ other unknown routes
def test_an_unexpected_exception_is_recorded_by_type_only(database, monkeypatch):
    def boom(self, dispatch, *, authority, tenant_id):
        raise RuntimeError('boom while talking to the provider with ' + TOKEN + ' and ' + BODY)
    monkeypatch.setattr(FansApiRealTransport, 'send', boom)
    store, authority, transport, intent, outcome = dispatch_unknown(database, Provider())
    assert outcome == 'unknown'
    assert status(database, intent).reason == 'ambiguous_transport'
    value, raw = kept(database)
    assert value['hold']['reason'] == 'ambiguous_transport'
    assert value['hold']['exception_type'] == 'RuntimeError'
    forbid(raw, TOKEN, BODY, 'boom while')


class Died(BaseException):
    """The process dies on the wire."""


def test_an_attempt_left_dispatching_and_expired_is_held_with_its_timestamps(database):
    provider = Provider(Died())
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    with pytest.raises(Died):
        authority.dispatch(TENANT, ACCOUNT, intent, transport)
    assert status(database, intent).status == 'dispatching'
    with database[1].begin() as c:
        c.execute(text("UPDATE of_text_intent SET deadline = clock_timestamp() - interval '1 second' WHERE status='dispatching'"))
    assert authority.expire_dispatches(TENANT, ACCOUNT, limit=10, synthetic_engine_only=True, allowed_fans=frozenset({FAN})) == 1
    assert status(database, intent).reason == 'dispatch_expired'
    value, raw = kept(database)
    assert value['hold']['reason'] == 'dispatch_expired'
    assert value['hold']['wire_started_at'] and value['hold']['dispatched_at'] and value['hold']['held_at']
    forbid(raw, TOKEN, BODY)


def test_accepted_sends_still_store_only_the_receipt_as_evidence(database):
    provider = Provider((200, '{"data":{"id":7001,"createdAt":"2026-11-07T00:00:00+00:00"}}', {'x-ofapi-trace-id': 'tr-fine'}))
    store, authority, engine, transport, _ = build(database, provider)
    intent = text_intent(engine, store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
    row = attempt(database)
    receipt = json.loads(row.evidence_json)
    assert row.outcome == 'accepted' and receipt['message_id'] == 7001 and 'kind' not in receipt


def test_evidence_write_failure_never_changes_the_outcome(database, monkeypatch):
    from onlyfans_runtime.text_delivery import TextAuthority

    def refuse(self, *a, **k):
        raise RuntimeError('evidence store down')
    monkeypatch.setattr(TextAuthority, 'retain_send_evidence', refuse)
    provider = Provider((503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"unknown"}', {}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown' and status(database, intent).reason == 'invalid_receipt'
    assert len(provider.posts) == 1


def test_positive_reconciliation_still_settles_the_attempt_and_audits_the_kept_evidence(database, monkeypatch):
    """The existing positive path is untouched: the unknown evidence is preserved in the audit row, then the receipt wins."""
    from types import SimpleNamespace
    import native_pilot.recovery as recovery
    from onlyfans_runtime import send_recovery
    from onlyfans_runtime.launch_operations_models import OfLaunchCommand
    provider = Provider((503, '{"error":"SERVICE_UNAVAILABLE","request_outcome":"unknown"}', {'x-ofapi-trace-id': 'tr-pos'}))
    store, authority, transport, intent, outcome = dispatch_unknown(database, provider)
    assert outcome == 'unknown'
    # the account id of this fixture is not an OnlyFans-form id, so stand in for the history matcher only
    monkeypatch.setattr(recovery, '_matches', lambda raw, snapshot: SimpleNamespace(message_id=raw['id'], created_at=raw['createdAt']))
    monkeypatch.setattr(recovery, 'proof_hash', lambda raw, snapshot: 'f' * 64)
    raw = {'id': 9001, 'createdAt': utcnow().isoformat() + 'Z'}
    result = send_recovery.reconcile(authority, TENANT, ACCOUNT, FAN, intent, history=[raw], complete=True,
                                     provider_message_id=9001, command_key='c' * 64, actor='op-session:' + 'a' * 64,
                                     reason='Operator found the exact message')
    assert result['outcome'] == 'accepted'
    row = attempt(database)
    assert row.outcome == 'accepted' and json.loads(row.evidence_json)['message_id'] == 9001, 'the receipt replaces unknown evidence'
    with Session(database[1]) as s:
        before = json.loads(s.scalar(select(OfLaunchCommand)).before_json)
        assert before['sendEvidence']['provider']['http_status'] == 503 and before['sendEvidence']['hold']['reason'] == 'invalid_receipt'
    assert len(provider.posts) == 1
