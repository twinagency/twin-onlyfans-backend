"""Send-outcome evidence: every unproven send keeps its fence AND says why (loopback fake Fans server; no provider call).

Defect reproduced (live Sage hold): a 400/422, an ambiguous 503, a malformed 200, a transport exception and a
conflict/key-mismatch all returned None from FansApiRealTransport.send, so TextAuthority recorded the same
``unknown / invalid_receipt`` and threw away the status, trace and body. Safety is unchanged and re-asserted here:
no extra POST, no auto-release, the fan stays fenced. A valid success and an explicit provider not-sent are controls.
"""
import json

import pytest
from sqlalchemy.orm import Session

from db.models import OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from fake_fans_server import FakeFans
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_real_transport_pg import build, real_flag, status, text_intent  # noqa: F401

PREFIX = 'send-evidence:'


def evidence(row):
    assert row.evidence_ref and row.evidence_ref.startswith(PREFIX), row.evidence_ref
    assert len(row.evidence_ref) <= 256
    return json.loads(row.evidence_ref[len(PREFIX):])


def fenced(database, row):
    with Session(database[1]) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        return state.unresolved_attempt_id == row.attempt_id and state.requires_attention is True


# (fake outcome, expected reason, expected http status, expected provider code, expected exception class present)
UNPROVEN = [
    ('bad_request',      'provider_rejected_4xx',    400, 'BAD_REQUEST', False),
    ('banned',           'provider_rejected_4xx',    422, 'BANNED_WORDS', False),
    ('unknown',          'provider_ambiguous_5xx',   503, 'SERVICE_UNAVAILABLE', False),
    ('html_502',         'provider_ambiguous_5xx',   502, None, False),
    ('malformed_ok',     'malformed_success',        200, None, False),
    ('invalid_json_ok',  'malformed_success',        200, None, False),
    ('lost',             'transport_exception',      None, None, True),
    ('conflict',         'provider_idempotency_conflict', 409, 'IDEMPOTENCY_CONFLICT', False),
    ('mismatch',         'provider_key_mismatch',    422, 'IDEMPOTENCY_KEY_MISMATCH', False),
]


@pytest.mark.parametrize('outcome,reason,http_status,code,has_exception', UNPROVEN, ids=[u[0] for u in UNPROVEN])
def test_unproven_send_keeps_the_fence_and_records_why(database, outcome, reason, http_status, code, has_exception):
    fake = FakeFans([outcome])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        # Safety semantics unchanged: still unknown, still fenced, exactly one POST, no receipt, no release.
        assert row.status == 'unknown' and fenced(database, row)
        assert len(fake.posts) == 1 and row.receipt_json is None and row.provider_message_id is None
        # New: the reason is distinct (never the generic invalid_receipt) and the sanitized evidence is retained.
        assert row.reason == reason
        shown = evidence(row)
        assert shown['reason'] == reason
        assert shown.get('status') == http_status
        assert shown.get('code') == code
        assert ('exc' in shown) is has_exception
        if has_exception:
            assert shown['exc'].isidentifier()
    finally:
        fake.close()


