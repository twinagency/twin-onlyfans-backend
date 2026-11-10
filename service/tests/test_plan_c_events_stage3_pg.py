"""Stage 3: one event classification contract through the real HTTP ingest-only route + worker ticks.

Text-only profile, two fans (FAN=101, FAN2=102). Every case posts signed bodies to
create_app(...).post('/onlyfans/webhooks/plan-c') and then runs worker ticks; assertions are on the
actual provider posts, model calls and durable rows. Nothing is seeded as a grant.
"""
import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conversation_engine import operator_v2, pilot_scope
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import (OfEnginePilotReconnectReceipt, OfEnginePilotWebhookDigest, OfEngineRequest,
                                       OfFansWebhookConflict)
from db.onlyfans_fans_intake_models import OfFansEvidence, OfFansLink, OfFansProjection
from fans_api_adapter import event_scope
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from plan_c_worker import create_app
from test_engine_real_worker_pg import FAN2, PROVIDER, CREATOR, inbound, iso, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401
from test_plan_c_text_no_offers_pg import text_no_offers

EVIDENCE = 'operator:fansapi:exact retained delivery reviewed'


def K(n):
    return f'{n:064x}'


def ev(name, payload):
    return {'event': name, 'account_id': PROVIDER, 'payload': payload}


COUNTERS = ['messages.unread_count_updated', 'messages.priority_count_updated', 'messages.unread_tips_count_updated',
            'calls.count_request_updated', 'notifications.count_updated']


def queue_event(name='chat_queue.finished', queue=50157853298, total=19730):
    # Documented chat_queue fields (docs.onlyfansapi.com/webhooks/available-events).
    return ev(name, {'id': queue, 'date': iso(), 'canUnsend': False, 'hasError': False, 'isCanceled': False,
                     'isCouplePeopleMedia': False, 'isDone': name.endswith('finished'), 'isReady': True,
                     'pending': 0, 'total': total, 'unsendSeconds': 0})


def tip_event(fan, tid='t1'):
    return ev('tips.received', {'id': tid, 'amountGross': 5, 'amountNet': 4, 'text': 'thanks', 'replacePairs': [],
                                'subType': 'tip', 'user_id': CREATOR, 'fan': {'id': fan}, 'user': {'id': CREATOR},
                                'fanData': {}, 'createdAt': iso()})


def sent_event(mid, fan, queued):
    return ev('messages.sent', {'id': mid, 'toUser': {'id': fan}, 'text': 'blast' if queued else 'hi it is me',
                                'createdAt': iso(), 'isFromQueue': queued, 'isSentByMe': True})


class Rig:
    def __init__(self, stopped):
        self.w, env, model, self.seen = text_no_offers(stopped)
        self.pilot = build_supervised_pilot(self.w.store, env, model_transport=model, fans_transport=self.w.fans.transport)
        self.env, self.model = env, model
        self.client = TestClient(create_app(self.pilot, run_loop=False))
        self.client.__enter__()
        self.n = 0

    def close(self):
        self.client.__exit__(None, None, None)

    def post(self, body, key=None, raw=None, status=202):
        self.n += 1
        key = key or f'stage3-{self.n}'
        if raw is None:
            raw, headers = signed(body, key=key)
        else:
            headers = signed({}, key=key)[1]
            import hmac
            from test_plan_c_draft_app_pg import SECRET
            headers['Signature'] = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        response = self.client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers)
        assert response.status_code == status, response.text
        return raw

    def fans_in(self, a='hello, how was your day?', b='hey are you there?', base=71000):
        self.post(inbound(base + 1, a, fan=FAN), key=f'in-a-{base}')
        self.post(inbound(base + 2, b, fan=FAN2), key=f'in-b-{base}')

    def ticks(self, n=4):
        return [self.pilot.tick() for _ in range(n)]

    def digests(self):
        with Session(self.w.database[1]) as s:
            return [(d.event_key, d.safety_hold) for d in s.scalars(select(OfEnginePilotWebhookDigest))]

    def requests(self):
        with Session(self.w.database[1]) as s:
            return {r.fan_id: r.status for r in s.scalars(select(OfEngineRequest))}

    def block(self, fan):
        with Session(self.w.database[1]) as s:
            runtime = self.w.store._runtime(s, TENANT, ACCOUNT, lock=False)
            return pilot_scope.delivery_block(s, runtime, fan)

    def items(self, kind):
        return [i for i in operator_v2.unresolved(self.w.store, TENANT, ACCOUNT, ACTOR) if i['kind'] == kind]

    def reconcile(self, item, n, raw=None, **patch):
        body = {'commandKey': K(n), 'actor': ACTOR, 'evidenceRef': EVIDENCE, 'scopeRevision': item['scopeRevision'],
                'eventEpoch': item['eventEpoch'], 'eventKey': item['eventKey']}
        if raw is not None:
            body.update(payloadSha256=item['payloadSha256'], rawBody=raw.decode())
        body.update(patch)
        return operator_v2.reconcile(self.w.store, TENANT, ACCOUNT, item['id'], body)


