"""W4 trace-isolation red test for W3FA-03 (inv. 8): a webhook for account B must never resolve to account A.

Two Fastt accounts; the SAME provider acct_ id and creator id (creator moved from A to B).
A keeps only a retired connection with a higher per-account epoch; B holds the active one.
`FansEventIntake._resolve` looks up by provider_account_id ordered by epoch, no account filter
(fans_api_adapter/events.py:215-227), so A's retired row outranks B's active row.
Synthetic ids only.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook
from fans_api_adapter.events import FansEventIntake
from onlyfans_runtime.store import Refused, utcnow
from test_onlyfans_foundation_pg import database, seed  # noqa: F401

PROVIDER = 'acct_w4_shared'
CREATOR = 'creator_w4_shared'
SECRET = b'w4-isolation-secret'


def two_accounts(database):
    url, engine = database
    store_a = seed(url, engine, account='acc-w4-a', tenant='tenant-w4')
    store_b = seed(url, engine, account='acc-w4-b', tenant='tenant-w4')
    now = utcnow()
    with Session(engine) as s, s.begin():
        s.add(OfFansConnection(account_id='acc-w4-a', epoch=5, binding_id='binding-acc-w4-a', provider_account_id=PROVIDER,
                               provider_creator_id=CREATOR, status='retired', created_at=now))
        s.add(OfFansConnection(account_id='acc-w4-b', epoch=1, binding_id='binding-acc-w4-b', provider_account_id=PROVIDER,
                               provider_creator_id=CREATOR, status='active', created_at=now))
    return engine, store_a, store_b


def test_resolve_never_returns_another_accounts_connection_for_a_shared_provider_id(database):
    engine, store_a, store_b = two_accounts(database)
    try:
        body = {'event': 'messages.received', 'account_id': PROVIDER, 'payload': {}}
        with Session(engine) as s:
            try:
                row = FansEventIntake(store_b)._resolve(s, body, 'messages.received', utcnow())
            except Refused:
                row = None  # refusing an ambiguous provider id is also acceptable
        # Rule (inv. 8): never another account's connection. B's active row, None or a refusal all pass.
        assert row is None or row.account_id == 'acc-w4-b', (row.account_id, row.epoch, row.status)
    finally:
        store_a.close()
        store_b.close()


def test_unscoped_intake_never_stores_account_bs_webhook_under_account_a(database):
    engine, store_a, store_b = two_accounts(database)
    try:
        stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        raw = json.dumps({'event': 'messages.received', 'account_id': PROVIDER, 'payload': {
            'id': 9001, 'fromUser': {'id': 101}, 'text': 'hi', 'createdAt': stamp, 'isSentByMe': False,
            'media': [], 'price': 0}}, separators=(',', ':')).encode()
        signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        try:
            FansEventIntake(store_b).accept(raw, signature, SECRET, 'w4-event-9001')
        except Refused:
            pass
        with Session(engine) as s:
            under_a = s.scalar(select(func.count()).select_from(OfFansWebhook).where(OfFansWebhook.account_id == 'acc-w4-a'))
        assert under_a == 0, 'account B provider event stored under account A'
    finally:
        store_a.close()
        store_b.close()


# ---- scoped paths (production always passes an expected account): G01-06 / FLG-02 ----------------------------

def _signed(provider, mid, event='messages.received', payload=None):
    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    raw = json.dumps({'event': event, 'account_id': provider, 'payload': payload if payload is not None else {
        'id': mid, 'fromUser': {'id': 101}, 'text': 'hi', 'createdAt': stamp, 'isSentByMe': False,
        'media': [], 'price': 0}}, separators=(',', ':')).encode()
    return raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest()


def _stored(engine, account):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(OfFansWebhook).where(OfFansWebhook.account_id == account))


def test_scoped_b_resolves_to_bs_active_row_and_is_stored_under_b(database):
    engine, store_a, store_b = two_accounts(database)
    try:
        raw, sig = _signed(PROVIDER, 9101)
        result = FansEventIntake(store_b).accept(raw, sig, SECRET, 'w4-scoped-b',
                                                 expected_tenant='tenant-w4', expected_account='acc-w4-b')
        assert (result['account'], result['epoch']) == ('acc-w4-b', 1)
        assert _stored(engine, 'acc-w4-b') == 1 and _stored(engine, 'acc-w4-a') == 0
    finally:
        store_a.close()
        store_b.close()


def test_scoped_a_refuses_a_provider_id_bound_only_to_another_account(database):
    engine, store_a, store_b = two_accounts(database)
    try:
        with Session(engine) as s, s.begin():
            s.add(OfFansConnection(account_id='acc-w4-b', epoch=2, binding_id='binding-acc-w4-b',
                  provider_account_id='acct_w4_b_only', provider_creator_id=CREATOR, status='retired', created_at=utcnow()))
        with Session(engine) as s:  # _resolve itself refuses, not only accept()'s outer scope check
            body = {'event': 'messages.received', 'account_id': 'acct_w4_b_only', 'payload': {}}
            try:
                FansEventIntake(store_a)._resolve(s, body, 'messages.received', utcnow(), expected_account='acc-w4-a')
                raise AssertionError('scoped _resolve returned a row bound only to B')
            except Refused as exc:
                assert str(exc) == 'webhook account scope mismatch', exc
        raw, sig = _signed('acct_w4_b_only', 9102)
        try:
            FansEventIntake(store_a).accept(raw, sig, SECRET, 'w4-scoped-a',
                                            expected_tenant='tenant-w4', expected_account='acc-w4-a')
            raise AssertionError('scoped A accepted a provider id bound only to B')
        except Refused as exc:
            assert str(exc) == 'webhook account scope mismatch', exc
        assert _stored(engine, 'acc-w4-a') == 0 and _stored(engine, 'acc-w4-b') == 0
    finally:
        store_a.close()
        store_b.close()


def test_scoped_account_with_no_rows_for_the_provider_id_is_unbound_and_never_rebinds_another_account(database):
    engine, store_a, store_b = two_accounts(database)
    try:
        with Session(engine) as s:
            body = {'event': 'messages.received', 'account_id': 'acct_w4_unknown', 'payload': {}}
            assert FansEventIntake(store_b)._resolve(s, body, 'messages.received', utcnow(),
                                                     expected_account='acc-w4-b') is None
        with Session(engine) as s, s.begin():  # a creator only A has ever held
            s.add(OfFansConnection(account_id='acc-w4-a', epoch=6, binding_id='binding-acc-w4-a',
                  provider_account_id='acct_w4_a_old', provider_creator_id='creator_w4_a_only', status='retired',
                  created_at=utcnow()))
        before = _rows(engine)
        with Session(engine) as s, s.begin():
            body = {'event': 'accounts.reconnected', 'account_id': 'acct_w4_new', 'payload': {'onlyfans_id': 'creator_w4_a_only'}}
            assert FansEventIntake(store_b)._resolve(s, body, 'accounts.reconnected', utcnow(),
                                                     expected_account='acc-w4-b') is None
        assert _rows(engine) == before, 'scoped reconnect touched another account'
    finally:
        store_a.close()
        store_b.close()


def _rows(engine):
    with Session(engine) as s:
        return sorted((c.account_id, c.epoch, c.provider_account_id, c.status) for c in s.scalars(select(OfFansConnection)))


def test_scoped_late_event_on_a_retired_epoch_of_the_same_account_still_routes(database):
    engine, store_a, store_b = two_accounts(database)
    try:
        with Session(engine) as s, s.begin():
            s.add(OfFansConnection(account_id='acc-w4-b', epoch=2, binding_id='binding-acc-w4-b',
                  provider_account_id='acct_w4_b_old', provider_creator_id=CREATOR, status='retired', created_at=utcnow()))
        with Session(engine) as s:
            body = {'event': 'messages.received', 'account_id': 'acct_w4_b_old', 'payload': {}}
            row = FansEventIntake(store_b)._resolve(s, body, 'messages.received', utcnow(), expected_account='acc-w4-b')
            assert (row.account_id, row.epoch, row.status) == ('acc-w4-b', 2, 'retired')
    finally:
        store_a.close()
        store_b.close()