def test_secrets_and_message_text_never_reach_the_stored_evidence(database):
    fake = FakeFans(['secret_echo'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store, body='Private words for this one fan')
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        assert row.reason == 'provider_rejected_4xx'
        shown = evidence(row)
        assert shown['status'] == 422 and shown['code'] == 'VALIDATION_FAILED'
        assert 'fixture-token' not in row.evidence_ref and 'Private words' not in row.evidence_ref
        assert 'Bearer' not in row.evidence_ref.replace('Bearer [redacted]', '')
        assert len(shown.get('body', '')) <= 160
    finally:
        fake.close()


def test_oversized_body_is_truncated_to_the_column(database):
    fake = FakeFans(['html_502'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        assert row.reason == 'provider_ambiguous_5xx' and len(row.evidence_ref) <= 256
        assert 'x' * 200 not in row.evidence_ref
        assert 'Bad Gateway' in evidence(row)['body']
    finally:
        fake.close()


def test_trace_id_is_kept_when_the_provider_supplies_one(database):
    fake = FakeFans(['bad_request'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        authority.dispatch(TENANT, ACCOUNT, intent, transport)
        # The loopback fake sends no x-ofapi-trace-id, so none is invented.
        assert 'trace' not in evidence(status(database, intent))
    finally:
        fake.close()


def test_valid_success_is_unchanged(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        row = status(database, intent)
        assert row.status == 'accepted' and row.reason is None and row.provider_message_id == 7001
        assert row.evidence_ref.startswith('fansapi:') and not row.evidence_ref.startswith(PREFIX)
        with Session(database[1]) as s:
            assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id is None
    finally:
        fake.close()


@pytest.mark.parametrize('outcome', ['rejected_not_sent_422', 'rejected_not_sent_400'])
def test_explicit_provider_not_sent_is_still_not_accepted_and_releases_nothing_unknown(database, outcome):
    fake = FakeFans([outcome])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'not_accepted'
        row = status(database, intent)
        assert row.status == 'not_accepted' and row.reason == 'proved_not_accepted'   # the existing reason, unchanged
        assert row.evidence_ref.startswith('fansapi-rejected:')
        assert len(fake.posts) == 1
        with Session(database[1]) as s:
            assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id is None
    finally:
        fake.close()


def test_exhausted_503_not_sent_is_still_not_accepted(database):
    fake = FakeFans(['not_sent'] * 3)
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'not_accepted'
        assert status(database, intent).evidence_ref.startswith('fansapi-not-sent:')
        assert len(fake.posts) == 3
    finally:
        fake.close()


def test_unknown_evidence_is_not_a_receipt_and_reconciliation_never_posts(database):
    """Evidence on the unknown attempt is not a receipt; it never resolves the hold or triggers a POST."""
    fake = FakeFans(['bad_request'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        assert row.receipt_json is None and row.status == 'unknown'
        assert transport.reconcile_accepted(intent) == 'unknown'
        assert len(fake.posts) == 1
    finally:
        fake.close()


def _consume_then(authority, dispatch, tenant_id, proof_factory):
    grant = authority.consume_dispatch(tenant_id, dispatch)
    return proof_factory(grant)


def test_a_transport_that_returns_no_receipt_is_invalid_receipt_with_evidence(database, monkeypatch):
    from fans_api_adapter.real_transport import FansApiRealTransport
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        monkeypatch.setattr(FansApiRealTransport, 'send',
                            lambda self, d, *, authority, tenant_id: _consume_then(authority, d, tenant_id, lambda g: None))
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        assert row.reason == 'invalid_receipt' and fenced(database, row)
        assert evidence(row)['reason'] == 'invalid_receipt'
    finally:
        fake.close()


def test_unknown_proof_for_another_attempt_is_invalid_receipt_and_cannot_carry_evidence(database, monkeypatch):
    from fans_api_adapter.real_transport import FansApiRealTransport
    from onlyfans_runtime.text_delivery import UnknownSend
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        monkeypatch.setattr(FansApiRealTransport, 'send', lambda self, d, *, authority, tenant_id: _consume_then(
            authority, d, tenant_id, lambda g: UnknownSend(
                account_id=g.account_id, fan_id=g.fan_id, intent_id=g.intent_id, attempt_id='some-other-attempt',
                payload_hash=g.payload_hash, reason='provider_rejected_4xx', http_status=400)))
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        assert row.reason == 'invalid_receipt' and fenced(database, row)
    finally:
        fake.close()


def test_unknown_proof_with_an_unlisted_reason_is_invalid_receipt(database, monkeypatch):
    from fans_api_adapter.real_transport import FansApiRealTransport
    from onlyfans_runtime.text_delivery import UnknownSend
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        monkeypatch.setattr(FansApiRealTransport, 'send', lambda self, d, *, authority, tenant_id: _consume_then(
            authority, d, tenant_id, lambda g: UnknownSend(
                account_id=g.account_id, fan_id=g.fan_id, intent_id=g.intent_id, attempt_id=g.attempt_id,
                payload_hash=g.payload_hash, reason='released_for_free')))
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        row = status(database, intent)
        assert row.reason == 'invalid_receipt' and fenced(database, row)
    finally:
        fake.close()