@pytest.fixture
def rig(stopped_text):
    r = Rig(stopped_text)
    try:
        yield r
    finally:
        r.close()


# ----------------------------------------------------------------------------- pure classification contract
@pytest.mark.parametrize('body,cls', [
    *[(ev(n, {'count': 3}), 'irrelevant') for n in COUNTERS],
    *[(ev(n, {'count': bad}), 'unknown') for n in COUNTERS for bad in (-1, True, '3', 1.5, None)],
    *[(ev(n, {'count': 1, 'extra': 1}), 'unknown') for n in COUNTERS],
    (ev('messages.unread_count_updated', {}), 'unknown'),
    (ev('users.typing', {'id': 318022380}), 'irrelevant'),
    (ev('users.typing', {'user': {'id': 318022380}}), 'unknown'),
    (ev('users.typing', {'id': 0}), 'unknown'),
    (ev('users.typing', {'id': True}), 'unknown'),
    (ev('users.online', {'fan': {'id': 5}, 'observed_at': 'x', 'status_changed_at': None, 'last_seen_online_at': None}), 'irrelevant'),
    (ev('users.offline', {'fan': {'id': 5}}), 'irrelevant'),
    (ev('users.online', {'fan': {'id': -5}}), 'unknown'),
    (ev('users.online', {'fan': {'id': 5}, 'surprise': 1}), 'unknown'),
    (ev('messages.liked', {'id': 5, 'chat_id': 6, 'fan_id': 6, 'fan': {'id': 6}}), 'irrelevant'),
    (ev('messages.liked', {'id': 5, 'chat_id': None, 'fan_id': None, 'fan': None}), 'irrelevant'),
    (ev('messages.unliked', {'id': 5}), 'irrelevant'),
    (ev('messages.liked', {'chat_id': 6}), 'unknown'),
    (ev('posts.liked', {'id': 1, 'type': 'like', 'text': '', 'user_id': 9, 'post_id': 77, 'fan': {'id': 6}, 'user': {}}), 'irrelevant'),
    (ev('posts.liked', {'id': 1}), 'unknown'),
    (ev('posts.new', {'id': 77}), 'unknown'),
    (queue_event('chat_queue.updated'), 'unknown'),
    (queue_event('chat_queue.finished'), 'unknown'),
    (ev('chat_queue.updated', {'queue_id': 1, 'total': -1}), 'unknown'),
    (ev('fan_summary.completed', {'data': {}}), 'unknown'),
    (ev('media_uploads.completed', {'id': 1}), 'unknown'),
    (tip_event(318022380), 'material'),
    (ev('tips.received', {'id': 'x', 'amountGross': 5}), 'unknown'),
    (ev('tips.received', {'fan': {'id': 'abc'}}), 'unknown'),
    (inbound(1, 'hi', fan=318022380), 'supported'),
    (ev('messages.received', {'id': 1, 'fromUser': {'id': 'nope'}}), 'unknown'),
    (ev('messages.deleted', {'id': 5}), 'supported'),
    (ev('messages.deleted', {}), 'unknown'),
    (ev('accounts.disconnected', {}), 'supported'),
    ({'event': 'messages.received', 'payload': {}}, 'unknown'),
    ({'payload': {}}, 'unknown'),
    ([], 'unknown'),
])
def test_shared_classification_requires_documented_shape_not_just_name(body, cls):
    assert event_scope.classify_body(body).cls == cls


