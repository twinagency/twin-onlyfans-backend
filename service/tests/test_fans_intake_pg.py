"""Fans API intake against the real orchestration: engine offer -> synthetic send -> signed webhooks -> canonical commerce.

Everything runs against PostgreSQL with the real EngineService, TextAuthority, store and CommerceIntake.
Only the provider is a fixture; there is no network.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_media_pg import shelf, media_policy
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from conversation_engine.commerce import SyntheticCoverage, approve_simulated_offer
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import ContentOffer, Message, OfConversationState, Transaction
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineRequest, OfFansConnection, OfFansWebhook
from db.onlyfans_fans_intake_models import OfFansBackfill, OfFansEvidence, OfFansLink, OfFansProjection
from fans_api_adapter.backfill import FansBackfill
from fans_api_adapter.events import FansEventIntake
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.protocol import FansApiClient, Response
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.commerce import held
from onlyfans_runtime.store import Refused, utcnow

SECRET = b'fans-intake-secret'
PROVIDER = 'acct_fixture_offer'
CREATOR = '777'
MESSAGE = 9100


def iso(seconds=2):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def plain(seconds=2):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')


def post(intake, body, key, *, secret=SECRET, signature=None):
    raw = json.dumps(body, separators=(',', ':')).encode()
    return intake.accept(raw, signature or hmac.new(secret, raw, hashlib.sha256).hexdigest(), SECRET, key)


def unlock(message=MESSAGE, amount=10, fan=FAN, provider=PROVIDER, creator=CREATOR, chat=None):
    return {'event': 'messages.ppv.unlocked', 'account_id': provider, 'payload': {
        'id': 'n-' + str(message), 'message_id': str(message), 'chat_id': str(chat or fan),
        # In THIS event user_id is the creator; the fan is chat_id / payload.fan.
        'user_id': creator, 'amountGross': amount, 'amountNet': amount * 0.8, 'createdAt': iso(),
        'fan': {'id': str(fan)}}}


def tx(txid='tx-1', amount=10, fan=FAN, provider=PROVIDER, status='done', kind='message'):
    return {'event': 'transactions.new', 'account_id': provider, 'payload': {
        'id': txid, 'created_at': plain(), 'type': kind, 'amount': amount, 'currency': 'USD',
        'status': status, 'fan': {'id': str(fan)}}}


def chargeback(txid='tx-1', amount=10, fan=FAN, provider=PROVIDER):
    return {'event': 'chargebacks.new', 'account_id': provider, 'payload': {
        'id': 1, 'createdAt': iso(5), 'paymentType': 'message',
        'payment': {'id': txid, 'amount': amount, 'status': 'undo', 'createdAt': iso(), 'user': {'id': fan}}}}


def account_event(name, provider=PROVIDER, creator=CREATOR):
    return {'event': 'accounts.' + name, 'account_id': provider, 'payload': {
        'onlyfans_id': creator, 'onlyfans_username': 'creator', 'state': 'authenticated'}}


class World:
    pass


@pytest.fixture
def world(database, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    w = World()
    w.database = database
    w.store, w.authority = configured(database, mode='automated')
    item_id = shelf(database[1])
    media_policy(w.authority)
    FansWebhookAdapter(w.store).bind_simulated(TENANT, ACCOUNT, PROVIDER, CREATOR)
    w.engine = EngineService(w.store, SyntheticModel({}))
    w.engine.select_simulated(TENANT, ACCOUNT, FAN)
    coverage = SyntheticCoverage(ACCOUNT, FAN, item_id, (11, 12, 13), 'synthetic:complete-ownership',
                                 utcnow() + timedelta(minutes=5))
    grant = approve_simulated_offer(w.store, TENANT, ACCOUNT, FAN, item_id, coverage)
    w.engine.model = SyntheticModel({'show me the set': SyntheticProposal('x', action='offer', offer_grant_id=grant)})
    from test_onlyfans_foundation_pg import event as make_event
    w.store.record(TENANT, ACCOUNT, make_event(mid=1, fan=FAN, body='show me the set'))
    w.store.drain(TENANT, ACCOUNT)
    decision = w.engine.plan(TENANT, ACCOUNT, FAN, 1)
    w.intent = w.engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(message_id=MESSAGE)
    assert w.authority.dispatch(TENANT, ACCOUNT, w.intent, FansApiSyntheticTransport(w.store, TENANT, ACCOUNT, provider)) == 'accepted'
    w.intake = FansEventIntake(w.store, human_grace_seconds=0, mismatch_settle_seconds=0)
    yield w
    w.store.close()


def count(world, model, *where):
    with Session(world.database[1]) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def evidence(world, kind=None):
    with Session(world.database[1]) as s:
        query = select(OfFansEvidence).order_by(OfFansEvidence.created_at)
        if kind:
            query = query.where(OfFansEvidence.kind == kind)
        return [(r.kind, r.status, r.reason) for r in s.scalars(query)]


def message_state(world):
    with Session(world.database[1]) as s:
        return s.get(Message, (ACCOUNT, FAN, MESSAGE)).is_paid


def transactions(world):
    with Session(world.database[1]) as s:
        return [(t.status, t.provider_transaction_id, t.amount_cents) for t in s.scalars(select(Transaction))]


# --------------------------------------------------------------------------- authentication / dedupe

def test_forged_signature_stores_nothing_and_is_refused(world):
    raw = json.dumps(unlock(), separators=(',', ':')).encode()
    for signature in ('0' * 64, hmac.new(b'another-secret', raw, hashlib.sha256).hexdigest(), 'sha256=' + '0' * 64, None):
        with pytest.raises(Refused):
            world.intake.accept(raw, signature, SECRET, 'evt-forged')
    # a body altered after signing is also forged
    good = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    with pytest.raises(Refused):
        world.intake.accept(raw.replace(b'"10"', b'"11"').replace(b'amountGross":10', b'amountGross":99'), good, SECRET, 'evt-forged')
    assert count(world, OfFansWebhook) == 0 and count(world, OfFansEvidence) == 0 and transactions(world) == []


def test_duplicate_idempotency_key_is_one_effect_and_conflicting_replay_is_quarantined(world):
    u, t = unlock(), tx()
    assert post(world.intake, u, 'evt-u')['outcome'] == 'accepted'
    assert post(world.intake, t, 'evt-t')['projection'] == 'projected'
    assert post(world.intake, u, 'evt-u')['outcome'] == 'duplicate'
    assert post(world.intake, t, 'evt-t')['outcome'] == 'duplicate'
    assert transactions(world) == [('cleared', 'tx-1', 1000)]
    assert count(world, OfFansEvidence) == 2 and count(world, OfFansWebhook) == 2
    # same key, different bytes: kept as conflict evidence, never re-applied
    assert post(world.intake, unlock(amount=99), 'evt-u')['outcome'] == 'quarantined'
    assert transactions(world) == [('cleared', 'tx-1', 1000)]
    # the same payment announced under a new key is still one transaction
    assert post(world.intake, tx(), 'evt-t-again')['outcome'] == 'accepted'
    assert transactions(world) == [('cleared', 'tx-1', 1000)]


# --------------------------------------------------------------------------- purchase evidence

def test_unlock_then_transaction_grants_once_and_links_the_frozen_grant(world):
    assert post(world.intake, unlock(), 'evt-u')['projection'] == 'projected'
    assert message_state(world) is None  # an unlock alone is not a sale, and is not "not purchased" either
    assert evidence(world, 'unlock') == [('unlock', 'pending', 'awaiting_transaction')]
    assert transactions(world) == []
    post(world.intake, tx(), 'evt-t')
    assert message_state(world) is True
    assert transactions(world) == [('cleared', 'tx-1', 1000)]
    assert [r[1] for r in evidence(world)] == ['applied', 'applied']
    with Session(world.database[1]) as s:
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'unlock'))
        assert row.intent_id == world.intent and row.grant_id is not None
        offer = s.scalar(select(ContentOffer).where(ContentOffer.account_id == ACCOUNT))
        assert offer.status == 'delivered'
        assert s.scalar(select(OfEngineRequest.next_step)) == 'verify_access'


def test_reordered_transaction_before_unlock_gives_the_same_result(world):
    post(world.intake, tx(), 'evt-t')
    assert evidence(world, 'transaction') == [('transaction', 'pending', 'awaiting_unlock')]
    assert message_state(world) is None and transactions(world) == []
    post(world.intake, unlock(), 'evt-u')
    assert message_state(world) is True
    assert transactions(world) == [('cleared', 'tx-1', 1000)]
    assert [r[1] for r in evidence(world)] == ['applied', 'applied']


def test_late_transaction_leaves_the_offer_open_not_unpaid_until_it_arrives(world):
    post(world.intake, unlock(), 'evt-u')
    world.intake.sweep(ACCOUNT)  # time passing without the transaction changes nothing
    with Session(world.database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, MESSAGE)).is_paid is None
        assert s.scalar(select(ContentOffer.status).where(ContentOffer.account_id == ACCOUNT)) == 'open'
    post(world.intake, tx(status='loading'), 'evt-late')  # up to 3 minutes late, still pending payout
    assert message_state(world) is True
    assert transactions(world) == [('pending', 'tx-1', 1000)]


def test_unlock_payload_user_id_is_the_creator_not_the_fan(world):
    body = unlock()
    body['payload']['user_id'] = str(FAN)  # a mapper that trusted user_id would credit the creator as the fan
    result = post(world.intake, body, 'evt-bad')
    assert result['projection'] == 'held' and evidence(world) == []
    body = unlock()
    body['payload']['chat_id'] = CREATOR  # the creator named as the fan
    assert post(world.intake, body, 'evt-bad2')['projection'] == 'held'
    assert evidence(world) == [] and transactions(world) == []


def test_amount_mismatch_is_held_for_review_and_never_granted(world):
    post(world.intake, unlock(amount=12), 'evt-u')  # offer was frozen at 10.00
    post(world.intake, tx(amount=12), 'evt-t')
    assert evidence(world, 'unlock') == [('unlock', 'held', 'amount_mismatch')]
    assert message_state(world) is None and transactions(world) == []
    with Session(world.database[1]) as s:
        assert held(s, ACCOUNT, FAN) is True  # the fan is not sold to while it is open
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'unlock'))
        evidence_id = row.evidence_id
    world.intake.resolve_held(ACCOUNT, evidence_id, actor='operator', reason='reviewed')
    assert transactions(world) == []  # dismissal is not a grant
    with Session(world.database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, MESSAGE)).is_paid is None


def test_transaction_amount_differing_from_the_unlock_is_held(world):
    post(world.intake, unlock(), 'evt-u')
    post(world.intake, tx(amount=25), 'evt-t')
    assert sorted(evidence(world)) == [('transaction', 'held', 'amount_mismatch'), ('unlock', 'held', 'amount_mismatch')]
    assert transactions(world) == [] and message_state(world) is None


def test_unlock_for_a_message_we_did_not_send_is_never_granted(world):
    post(world.intake, unlock(message=424242), 'evt-u')
    post(world.intake, tx(), 'evt-t')
    assert evidence(world, 'unlock') == [('unlock', 'pending', 'no_matching_send')]
    assert transactions(world) == [] and message_state(world) is None


# --------------------------------------------------------------------------- chargebacks

def test_chargeback_revokes_through_the_canonical_owner(world):
    post(world.intake, unlock(), 'evt-u')
    post(world.intake, tx(), 'evt-t')
    assert message_state(world) is True
    post(world.intake, chargeback(), 'evt-cb')
    assert message_state(world) is False
    assert transactions(world) == [('chargedback', 'tx-1', 1000)]
    assert evidence(world, 'chargeback') == [('chargeback', 'applied', None)]
    with Session(world.database[1]) as s:
        assert s.scalar(select(ContentOffer.status).where(ContentOffer.account_id == ACCOUNT)) == 'cancelled'
        assert s.scalar(select(OfEngineRequest.next_step)) == 'review_refund_or_chargeback'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention is True
    post(world.intake, chargeback(), 'evt-cb-2')  # a second announcement changes nothing
    assert transactions(world) == [('chargedback', 'tx-1', 1000)]


def test_chargeback_arriving_before_the_purchase_waits_then_revokes(world):
    post(world.intake, chargeback(), 'evt-cb')
    assert evidence(world, 'chargeback') == [('chargeback', 'pending', 'awaiting_purchase')]
    assert message_state(world) is None
    post(world.intake, tx(), 'evt-t')
    post(world.intake, unlock(), 'evt-u')
    assert message_state(world) is False
    assert transactions(world) == [('chargedback', 'tx-1', 1000)]
    assert evidence(world, 'chargeback') == [('chargeback', 'applied', None)]


def test_provider_undo_status_on_a_transaction_is_a_chargeback(world):
    post(world.intake, unlock(), 'evt-u')
    post(world.intake, tx(status='done'), 'evt-t')
    post(world.intake, tx(status='undo'), 'evt-t-undo')
    assert message_state(world) is False and transactions(world) == [('chargedback', 'tx-1', 1000)]


# --------------------------------------------------------------------------- connection

def test_reconnect_with_a_new_acct_id_keeps_the_creator_link_and_old_evidence_valid(world):
    post(world.intake, account_event('connected'), 'evt-first')  # link now exists
    old = post(world.intake, unlock(), 'evt-u-old')
    assert old['epoch'] == 1
    result = post(world.intake, account_event('reconnected', provider='acct_new_id'), 'evt-recon')
    assert result['epoch'] == 2 and result['projection'] == 'projected'
    with Session(world.database[1]) as s:
        link = s.get(OfFansLink, (ACCOUNT, CREATOR))
        assert (link.provider_account_id, link.reconnect_count, link.state) == ('acct_new_id', 1, 'connected')
        active = s.scalars(select(OfFansConnection).where(OfFansConnection.status == 'active')).all()
        assert [c.provider_account_id for c in active] == ['acct_new_id']
    # a late transaction still addressed to the replaced acct_ id is real evidence for the same creator
    late = post(world.intake, tx(provider=PROVIDER), 'evt-t-old-acct')
    assert late['epoch'] == 1 and message_state(world) is True
    # a late "disconnected" about the replaced acct_ id must not take the new connection down
    assert post(world.intake, account_event('disconnected'), 'evt-old-disc')['projection'] == 'ignored'
    with Session(world.database[1]) as s:
        assert s.get(OfFansLink, (ACCOUNT, CREATOR)).send_blocked is False
        assert s.scalar(select(OfFansConnection.status).where(OfFansConnection.provider_account_id == 'acct_new_id')) == 'active'
    # the new id works for new evidence
    assert post(world.intake, unlock(message=555, provider='acct_new_id'), 'evt-u-new')['epoch'] == 2


def test_unknown_creator_reconnect_cannot_create_a_binding(world):
    with pytest.raises(Refused):
        post(world.intake, account_event('reconnected', provider='acct_stranger', creator='999'), 'evt-x')
    assert count(world, OfFansConnection) == 1


def test_session_expired_is_informational_and_auth_failure_stops_sends(world):
    post(world.intake, account_event('session_expired'), 'evt-exp')
    with Session(world.database[1]) as s:
        link = s.get(OfFansLink, (ACCOUNT, CREATOR))
        assert (link.session_refreshes, link.send_blocked, link.state) == (1, False, 'connected')
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is True
        assert link.last_event_at is not None and link.last_event_type == 'accounts.session_expired'
    post(world.intake, account_event('authentication_failed'), 'evt-auth')
    with Session(world.database[1]) as s:
        link = s.get(OfFansLink, (ACCOUNT, CREATOR))
        assert (link.state, link.send_blocked) == ('auth_failed', True)
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is False
    # a reconnect does not silently re-enable; a person releases the block
    post(world.intake, account_event('connected'), 'evt-ok')
    with Session(world.database[1]) as s:
        assert s.get(OfFansLink, (ACCOUNT, CREATOR)).send_blocked is True
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is False
    world.intake.release_send_block(TENANT, ACCOUNT, CREATOR, actor='operator', reason='re-authenticated')
    with Session(world.database[1]) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is True


def test_disconnected_stops_sends_and_retires_that_acct_id(world):
    post(world.intake, account_event('disconnected'), 'evt-disc')
    with Session(world.database[1]) as s:
        link = s.get(OfFansLink, (ACCOUNT, CREATOR))
        assert (link.state, link.send_blocked) == ('disconnected', True)
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is False
        assert s.scalar(select(OfFansConnection.status)) == 'retired'


# --------------------------------------------------------------------------- takeover

def sent(message_id=5555, fan=FAN, **extra):
    payload = {'id': message_id, 'text': '<p>hey it is really me</p>', 'createdAt': iso(1), 'price': 0, 'media': [],
               'isFromQueue': False, 'queueId': 1, 'toUser': {'id': fan}}
    payload.update(extra)
    return {'event': 'messages.sent', 'account_id': PROVIDER, 'payload': payload}


def test_human_reply_is_takeover_evidence_but_our_own_send_is_not(world):
    post(world.intake, sent(message_id=MESSAGE), 'evt-own')
    assert evidence(world, 'human_sent') == [('human_sent', 'dismissed', 'own_send')]
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
    post(world.intake, sent(), 'evt-human')
    assert evidence(world, 'human_sent')[-1] == ('human_sent', 'applied', 'takeover_requested')
    with Session(world.database[1]) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert state.mode == 'human_controlled' and state.takeover_state != 'none'
    post(world.intake, sent(message_id=5556), 'evt-human-2')
    assert evidence(world, 'human_sent')[-1] == ('human_sent', 'applied', 'already_human')
    # a scheduled / mass send is not a reply in this chat
    assert post(world.intake, sent(message_id=5557, isFromQueue=True), 'evt-mass')['projection'] == 'ignored'


def test_human_reply_waits_while_our_own_send_is_unresolved(world):
    world.intake.grace = timedelta(seconds=3600)
    post(world.intake, sent(), 'evt-human')
    assert evidence(world, 'human_sent') == [('human_sent', 'pending', 'within_grace')]
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
    world.intake.grace = timedelta(0)
    world.intake.sweep(ACCOUNT)
    assert evidence(world, 'human_sent') == [('human_sent', 'applied', 'takeover_requested')]


# --------------------------------------------------------------------------- projection is repairable

def test_projection_failure_keeps_raw_evidence_and_is_redriven(world, monkeypatch):
    original = FansEventIntake._dispatch
    calls = {'n': 0}

    def flaky(self, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('simulated crash between raw store and projection')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FansEventIntake, '_dispatch', flaky)
    body = unlock()
    with pytest.raises(OSError):
        post(world.intake, body, 'evt-u')
    assert count(world, OfFansWebhook) == 1  # raw first
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansProjection.status)) == 'pending'
    assert world.intake.reproject_pending(ACCOUNT) == {'evt-u': 'projected'}
    assert post(world.intake, body, 'evt-u')['outcome'] == 'duplicate'
    assert evidence(world, 'unlock') == [('unlock', 'pending', 'awaiting_transaction')]


# --------------------------------------------------------------------------- backfill

class FakeProvider:
    """A deterministic stand-in for the account's REST surface; records the query strings it was given."""
    def __init__(self, tx_pages, history, chargebacks=()):
        self.tx_pages, self.history, self.chargebacks, self.requests = tx_pages, history, list(chargebacks), []

    def __call__(self, *, method, url, headers, body, timeout, follow_redirects):
        assert follow_redirects is False and method == 'GET'
        self.requests.append(url)
        base = 'https://app.onlyfansapi.com'
        if url == base + '/api/accounts':
            return Response(200, json.dumps([{'id': PROVIDER, 'onlyfans_id': CREATOR, 'is_authenticated': True}]).encode())
        path, _, query = url.removeprefix(base).partition('?')
        if path.endswith('/transactions'):
            params = dict(p.split('=', 1) for p in query.split('&'))
            index = int(params.get('marker', '0').removeprefix('m') or 0)
            rows = self.tx_pages[index]
            more = index + 1 < len(self.tx_pages)
            return Response(200, json.dumps({'data': {'list': rows, 'hasMore': more,
                'nextMarker': 'm%d' % (index + 1) if more else None}}).encode())
        if path.endswith('/chargebacks'):
            return Response(200, json.dumps({'data': {'list': self.chargebacks, 'marker': None}}).encode())
        if '/chats/' in path:
            return Response(200, json.dumps({'data': self.history, '_pagination': {'next_page': None}}).encode())
        return Response(404, b'{}')


