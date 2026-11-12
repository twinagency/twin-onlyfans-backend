"""Shared world for the Step 4 sale / unlock / continuation suites: a COMMERCIAL account driven through the real worker path.

Nothing here seeds a per-fan grant, forces backfill_complete, fakes ownership coverage or opens a guard. The account is built the
way production builds it: preparation (read-only import through the guarded fake provider) -> agency promotion to commercial.v1
with an approved catalogue snapshot -> operator launch -> signed webhooks -> the real supervised scheduler/worker with a fake
model transport and a fake FansAPI transport. Synthetic identities only.
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

from commercial_terms_fixture import ACTOR as AGENCY_ACTOR, add_item, add_vault, approve
from history_support import Provider, chat_page, prepared as history_prepared
from test_commercial_scope_lifecycle_pg import add_fan, unblock
from test_commercial_preparation_pg import TENANT
from test_engine_real_worker_pg import MockFans, answer, iso
from test_history_coverage_pg import read_everything
from conversation_engine import approval, commercial_lifecycle as cs, pilot_scope
from conversation_engine.model_config import ModelConfig
from conversation_engine.writer_context import CreatorProfile, profile_data, set_active_profiles
from db.models import CatalogItem
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import TextAuthority

SECRET = b'commercial-worker-secret'
OPERATOR = 'op-session:' + 'a' * 64
FAN_A, FAN_B, FAN_C, FAN_D = 401, 402, 403, 404
HASH_KIND = 'vault-metadata-v1'
PERMISSIONS = ['configure', 'activate', 'approve', 'reject', 'edit', 'stop', 'takeover', 'handback', 'reconcile', 'correct', 'read']


def key(n):
    return f'{n:064x}'


class CommercialFans(MockFans):
    """The fake provider: identity for THIS account, records every POST, replays by idempotency key."""
    def __init__(self, provider, creator):
        super().__init__()
        self.provider, self.creator = provider, creator

    def handle(self, request):
        if request.method == 'GET' and request.url.path == '/api/accounts':
            return answer(200, [{'id': self.provider, 'onlyfans_id': self.creator, 'is_authenticated': True}])
        return super().handle(request)


class World:
    """One commercial account. `product(...)` adds approved products; `say`/`approve_turn`/`tick` drive the real worker."""
    def __init__(self):
        self.model_prompts, self.mid = [], 1000
        self.choose = None

    # ---------------------------------------------------------------- catalogue
    def product(self, label, media, *, previews=(), standard=1200, discount=None, description=None, kinds=None):
        """media: ids (photos unless `kinds` {id: 'video'}). Written through the production terms writer; returns the item id."""
        kinds = kinds or {}
        add_vault(self.database[1], {m: kinds.get(m, 'image') for m in media}, account=self.account, hash_kind=HASH_KIND)
        item = add_item(self.database[1], label=label, media=list(media), previews=list(previews), price=standard,
                        description=description or f'{label}, an approved description.', account=self.account)
        approve(self.store, item, standard=standard, discount=discount, tenant=TENANT, account=self.account)
        return item

    # ---------------------------------------------------------------- scope
    def launch(self, *, valid_for=timedelta(days=1), authorization_mode='supervised', after_snapshot=None, facts=(), limits=None):
        env = self.env
        profile = CreatorProfile(name='Fixture creator', limits=('no meetups',), role_statement='the creator herself',
                                 offers_permitted=True, approved_item_ids=(), facts=tuple(facts))
        self.profile = profile_data(profile)
        self.profiles_path.write_text(json.dumps({self.account: self.profile}))
        valid_until = (utcnow() + valid_for).replace(microsecond=0)
        snapshot = cs.catalogue_snapshot(self.store, TENANT, self.account, evidence_ref='vault:agency-catalogue-1',
                                         valid_until=valid_until)
        if after_snapshot is not None:
            after_snapshot(self)                       # a catalogue change AFTER the agency's approval
        body = {'fans': list(self.fans), 'creator_profile': self.profile, 'delivery_mode': 'full', 'catalogue': snapshot,
                'operator': {'actor': OPERATOR, 'name': 'Fixture operator', 'permissions': PERMISSIONS},
                'model': pilot_scope.model_spec(ModelConfig.from_env(env)),
                'limits': {'send_units': 200, 'daily_send_units': 100, 'throttle_seconds': 1, 'provider_requests': 4000,
                           'provider_credit_budget': 8000, 'provider_credits_per_request': 2}}
        if limits:
            body['limits'].update(limits)
        self.snapshot = snapshot
        promoted = cs.promote(self.store, TENANT, self.account, body, expected_revision=1, command_key=key(0x7001),
                              actor=AGENCY_ACTOR, reason='Promote prepared account to a commercial scope')
        revision = promoted['revision']
        if authorization_mode == 'autonomous':
            config = pilot_scope.describe(self.store, TENANT, self.account)['config']
            done = pilot_scope.configure(self.store, TENANT, self.account, {**config, 'authorization_mode': 'autonomous'},
                                         expected_revision=revision, command_key=key(0x7002), actor=OPERATOR,
                                         reason='Operator selects autonomous replies')
            revision = done['revision']
        with Session(self.database[1], expire_on_commit=False) as s, s.begin():
            runtime = s.get(OfRuntime, self.account)
            # Explicit fixture evidence of a completed recent synchronization, exactly as the legacy suites state it
            # (`test_onlyfans_text_delivery_pg.current`). It is not coverage and grants nothing.
            runtime.enabled, runtime.recovery_needed, runtime.last_sync_at = True, False, utcnow()
            policy = s.get(OfDeliveryPolicy, self.account)
            policy.daily_limit_units = body['limits']['daily_send_units']
            policy_revision, throttle = policy.revision, policy.throttle_seconds
        TextAuthority(self.store).configure(TENANT, self.account, expected_revision=policy_revision, enabled=True, limit_units=body['limits']['send_units'],
            throttle_seconds=throttle, media_enabled=True, actor=OPERATOR, reason='Operator opens the delivery policy')
        launched = pilot_scope.set_status(self.store, TENANT, self.account, 'active', expected_revision=revision,
                                          command_key=key(0x7003), actor=OPERATOR, reason='Operator launch decision')
        self.scope_revision = launched['revision']
        self.pilot = build_supervised_pilot(self.store, env, model_transport=self.model_transport, fans_transport=self.fans_mock.transport)
        return launched

    # ---------------------------------------------------------------- webhooks
    def inbound(self, mid, text, fan=FAN_A, seconds=0):
        return {'event': 'messages.received', 'account_id': self.provider, 'payload': {
            'id': mid, 'fromUser': {'id': fan}, 'text': text, 'createdAt': iso(seconds), 'isSentByMe': False, 'media': [], 'price': 0}}

    def unlock_event(self, message, amount, fan=FAN_A, seconds=2, **changes):
        payload = {'id': 'n-' + str(message), 'message_id': str(message), 'chat_id': str(fan), 'user_id': self.creator,
                   'amountGross': amount, 'createdAt': iso(seconds), 'fan': {'id': str(fan)}}
        payload.update(changes)
        return {'event': 'messages.ppv.unlocked', 'account_id': self.provider, 'payload': payload}

    def signed(self, body, event_key):
        raw = json.dumps(body, separators=(',', ':')).encode()
        return raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), event_key

    def deliver(self, body, event_key=None):
        raw, signature, event_key = self.signed(body, event_key or 'evt-' + hashlib.sha256(json.dumps(body).encode()).hexdigest()[:20])
        return self.pilot.handle(raw, signature, event_key)

    def say(self, text, fan=FAN_A, event_key=None):
        self.mid += 1
        return self.deliver(self.inbound(self.mid, text, fan), event_key), self.mid

    # ---------------------------------------------------------------- operator
    def approve_turn(self, result):
        assert result.get('outcome') == 'awaiting_approval', result
        info = result['approval']
        return approval.approve(self.store, TENANT, self.account, info['requestId'], info['generation'],
                                expected_version=info['version'], actor=OPERATOR)

    def converse(self, text, fan=FAN_A):
        """One inbound message -> staged draft -> named operator approves -> real dispatch. Returns (staged result, tick result)."""
        staged, mid = self.say(text, fan)
        if staged.get('outcome') != 'awaiting_approval':
            return staged, None
        self.approve_turn(staged)
        self.unthrottle()
        return staged, self.pilot.tick()

    def unthrottle(self):
        with self.database[1].begin() as c:
            c.exec_driver_sql("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")

    # ---------------------------------------------------------------- reads
    def cards(self):
        return [p for p in self.fans_mock.posts if p['body'].get('price', 0) > 0]

    def rows(self, model, *where):
        with Session(self.database[1]) as s:
            rows = list(s.scalars(select(model).where(model.account_id == self.account, *where)))
            s.expunge_all()
            return rows


PLAIN = ['Tell me more about that.', 'Happy to hear it.', 'Honestly, that made my day.', 'Sure thing, what else?', 'Okay, noted.',
         'Alright then, go on.', 'Well, that sounds fun.', 'Hmm, interesting, go on.', 'Ha, fair enough.', 'Right, and then what?']


def writer(world, *, choose=None):
    """The fake model. It follows the prompt: offers the APPROVED OFFER only when the fan's words ask for something."""
    seen = world.model_prompts

    def respond(request):
        data = json.loads(request.content)
        prompt = '\n\n'.join(message['content'] for message in data['messages'])
        seen.append(prompt)
        approved = re.search(r'APPROVED OFFER: id ([0-9a-f-]{36}),', prompt)
        latest = prompt.split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0] if 'LATEST:\nFAN: ' in prompt else ''
        asks = re.search(r'\b(show|send|want|buy|unlock|see)\b', latest.lower()) is not None
        # A particular selection answering the preceding product clarification
        # follows the application's already-authorized offer grant.
        asks = asks or bool(approved and re.search(r'\b(photo|video|bundle|set)\b', latest.lower())
                            and not latest.rstrip().endswith('?'))
        if 'PRODUCT SELECTION REQUIRED:' in prompt:
            proposal = {'body': 'Which particular photo or video would you like to see?'}
        elif approved and asks:
            proposal = {'body': 'Here you go.', 'action': 'offer', 'offer_grant_id': approved[1], 'pitch': True}
        elif 'where are you from' in latest.lower() and 'Brisbane' in ' '.join(m['content'] for m in data['messages']):
            proposal = {'body': 'I am from Brisbane.'}
        elif latest.lower().strip() == 'no thanks':
            proposal = {'body': 'Of course, we can just chat.', 'sales_signal': 'decline'}
        else:
            proposal = {'body': PLAIN[len(seen) % len(PLAIN)]}                    # varied openers: the repeated-opener rule is real
        decide = choose or world.choose
        if decide is not None:
            proposal = decide(latest, prompt, approved[1] if approved else None, proposal)
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [
            {'message': {'content': json.dumps(proposal)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})
    return httpx.MockTransport(respond)


def build_world(database, tmp_path, monkeypatch, *, i=1, fans=(FAN_A, FAN_B, FAN_C, FAN_D), choose=None, read=None, folder=None, history=None):
    """A prepared account with complete per-fan history for `fans`, and the fake model/provider transports. Not yet launched."""
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    store, account, cfg = history_prepared(database, i, requests=1200, credits=2400)
    for fan in fans:
        add_fan(database[1], account, fan)
    wire = Provider(cfg)
    scripted = history(cfg) if history is not None else {}           # optional imported history per fan (older cards, rows)
    for fan in fans:
        wire.chats[fan] = [chat_page(list(scripted.get(fan, [])))]
    read_everything(store, database, account, wire, tuple(fans if read is None else read))   # `read`: only these fans' history is imported
    unblock(database[1], account, cfg['creator_id'])
    w = World()
    w.database, w.store, w.account, w.cfg, w.fans = database, store, account, cfg, tuple(fans)
    w.provider, w.creator = cfg['provider_account'], cfg['creator_id']
    tmp_path = tmp_path if folder is None else folder            # one folder per account when several accounts share a test
    secret = tmp_path / 'secret'
    secret.write_bytes(SECRET)
    secret.chmod(0o600)
    creds = tmp_path / 'fans-creds'
    creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=' + w.provider + '\n')
    creds.chmod(0o600)
    w.profiles_path = tmp_path / 'profiles.json'
    w.profiles_path.write_text('{}')
    w.env = {'TWIN_PLAN_C_SUPERVISED': '1', 'TWIN_PLAN_C_DRAFT_ONLY': '0', 'TWIN_PLAN_C_TENANT': TENANT, 'TWIN_PLAN_C_ACCOUNT': account,
             'TWIN_PLAN_C_WEBHOOK_SECRET_FILE': str(secret), 'ONLYFANS_PROCESS_ROLE': 'worker', 'TWIN_ENGINE_FANSAPI_REAL': '1',
             'TWIN_ENGINE_FANSAPI_ENV_FILE': str(creds), 'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': 'm/primary',
             'ENGINE_MODEL_UPSTREAM': 'fixture-provider', 'ENGINE_MODEL_UPSTREAM_REPORTED_NAME': 'Fixture Provider',
             'ENGINE_MODEL_ALLOWED': 'm/primary', 'ENGINE_MODEL_ACCOUNTS': account, 'ENGINE_MODEL_KEY_ENV': 'FIXTURE_KEY',
             'FIXTURE_KEY': 'fixture-not-a-real-key', 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '1000000',
             'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '2000000', 'ENGINE_MODEL_DAILY_CAP_MICROUSD': '1000000',
             'ENGINE_MODEL_CREATOR_CAP_MICROUSD': '1000000', 'ENGINE_MODEL_PROFILES_FILE': str(w.profiles_path)}
    w.fans_mock = CommercialFans(w.provider, w.creator)
    w.choose = choose
    w.model_transport = writer(w)
    return w


@pytest.fixture
def world(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch)
    yield w
    set_active_profiles(None)
    w.store.close()