def test_classification_of_raw_bytes_and_malformed_bytes():
    assert event_scope.classify_raw(json.dumps(ev('users.typing', {'id': 5})).encode()).cls == 'irrelevant'
    for bad in (b'', b'{', b'\xff', b'[]', b'{"event":"users.typing","payload":{"id":5}}',
                b'{"event":"x","account_id":"a","payload":{"id":1},"event":"users.typing"}'):
        assert event_scope.classify_raw(bad).cls == 'unknown'
    assert event_scope.known_irrelevant_event(ev('notifications.count_updated', {'count': 0}))
    assert not event_scope.known_irrelevant_event(queue_event())


# ----------------------------------------------------------------------------- safe counters / irrelevant
def test_safe_counters_allow_replies_and_pass_interval_recovery(rig):
    for i, name in enumerate(COUNTERS):
        rig.post(ev(name, {'count': i}), key=f'ctr-{i}')
    rig.fans_in()
    rig.ticks()
    assert len(rig.w.fans.posts) == 2 and len(rig.seen) == 2
    assert rig.requests() == {FAN: 'completed', FAN2: 'completed'}
    digests = rig.digests()
    assert len(digests) == 5 and not any(hold for _, hold in digests)
    _recovery_accepts(rig, [ev(n, {'count': i}) for i, n in enumerate(COUNTERS)], [f'ctr-{i}' for i in range(5)])


def _recovery_accepts(rig, bodies, keys, expect=None, hold=None):
    """Feed the real retained digests to ops validate_interval (same classification, no unguarded call)."""
    import validate_interval as v
    import tempfile
    from pathlib import Path
    with Session(rig.w.database[1]) as s:
        dig = {d.event_key: d for d in s.scalars(select(OfEnginePilotWebhookDigest))}
    anchor = inbound(9, 'anchor', fan=FAN)
    anchor['account_id'] = v.PROVIDER
    anchor_raw = json.dumps(anchor, separators=(',', ':')).encode()
    rows = [{'idempotency_key': 'anchor', 'event': 'messages.received', 'payload': anchor, 'succeeded': True,
             'status_code': 202, 'url': v.URL}]
    for body, key in zip(bodies, keys):
        p = json.loads(json.dumps(body))
        p['account_id'] = v.PROVIDER
        rows.append({'idempotency_key': key, 'event': body['event'], 'payload': p, 'succeeded': True,
                     'status_code': 202, 'url': v.URL})
    local = {'receipts': [{'key': 'anchor', 'raw': base64.b64encode(anchor_raw).decode(),
                           'hash': hashlib.sha256(anchor_raw).hexdigest(), 'status': 'accepted', 'projection': 'projected'}],
             'digests': [{'key': k, 'hash': dig[k].payload_sha256, 'hold': dig[k].safety_hold} for k in keys if k in dig]}
    old = v.REQUIRED_PILOT_RECEIPT
    v.REQUIRED_PILOT_RECEIPT = 'anchor'
    try:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for name, obj in [('manifest.json', {'provider_delivery_count': len(rows)}),
                              ('deliveries.json', {'data': rows, '_pagination': {'next_page': None}}),
                              ('local-receipts.json', local)]:
                (root / name).write_text(json.dumps(obj))
            if expect is RuntimeError:
                with pytest.raises(RuntimeError):
                    v.validate(root)
                return None
            result = v.validate(root)
            assert result['interval_valid_for_bounded_scope'] and not result['full_event_continuity_claimed']
            return result
    finally:
        v.REQUIRED_PILOT_RECEIPT = old


