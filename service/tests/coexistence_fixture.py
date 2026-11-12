"""Shared world for the Step 5 (staff coexistence, voluntary tips) suites: a COMMERCIAL account driven through the real
signed-webhook intake, the real supervised scheduler/worker and the real delivery path, with a fake model transport and a fake
FansAPI transport. Synthetic identities only.

The account is built the way production builds it: preparation (read-only import through the guarded fake provider) ->
agency promotion to commercial.v1 -> operator launch. Nothing here seeds a grant, a purchase or an ownership fact, forces a
coverage flag or opens a guard. Delivery mode is text_only: coexistence and tips are conversation behaviours, not sales.
"""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import approval, commercial_lifecycle as cs, pilot_scope
from conversation_engine.model_config import ModelConfig
from conversation_engine.writer_context import CreatorProfile, profile_data
from db.models import Message, OfConversationState
from db.onlyfans_fans_intake_models import OfFansEvidence, OfFansProjection
from history_support import Provider, chat_page, prepared as history_prepared
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import utcnow
from test_commercial_preparation_pg import TENANT
from test_commercial_scope_lifecycle_pg import add_fan, recover, unblock
from test_engine_real_worker_pg import MockFans, answer, iso
from test_history_coverage_pg import read_everything

SECRET = b'coexistence-secret'
OPERATOR = 'op-session:' + 'a' * 64
FAN_A, FAN_B = 401, 402
PERMISSIONS = ['configure', 'activate', 'approve', 'reject', 'edit', 'stop', 'takeover', 'handback', 'reconcile', 'correct', 'read']
TIP_ACK = 'Thank you so much for the tip, that was really sweet of you.'


def key(n):
    return f'{n:064x}'


class CoexistenceFans(MockFans):
    """The fake provider: identity for THIS account, records every POST, replays by idempotency key."""
    def __init__(self, provider, creator):
        super().__init__()
        self.provider, self.creator = provider, creator

    def handle(self, request):
        if request.method == 'GET' and request.url.path == '/api/accounts':
            return answer(200, [{'id': self.provider, 'onlyfans_id': self.creator, 'is_authenticated': True}])
        return super().handle(request)