def client_for(provider):
    c = FansApiClient(account=PROVIDER, creator_id=CREATOR, epoch=1, token='fixture-token', http=provider, timeout=2, enabled=True)
    assert c.identity().state == 'verified'
    return c


def tx_row(txid, amount, fan=FAN, status='done'):
    return {'id': txid, 'amount': amount, 'currency': 'USD', 'status': status, 'type': 'message',
            'createdAt': iso(), 'user': {'id': fan}}


def test_pause_gap_is_repaired_by_transactions_and_history_with_a_budget(world):
    # The endpoint was paused: neither the unlock nor the transaction webhook was ever delivered.
    post(world.intake, account_event('connected'), 'evt-last-seen')
    with Session(world.database[1]) as s, s.begin():
        s.get(OfFansLink, (ACCOUNT, CREATOR)).last_event_at = utcnow() - timedelta(hours=3)
    runner = FansBackfill(world.intake)
    start = runner.gap_start(ACCOUNT, now=utcnow())
    assert start is not None and utcnow() - start >= timedelta(hours=3)
    assert runner.gap_start(ACCOUNT, now=utcnow() - timedelta(hours=3)) is None  # no silence, no suspected gap
    history = [{'id': MESSAGE, 'text': 'the set', 'price': 10.0, 'isOpened': True, 'isFromQueue': False,
                'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), 'changedAt': iso(),
                'fromUser': {'id': CREATOR}},
               {'id': 77, 'text': 'hello?', 'price': 0, 'isOpened': False, 'createdAt': iso(1), 'fromUser': {'id': FAN},
                'media': []}]
    provider = FakeProvider([[], [tx_row('tx-gap', 10)]], history)
    client = client_for(provider)
    # one page is not enough: the run says so and keeps its cursor instead of claiming completeness
    first = runner.run(TENANT, ACCOUNT, client, reason='pause_gap', start_date=start, pages_budget=1,
                       history_chats=[FAN])
    assert first['status'] == 'budget_exhausted' and first['pages'] == 1
    assert message_state(world) is None and transactions(world) == []
    second = runner.run(TENANT, ACCOUNT, client, reason='pause_gap', pages_budget=6)
    assert second['status'] == 'complete' and second['run'] == first['run']
    assert second['pages'] <= 1 + 6
    assert message_state(world) is True
    assert transactions(world) == [('cleared', 'tx-gap', 1000)]
    assert [r[1] for r in evidence(world)] == ['applied', 'applied']
    # every transaction request carried the explicit startDate; markers are the provider's own
    tx_urls = [u for u in provider.requests if '/transactions' in u]
    assert tx_urls and all('startDate=' in u for u in tx_urls)
    assert any('marker=m1' in u for u in tx_urls)
    with Session(world.database[1]) as s:
        run = s.scalar(select(OfFansBackfill))
        assert run.status == 'complete' and run.rows_seen >= 2
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == 'in')) >= 1
    # running it again over the same window is idempotent
    again = runner.run(TENANT, ACCOUNT, client, reason='pause_gap', start_date=start, pages_budget=6, history_chats=[FAN])
    assert again['status'] == 'complete' and transactions(world) == [('cleared', 'tx-gap', 1000)]
    assert count(world, OfFansEvidence) == 2