def test_malformed_negative_bool_extra_key_counters_stay_held_and_recovery_refuses(rig):
    variants = [{'count': -1}, {'count': True}, {'count': 1, 'extra': 1}, {'count': '3'}, {}]
    bodies = [ev('messages.unread_count_updated', p) for p in variants]
    for i, b in enumerate(bodies):
        rig.post(b, key=f'badctr-{i}')
    rig.fans_in()
    rig.ticks()
    assert [hold for _, hold in rig.digests()] == [True] * 5
    assert rig.w.fans.posts == [] and rig.seen == [], 'held evidence must not spend model calls or send'
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_evidence_pending'
    _recovery_accepts(rig, bodies[:1], ['badctr-0'], expect=RuntimeError)


def test_irrelevant_events_for_other_and_pilot_fans_do_not_freeze_either_fan(rig):
    events = [ev('users.typing', {'id': 999000111}), ev('users.typing', {'id': FAN}),
              ev('users.online', {'fan': {'id': 999000111}, 'observed_at': iso(), 'status_changed_at': iso(),
                                  'last_seen_online_at': iso()}),
              ev('users.offline', {'fan': {'id': FAN2}, 'observed_at': iso(), 'status_changed_at': iso(),
                                   'last_seen_online_at': iso()}),
              ev('messages.liked', {'id': 5, 'chat_id': 999000111, 'fan_id': 999000111, 'fan': {'id': 999000111}}),
              ev('messages.unliked', {'id': 5, 'chat_id': FAN, 'fan_id': FAN, 'fan': {'id': FAN}}),
              ev('posts.liked', {'id': 1, 'type': 'like', 'text': '', 'user_id': CREATOR, 'post_id': 77,
                                 'fan': {'id': FAN}, 'user': {'id': CREATOR}})]
    for i, e in enumerate(events):
        rig.post(e, key=f'irr-{i}')
    rig.fans_in()
    rig.ticks()
    assert len(rig.w.fans.posts) == 2 and rig.requests() == {FAN: 'completed', FAN2: 'completed'}
    assert not any(hold for _, hold in rig.digests())
    _recovery_accepts(rig, events, [f'irr-{i}' for i in range(len(events))])


# ----------------------------------------------------------------------------- unknown / chat_queue stay held
def test_chat_queue_and_unknown_events_hold_account_spend_nothing_and_cannot_be_cleared(rig):
    raws = [rig.post(queue_event('chat_queue.updated'), key='q-up'), rig.post(queue_event('chat_queue.finished'), key='q-fin'),
            rig.post(ev('posts.new', {'id': 77}), key='posts-new'),
            rig.post(ev('users.typing', {'user': {'id': FAN}}), key='typing-undocumented-shape')]
    big = ev('chat_queue.updated', {'id': 1, 'pad': 'x' * 70000})
    big_raw = rig.post(big, key='q-big')
    rig.fans_in()
    rig.ticks()
    assert rig.w.fans.posts == [] and rig.seen == []
    assert rig.requests() == {} or set(rig.requests().values()) == {'open'}
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_evidence_pending'
    with Session(rig.w.database[1]) as s:
        receipts = {r.event_key: bytes(r.raw_body) for r in s.scalars(select(OfEnginePilotReconnectReceipt))}
    assert [receipts[k] for k in ('q-up', 'q-fin', 'posts-new', 'typing-undocumented-shape')] == raws, \
        'exact authenticated bytes of held events are retained in the durable receipt table'
    assert 'q-big' not in receipts, 'oversized held bytes keep digest evidence only (size limit)'
    assert dict(rig.digests())['q-big'] is True
    for n, (item, raw) in enumerate(zip(sorted(rig.items('provider_event'), key=lambda i: i['eventKey']), [None] * 9)):
        raw_by_key = {'q-up': raws[0], 'q-fin': raws[1], 'posts-new': raws[2], 'typing-undocumented-shape': raws[3]}
        if item['eventKey'] in raw_by_key:
            with pytest.raises(operator_v2.Conflict):
                rig.reconcile(item, 100 + n, raw=raw_by_key[item['eventKey']])
    assert rig.block(FAN) == 'pilot_evidence_pending'
    rig.ticks()
    assert rig.w.fans.posts == [] and rig.seen == []