class World:
    def __init__(self):
        self.model_prompts, self.mid, self.n = [], 5000, 0
        self.choose = None

    # ------------------------------------------------------------------ launch
    def promote(self, *, coexist=False, coexistence=None):
        profile = CreatorProfile(name='Fixture creator', limits=('no meetups',), role_statement='the creator herself',
                                 offers_permitted=False, approved_item_ids=())
        self.profile = profile_data(profile)
        self.profiles_path.write_text(json.dumps({self.account: self.profile}))
        body = {'fans': list(self.fans), 'creator_profile': self.profile, 'delivery_mode': 'text_only',
                'operator': {'actor': OPERATOR, 'name': 'Fixture operator', 'permissions': PERMISSIONS},
                'model': pilot_scope.model_spec(ModelConfig.from_env(self.env)),
                'limits': {'send_units': 200, 'daily_send_units': 100, 'throttle_seconds': 1, 'provider_requests': 4000,
                           'provider_credit_budget': 8000, 'provider_credits_per_request': 2}}
        if coexistence is not None:
            body['coexistence'] = coexistence
        elif coexist:
            body['coexistence'] = {'mode': 'coexist'}
        return cs.promote(self.store, TENANT, self.account, body, expected_revision=1, command_key=key(0x8001 + self.index),
                          actor=OPERATOR, reason='Promote prepared account to a commercial scope')

    def activate(self, revision, *, authorization_mode='supervised'):
        if authorization_mode == 'autonomous':
            config = pilot_scope.describe(self.store, TENANT, self.account)['config']
            revision = pilot_scope.configure(self.store, TENANT, self.account, {**config, 'authorization_mode': 'autonomous'},
                                             expected_revision=revision, command_key=key(0x8101 + self.index), actor=OPERATOR,
                                             reason='Operator selects autonomous replies')['revision']
        launched = pilot_scope.set_status(self.store, TENANT, self.account, 'active', expected_revision=revision,
                                          command_key=key(0x8201 + self.index), actor=OPERATOR, reason='Operator launch decision')
        self.scope_revision = launched['revision']
        self.pilot = build_supervised_pilot(self.store, self.env, model_transport=self.model_transport,
                                            fans_transport=self.fans_mock.transport)
        # Provider-side human-send evidence is classified straight away (no 30 s grace) so tests need no clock games.
        self.pilot.worker.intake.grace = timedelta(0)
        return launched

    def launch(self, *, coexist=False, authorization_mode='supervised'):
        return self.activate(self.promote(coexist=coexist)['revision'], authorization_mode=authorization_mode)

    # ------------------------------------------------------------------ webhooks
    def inbound(self, mid, text, fan=FAN_A, seconds=0):
        return {'event': 'messages.received', 'account_id': self.provider, 'payload': {
            'id': mid, 'fromUser': {'id': fan}, 'text': text, 'createdAt': iso(seconds), 'isSentByMe': False, 'media': [], 'price': 0}}

    def sent(self, mid, fan, text='hi it is me', queued=False, seconds=0):
        return {'event': 'messages.sent', 'account_id': self.provider, 'payload': {
            'id': mid, 'toUser': {'id': fan}, 'text': text, 'createdAt': iso(seconds), 'isFromQueue': queued, 'isSentByMe': True}}

    def queue(self, name='chat_queue.finished', queue_id=50157853298):
        return {'event': name, 'account_id': self.provider, 'payload': {
            'id': queue_id, 'date': iso(), 'canUnsend': False, 'hasError': False, 'isCanceled': False, 'isCouplePeopleMedia': False,
            'isDone': name.endswith('finished'), 'isReady': True, 'pending': 0, 'total': 120, 'unsendSeconds': 0}}

    def tip(self, fan, tid='tip-1', gross=5, net=4, **changes):
        payload = {'id': tid, 'amountGross': gross, 'amountNet': net, 'text': 'thanks', 'replacePairs': [], 'subType': 'tip',
                   'user_id': self.creator, 'fan': {'id': fan}, 'user': {'id': self.creator}, 'fanData': {}, 'createdAt': iso()}
        payload.update(changes)
        return {'event': 'tips.received', 'account_id': self.provider, 'payload': payload}

    def raw(self, body):
        return json.dumps(body, separators=(',', ':')).encode()

    def deliver(self, body, event_key=None, raw=None):
        self.n += 1
        raw = raw if raw is not None else self.raw(body)
        event_key = event_key or f'evt-{self.index}-{self.n}'
        signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        return self.pilot.handle(raw, signature, event_key)

    def say(self, text, fan=FAN_A, event_key=None):
        self.mid += 1
        return self.deliver(self.inbound(self.mid, text, fan), event_key), self.mid

    # ------------------------------------------------------------------ operator
    def approve(self, result):
        assert result.get('outcome') == 'awaiting_approval', result
        info = result['approval']
        return approval.approve(self.store, TENANT, self.account, info['requestId'], info['generation'],
                                expected_version=info['version'], actor=OPERATOR)

    def unthrottle(self):
        with self.database[1].begin() as c:
            c.exec_driver_sql("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")

    def tick(self, n=1):
        out = []
        for _ in range(n):
            self.unthrottle()
            out.append(self.pilot.tick())
        return out

    def converse(self, text, fan=FAN_A):
        """Inbound -> staged draft -> the named operator approves -> real dispatch. Returns (staged result, inbound id)."""
        staged, mid = self.say(text, fan)
        self.approve(staged)
        self.unthrottle()
        self.pilot.tick()
        return staged, mid

    # ------------------------------------------------------------------ reads
    def rows(self, model, *where):
        with Session(self.database[1]) as s:
            rows = list(s.scalars(select(model).where(model.account_id == self.account, *where)))
            s.expunge_all()
            return rows

    def approvals(self, fan=FAN_A):
        from db.onlyfans_engine_models import OfEngineApproval
        return sorted(self.rows(OfEngineApproval, OfEngineApproval.fan_id == fan), key=lambda a: (a.generation, a.version))

    def approve_pending(self, fan=FAN_A):
        """Approve the fan's current awaiting version (the named operator's explicit act)."""
        live = [a for a in self.approvals(fan) if a.status == 'awaiting']
        assert len(live) == 1, [(a.generation, a.version, a.status, a.invalidation_reason) for a in self.approvals(fan)]
        a = live[0]
        return approval.approve(self.store, TENANT, self.account, a.request_id, a.generation, expected_version=a.version, actor=OPERATOR)

    def posts(self):
        return [p['body'] for p in self.fans_mock.posts]

    def state(self, fan=FAN_A):
        with Session(self.database[1]) as s:
            st = s.get(OfConversationState, (self.account, fan))
            return st.mode, st.takeover_state

    def message(self, mid, fan=FAN_A):
        with Session(self.database[1]) as s:
            row = s.get(Message, (self.account, fan, mid))
            return None if row is None else (row.direction, row.body)

    def evidence(self, kind, fan=None):
        where = [OfFansEvidence.kind == kind] + ([OfFansEvidence.fan_id == fan] if fan is not None else [])
        return self.rows(OfFansEvidence, *where)

    def projections(self):
        return {p.event_key: (p.status, p.detail) for p in self.rows(OfFansProjection)}

    def block(self, fan):
        with Session(self.database[1]) as s:
            runtime = self.store._runtime(s, TENANT, self.account, lock=False)
            return pilot_scope.delivery_block(s, runtime, fan)