def test_backfill_transaction_without_a_provable_unlock_stays_pending_not_unpaid(world):
    runner = FansBackfill(world.intake)
    provider = FakeProvider([[tx_row('tx-orphan', 10)]], [])
    result = runner.run(TENANT, ACCOUNT, client_for(provider), reason='missed_webhooks',
                        start_date=utcnow() - timedelta(days=1), pages_budget=4)
    assert result['status'] == 'complete'
    assert evidence(world) == [('transaction', 'pending', 'awaiting_unlock')]
    assert message_state(world) is None and transactions(world) == []


def test_backfill_replays_chargebacks_and_refuses_unusable_pages(world):
    post(world.intake, unlock(), 'evt-u')
    post(world.intake, tx(), 'evt-t')
    runner = FansBackfill(world.intake)
    row = {'id': 3, 'createdAt': iso(5), 'paymentType': 'message',
           'payment': {'id': 'tx-1', 'amount': 10, 'status': 'undo', 'createdAt': iso(), 'user': {'id': FAN}}}
    provider = FakeProvider([[]], [], chargebacks=[row])
    result = runner.run(TENANT, ACCOUNT, client_for(provider), reason='missed_webhooks',
                        start_date=utcnow() - timedelta(days=1), pages_budget=4)
    assert result['status'] == 'complete' and message_state(world) is False
    # a page the provider did not answer is a failed run, never "complete"
    class Down(FakeProvider):
        def __call__(self, **kw):
            if kw['url'].endswith('/api/accounts'):
                return super().__call__(**kw)
            return Response(503, b'{}')
    failed = runner.run(TENANT, ACCOUNT, client_for(Down([[]], [])), reason='operator',
                        start_date=utcnow() - timedelta(days=1), pages_budget=2)
    assert failed['status'] == 'failed'
    with pytest.raises(Refused):
        runner.run(TENANT, ACCOUNT, client_for(provider), reason='operator', start_date=utcnow(), pages_budget=0)


def test_schema_head_is_the_intake_revision(database):
    from db.schema import HEAD, check_schema
    assert HEAD == '0120_fans_intake'
    with database[1].connect() as c:
        check_schema(c)