def test_old_incident_with_missing_evidence_stays_held_for_both_fans(rig):
    # A legacy digest hold has no retained bytes (and new retention cannot recreate them).
    with Session(rig.w.database[1]) as s, s.begin():
        runtime = rig.w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        from db.onlyfans_engine_models import OfFansConnection
        conn = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == ACCOUNT))
        s.add(OfEnginePilotWebhookDigest(account_id=ACCOUNT, epoch=conn.epoch, event_key='old-lost',
                                         payload_sha256='ab' * 32, safety_hold=True, received_at=utcnow()))
    rig.fans_in()
    rig.ticks()
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_evidence_pending'
    assert rig.w.fans.posts == [] and rig.seen == []
    [item] = rig.items('provider_event')
    for raw in (b'{}', json.dumps(tip_event(FAN)).encode()):
        with pytest.raises((operator_v2.Conflict, operator_v2.BadRequest)):
            rig.reconcile(item, 200, raw=raw)
    assert rig.block(FAN) == 'pilot_evidence_pending'


# ----------------------------------------------------------------------------- material event holds one fan
def test_material_tip_holds_that_fan_only_then_audited_release_resumes_its_request(rig):
    tip_raw = rig.post(tip_event(FAN), key='tip-fan')
    rig.post(tip_event(999000111, tid='t2'), key='tip-other')       # a non-pilot fan: retained, no hold
    rig.fans_in()
    rig.ticks()
    assert rig.digests() and dict(rig.digests())['tip-fan'] is True and dict(rig.digests())['tip-other'] is False
    assert len(rig.w.fans.posts) == 1 and len(rig.seen) == 1, 'only the unaffected fan is planned and answered'
    assert rig.requests().get(FAN2) == 'completed' and rig.requests().get(FAN, 'open') == 'open'
    assert rig.block(FAN) == 'pilot_evidence_pending' and rig.block(FAN2) is None
    [item] = rig.items('provider_event')
    wrong = ({'rawBody': tip_raw.decode() + ' '}, {'actor': 'op-session:' + 'b' * 64})
    for patch in wrong:
        with pytest.raises((operator_v2.Conflict, operator_v2.Refused)):
            rig.reconcile(item, 300, raw=tip_raw, **patch)
    assert rig.block(FAN) == 'pilot_evidence_pending'
    result = rig.reconcile(item, 301, raw=tip_raw)
    assert result['resolved'] and result['outcome'] == 'material_event_reviewed'
    assert rig.reconcile(item, 301, raw=tip_raw)['replayed']
    assert rig.reconcile(item, 302, raw=tip_raw)['outcome'] == 'already_classified'
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.kind == 'plan_c_webhook_resolution')) == 1
        assert s.get(OfDeliveryPolicy, ACCOUNT).limit_units >= 1
    assert len(rig.w.fans.posts) == 1, 'resolving evidence sends nothing by itself'
    rig.ticks()
    assert len(rig.w.fans.posts) == 2 and rig.requests() == {FAN: 'completed', FAN2: 'completed'}
    assert len(rig.seen) == 2, 'exactly one model call per fan: no spend while held, resume after resolution'