def writer(world):
    """The fake model. Thanks for a tip only when the application's snapshot established one; otherwise plain chat."""
    seen = world.model_prompts

    def respond(request):
        data = json.loads(request.content)
        prompt = data['messages'][-1]['content']
        seen.append(prompt)
        latest = prompt.split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0] if 'LATEST:\nFAN: ' in prompt else ''
        proposal = {'body': TIP_ACK} if 'VERIFIED TIP' in prompt else {'body': 'I hear you, tell me more.'}
        if world.choose is not None:
            proposal = world.choose(latest, prompt, proposal)
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [
            {'message': {'content': json.dumps(proposal)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})
    return httpx.MockTransport(respond)


def build_world(database, tmp_path, monkeypatch, *, i=1, fans=(FAN_A, FAN_B), choose=None):
    """A prepared account with complete per-fan history for `fans`, and fake model/provider transports. Not yet launched."""
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    store, account, cfg = history_prepared(database, i, requests=1200, credits=2400)
    for fan in fans:
        add_fan(database[1], account, fan)
    wire = Provider(cfg)
    for fan in fans:
        wire.chats[fan] = [chat_page([])]
    read_everything(store, database, account, wire, tuple(fans))
    recover(store, account, tuple(fans))          # the text-only launch also needs the per-fan recovery the legacy path records
    unblock(database[1], account, cfg['creator_id'])
    w = World()
    w.index = i
    w.database, w.store, w.account, w.cfg, w.fans = database, store, account, cfg, tuple(fans)
    w.provider, w.creator = cfg['provider_account'], cfg['creator_id']
    folder = tmp_path / f'acct{i}'
    folder.mkdir()
    secret = folder / 'secret'
    secret.write_bytes(SECRET)
    secret.chmod(0o600)
    creds = folder / 'fans-creds'
    creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=' + w.provider + '\n')
    creds.chmod(0o600)
    w.profiles_path = folder / 'profiles.json'
    w.profiles_path.write_text('{}')
    w.env = {'TWIN_PLAN_C_SUPERVISED': '1', 'TWIN_PLAN_C_DRAFT_ONLY': '0', 'TWIN_PLAN_C_TENANT': TENANT, 'TWIN_PLAN_C_ACCOUNT': account,
             'TWIN_PLAN_C_WEBHOOK_SECRET_FILE': str(secret), 'ONLYFANS_PROCESS_ROLE': 'worker', 'TWIN_ENGINE_FANSAPI_REAL': '1',
             'TWIN_ENGINE_FANSAPI_ENV_FILE': str(creds), 'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': 'm/primary',
             'ENGINE_MODEL_UPSTREAM': 'fixture-provider', 'ENGINE_MODEL_UPSTREAM_REPORTED_NAME': 'Fixture Provider',
             'ENGINE_MODEL_ALLOWED': 'm/primary', 'ENGINE_MODEL_ACCOUNTS': account, 'ENGINE_MODEL_KEY_ENV': 'FIXTURE_KEY',
             'FIXTURE_KEY': 'fixture-not-a-real-key', 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '1000000',
             'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '2000000', 'ENGINE_MODEL_DAILY_CAP_MICROUSD': '1000000',
             'ENGINE_MODEL_CREATOR_CAP_MICROUSD': '1000000', 'ENGINE_MODEL_PROFILES_FILE': str(w.profiles_path)}
    w.fans_mock = CoexistenceFans(w.provider, w.creator)
    w.choose = choose
    w.model_transport = writer(w)
    return w
