"""Real-path worker and journey rules, offline.

Provider = httpx.MockTransport (no network). Model = in-process fixture. Everything else is the real code on
PostgreSQL: FansEventIntake, EngineService, TextAuthority, FansApiRealTransport, CommerceIntake, operator cases.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from approval_helpers import queue_decision
from test_onlyfans_media_pg import shelf, media_policy
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from conversation_engine import approval
from conversation_engine.commerce import RealCatalogueCoverage, approve_catalogue_offer, verified_terms
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from conversation_engine.journey import (JourneyConfig, JourneyService, configure_journey, enforce_journey,
                                         journey_config, signals, verified_spend)
from conversation_engine.real_worker import RealConversationWorker
from conversation_engine.worker import SyntheticConversationWorker
from db.models import CatalogItem, Message, OfConversationState, VaultItem
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import (OfEngineAccessEvidence, OfEngineDecision, OfEngineOfferGrant, OfEngineOwner,
                                       OfEngineRequest, OfFansWebhook)
from db.onlyfans_fans_intake_models import OfFansBackfill, OfFansEvidence, OfFansProjection
from fans_api_adapter.backfill import FansBackfill
from fans_api_adapter.events import FansEventIntake
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.protocol import FansApiClient, Response
from fans_api_adapter.real_transport import FansApiRealTransport, RealFansConfig, build_client
from onlyfans_runtime.store import Refused, utcnow

SECRET = b'real-worker-secret'
PROVIDER = 'acct_real_worker'
CREATOR = '777'
FAN2 = 102


def iso(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def inbound(mid, text, fan=FAN, seconds=0):
    return {'event': 'messages.received', 'account_id': PROVIDER, 'payload': {
        'id': mid, 'fromUser': {'id': fan}, 'text': text, 'createdAt': iso(seconds), 'isSentByMe': False,
        'media': [], 'price': 0}}


def subscription(fan=FAN, creator=CREATOR):
    # In THIS event payload.user_id is the CREATOR and payload.fan.id is the fan.
    return {'event': 'subscriptions.new', 'account_id': PROVIDER, 'payload': {
        'user_id': creator, 'fan': {'id': fan}, 'createdAt': iso()}}


def unlock(message, amount=10, fan=FAN, seconds=2):
    return {'event': 'messages.ppv.unlocked', 'account_id': PROVIDER, 'payload': {
        'id': 'n-' + str(message), 'message_id': str(message), 'chat_id': str(fan), 'user_id': CREATOR,
        'amountGross': amount, 'createdAt': iso(seconds), 'fan': {'id': str(fan)}}}


def tx(txid, amount=10, fan=FAN):
    return {'event': 'transactions.new', 'account_id': PROVIDER, 'payload': {
        'id': txid, 'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'type': 'message',
        'amount': amount, 'currency': 'USD', 'status': 'done', 'fan': {'id': str(fan)}}}


def answer(status, payload, headers=None):
    # A streamed body: FansHttpsTransport reads with iter_raw, which a pre-read Response(json=...) refuses.
    raw = json.dumps(payload).encode()
    return httpx.Response(status, headers={'content-type': 'application/json', 'content-length': str(len(raw)), **(headers or {})},
                          stream=httpx.ByteStream(raw))


class MockFans:
    """The provider as httpx.MockTransport: identity check and send. Records every POST."""
    def __init__(self):
        self.posts, self.stored, self.next_id, self.hooks = [], {}, 7001, []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        if request.method == 'GET' and request.url.path == '/api/accounts':
            return answer(200, [{'id': PROVIDER, 'onlyfans_id': CREATOR, 'is_authenticated': True}])
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            key = request.headers['Idempotency-Key']
            self.posts.append({'path': request.url.path, 'key': key, 'body': json.loads(request.content),
                               'auth': request.headers.get('Authorization')})
            if key in self.stored:
                return answer(200, self.stored[key], {'Idempotent-Replayed': 'true'})
            reply = {'data': {'id': self.next_id, 'createdAt': iso()}}
            self.next_id += 1
            self.stored[key] = reply
            for hook in list(self.hooks):
                hook(len(self.posts))
            return answer(200, reply)
        return answer(404, {})


class World:
    def rows(self, model, *where):
        with Session(self.database[1]) as s:
            return list(s.scalars(select(model).where(*where)))

    # The worker only STAGES a draft ('awaiting_approval'). These helpers add the explicit, named operator step
    # (approve the exact staged version, then deliver it) so the older journey tests keep exercising delivery.
    # `operator_on = False` turns it off; the gate itself is tested in test_engine_approval_pg.py.
    operator_on = True

    def operate(self, result):
        if not self.operator_on or result.get('outcome') != 'awaiting_approval':
            return result
        staged = result['approval']
        approval.approve(self.store, TENANT, ACCOUNT, staged['requestId'], staged['generation'],
                         expected_version=staged['version'], actor='test-operator')
        self.no_throttle()
        sent = self.worker.send_approved(TENANT, ACCOUNT, result['decisionId'])
        if 'intake' in result:
            sent['intake'] = result['intake']
        return sent

    def send(self, body, key=None):
        self.no_throttle()
        raw = json.dumps(body, separators=(',', ':')).encode()
        signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        return self.operate(self.worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=signature, secret=SECRET,
                                               event_key=key or 'evt-' + hashlib.sha256(raw).hexdigest()[:24]))

    def turn(self, tenant, account, fan, mid):
        self.no_throttle()
        return self.operate(self.worker.turn(tenant, account, fan, mid))

    def resume(self, tenant, account, fan, mid):
        self.no_throttle()
        return self.operate(self.worker.resume(tenant, account, fan, mid))

    def say(self, text, fan=FAN, seconds=0):
        self.mid += 1
        return self.send(inbound(self.mid, text, fan, seconds)), self.mid

    def reply(self, fn):
        """Model: fn(snapshot) -> proposal. Snapshots are kept; the model class stays the fixture type."""
        self.snaps = []
        def generate(snapshot):
            self.snaps.append(snapshot)
            return fn(snapshot)
        self.engine.model.generate = generate

    def say_plain(self, text='ok'):
        self.reply(lambda snap: SyntheticProposal(text))

    def owner(self, fan=FAN):
        with Session(self.database[1]) as s:
            row = s.get(OfEngineOwner, (ACCOUNT, fan))
            return (row.mode, row.sales_mode, row.sales_source_message_id) if row else None

    def cases(self, category=None):
        with Session(self.database[1]) as s:
            query = select(OfOperatorCase.category, OfOperatorCase.scope, OfOperatorCase.status)
            if category:
                query = query.where(OfOperatorCase.category == category)
            return s.execute(query).all()

    def backfilled(self, *fans):
        with Session(self.database[1]) as s, s.begin():
            now = utcnow()
            s.add(OfFansBackfill(account_id=ACCOUNT, run_id=str(uuid.uuid4()), reason='operator', start_date=now - timedelta(days=3650),
                status='complete', pages_used=1, pages_budget=1, marker=json.dumps({'phase': 'done', 'chats': [], 'done_chats': [str(f) for f in fans]}),
                rows_seen=0, rows_new=0, started_at=now, updated_at=now))

    def grant(self, item=None, fan=FAN, ref='vault:synced'):
        self.engine.select_simulated(TENANT, ACCOUNT, fan)
        return approve_catalogue_offer(self.store, TENANT, ACCOUNT, fan, item or self.item,
            RealCatalogueCoverage(ACCOUNT, fan, item or self.item, ref, utcnow() + timedelta(minutes=30)))

    def automate(self, fan):
        with Session(self.database[1]) as s, s.begin():
            s.get(OfConversationState, (ACCOUNT, fan)).mode = 'automated'

    def second_item(self):
        with Session(self.database[1]) as s, s.begin():
            for mid in (21, 22):
                s.add(VaultItem(account_id=ACCOUNT, media_id=mid, kind='image', created_at=utcnow(),
                                content_hash='synthetic-file-%d' % mid, content_hash_kind='vault-metadata-v1'))
            item = CatalogItem(account_id=ACCOUNT, kind='image', description_for_ai='A second photo set.',
                               media_ids='[21,22]', preview_media_ids='[21]', price_cents=1500, enabled=True)
            s.add(item)
            s.flush()
            return item.id


@pytest.fixture
def world(database, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    configure_journey(JourneyConfig())
    w = World()
    w.database = database
    w.store, w.authority = configured(database, mode='automated')
    w.item = shelf(database[1])
    from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles
    set_active_profiles(StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Fixture creator', limits=('no meetups',),
        offers_permitted=True, approved_item_ids=(str(w.item),))}))
    media_policy(w.authority)
    with Session(database[1]) as s, s.begin():
        for v in s.scalars(select(VaultItem)):
            v.content_hash_kind = 'vault-metadata-v1'
        revision = s.get(OfDeliveryPolicy, ACCOUNT).revision
    w.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=True, limit_units=200, throttle_seconds=1,
                          media_enabled=True, actor='worker-test', reason='real provider kind', transport_kind='fansapi')
    FansWebhookAdapter(w.store).bind_simulated(TENANT, ACCOUNT, PROVIDER, CREATOR)
    w.fans = MockFans()
    client = build_client(RealFansConfig('fixture-token', PROVIDER, 'https://api.fansapi.com', 5), creator_id=CREATOR,
                          epoch=1, transport=w.fans.transport)
    w.transport = FansApiRealTransport(w.store, TENANT, ACCOUNT, client, allowed_chats=frozenset({str(FAN), str(FAN2)}),
                                       sleep=lambda s: None, enabled=True)
    w.engine = EngineService(w.store, SyntheticModel({}))
    w.intake = FansEventIntake(w.store, human_grace_seconds=0, mismatch_settle_seconds=0)
    w.sleeps = []
    w.worker = RealConversationWorker(w.engine, w.intake, w.transport, sleep=lambda sec: (w.sleeps.append(sec), w.no_throttle()))
    w.mid = 100
    w.snaps = []

    def no_throttle(count):      # the one-second pacing gap is not what these tests are about
        with Session(database[1]) as s, s.begin():
            s.get(OfDeliveryPolicy, ACCOUNT).next_dispatch_at = None
    w.fans.hooks.append(no_throttle)
    w.no_throttle = lambda: no_throttle(0)
    yield w
    configure_journey(JourneyConfig())
    set_active_profiles(None)
    w.store.close()


def posts_text(w):
    return [p['body']['text'] for p in w.fans.posts]


# ======================================================================== the real path

def test_webhook_to_send_chain_end_to_end(world):
    world.say_plain('Hey you, glad you wrote')
    result, mid = world.say('hello there')
    assert result['intake'] == 'accepted' and result['outcome'] == 'accepted'
    assert len(world.fans.posts) == 1
    post = world.fans.posts[0]
    assert post['path'] == f'/api/{PROVIDER}/chats/{FAN}/messages' and post['auth'] == 'Bearer fixture-token'
    assert post['body']['text'] == 'Hey you, glad you wrote' and post['body']['blockBannedWords'] == 'strict_ban'
    assert 'price' not in post['body'] and 'mediaFiles' not in post['body']
    intent = world.rows(OfTextIntent)[0]
    assert intent.status == 'accepted' and intent.provider_message_id == 7001 and intent.projected_at is not None
    decision = world.rows(OfEngineDecision)[0]
    assert decision.status == 'completed'
    with Session(world.database[1]) as s:
        out = s.get(Message, (ACCOUNT, FAN, 7001))
        assert out.direction == 'out' and out.body == 'Hey you, glad you wrote'
    assert len(world.rows(OfFansWebhook)) == 1


def test_duplicate_webhook_is_one_send(world):
    world.say_plain('One reply only')
    body = inbound(500, 'ping')
    first = world.send(body, key='evt-dup')
    again = world.send(body, key='evt-dup')
    assert first['outcome'] == 'accepted'
    assert again['intake'] == 'duplicate' and again['outcome'] == 'already_sent'   # the consumed approval is never revived
    assert len(world.fans.posts) == 1
    assert len(world.rows(OfEngineDecision)) == 1 and len(world.rows(OfTextIntent)) == 1


def test_offer_chain_sends_the_frozen_media_and_price(world):
    world.backfilled(FAN)
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    grant = world.grant()
    world.reply(lambda snap: SyntheticProposal('Here you go', action='offer', offer_grant_id=grant))
    result, mid = world.say('show me the set')
    assert result['outcome'] == 'accepted'
    body = world.fans.posts[0]['body']
    assert body['price'] == 10.0 and body['mediaFiles'] == ['11', '12', '13'] and body['previews'] == ['11']
    assert body['text'].startswith('Here you go') and 'USD 10.00' in body['text']
    assert world.rows(OfEngineOfferGrant)[0].used_intent_id is not None


def test_default_off_flag_policy_and_scope(world, monkeypatch):
    world.say_plain('never sent')
    monkeypatch.delenv('TWIN_ENGINE_FANSAPI_REAL')
    with pytest.raises(Refused, match='not enabled'):
        world.say('hello')
    assert world.rows(OfFansWebhook) == [] and world.fans.posts == []   # refused before any evidence or send
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    with Session(world.database[1]) as s, s.begin():
        s.get(OfDeliveryPolicy, ACCOUNT).enabled = False
    with pytest.raises(Refused, match='enabled fansapi'):
        world.say('hello')
    with Session(world.database[1]) as s, s.begin():
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        policy.enabled = True
        policy.transport_kind = 'synthetic'
    with pytest.raises(Refused, match='fansapi account policy'):
        world.say('hello')
    assert world.fans.posts == []
    with pytest.raises(Refused, match='scope mismatch'):
        world.worker.handle('other-tenant', ACCOUNT, raw_body=b'{}', signature='x', secret=SECRET, event_key='k')


def test_synthetic_worker_is_unchanged_and_refuses_the_real_transport(world):
    with pytest.raises(Refused, match='invalid synthetic worker dependencies'):
        SyntheticConversationWorker(world.engine, FansWebhookAdapter(world.store), world.transport)
    with pytest.raises(Refused, match='invalid real worker dependencies'):
        RealConversationWorker(world.engine, world.intake, object())


def test_quarantined_webhook_fences_unsent_work(world):
    world.say_plain('prepared but never sent')
    body = inbound(600, 'first bytes')
    raw = json.dumps(body, separators=(',', ':')).encode()
    world.intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, 'evt-q')
    world.store.drain(TENANT, ACCOUNT)
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    decision = world.engine.plan(TENANT, ACCOUNT, FAN, 600)
    intent = queue_decision(world.engine, TENANT, ACCOUNT, decision)      # approved, queued, never dispatched
    conflicting = inbound(600, 'DIFFERENT bytes, same key')
    result = world.send(conflicting, key='evt-q')
    assert result['outcome'] == 'quarantined' and len(result['fenced']) == 1
    assert world.fans.posts == []
    with Session(world.database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'cancelled'
        assert s.get(OfTextIntent, (ACCOUNT, intent)).reason == 'webhook_conflict'
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'cancelled'
        assert s.get(OfDeliveryPolicy, ACCOUNT).queued_count == 0
        assert s.scalar(select(OfEngineRequest.next_step)) == 'review_webhook_conflict'
    # the worker never sends for a conflicted source, even when asked again
    again = world.turn(TENANT, ACCOUNT, FAN, 600)
    assert again['outcome'] == 'refused' and 'conflicting webhook' in again['detail']
    assert world.fans.posts == []


# ======================================================================== re-check before every part

def test_pacing_gap_is_waited_for_between_parts_and_each_part_is_rechecked(world):
    world.reply(lambda snap: SyntheticProposal('one', parts=('two', 'three')))
    result, mid = world.say('talk to me')
    assert result['outcome'] == 'accepted' and posts_text(world) == ['one', 'two', 'three']
    assert len(world.sleeps) >= 2 and all(0 < s <= 2 for s in world.sleeps)
    assert {d.status for d in world.rows(OfEngineDecision)} == {'completed'}
    # a fan message arriving while the worker waits for the pacing gap cancels the waiting part
    world.reply(lambda snap: SyntheticProposal('four', parts=('five',)))
    state = {'n': 0}
    def sleeper(sec):
        state['n'] += 1
        raw = json.dumps(inbound(950, 'sorry, one more thing', seconds=6), separators=(',', ':')).encode()
        world.intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, 'evt-950')
        world.no_throttle()
    world.worker.sleep = sleeper
    again, mid2 = world.say('and another question', seconds=3)
    assert posts_text(world)[-1] == 'four' and 'five' not in posts_text(world)
    assert again['outcome'].startswith('superseded:') and state['n'] == 1
    intents = [i for i in world.rows(OfTextIntent) if i.body == 'five']
    assert len(intents) == 1 and intents[0].status == 'cancelled'


def test_recheck_before_the_first_part(world):
    world.say_plain('stale answer')
    raw = json.dumps(inbound(700, 'question one'), separators=(',', ':')).encode()
    world.intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, 'evt-700')
    world.store.drain(TENANT, ACCOUNT)
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    decision = world.engine.plan(TENANT, ACCOUNT, FAN, 700)
    # a newer message is accepted by the webhook but not yet applied to the transcript
    newer = json.dumps(inbound(701, 'actually, question two', seconds=1), separators=(',', ':')).encode()
    world.intake.accept(newer, hmac.new(SECRET, newer, hashlib.sha256).hexdigest(), SECRET, 'evt-701')
    result = world.worker._send(TENANT, ACCOUNT, decision)
    assert result['outcome'].startswith('superseded:') and world.fans.posts == []
    assert world.rows(OfEngineDecision)[0].status == 'cancelled'
    assert world.rows(OfEngineRequest)[0].next_step == 'review_changed_conversation'
    assert world.rows(OfTextIntent) == []


def test_recheck_before_every_remaining_part(world):
    world.reply(lambda snap: SyntheticProposal('part one', parts=('part two', 'part three')))
    # while part one is on the wire the fan writes again; the webhook is accepted but not yet drained
    def inject(count):
        if count == 1:
            raw = json.dumps(inbound(801, 'wait, one more thing', seconds=1), separators=(',', ':')).encode()
            world.intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, 'evt-801')
    world.fans.hooks.append(inject)
    result, mid = world.say('first question')
    assert posts_text(world) == ['part one']            # parts two and three were never submitted
    assert [p['outcome'] for p in result['parts']][0] == 'accepted'
    assert result['outcome'].startswith('superseded:')
    decisions = {d.part_index: d.status for d in world.rows(OfEngineDecision)}
    assert decisions == {0: 'completed', 1: 'cancelled', 2: 'cancelled'}
    # the newer message is a normal turn of its own
    world.say_plain('answering the new message')
    follow = world.turn(TENANT, ACCOUNT, FAN, 801)
    assert follow['outcome'] == 'accepted' and posts_text(world)[-1] == 'answering the new message'


def test_recheck_just_before_wire_cancels_a_queued_part(world):
    world.say_plain('would have been sent')
    raw = json.dumps(inbound(850, 'hello'), separators=(',', ':')).encode()
    world.intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, 'evt-850')
    world.store.drain(TENANT, ACCOUNT)
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    decision = world.engine.plan(TENANT, ACCOUNT, FAN, 850)
    calls = {'n': 0}
    real = world.worker.recheck
    def recheck(tenant, account, part):
        calls['n'] += 1
        if calls['n'] == 2:      # between queue and dispatch the fan writes again
            raw2 = json.dumps(inbound(851, 'hold on', seconds=1), separators=(',', ':')).encode()
            world.intake.accept(raw2, hmac.new(SECRET, raw2, hashlib.sha256).hexdigest(), SECRET, 'evt-851')
        return real(tenant, account, part)
    world.worker.recheck = recheck
    staged = world.worker._send(TENANT, ACCOUNT, decision)
    assert staged['outcome'] == 'awaiting_approval' and world.fans.posts == []
    result = world.operate(staged)
    assert result['outcome'].startswith('superseded:') and world.fans.posts == []
    intent = world.rows(OfTextIntent)[0]
    assert intent.status == 'cancelled' and world.rows(OfDeliveryPolicy)[0].queued_count == 0


# ======================================================================== backfill precondition

def _backfill_client(account=PROVIDER):
    def http(*, method, url, headers, body, timeout, follow_redirects):
        if url.endswith('/api/accounts'):
            return Response(200, json.dumps([{'id': account, 'onlyfans_id': CREATOR, 'is_authenticated': True}]).encode())
        if '/transactions' in url:
            return Response(200, json.dumps({'data': {'list': [], 'hasMore': False}}).encode())
        if '/chargebacks' in url:
            return Response(200, json.dumps({'data': {'list': []}}).encode())
        if '/messages' in url:
            return Response(200, json.dumps({'data': [], '_pagination': {'next_page': None}}).encode())
        return Response(404, b'{}')
    client = FansApiClient(account=account, creator_id=CREATOR, epoch=1, token='fixture-token', http=http, timeout=5,
                           enabled=True, base='https://api.fansapi.com')
    assert client.identity().state == 'verified'
    return client


def test_real_offers_wait_for_a_finished_backfill_of_that_fan(world):
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    grant = world.grant()
    with Session(world.database[1]) as s:
        runtime = world.store._runtime(s, TENANT, ACCOUNT)
        owner, row = s.get(OfEngineOwner, (ACCOUNT, FAN)), s.get(OfEngineOfferGrant, (ACCOUNT, grant))
        with pytest.raises(Refused, match='finished backfill'):
            verified_terms(s, runtime, owner, row)       # the central choke point: plan, prepare and dispatch all use it
    world.reply(lambda snap: SyntheticProposal('look', action='offer', offer_grant_id=grant))
    blocked, mid = world.say('show me the set')
    assert blocked['outcome'] == 'refused' and 'no offer is allowed' in blocked['detail']
    assert world.snaps[-1]['journey']['offers_allowed'] is False and world.snaps[-1]['journey']['backfill_complete'] is False
    assert world.fans.posts == []
    # plain replies are not gated
    world.say_plain('chatting is fine')
    ok, _ = world.say('how are you')
    assert ok['outcome'] == 'accepted'
    # an unfinished run (budget exhausted) does not open the gate either
    with Session(world.database[1]) as s, s.begin():
        now = utcnow()
        s.add(OfFansBackfill(account_id=ACCOUNT, run_id=str(uuid.uuid4()), reason='operator', start_date=now, status='budget_exhausted',
            pages_used=1, pages_budget=1, marker=json.dumps({'phase': 'hist', 'chats': [str(FAN)], 'done_chats': [], 'hist_next': None, 'tx_marker': None, 'cb_offset': 0}),
            rows_seen=0, rows_new=0, started_at=now, updated_at=now))
    # a real, finished run for THIS fan opens it
    summary = FansBackfill(world.intake).run(TENANT, ACCOUNT, _backfill_client(), reason='operator',
        start_date=utcnow() - timedelta(days=30), pages_budget=5, history_chats=[FAN])
    assert summary['status'] in {'complete', 'budget_exhausted'}, summary
    with Session(world.database[1]) as s:
        runs = list(s.scalars(select(OfFansBackfill).where(OfFansBackfill.status == 'complete')))
        assert runs and json.loads(runs[0].marker)['done_chats'] == [str(FAN)]
    world.reply(lambda snap: SyntheticProposal('look', action='offer', offer_grant_id=grant))
    resumed, _ = world.say('ok show me the set please', seconds=5)
    assert resumed['outcome'] == 'accepted' and world.fans.posts[-1]['body']['price'] == 10.0


def test_backfill_of_another_fan_does_not_open_the_gate(world):
    world.backfilled(FAN2)
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    grant = world.grant()
    with Session(world.database[1]) as s:
        runtime = world.store._runtime(s, TENANT, ACCOUNT)
        with pytest.raises(Refused, match='finished backfill'):
            verified_terms(s, runtime, s.get(OfEngineOwner, (ACCOUNT, FAN)), s.get(OfEngineOfferGrant, (ACCOUNT, grant)))


def test_auto_backfill_is_off_by_default_and_opt_in_runs_before_the_offer(world):
    assert journey_config().auto_backfill is False
    world.say_plain('hi')
    world.say('hello')
    assert world.rows(OfFansBackfill) == []              # nothing was fetched by default
    configure_journey(JourneyConfig(auto_backfill=True, backfill_pages=5))
    runner = FansBackfill(world.intake)
    worker = RealConversationWorker(world.engine, world.intake, world.transport, backfill=runner)
    world.worker = worker
    original = FansBackfill.run
    seen = {}
    def run(self, tenant, account, client, **kw):
        seen.update(kw)
        return original(self, tenant, account, _backfill_client(), **kw)
    FansBackfill.run = run
    try:
        result, mid = world.say('second message')
    finally:
        FansBackfill.run = original
    assert seen['history_chats'] == (FAN,) and result['outcome'] == 'accepted'
    assert [json.loads(r.marker)['done_chats'] for r in world.rows(OfFansBackfill)] == [[str(FAN)]]


# ======================================================================== journey (a): welcome (DEFERRED for Plan C)

def test_no_welcome_offer_is_ever_created_from_a_subscription(world):
    world.backfilled(FAN)
    world.send(subscription(), key='evt-sub-1')
    world.say_plain('hi, glad you are here')
    world.say('hi!')
    assert world.rows(OfEngineOfferGrant) == []        # the welcome offer is deferred: nothing is granted or sent unprompted
    assert 'welcome' not in world.snaps[-1].get('journey', {})


def test_subscription_for_another_creator_is_held(world):
    result = world.send(subscription(creator='999'), key='evt-bad-sub')
    assert result['outcome'] == 'projection_held'
    assert world.rows(OfFansProjection)[0].detail.startswith('invalid:')


# ======================================================================== journey (b): post-purchase

def _sell(world, item=None):
    """Sell the first item through the real chain and settle it with a verified unlock + transaction."""
    world.backfilled(FAN)
    grant = world.grant(item)
    world.reply(lambda snap: SyntheticProposal('Here you go', action='offer', offer_grant_id=grant))
    sent, mid = world.say('show me the set')
    assert sent['outcome'] == 'accepted'
    message_id = world.fans.next_id - 1
    return grant, message_id


def test_compliment_after_a_verified_purchase_gets_a_reply_but_no_new_card(world):
    _, offer_message = _sell(world)
    second = world.second_item()
    paid = world.send(unlock(offer_message), key='evt-unlock')       # message-specific unlock; finances stay unlinked
    assert paid['outcome'] == 'evidence' and paid['journey']['action'] == 'none'
    access = world.rows(OfEngineAccessEvidence)
    assert [(a.status, a.applied) for a in access] == [('confirmed', True)]
    world.send(tx('tx-first'), key='evt-tx')                         # a separate money record is never linked to it
    assert [(a.status, a.applied) for a in world.rows(OfEngineAccessEvidence)] == [('confirmed', True)]
    with Session(world.database[1]) as s:
        assert verified_spend(s, ACCOUNT, FAN) is None
    posts_before = len(world.fans.posts)
    offer2 = world.grant(second, ref='vault:synced')
    world.reply(lambda snap: SyntheticProposal('Thank you!! Want more?', action='offer', offer_grant_id=offer2))
    refused, mid = world.say('omg thank you, that was so hot', seconds=10)
    assert refused['outcome'] == 'refused' and 'no offer is allowed' in refused['detail']
    assert world.snaps[-1]['journey']['compliment_after_purchase'] is True
    world.reply(lambda snap: SyntheticProposal('Thank you, that really made my day', pitch=True))
    assert world.turn(TENANT, ACCOUNT, FAN, mid)['outcome'] == 'refused'   # a pitch on a compliment turn is refused too
    assert len(world.fans.posts) == posts_before
    world.reply(lambda snap: SyntheticProposal('Thank you, that really made my day. Tell me what you liked best', parts=('and what else is on your mind',)))
    reaction = world.turn(TENANT, ACCOUNT, FAN, mid)                        # no 160-character cap, parts allowed: only the card is barred
    assert reaction['outcome'] == 'accepted' and len(world.fans.posts) == posts_before + 2
    # a message that actually asks for something is not a compliment turn: the next card may go out
    world.reply(lambda snap: SyntheticProposal('Since you asked', action='offer', offer_grant_id=offer2))
    nxt, mid2 = world.say('what else do you have', seconds=20)
    assert 'compliment_after_purchase' not in world.snaps[-1]['journey'] and world.snaps[-1]['journey']['offers_allowed'] is True
    assert nxt['outcome'] == 'accepted' and world.fans.posts[-1]['body']['price'] == 15.0


def test_an_unlock_for_a_message_that_is_not_our_offer_is_not_a_purchase(world):
    _sell(world)
    world.send(unlock(999999), key='evt-foreign-unlock')
    world.send(tx('tx-foreign'), key='evt-foreign-tx')
    assert world.rows(OfEngineAccessEvidence) == []
    world.say_plain('hello again')
    world.say('hello again?', seconds=10)
    assert 'compliment_after_purchase' not in world.snaps[-1].get('journey', {})


# ======================================================================== journey (c): whale hold (DEFERRED for Plan C)

def _spend(world, cents, txid, status='applied', kind='unlock'):
    with Session(world.database[1]) as s, s.begin():
        now = utcnow()
        s.add(OfFansEvidence(account_id=ACCOUNT, evidence_id=str(uuid.uuid4()), dedupe_key=f'{kind}:{txid}', kind=kind,
            source='webhook', fan_id=FAN, provider_transaction_id=txid, amount_cents=cents, occurred_at=now, status=status,
            raw_sha256='0' * 64, created_at=now, updated_at=now))


def test_large_spend_never_holds_flags_or_silences_the_engine(world):
    world.backfilled(FAN)
    world.say_plain('hi love')
    _spend(world, 80_000, 'a1')
    _spend(world, 80_000, 'a2')
    with Session(world.database[1]) as s:
        assert verified_spend(s, ACCOUNT, FAN) is None         # an unlock is not cleared ledger spend
    held = world.send(tx('tx-trigger'), key='evt-trigger')
    assert held['journey'] == {'action': 'none'}               # whale hold/flag thresholds are deferred
    result, mid = world.say('hello', seconds=5)
    assert result['outcome'] == 'accepted' and world.owner()[0] == 'active'
    assert world.cases('vip_flag') == [] and world.cases('journey_whale_hold') == []


# ======================================================================== journey (d): handover rules

def test_identity_question_goes_to_a_human_until_an_approved_line_exists(world):
    world.say_plain('I am a real girl')
    calls = len(world.snaps)
    result, mid = world.say('wait are you a bot?')
    assert result['outcome'] == 'handed_to_human' and result['reason'] == 'identity'
    assert world.fans.posts == [] and len(world.snaps) == calls
    request = world.rows(OfEngineRequest)[0]
    assert request.status == 'handover_pending' and request.handover_target == 'human-queue'
    assert world.owner()[0] == 'human' and ('journey_identity', 'all', 'open') in world.cases()


def test_agency_question_also_goes_to_a_human(world):
    world.say_plain('x')
    result, mid = world.say('do you work for an agency?')
    assert result['outcome'] == 'handed_to_human' and world.fans.posts == []


def test_identity_question_is_answered_when_an_approved_line_is_configured(world):
    configure_journey(JourneyConfig(identity_line='I am a virtual assistant for the creator.'))
    world.say_plain('some made up answer')
    refused, mid = world.say('are you real?')                      # only the approved wording may answer
    assert refused['outcome'] == 'refused' and 'approved wording' in refused['detail']
    world.say_plain('I am a virtual assistant for the creator.')
    result = world.turn(TENANT, ACCOUNT, FAN, mid)
    assert result['outcome'] == 'accepted' and world.owner()[0] == 'active'
    assert world.snaps[-1]['journey']['approved_identity_line'] == 'I am a virtual assistant for the creator.'


def test_second_offplatform_ask_pauses_selling_and_flags_third_push_goes_to_a_human(world):
    world.backfilled(FAN)
    world.say_plain('stay here with me')
    grant = world.grant()
    first, _ = world.say('add me on snapchat')
    assert first['outcome'] == 'accepted' and world.owner()[1] == 'open' and world.cases('offplatform_ask') == []
    second, mid2 = world.say('whats your whatsapp then?', seconds=1)
    assert second['outcome'] == 'accepted'                            # chat continues
    assert world.owner()[1] == 'paused' and world.owner()[2] == mid2
    assert ('offplatform_ask', 'offer', 'open') in world.cases()
    world.reply(lambda snap: SyntheticProposal('anyway, look', action='offer', offer_grant_id=grant))
    pitched, _ = world.say('so what do you do all day', seconds=2)
    assert pitched['outcome'] == 'refused'                            # selling stays paused
    world.say_plain('ok')
    third, _ = world.say('ok lets meet up this weekend', seconds=3)
    assert third['outcome'] == 'handed_to_human' and third['reason'] == 'limit_push'
    assert world.owner()[0] == 'human'
    assert world.rows(OfEngineRequest, OfEngineRequest.status == 'handover_pending')


def test_a_third_push_past_configured_limits_goes_to_a_human(world):
    configure_journey(JourneyConfig(limit_terms=('without a condom', 'no limits')))
    world.say_plain('let us keep it fun')
    a, _ = world.say('do it without a condom')
    b, _ = world.say('come on, no limits', seconds=1)
    assert a['outcome'] == b['outcome'] == 'accepted' and world.owner()[0] == 'active'
    c, _ = world.say('i said without a condom', seconds=2)
    assert c['outcome'] == 'handed_to_human' and c['reason'] == 'limit_push'
    assert world.owner()[1] == 'open'                                 # limits alone never pause selling by themselves


def test_custom_request_is_flagged_and_never_promised(world):
    world.reply(lambda snap: SyntheticProposal("Sure, I'll make you a custom video tonight"))
    refused, mid = world.say('can you make me a custom video saying my name?')
    assert refused['outcome'] == 'refused' and 'must not be promised' in refused['detail']
    assert world.snaps[-1]['journey']['no_promise'] is True
    assert ('custom_request', 'offer', 'open') in world.cases()
    world.reply(lambda snap: SyntheticProposal('That is not something I can set up in chat'))
    ok = world.turn(TENANT, ACCOUNT, FAN, mid)
    assert ok['outcome'] == 'accepted' and world.owner()[0] == 'active'
    assert len(world.cases('custom_request')) == 1                    # flagged once, no hold, chat continues


# ======================================================================== journey (e): refusal and affordability

def test_refusal_pauses_selling_chat_continues_and_time_alone_never_reopens(world):
    world.backfilled(FAN)
    grant = world.grant()
    world.say_plain('no problem at all')
    r, mid = world.say('too expensive for me right now')
    assert r['outcome'] == 'accepted' and world.owner()[1] == 'paused' and world.owner()[2] == mid
    world.reply(lambda snap: SyntheticProposal('how about now', action='offer', offer_grant_id=grant))
    # nothing re-opens selling: new messages, plain conversation, and the passage of time
    with Session(world.database[1]) as s, s.begin():
        row = s.get(OfEngineOwner, (ACCOUNT, FAN))
        row.updated_at = row.updated_at - timedelta(days=30)
        for message in s.scalars(select(Message).where(Message.account_id == ACCOUNT)):
            message.created_at = message.created_at - timedelta(days=30)
    for text in ('what are you up to', 'good morning', 'thinking of you'):
        blocked, _ = world.say(text, seconds=1)
        assert blocked['outcome'] == 'refused' and world.owner()[1] == 'paused'
    assert world.fans.posts and all('price' not in p['body'] for p in world.fans.posts)
    # an explicit renewed interest is the only way back, and it is scoped (existing R1/R8 state)
    world.reply(lambda snap: SyntheticProposal('sure, this one', action='offer', offer_grant_id=grant,
                                               sales_signal='reopen', item_scope=(str(world.item),)))
    back, _ = world.say('actually i want the set, how do i buy it', seconds=2)
    assert back['outcome'] == 'accepted' and world.owner()[1] == 'reopened_scoped'


def test_affordability_concern_pauses_selling(world):
    world.say_plain('totally understand')
    r, mid = world.say("i can't afford that this month")
    assert r['outcome'] == 'accepted' and world.owner()[1] == 'paused'
    assert world.snaps[-1]['journey']['offers_allowed'] is False


# ======================================================================== units

def test_signals_are_narrow():
    assert signals('Are you a bot?') == {'identity'}
    assert signals("what's your whatsapp") == {'offplatform'}
    assert 'meetup' in signals('lets meet up')
    assert signals('i love your last pic') == frozenset()
    assert signals("i'm good, how are you?") == frozenset()
    assert 'refusal' in signals('thats too expensive')
    assert signals('can you do a custom clip') & {'custom'}


def test_enforce_is_a_no_op_for_accounts_without_journey_facts():
    enforce_journey({}, SyntheticProposal('anything', action='offer', offer_grant_id='x' * 36))
    enforce_journey(None, SyntheticProposal('anything'))
    with pytest.raises(Refused, match='needs a human'):
        enforce_journey({'human_required': 'identity'}, SyntheticProposal('anything'))


def test_synthetic_accounts_get_no_journey_facts(database, monkeypatch):
    store, authority = configured(database, mode='automated')
    engine = EngineService(store, SyntheticModel({'hello': SyntheticProposal('hi')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    from test_onlyfans_foundation_pg import event
    store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='hello'))
    store.drain(TENANT, ACCOUNT)
    seen = []
    original = engine.model.generate
    engine.model.generate = lambda snap: (seen.append(snap), original(snap))[1]
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert 'journey' not in seen[0]
    store.close()


def test_real_unlock_is_scoped_access_not_a_synthetic_financial_transaction(world):
    from db.models import Fan, Transaction
    _, message_id = _sell(world)
    body = unlock(message_id)
    first = world.send(body, key='event-unlock-1')
    assert first['outcome'] == 'evidence'
    world.send(body, key='event-unlock-2')  # distinct webhook delivery, same provider event
    world.send(tx('unlinked-same-amount'), key='event-tx')
    with Session(world.database[1]) as s:
        access = list(s.scalars(select(OfEngineAccessEvidence)))
        evidence = list(s.scalars(select(OfFansEvidence)))
        assert len(access) == 1 and access[0].status == 'confirmed' and access[0].applied
        assert [(e.kind, e.status, e.reason) for e in evidence if e.kind == 'unlock'] == [
            ('unlock', 'applied', 'unlock_confirmed_finance_unlinked')]
        assert [(e.status, e.reason) for e in evidence if e.kind == 'transaction'] == [
            ('pending', 'unlinked_transaction')]
        assert list(s.scalars(select(Transaction))) == []
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 0
        assert s.get(Message, (ACCOUNT, FAN, message_id)).is_paid is not True
        assert verified_spend(s, ACCOUNT, FAN) is None
        from conversation_engine.engine import _verified_purchases
        from db.onlyfans_models import OfRuntime
        assert _verified_purchases(s, s.get(OfRuntime, ACCOUNT), FAN) is None
    from conversation_engine.operator_v2 import unresolved
    items = unresolved(world.store, TENANT, ACCOUNT)
    observed = [i for i in items if i['kind'] == 'unreconciled_access' and 'Provider reported an unlock' in i['summary']]
    assert len(observed) == 1 and 'settlement are not linked' in observed[0]['summary']
    # The confirmed unlock still protects its exact media from being re-offered.
    with pytest.raises(Refused):
        world.grant()


def test_unlinked_reversal_is_visible_without_guessing_access_or_money(world):
    from conversation_engine.operator_v2 import unresolved
    from conversation_engine.engine import _verified_purchases
    from db.onlyfans_models import OfRuntime
    from db.models import Transaction
    _, message_id = _sell(world)
    world.send(unlock(message_id), key='unlock-before-reversal')
    body = {'event': 'chargebacks.new', 'account_id': PROVIDER, 'payload': {
        'id': 1, 'createdAt': iso(4), 'paymentType': 'message',
        'payment': {'id': 'unlinked-tx', 'amount': 10, 'status': 'undo',
                    'createdAt': iso(3), 'user': {'id': FAN}}}}
    world.send(body, key='unlinked-reversal')
    with Session(world.database[1]) as s:
        assert _verified_purchases(s, s.get(OfRuntime, ACCOUNT), FAN) is None
        assert list(s.scalars(select(Transaction))) == []
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention
    items = unresolved(world.store, TENANT, ACCOUNT)
    reversals = [i for i in items if i['kind'] == 'unlinked_transaction' and 'chargeback' in i['summary']]
    assert len(reversals) == 1 and 'does not automatically revoke' in reversals[0]['summary']


def test_wrong_account_webhook_cannot_be_persisted_or_projected_through_pilot_worker(world):
    from test_onlyfans_foundation_pg import seed
    from db.onlyfans_models import OfIngress
    from onlyfans_runtime.text_delivery import TextAuthority
    second_account, second_tenant, second_provider = 'other-creator', 'other-tenant', 'acct_other_creator'
    other = seed(world.database[0], world.database[1], account=second_account, tenant=second_tenant, fan=FAN)
    try:
        TextAuthority(other).configure(second_tenant, second_account, expected_revision=0, enabled=True,
            limit_units=10, throttle_seconds=1, actor='synthetic-operator', reason='separate fixture')
        FansWebhookAdapter(other).bind_simulated(second_tenant, second_account, second_provider, '888')
        body = inbound(98765, 'separate fan message')
        body['account_id'] = second_provider
        raw = json.dumps(body, separators=(',', ':')).encode()
        sig = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        with pytest.raises(Refused, match='webhook account scope mismatch'):
            world.worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=sig, secret=SECRET, event_key='other-account-event')
        with Session(world.database[1]) as session:
            assert session.get(OfFansWebhook, (second_account, 1, 'other-account-event')) is None
            assert session.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.account_id == second_account)) == 0
            assert session.get(Message, (second_account, FAN, 98765)) is None
        result = FansEventIntake(other).accept(raw, sig, SECRET, 'other-account-event',
                                                expected_tenant=second_tenant, expected_account=second_account)
        assert result['account'] == second_account and result['projection'] == 'projected'
    finally:
        other.close()