def test_material_event_whose_provider_account_is_unbound_holds_the_account(rig):
    other = ev('tips.received', tip_event(FAN)['payload'])
    other['account_id'] = 'acct_someone_else'
    rig.post(other, key='tip-unbound')
    rig.fans_in()
    rig.ticks()
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_evidence_pending'
    assert rig.w.fans.posts == [] and rig.seen == []


# ----------------------------------------------------------------------------- idempotency / conflict / replay
def test_duplicate_same_key_same_body_is_idempotent_and_conflicting_body_is_quarantined(rig):
    rig.post(ev('users.typing', {'id': 5}), key='dup-typing')
    rig.post(ev('users.typing', {'id': 5}), key='dup-typing')
    assert len(rig.digests()) == 1
    same = inbound(72001, 'hello there', fan=FAN)
    raw = rig.post(same, key='dup-in')
    assert rig.post(same, key='dup-in') == raw
    rig.ticks()
    assert len(rig.w.fans.posts) == 1 and len(rig.seen) == 1
    # same key, different body -> quarantined (kept as evidence), never answered
    rig.post(inbound(72001, 'completely different words', fan=FAN), key='dup-in')
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfFansWebhookConflict)) == 1
    rig.ticks()
    assert len(rig.w.fans.posts) == 1
    # a conflicting irrelevant body under an already-recorded key re-holds the account
    rig.post(ev('users.typing', {'id': 6}), key='dup-typing')
    assert any(hold for key, hold in rig.digests() if key == 'dup-typing')
    rig.post(inbound(72050, 'anyone?', fan=FAN2), key='after-conflict')
    rig.ticks()
    assert len(rig.w.fans.posts) == 1, 'a conflicting body under a recorded key holds sends until reviewed'


def test_forged_signature_is_rejected_and_replay_under_another_key_does_not_double_reply(rig):
    raw, headers = signed(inbound(73001, 'hello', fan=FAN), key='forge')
    forged = {**headers, 'Signature': '0' * 64}
    assert rig.client.post('/onlyfans/webhooks/plan-c', content=raw, headers=forged).status_code == 401
    assert rig.digests() == []
    same = inbound(73001, 'hello', fan=FAN)
    rig.post(same, key='orig-key')
    rig.post(same, key='replay-other-key')   # cross-key replay of the same signed body
    rig.post(ev('messages.unread_count_updated', {'count': 2}), key='replay-ctr-a')
    rig.post(ev('messages.unread_count_updated', {'count': 2}), key='replay-ctr-b')
    rig.ticks()
    assert len(rig.w.fans.posts) == 1 and len(rig.seen) == 1


# ----------------------------------------------------------------------------- queue / manual / deletion / auth
def test_queued_send_to_owned_fan_holds_that_fan_only_and_is_released_by_audited_review(rig):
    rig.post(sent_event(90001, FAN, queued=True), key='queued-to-fan')
    rig.post(sent_event(90009, 999000111, queued=True), key='queued-elsewhere')
    rig.fans_in()
    rig.ticks()
    assert len(rig.w.fans.posts) == 1 and len(rig.seen) == 1 and rig.requests().get(FAN2) == 'completed'
    assert rig.block(FAN) == 'pilot_evidence_pending' and rig.block(FAN2) is None
    with Session(rig.w.database[1]) as s:
        held = list(s.scalars(select(OfFansEvidence).where(OfFansEvidence.kind == 'human_sent')))
        assert [(h.fan_id, h.status, h.reason) for h in held] == [(FAN, 'held', 'queued_send_to_engine_owned_fan')]
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated', 'a queue is not takeover'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention
    [item] = rig.items('held_send_evidence')
    body = {'commandKey': K(400), 'actor': ACTOR, 'evidenceRef': EVIDENCE}
    result = operator_v2.reconcile(rig.w.store, TENANT, ACCOUNT, item['id'], body)
    assert result['resolved'] and result['outcome'] == 'dismissed'
    assert operator_v2.reconcile(rig.w.store, TENANT, ACCOUNT, item['id'], body)['replayed']
    with pytest.raises(operator_v2.Conflict):
        operator_v2.reconcile(rig.w.store, TENANT, ACCOUNT, item['id'], {**body, 'commandKey': K(401)})
    assert len(rig.w.fans.posts) == 1
    rig.ticks()
    assert len(rig.w.fans.posts) == 2 and rig.requests() == {FAN: 'completed', FAN2: 'completed'}


def test_manual_send_to_one_fan_holds_that_fan_while_the_other_progresses(rig):
    rig.post(sent_event(90002, FAN, queued=False), key='manual-to-fan')
    rig.fans_in()
    rig.ticks()
    assert len(rig.w.fans.posts) == 1 and rig.requests().get(FAN2) == 'completed'
    assert rig.requests().get(FAN, 'open') == 'open' and rig.block(FAN) is not None and rig.block(FAN2) is None


def test_resolving_one_hold_class_never_clears_another(rig):
    rig.post(queue_event(), key='q-account-hold')                       # digest hold (account)
    rig.post(sent_event(90003, FAN, queued=True), key='queued-fan')     # evidence hold (fan)
    rig.post(inbound(74001, 'please stop texting me', fan=FAN2), key='stop-fan2')   # no-contact control
    rig.ticks()
    [send] = rig.items('held_send_evidence')
    operator_v2.reconcile(rig.w.store, TENANT, ACCOUNT, send['id'], {'commandKey': K(500), 'actor': ACTOR, 'evidenceRef': EVIDENCE})
    assert rig.block(FAN) == 'pilot_evidence_pending', 'the queued-send resolution must not clear the digest hold'
    with Session(rig.w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN2)).mode == 'no_contact'
    rig.fans_in(base=74100)
    rig.ticks()
    assert rig.w.fans.posts == [] and rig.seen == []
    assert [h for _, h in rig.digests()] == [True]


def test_deletion_before_its_message_holds_then_audited_reprojection_resolves_without_authority(rig):
    rig.post(ev('messages.deleted', {'id': 75001}), key='del-early')
    rig.post(inbound(75001, 'oops wrong chat', fan=FAN), key='in-deleted')
    rig.post(inbound(75002, 'hello there', fan=FAN2), key='in-fan2')
    rig.ticks()
    with Session(rig.w.database[1]) as s:
        proj = s.scalar(select(OfFansProjection).where(OfFansProjection.event_key == 'del-early'))
        assert (proj.status, proj.detail) == ('held', 'deletion_mapping_missing_or_ambiguous')
    assert rig.w.fans.posts == [] and rig.seen == [], 'a held projection stops planning for the account'
    [item] = rig.items('held_projection')
    with pytest.raises(operator_v2.BadRequest):
        operator_v2.reconcile(rig.w.store, TENANT, ACCOUNT, item['id'], {'commandKey': K(600), 'actor': ACTOR})
    first = rig.reconcile(item, 601)
    assert first['resolved'] and first['projection'] == 'projected', first
    assert rig.reconcile(item, 601)['replayed']
    with pytest.raises(operator_v2.Conflict):
        rig.reconcile(item, 602)                                         # no longer held
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(Message.is_unsent).where(Message.message_id == 75001))
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.kind == 'plan_c_reconcile', OfLaunchCommand.command_key == K(601))) == 1
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        enabled_before = policy.enabled
    assert rig.w.fans.posts == [] and enabled_before
    rig.ticks()
    assert len(rig.w.fans.posts) == 1 and rig.requests().get(FAN2) == 'completed'
    assert rig.requests().get(FAN, 'open') != 'completed', 'the deleted message is not answered'


def test_conflicting_and_historical_projection_holds_cannot_be_reprojected(rig):
    rig.post(inbound(76001, 'first body', fan=FAN), key='conf')
    rig.post(inbound(76001, 'other body', fan=FAN), key='conf')
    rig.ticks()
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfFansWebhookConflict)) == 1
    assert rig.items('held_projection') == []


def test_authentication_change_blocks_both_fans_and_irrelevant_events_do_not_clear_it(rig):
    rig.post(ev('accounts.authentication_failed', {'onlyfans_id': CREATOR, 'onlyfans_username': 'x', 'state': 'failed'}),
             key='auth-failed')
    rig.post(ev('messages.unread_count_updated', {'count': 4}), key='after-auth-ctr')
    rig.fans_in()
    with pytest.raises(Refused):      # the provider event disabled the sender policy: the worker refuses to run
        rig.pilot.tick()
    assert rig.w.fans.posts == [] and rig.seen == []
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_provider_link_blocked'
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(OfFansLink.send_blocked)) is True


def test_late_reordered_supported_events_are_projected_once(rig):
    # The reply-bearing inbound arrives after a later counter and an earlier-numbered duplicate id.
    rig.post(ev('users.typing', {'id': FAN}), key='late-1')
    rig.post(inbound(77002, 'second message', fan=FAN, seconds=1), key='late-b')
    rig.post(inbound(77001, 'first message', fan=FAN, seconds=-1), key='late-a')
    rig.post(inbound(77003, 'hello fan two', fan=FAN2), key='late-c')
    rig.ticks(6)
    assert len(rig.w.fans.posts) >= 2 and rig.requests().get(FAN2) == 'completed'
    assert not any(hold for _, hold in rig.digests())



def test_intake_only_receiver_uses_the_same_classification_and_retains_held_bytes(stopped_text):
    from model_transport.pilot_supervised import IntakeOnlyPilot
    w, env, model, seen, _ = stopped_text
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    assert isinstance(pilot, IntakeOnlyPilot)
    cases = {'ctr': ev('notifications.count_updated', {'count': 1}), 'typing': ev('users.typing', {'id': 5}),
             'queue': queue_event(), 'tip-fan': tip_event(FAN), 'tip-other': tip_event(999000111, 't9'),
             'bad-ctr': ev('notifications.count_updated', {'count': -2})}
    raws = {}
    with TestClient(create_app(pilot, run_loop=False)) as client:
        for key, body in cases.items():
            raw, headers = signed(body, key=key)
            raws[key] = raw
            assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    with Session(w.database[1]) as s:
        holds = {d.event_key: d.safety_hold for d in s.scalars(select(OfEnginePilotWebhookDigest))}
        receipts = {r.event_key: bytes(r.raw_body) for r in s.scalars(select(OfEnginePilotReconnectReceipt))}
    assert holds == {'ctr': False, 'typing': False, 'queue': True, 'tip-fan': True, 'tip-other': False, 'bad-ctr': True}
    assert receipts == {k: raws[k] for k in ('queue', 'tip-fan', 'bad-ctr')}
    assert w.fans.posts == [] and seen == []


def test_legacy_held_presence_digest_is_resolved_only_with_exact_bytes_and_audit(rig, monkeypatch):
    # Reproduce a digest held by the previous release (which knew only five counters) through real intake.
    with monkeypatch.context() as legacy:
        legacy.setattr('model_transport.pilot_supervised._known_irrelevant', lambda raw: False)
        raw = rig.post(ev('users.typing', {'id': 5}), key='legacy-typing')
    assert dict(rig.digests()) == {'legacy-typing': True} and rig.block(FAN) == 'pilot_evidence_pending'
    [item] = rig.items('provider_event')
    with pytest.raises(operator_v2.Conflict):
        rig.reconcile(item, 700, raw=raw.replace(b'"id":5', b'"id":6'))
    assert rig.block(FAN) == 'pilot_evidence_pending'
    assert rig.reconcile(item, 701, raw=raw)['outcome'] == 'documented_irrelevant_event_classified'
    assert rig.block(FAN) is None and dict(rig.digests()) == {'legacy-typing': True}, 'original evidence is immutable'
    rig.fans_in()
    rig.ticks()
    assert len(rig.w.fans.posts) == 2
