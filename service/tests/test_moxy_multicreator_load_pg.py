"""Concurrent multi-creator Moxy load on PostgreSQL. Synthetic only.

Three Moxy creators (Amelia, Cristina and a third creator added by configuration
alone) with tens of fans each run through the real MoxyService + TextAuthority at
the same time. The model is a deterministic stub (a pure function of the prompt
snapshot) and the transport is SyntheticTextTransport: no real model, provider,
customer message, or spend. Mixed events: ordinary chat, direct buy, invitation
then acceptance, refusal and hardship (the stub writer deliberately tries to
sell anyway), a new inbound arriving mid-generation, takeovers (before the event,
mid-generation, and between queue and dispatch), a purchase webhook, duplicate
and racing duplicate purchase webhooks. Legacy automation jobs run alongside for
the Moxy accounts and for one non-Moxy account.

FANS per creator comes from MOXY_LOAD_FANS (default 40).
"""
import asyncio
import itertools
import json
import os
import re
import sqlite3  # noqa: F401  (aiosqlite backend for the legacy job tables)
import threading
import time
from copy import deepcopy
from datetime import timezone
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_onlyfans_foundation_pg import database, seed, event  # noqa: E402
from test_onlyfans_text_delivery_pg import current  # noqa: E402
from db.models import (AccountAiConfig, CatalogItem, ContentOffer, Fan, GrokCall, Message,  # noqa: E402
                       OfConversationState, Transaction, VaultItem, AutomationRule, AutomationRun, ScheduledJob)
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent  # noqa: E402
from db.onlyfans_moxy_models import MoxyAccount, MoxyEvent  # noqa: E402
from moxy_replacement.configuration import digest, load  # noqa: E402
from moxy_replacement.runtime import MoxyService  # noqa: E402
from onlyfans_runtime.commerce import CommerceIntake, SyntheticCommerceEvent  # noqa: E402
from onlyfans_runtime.native_planning import SyntheticNativeModel  # noqa: E402
from onlyfans_runtime.store import Store, utcnow  # noqa: E402
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority  # noqa: E402

N_FANS = int(os.environ.get('MOXY_LOAD_FANS', '40'))
STEP_LIMIT = int(os.environ.get('MOXY_STEP_LIMIT', '2'))
RACERS = int(os.environ.get('MOXY_RACERS', '4'))
POOL = int(os.environ.get('MOXY_LOAD_POOL', '16'))
FAN0 = 5000                 # same fan ids in every creator: cross-account mix-ups cannot hide behind unique ids
SEQ = 'fixture_level'
NAMES = ['first', 'second', 'third', 'fourth']
FREE_ACCOUNT, FREE_TENANT = 'of-100000099', 'tenant-free'


class Creator:
    def __init__(self, key, tag, account, tenant, price0, media0):
        self.key, self.tag, self.account, self.tenant = key, tag, account, tenant
        self.prices = {n: price0 + 100 * i for i, n in enumerate(NAMES)}
        self.media = {n: media0 + i for i, n in enumerate(NAMES)}
        self.fans = [FAN0 + i for i in range(N_FANS)]
        self.items = {}      # product -> catalog item id
        self.settings = None


CREATORS = [Creator('amelia', 'AM', 'of-100000001', 'tenant-amelia', 8100, 71000),
            Creator('cristina', 'CR', 'of-100000002', 'tenant-cristina', 7100, 81000),
            Creator('synthetic_third', 'TH', 'of-100000003', 'tenant-third', 9100, 91000)]
BY_ACCOUNT = {c.account: c for c in CREATORS}

# scenario of fan index i, in blocks of 20
def scenario(i):
    k = i % 20
    return ('chat' if k < 7 else 'direct' if k < 10 else 'invite' if k < 13 else 'refuse' if k == 13
            else 'hardship' if k == 14 else 'midgen' if k == 15 else 'takeover_pre' if k == 16
            else 'takeover_mid' if k == 17 else 'takeover_queue' if k == 18 else 'chat')

ROUND1 = {'chat': 'How was your day? I just got back from the gym',
          'direct': 'Can I buy the first outfit photo please?',
          'invite': 'Anything fun to see today?',
          'refuse': 'I am really not interested in buying anything',
          'hardship': 'I am broke this month, cannot spend a cent',
          'midgen': 'What are you up to tonight?',
          'takeover_pre': 'hey you', 'takeover_mid': 'hello there', 'takeover_queue': 'good morning'}
MIDGEN_INJECT = 'Actually one more thing, are you free later?'
ROUND2 = {'chat': 'Haha love that', 'invite': 'Yes please send that one',
          'refuse': "ok what's new", 'hardship': "ok what's new", 'takeover_pre': 'still there?'}
# a purchase is injected for these direct-buy fans; a second, racing, duplicate for the middle one
def purchase_kind(i):
    return {7: 'dup_seq', 8: 'dup_race'}.get(i % 20)


def make_doc(base):
    d = deepcopy(base)
    d['creator_key'] = 'synthetic_third'
    d['unresolved'] = []
    return d


def classify(text):
    for needle, name in [(MIDGEN_INJECT, 'chat'), ('buy the first', 'direct'), ('Anything fun', 'invite'),
                         ('not interested in buying', 'refuse'), ('broke this month', 'hardship'),
                         ("ok what's new", 'naive'), ('Yes please send that one', 'accept'),
                         ('What are you up to', 'midgen'), ('hello there', 'takeover_mid')]:
        if needle in text:
            return name
    return 'chat'


class Ctx:
    def __init__(self, engine, store, service, authority):
        self.engine, self.store, self.service, self.authority = engine, store, service, authority
        self.lock = threading.Lock()
        self.errors, self.violations, self.model_calls, self.accept_no_inv = [], [], [], []
        self.wires, self.manual_wires, self.results = [], [], {}
        self.mids, self.provider_ids = {}, {}
        self._pid = itertools.count(1_000_000)
        self.flip_pending, self.flip_lock = set(), threading.Lock()
        self.stop = threading.Event()
        self.webhooks_done = threading.Event()
        self.fence_log = []

    def next_mid(self, account, fan):
        with self.lock:
            self.mids[(account, fan)] = self.mids.get((account, fan), 0) + 1
            return self.mids[(account, fan)]

    def pid(self, account, intent):
        with self.lock:
            if (account, intent) not in self.provider_ids:
                self.provider_ids[(account, intent)] = next(self._pid)
            return self.provider_ids[(account, intent)]

    def inbound(self, c, fan, body):
        self.store.record(c.tenant, c.account, event(mid=self.next_mid(c.account, fan), fan=fan, body=body))

    def set_mode(self, account, fan, mode):
        with Session(self.engine) as s, s.begin():
            s.get(OfConversationState, (account, fan)).mode = mode


def decision(action='reply', text='', product_id=None, sequence=None, flags=(), **extra):
    return {'action': action, 'text': text, 'product_id': product_id, 'sequence': sequence, 'flags': list(flags),
            'reason': 'Deterministic load fixture; not a model-quality result.', 'permission': None,
            'question': None, 'resolve_obligation': None, **extra}


def make_stub(ctx, c):
    """Deterministic writer. A pure function of the prompt snapshot (plus the two
    side effects the scenarios call for: a mid-generation inbound and a takeover)."""
    m = SyntheticNativeModel(reported_cost_millicents=1)
    others = [o.tag for o in CREATORS if o is not c]

    async def generate(messages):
        m.completed = False
        snap = json.loads(messages[1]['content'])
        fan = snap['fan_id']
        blob = json.dumps(messages)
        # prompt isolation: nothing of another creator may be in this creator's prompt
        for t in others:
            if 'Synth' + t in blob:
                ctx.violations.append(('prompt_has_other_creator_token', c.tag, fan, t))
        for key, terms in snap['inventory'].items():
            if terms['price_cents'] != c.prices[key] or terms['media_ids'] != [c.media[key]] \
                    or terms['item']['account_id'] != c.account or terms['item']['id'] != c.items[key]:
                ctx.violations.append(('prompt_inventory_not_own', c.tag, fan, key))
        mark = ' (%s-%d)' % (c.tag, fan)
        kind = snap['event']['kind']
        incoming = [h for h in snap['history'] if h['direction'] == 'in']
        last = incoming[-1]['text'] if incoming else ''
        scn = classify(last)
        with ctx.lock:
            ctx.model_calls.append((c.account, fan, kind, scn))
        if kind == 'purchase':
            bought = set(snap['fan_state']['purchased_products'])
            nxt = next((p for p in snap['sequences'][SEQ] if p not in bought), None)
            d = decision('continue', 'Thank you! Here is the next one for you' + mark, nxt, SEQ)
        elif kind in ('refund', 'chargeback'):
            d = decision('none')
        elif scn == 'direct':
            d = decision('offer', 'Sure, here is the outfit photo you asked for' + mark, 'first', SEQ,
                         request_kind='direct', request_quote=last)
        elif scn == 'invite':
            d = decision('invite', 'Want to see a sports outfit photo?' + mark, 'first', SEQ)
        elif scn == 'accept':
            inv = snap.get('invitation')
            if not inv:
                ctx.accept_no_inv.append((c.account, c.tag, fan))
                d = decision('reply', 'Tell me more' + mark)
            else:
                d = decision('offer', 'Here it is' + mark, inv['product_id'], inv['sequence'],
                             request_kind='accept', request_quote=last)
        elif scn == 'refuse':       # adversarial: flags the refusal but tries to sell anyway
            d = decision('offer', 'You will love this one' + mark, 'first', SEQ, flags=['refusal'])
        elif scn == 'hardship':
            d = decision('offer', 'This one is worth it' + mark, 'first', SEQ, flags=['hardship'])
        elif scn == 'naive':        # adversarial: ignores the retained refusal/hardship state
            d = decision('offer', 'Look what I have for you' + mark, 'first', SEQ)
        else:
            if scn == 'midgen':
                ctx.store.record(c.tenant, c.account, event(mid=ctx.next_mid(c.account, fan), fan=fan, body=MIDGEN_INJECT))
                ctx.store.drain(c.tenant, c.account)
            elif scn == 'takeover_mid':
                ctx.set_mode(c.account, fan, 'human_controlled')
            d = decision('reply', 'Aw that sounds lovely, tell me more' + mark)
        m.completed = True
        return json.dumps(d)

    m.generate = generate
    m.fork = lambda: make_stub(ctx, c)
    return m


# ---------------------------------------------------------------- setup

def build_creator(ctx, url, engine, c, base_docs):
    store = ctx.store
    seed(url, engine, account=c.account, tenant=c.tenant, fan=c.fans[0]).close()
    current(engine, mode='automated', account=c.account, fan=c.fans[0])
    ctx.authority.configure(c.tenant, c.account, expected_revision=0, enabled=True, limit_units=2000,
                            throttle_seconds=1, actor='load-operator', reason='Offline load acceptance')
    doc = make_doc(base_docs['amelia']) if c.key == 'synthetic_third' else None
    cfg = load(c.key) if doc is None else None
    creator_doc = doc if doc is not None else cfg.creator
    products, bindings = [], {}
    with Session(engine) as s, s.begin():
        s.add(AccountAiConfig(account_id=c.account, model='grok-4-1-fast-non-reasoning',
                              persona='Synthetic Creator enjoys hiking and coffee.',
                              persona_facts_json='{"name":"Synthetic Creator","location":"Brisbane"}'))
        p = s.get(OfDeliveryPolicy, c.account)
        p.media_enabled = True
        p.limit_units = 5000
        p.delivery_mode = 'concurrent'
        p.provider_requests_per_second = 20
        p.provider_burst = 4
        p.provider_max_concurrent = 4
        for name in NAMES:
            now = utcnow()
            mid = c.media[name]
            desc = 'Synthetic fixture photograph Synth%s %s, no live/custom claim.' % (c.tag, name)
            v = VaultItem(account_id=c.account, media_id=mid, kind='photo', description=desc, describe_status='described',
                          describe_generated_at=now, content_hash='fixture-%s-%s' % (c.tag, name), created_at=now, updated_at=now)
            s.add(v)
            item = CatalogItem(account_id=c.account, kind='image_set', label='Synth%s %s' % (c.tag, name),
                               description_for_ai=desc, media_ids=json.dumps([mid]), preview_media_ids='[]',
                               price_cents=c.prices[name], pricing_mode='fixed', enabled=True, is_free_teaser=False,
                               created_at=now, updated_at=now)
            s.add(item)
            s.flush()
            ref = 'asset:' + digest({'media_id': mid, 'content_hash': v.content_hash, 'description': desc,
                                     'updated_at': now.isoformat()})
            products.append({'id': name, 'revision': 1, 'category': 'nude_booty', 'price_cents': c.prices[name],
                             'currency': 'USD', 'assets': [{'id': mid, 'revision': 1, 'facts': [desc], 'evidence_ref': ref}],
                             'source': {'document': '03_Cristina.pdf' if c.key == 'cristina' else '01_Amelia.pdf', 'pages': [2]},
                             'approval_ref': 'fixture:approved-' + name, 'available': True, 'requires': {},
                             'caption_instructions': 'Accurate outfit photograph; no unsupported promise.'})
            bindings[name] = item.id
        # many fans (first fan was seeded)
        s.add_all([Fan(account_id=c.account, fan_id=f) for f in c.fans[1:]])
        s.flush()
        s.add_all([OfConversationState(account_id=c.account, fan_id=f, mode='automated', revision=0,
                                       input_revision=0, takeover_state='none') for f in c.fans[1:]])
    c.items = bindings
    settings = {'creator_key': c.key, 'products': products, 'catalogue_bindings': bindings,
                'sequences': {SEQ: list(NAMES)}, 'sequence_levels': {SEQ: [list(NAMES)]},
                'operational': {f: 'fixture-authority' for f in creator_doc['required_operational_fields']},
                'public_facts': {'hobby': 'gym; approved synthetic fixture fact'},
                'coordination_verified': True, 'fixture': True,
                'approved_remedies': {'first': {'complimentary': True, 'approval_ref': 'fixture:recovery'}}}
    settings['operational']['assigned_operator'] = 'fixture-operator'
    if doc is not None:
        settings['creator_document'] = doc
    c.settings = settings
    ctx.service.configure(c.tenant, c.account, expected_revision=0, mode='on', settings=settings, actor='fixture-operator')
    for f in c.fans:
        ctx.service.fan_facts(c.tenant, c.account, f, {'ownership_verified': True, 'purchase_links_verified': True,
                              'vip': True, 'spender': True, 'high_spender': True},
                              actor='fixture-operator', evidence_ref='fixture:fan-authority')


def add_event(ctx, c, fan, key, kind='turn'):
    return ctx.service.add_event(c.tenant, c.account, fan, kind, key, {})


# ---------------------------------------------------------------- drivers

def stepper(ctx, c):
    while not ctx.stop.is_set():
        try:
            n = ctx.service.step(c.tenant, c.account, make_stub(ctx, c), limit=STEP_LIMIT)
        except Exception as exc:  # noqa: BLE001 - recorded, asserted empty
            ctx.errors.append((c.tag, 'step', repr(exc)[:300]))
            n = 0
        if not n:
            time.sleep(0.1)


def dispatcher(ctx, c, reverse):
    while not ctx.stop.is_set():
        with Session(ctx.engine) as s:
            queued = [(r.intent_id, r.fan_id) for r in s.scalars(select(OfTextIntent).where(
                OfTextIntent.account_id == c.account, OfTextIntent.status == 'queued').order_by(OfTextIntent.created_at))]
        if reverse:
            queued.reverse()
        if not queued:
            time.sleep(0.05)
            continue
        for intent, fan in queued:
            with ctx.flip_lock:
                if (c.account, fan) in ctx.flip_pending:      # takeover between queue and dispatch
                    ctx.set_mode(c.account, fan, 'human_controlled')
                    ctx.flip_pending.discard((c.account, fan))
            wire = SyntheticTextTransport(c.account, message_id=ctx.pid(c.account, intent))
            try:
                res = ctx.authority.dispatch(c.tenant, c.account, intent, wire)
            except Exception as exc:  # noqa: BLE001
                ctx.errors.append((c.tag, 'dispatch', repr(exc)[:300]))
                res = 'exception'
            with ctx.lock:
                if wire.calls:
                    ctx.wires.append((c.account, wire))
                ctx.results.setdefault((c.account, intent), []).append(res)
        time.sleep(0.02)


def nonterminal(engine):
    with Session(engine) as s:
        ev = s.scalar(select(func.count()).select_from(MoxyEvent).where(
            MoxyEvent.status.in_(('pending', 'generating', 'ready', 'queued'))))
        it = s.scalar(select(func.count()).select_from(OfTextIntent).where(
            OfTextIntent.status.in_(('queued', 'dispatching'))))
    return ev, it


def dump_state(ctx):
    with Session(ctx.engine) as s:
        ev = {}
        for e in s.scalars(select(MoxyEvent)):
            k = (e.account_id[-1], e.kind, e.status, e.reason)
            ev[k] = ev.get(k, 0) + 1
        it = {}
        for i in s.scalars(select(OfTextIntent)):
            k = (i.account_id[-1], i.status, i.reason)
            it[k] = it.get(k, 0) + 1
    print('\nDUMP events', sorted(ev.items(), key=str))
    print('DUMP intents', sorted(it.items(), key=str))
    print('DUMP errors', ctx.errors[:10])
    print('DUMP results', {k[1][:6]: v for k, v in list(ctx.results.items())[:10]})


def drive(ctx, extra_threads=(), timeout=int(os.environ.get('MOXY_LOAD_PHASE_TIMEOUT', '150')), done=lambda: True):
    ctx.stop.clear()
    threads = []
    for c in CREATORS:
        threads.append(threading.Thread(target=stepper, args=(ctx, c), daemon=True))
        threads += [threading.Thread(target=dispatcher, args=(ctx, c, r), daemon=True) for r in (False, True)]
    threads += list(extra_threads)
    t0 = time.monotonic()
    for t in threads:
        t.start()
    settled = 0
    while time.monotonic() - t0 < timeout:
        time.sleep(0.4)
        if done() and nonterminal(ctx.engine) == (0, 0):
            settled += 1
            if settled >= 3:          # stable for ~1s: nothing re-appears
                break
        else:
            settled = 0
    elapsed = time.monotonic() - t0
    timed_out = elapsed >= timeout
    if timed_out:
        dump_state(ctx)
    ctx.stop.set()
    for t in threads:
        t.join(timeout=30)
    return elapsed, timed_out


# ---------------------------------------------------------------- legacy fence

def fence_harness(tmp_path, monkeypatch):
    import automation_executor as ax
    from contextlib import asynccontextmanager
    path = tmp_path / 'legacy_jobs.db'
    sync = create_engine('sqlite:///%s' % path)
    for model in (AutomationRun, ScheduledJob, AutomationRule):
        model.__table__.create(sync)
    sync.dispose()
    eng = create_async_engine('sqlite+aiosqlite:///%s' % path)
    factory = async_sessionmaker(eng, expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as s:
            yield s
            await s.commit()

    class Probe:
        def __init__(self):
            self.calls = []

        async def __call__(self, account_id, payload, *, run_id):
            self.calls.append((account_id, payload))
            return {'sent': 1}

    probe = Probe()

    async def no_dead(_a):
        return False
    monkeypatch.setattr(ax, 'get_session', get_session)
    monkeypatch.setattr(ax, 'get_automation', lambda kind: probe)
    monkeypatch.setattr(ax.account_health, 'is_session_dead', no_dead)
    ax._MOXY_FENCE_RECORDED.clear()
    return ax, probe, eng, 'sqlite:///%s' % path


def fence_worker(ctx, ax, probe, free_wire_state):
    """Legacy jobs and legacy sends for Moxy accounts and for one non-Moxy account, concurrently with the load."""
    from moxy_replacement import routing
    from moxy_replacement.routing import MOXY_FENCED_KINDS
    from twin_bridge import coordination
    from onlyfans_runtime.store import Refused
    passes = 0
    while not ctx.stop.is_set() and passes < 3:
        passes += 1
        for acct in [c.account for c in CREATORS] + [FREE_ACCOUNT]:
            for kind in sorted(MOXY_FENCED_KINDS):
                try:
                    if passes == 1:
                        asyncio.run(ax.enqueue_job(acct, kind, payload={'fan_id': 7, 'text': 'hi'}))
                        out = asyncio.run(ax.run_once(acct, kind))
                    else:
                        out = asyncio.run(ax.run_once(acct, kind, payload={'fan_id': 7}))
                    ctx.fence_log.append(('job', acct, kind, out.get('status'), out.get('reason')))
                except Exception as exc:  # noqa: BLE001
                    ctx.fence_log.append(('job', acct, kind, 'exception', repr(exc)[:200]))
            for purpose in ('other', 'gated', 'scheduled', 'broadcast'):
                try:
                    with coordination.guarded_send(acct, 55, purpose=purpose):
                        ctx.fence_log.append(('send', acct, purpose, 'ran', None))
                except Refused as exc:
                    ctx.fence_log.append(('send', acct, purpose, 'refused', str(exc)))
                except Exception as exc:  # noqa: BLE001
                    ctx.fence_log.append(('send', acct, purpose, 'exception', repr(exc)[:200]))
            try:
                with coordination.guarded_send(acct, 55, purpose='manual'):
                    ctx.fence_log.append(('send', acct, 'manual', 'ran', None))
            except Exception as exc:  # noqa: BLE001
                ctx.fence_log.append(('send', acct, 'manual', 'exception', repr(exc)[:200]))
        time.sleep(0.3)
    free_wire_state['passes'] = passes


# ---------------------------------------------------------------- the test

def test_three_creators_concurrent_mixed_load(database, monkeypatch, tmp_path):
    url, engine = database
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('DATABASE_URL', url)
    from twin_bridge.store import ensure_runtime_schema
    ensure_runtime_schema(url)
    store = Store(url, pool_size=POOL)
    service = MoxyService(store)
    authority = service.authority
    ctx = Ctx(engine, store, service, authority)
    ax, probe, jobs_engine, _ = fence_harness(tmp_path, monkeypatch)
    results = []   # (assertion, ok, detail)

    def check(name, ok, detail=''):
        results.append((name, bool(ok), str(detail)[:400]))

    base_docs = {'amelia': load('amelia').creator}
    t_setup = time.monotonic()
    for c in CREATORS:
        build_creator(ctx, url, engine, c, base_docs)
    # one non-Moxy account on the same database
    seed(url, engine, account=FREE_ACCOUNT, tenant=FREE_TENANT, fan=9001).close()
    current(engine, mode='human_controlled', account=FREE_ACCOUNT, fan=9001)
    authority.configure(FREE_TENANT, FREE_ACCOUNT, expected_revision=0, enabled=True, limit_units=100,
                        throttle_seconds=1, actor='load-operator', reason='Offline load acceptance')

    # Round 1 inbound, one batch per creator, then events.
    flips = []
    for c in CREATORS:
        for i, f in enumerate(c.fans):
            ctx.inbound(c, f, ROUND1[scenario(i)])
        store.drain(c.tenant, c.account)
        for i, f in enumerate(c.fans):
            if scenario(i) == 'takeover_pre':
                ctx.set_mode(c.account, f, 'human_controlled')
            if scenario(i) == 'takeover_queue':
                flips.append((c.account, f))
    ctx.flip_pending.update(flips)
    ev1 = {}
    for c in CREATORS:
        for i, f in enumerate(c.fans):
            ev1[(c.account, f)] = add_event(ctx, c, f, 'r1-%s-%d' % (scenario(i), f))
    setup_s = time.monotonic() - t_setup
    check('setup: 3 creators x %d fans, %d round-1 events' % (N_FANS, len(ev1)), len(ev1) == 3 * N_FANS)

    free_state = {}
    fence_thread = threading.Thread(target=fence_worker, args=(ctx, ax, probe, free_state), daemon=True)
    t1, to1 = drive(ctx, extra_threads=[fence_thread])
    print('PHASE A %.1fs timed_out=%s' % (t1, to1))
    check('phase A settled within timeout', not to1, 'elapsed %.1fs; nonterminal=%s errors=%s' % (t1, nonterminal(engine), ctx.errors[:3]))

    # Round 2: follow-ups, purchases and duplicate purchase webhooks injected while the engine runs.
    ev2 = {}
    for c in CREATORS:
        for i, f in enumerate(c.fans):
            if scenario(i) in ROUND2:
                ctx.inbound(c, f, ROUND2[scenario(i)])
        store.drain(c.tenant, c.account)
    for c in CREATORS:
        for i, f in enumerate(c.fans):
            if scenario(i) in ROUND2 or scenario(i) == 'midgen':
                ev2[(c.account, f)] = add_event(ctx, c, f, 'r2-%s-%d' % (scenario(i), f))
    webhook = {'results': [], 'errors': []}

    def webhook_worker():
        intake = CommerceIntake(store)
        t0 = time.monotonic()
        racers = []
        for c in CREATORS:
            for i, f in enumerate(c.fans):
                kind = purchase_kind(i)
                if scenario(i) != 'direct' or kind is None:
                    continue
                with Session(engine) as s:
                    it = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == c.account, OfTextIntent.fan_id == f,
                                                             OfTextIntent.status == 'accepted', OfTextIntent.catalog_item_id.is_not(None)))
                if it is None:
                    webhook['errors'].append(('no accepted offer for purchase', c.tag, f))
                    continue
                proof = SyntheticCommerceEvent(c.account, f, it.provider_message_id, 'ev-%s-%d' % (c.tag, f),
                                               'tx-%s-%d' % (c.tag, f), 1, 'purchase', it.price_cents, 'USD',
                                               utcnow().replace(tzinfo=timezone.utc).isoformat(), 'fixture:exact-link')
                r = []
                if kind == 'dup_seq':
                    for _ in range(3):
                        try:
                            r.append(intake.record(c.tenant, c.account, proof))
                        except Exception as exc:  # noqa: BLE001
                            r.append('exc:' + repr(exc)[:120])
                    webhook['results'].append((c.account, f, kind, r))
                else:
                    racers.append((c, f, proof, r))
        # racing duplicates: two threads deliver the same webhook at the same instant
        def deliver(c, proof, r, barrier):
            barrier.wait()
            try:
                r.append(intake.record(c.tenant, c.account, proof))
            except Exception as exc:  # noqa: BLE001
                r.append('exc:' + repr(exc)[:160])
        ts = []
        for c, f, proof, r in racers:
            b = threading.Barrier(2)
            for _ in range(2):
                ts.append(threading.Thread(target=deliver, args=(c, proof, r, b)))
            webhook['results'].append((c.account, f, 'dup_race', r))
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        # an even later replay of the same webhook
        for c, f, proof, r in racers:
            try:
                r.append(intake.record(c.tenant, c.account, proof))
            except Exception as exc:  # noqa: BLE001
                r.append('exc:' + repr(exc)[:160])
        ctx.webhooks_done.set()

    ctx.webhooks_done.clear()
    wh = threading.Thread(target=webhook_worker, daemon=True)
    ctx.fence_log.append(('marker', 'phase', 'B', None, None))
    fence_thread2 = threading.Thread(target=fence_worker, args=(ctx, ax, probe, {}), daemon=True)
    t2, to2 = drive(ctx, extra_threads=[wh, fence_thread2], done=ctx.webhooks_done.is_set)
    print('PHASE B %.1fs timed_out=%s' % (t2, to2))
    check('phase B settled within timeout', not to2, 'elapsed %.1fs; nonterminal=%s errors=%s' % (t2, nonterminal(engine), ctx.errors[:3]))

    # ---------------- assertions ----------------
    check('no engine/dispatch/step exceptions', not ctx.errors, ctx.errors[:5])
    check('writer prompts contained only the creator\'s own inventory/tokens', not ctx.violations, ctx.violations[:5])

    with Session(engine) as s:
        intents = s.scalars(select(OfTextIntent)).all()
        msgs_out = s.scalars(select(Message).where(Message.direction == 'out')).all()
        events = s.scalars(select(MoxyEvent)).all()
        offers = s.scalars(select(ContentOffer)).all()
        items = {i.id: i for i in s.scalars(select(CatalogItem))}
        calls = s.scalars(select(GrokCall)).all()
        txs = s.scalars(select(Transaction)).all()
        states = {(r.account_id, r.fan_id): r for r in s.scalars(select(OfConversationState))}
    fan_index = {(c.account, f): i for c in CREATORS for i, f in enumerate(c.fans)}
    scn_of = {k: scenario(i) for k, i in fan_index.items()}
    transient = {(i.account_id, i.fan_id) for i in intents if i.status == 'refused'
                 and i.reason in ('history_not_current', 'prewire_history_not_current')}
    marker = re.compile(r'\((AM|CR|TH)-(\d+)\)$')

    # 1. wrong fan / account
    bad = []
    all_calls = [(acct, d) for acct, w in ctx.wires for d in w.calls]
    for acct, d in all_calls:
        mm = marker.search(d.body)
        c = BY_ACCOUNT[acct]
        if d.account_id != acct or not mm or mm.group(1) != c.tag or int(mm.group(2)) != d.fan_id:
            bad.append(('wire', acct, d.fan_id, d.body[-20:]))
    for m in msgs_out:
        mm = marker.search(m.body or '')
        c = BY_ACCOUNT.get(m.account_id)
        if c is None or not mm or mm.group(1) != c.tag or int(mm.group(2)) != m.fan_id:
            bad.append(('message', m.account_id, m.fan_id, (m.body or '')[-20:]))
    for it in intents:
        mm = marker.search(it.body or '')
        c = BY_ACCOUNT[it.account_id]
        if not mm or mm.group(1) != c.tag or int(mm.group(2)) != it.fan_id:
            bad.append(('intent', it.account_id, it.fan_id))
    check('no message to the wrong fan/account (%d wire calls, %d outbound rows, %d intents)' % (len(all_calls), len(msgs_out), len(intents)), not bad, bad[:5])

    # 2. no duplicate send per decision
    per_intent = {}
    for acct, d in all_calls:
        per_intent[d.intent_id] = per_intent.get(d.intent_id, 0) + 1
    dup_wire = {k: v for k, v in per_intent.items() if v != 1}
    by_event = {}
    for it in intents:
        if it.moxy_event_id:
            by_event.setdefault((it.account_id, it.moxy_event_id), []).append(it)
    dup_intent = {k: len(v) for k, v in by_event.items() if len(v) > 1}
    accepted = [it for it in intents if it.status == 'accepted']
    out_per_fan, acc_per_fan = {}, {}
    for m in msgs_out:
        out_per_fan[(m.account_id, m.fan_id)] = out_per_fan.get((m.account_id, m.fan_id), 0) + 1
    for it in accepted:
        acc_per_fan[(it.account_id, it.fan_id)] = acc_per_fan.get((it.account_id, it.fan_id), 0) + 1
    raced = sum(1 for k, v in ctx.results.items() if len(v) > 1)
    check('each intent hit the wire at most once (and each accepted intent exactly once); %d intents were attempted by both dispatchers' % raced,
          not dup_wire and len(per_intent) == len(accepted) and set(per_intent) == {i.intent_id for i in accepted}, (dup_wire, len(per_intent), len(accepted)))
    check('at most one intent per Moxy decision', not dup_intent, dup_intent)
    check('outbound message rows == accepted intents per fan', out_per_fan == acc_per_fan and len(msgs_out) == len(accepted),
          (len(msgs_out), len(accepted)))

    # 3. cross-creator inventory / price / product leakage
    leaks = []
    for it in intents:
        if not it.catalog_item_id:
            continue
        c = BY_ACCOUNT[it.account_id]
        item = items[it.catalog_item_id]
        name = next((n for n, i in c.items.items() if i == it.catalog_item_id), None)
        if name is None or item.account_id != it.account_id:
            leaks.append(('item_not_own', it.account_id, it.fan_id, it.catalog_item_id))
            continue
        if json.loads(it.media_json) != [c.media[name]]:
            leaks.append(('media', it.account_id, it.fan_id, it.media_json))
        if it.price_cents != c.prices[name]:
            leaks.append(('price', it.account_id, it.fan_id, it.price_cents))
    for acct, d in all_calls:
        c = BY_ACCOUNT[acct]
        if d.media_ids:
            names = [n for n in NAMES if (c.media[n],) == tuple(d.media_ids)]
            if not names or d.price_cents != c.prices[names[0]]:
                leaks.append(('wire', acct, d.fan_id, d.media_ids, d.price_cents))
    for o in offers:
        c = BY_ACCOUNT[o.account_id]
        if items[o.item_id].account_id != o.account_id or o.price_cents not in c.prices.values():
            leaks.append(('offer', o.account_id, o.fan_id, o.item_id, o.price_cents))
    for tx in txs:
        c = BY_ACCOUNT.get(tx.account_id)
        if c is not None and tx.amount_cents not in c.prices.values():
            leaks.append(('tx', tx.account_id, tx.fan_id, tx.amount_cents))
    offered = sum(1 for it in intents if it.catalog_item_id)
    check('no cross-creator inventory/price/product leakage (%d offer intents, %d wire offers, %d purchases)' % (
        offered, sum(1 for _, d in all_calls if d.media_ids), len(txs)), not leaks and offered > 0, leaks[:5])

    # 4. held / refused fans get no offer or send
    held = [k for k, s_ in scn_of.items() if s_ in ('refuse', 'hardship')]
    problems = []
    for k in held:
        if [it for it in intents if (it.account_id, it.fan_id) == k] or [o for o in offers if (o.account_id, o.fan_id) == k]:
            problems.append(('sent_or_offered', k))
        if [1 for acct, d in all_calls if (acct, d.fan_id) == k] or out_per_fan.get(k):
            problems.append(('wire', k))
    by_fan_events = {}
    for e in events:
        by_fan_events.setdefault((e.account_id, e.fan_id), []).append(e)
    for k in held:
        es = sorted(by_fan_events.get(k, []), key=lambda e: e.created_at)
        if len(es) != 2 or any(e.status != 'held' for e in es):
            problems.append(('events', k, [(e.status, e.reason) for e in es]))
    check('refusal/hardship fans (%d) got no offer and no send, both the flagged-sale and the later naive-sale attempts held' % len(held), not problems, problems[:5])
    reasons = sorted({(scn_of[(e.account_id, e.fan_id)], e.reason) for e in events
                      if scn_of.get((e.account_id, e.fan_id)) in ('refuse', 'hardship')}, key=str)
    check('held reasons recorded (info)', True, reasons)

    # takeover / mid-generation
    tp = []
    for k, s_ in scn_of.items():
        es = sorted(by_fan_events.get(k, []), key=lambda e: e.created_at)
        if s_ == 'takeover_pre':
            if [it for it in intents if (it.account_id, it.fan_id) == k] or any(e.status != 'held' for e in es):
                tp.append((s_, k, [(e.status, e.reason) for e in es]))
        elif s_ == 'takeover_mid':
            if [it for it in intents if (it.account_id, it.fan_id) == k] or [e.status for e in es] != ['stale']:
                tp.append((s_, k, [(e.status, e.reason) for e in es]))
        elif s_ == 'takeover_queue':
            its = [it for it in intents if (it.account_id, it.fan_id) == k]
            if len(its) != 1 or its[0].status not in ('cancelled', 'refused') or out_per_fan.get(k) or states[k].mode != 'human_controlled':
                tp.append((s_, k, [it.status for it in its]))
        elif s_ == 'midgen' and k not in transient:
            its = [it for it in intents if (it.account_id, it.fan_id) == k]
            if [e.status for e in es] != ['stale', 'done'] or len(its) != 1 or its[0].status != 'accepted' or out_per_fan.get(k) != 1:
                tp.append((s_, k, [(e.status, e.reason) for e in es], [it.status for it in its]))
    check('takeovers (before event / mid-generation / between queue and dispatch) sent nothing; mid-generation inbound dropped the stale draft and the follow-up got exactly one reply', not tp, tp[:5])

    # expected per-scenario outcomes
    exp = []
    for k, s_ in scn_of.items():
        i = fan_index[k]
        if k in transient:
            continue
        n = {'chat': 2, 'direct': 1 + (1 if purchase_kind(i) else 0), 'invite': 2}.get(s_, 1 if s_ == 'midgen' else 0)
        if acc_per_fan.get(k, 0) != n:
            exp.append((s_, k, acc_per_fan.get(k, 0), n))
    check('accepted sends per fan match the scenario (chat 2, direct 1+continuation, invitation+acceptance 2, midgen 1, others 0)', not exp, exp[:6])
    inv = []
    for k, s_ in scn_of.items():
        if s_ == 'invite' and k not in transient:
            its = sorted([it for it in intents if (it.account_id, it.fan_id) == k], key=lambda it: it.created_at)
            c = BY_ACCOUNT[k[0]]
            if len(its) != 2 or its[0].catalog_item_id or its[1].catalog_item_id != c.items['first']:
                inv.append((k, [(it.catalog_item_id) for it in its]))
    if inv:
        from db.onlyfans_moxy_models import MoxyFan
        with Session(engine) as s2:
            for v in [('x', t, f) for _, t, f in ctx.accept_no_inv]:
                if True:
                    acct = next(c.account for c in CREATORS if c.tag == v[1])
                    mf = s2.get(MoxyFan, (acct, v[2]))
                    print('DIAG invite fan', v[1], v[2], 'state', mf.state_json[:300], 'facts', mf.facts_json[:200])
                    for e in sorted(by_fan_events.get((acct, v[2]), []), key=lambda e: e.created_at):
                        d = json.loads(e.decision_json) if e.decision_json else None
                        print('DIAG   event', e.kind, e.status, e.reason, d and (d['action'], d['product_id'], d['sequence']))
                    for it in s2.scalars(select(OfTextIntent).where(OfTextIntent.account_id == acct, OfTextIntent.fan_id == v[2])):
                        print('DIAG   intent', it.status, it.reason, it.moxy_event_id, it.catalog_item_id)
    unexplained = [(t, f) for a, t, f in ctx.accept_no_inv if (a, f) not in transient]
    check('acceptance always found its retained invitation (except fans whose invitation was refused by the history gate: %d)' % len(ctx.accept_no_inv), not unexplained, unexplained[:5])
    check('accepted invitation produced the invited product\'s own paid card (no card on the invitation)', not inv, inv[:5])

    # purchases and duplicate webhooks
    pu = []
    kinds = {}
    for e in events:
        kinds.setdefault((e.account_id, e.fan_id), []).append(e.kind)
    for acct, f, kind, r in webhook['results']:
        k = (acct, f)
        c = BY_ACCOUNT[acct]
        pe = [e for e in by_fan_events.get(k, []) if e.kind == 'purchase']
        ptx = [t for t in txs if (t.account_id, t.fan_id) == k]
        cont = [it for it in intents if (it.account_id, it.fan_id) == k and it.catalog_item_id == c.items['second']]
        ok_status = [x for x in r if x == 'applied']
        if len(pe) != 1 or len(ptx) != 1 or len(cont) != 1 or cont[0].status != 'accepted':
            pu.append((c.tag, f, kind, len(pe), len(ptx), len(cont), r))
        if any(str(x).startswith('exc:') for x in r) or not ok_status:
            pu.append(('webhook_result', c.tag, f, kind, r))
    check('purchase webhooks (%d, sequential + racing duplicates): one Transaction, one purchase event, one continuation card each; duplicate deliveries returned applied without exception' % len(webhook['results']),
          not pu and not [e for e in webhook['errors'] if (next(c.account for c in CREATORS if c.tag == e[1]), e[2]) not in transient] and len(webhook['results']) > 0, (pu[:4], webhook['errors'][:3]))

    # 6. terminal states
    badi = [(it.account_id, it.fan_id, it.status) for it in intents if it.status not in ('accepted', 'cancelled', 'refused', 'not_accepted')]
    bade = [(e.account_id, e.fan_id, e.kind, e.status, e.reason) for e in events if e.status not in ('done', 'held', 'stale')]
    dly = None
    with Session(engine) as s:
        dly = {r.account_id: (r.queued_count, r.active_intent_id) for r in s.scalars(select(OfDeliveryPolicy))}
    stuck_policy = {a: v for a, v in dly.items() if a in BY_ACCOUNT and (v[0] != 0 or v[1] is not None)}
    check('every intent terminal (%s), none queued/dispatching/unknown; policy queued_count 0 and no active intent' % dict(sorted({s_: sum(1 for i in intents if i.status == s_) for s_ in {i.status for i in intents}}.items())),
          not badi and not stuck_policy, (badi[:5], stuck_policy))
    check('every Moxy event terminal (%s)' % dict(sorted({s_: sum(1 for e in events if e.status == s_) for s_ in {e.status for e in events}}.items())), not bade, bade[:5])
    with_model = [e for e in events if e.model_call_id]
    gstat = {}
    for g in calls:
        gstat[g.status] = gstat.get(g.status, 0) + 1
    check('model calls: one per generated event, all settled (grok_calls by status: %s; stub invocations %d, events with call %d)' % (gstat, len(ctx.model_calls), len(with_model)),
          len(ctx.model_calls) == len(with_model) == len(calls) and set(gstat) <= {'done'} and all(e.charged_millicents is not None for e in with_model),
          (len(ctx.model_calls), len(with_model), len(calls), gstat))

    # 5. legacy fence (collected while the load ran)
    jobs = [r for r in ctx.fence_log if r[0] == 'job']
    sends = [r for r in ctx.fence_log if r[0] == 'send']
    fence_bad = []
    n_moxy_jobs = n_free_jobs = 0
    for _, acct, kind, status, reason in jobs:
        if acct in BY_ACCOUNT:
            n_moxy_jobs += 1
            if not (status == 'skipped' and reason == 'moxy_canonical_owner_required') and not (kind == 'scheduled_send' and False):
                fence_bad.append((acct, kind, status, reason))
        else:
            n_free_jobs += 1
            if status != 'ok':
                fence_bad.append((acct, kind, status, reason))
    probe_accts = {a for a, _ in probe.calls}
    if probe_accts & set(BY_ACCOUNT):
        fence_bad.append(('legacy automation ran for a Moxy account', sorted(probe_accts & set(BY_ACCOUNT))))
    free_runs = len([1 for a, _ in probe.calls if a == FREE_ACCOUNT])
    if free_runs != n_free_jobs:
        fence_bad.append(('free account runs', free_runs, n_free_jobs))
    check('legacy jobs fenced for Moxy accounts, none ran a model/send (%d fenced-job attempts on 3 Moxy accounts, 0 reached the automation; non-Moxy account ran %d/%d)' % (n_moxy_jobs, free_runs, n_free_jobs),
          not fence_bad and n_moxy_jobs > 0 and n_free_jobs > 0, fence_bad[:5])
    send_bad = []
    for _, acct, purpose, outcome, detail in sends:
        if acct in BY_ACCOUNT:
            if purpose == 'manual' and outcome != 'ran':
                send_bad.append((acct, purpose, outcome, detail))
            if purpose != 'manual' and not (outcome == 'refused' and 'moxy_canonical_owner_required' in (detail or '')):
                send_bad.append((acct, purpose, outcome, detail))
        elif outcome != 'ran':
            send_bad.append((acct, purpose, outcome, detail))
    check('legacy automated sends refused at the transport boundary for Moxy accounts, manual passes, non-Moxy untouched (%d probes)' % len(sends), not send_bad and sends, send_bad[:5])

    # more fence: authoritative-writer refusal, route_legacy, manual operator send on a Moxy-owned taken-over fan
    from moxy_replacement.routing import route_legacy
    from onlyfans_runtime.store import Refused
    extra_bad = []
    for c in CREATORS:
        f = c.fans[0]
        try:
            authority.prepare(c.tenant, c.account, f, command_key='old-automated', expected_revision=states[(c.account, f)].revision,
                              body='Old caption must not send', origin='automated', actor='fixture-old')
            extra_bad.append(('legacy automated prepare accepted', c.tag))
        except Refused as exc:
            if 'authoritative_writer' not in str(exc):
                extra_bad.append(('legacy automated prepare wrong refusal', c.tag, str(exc)))
        for origin in ('campaign', 'engagement'):
            try:
                route_legacy(authority, c.tenant, c.account, f, origin, 'k', {})
                extra_bad.append(('route_legacy did not refuse', c.tag, origin))
            except Refused as exc:
                if 'moxy_canonical_scheduler_required' not in str(exc):
                    extra_bad.append(('route_legacy wrong refusal', c.tag, origin, str(exc)))
    if route_legacy(authority, FREE_TENANT, FREE_ACCOUNT, 9001, 'campaign', 'k', {}) is not None:
        extra_bad.append(('route_legacy touched the non-Moxy account',))
    check('legacy authority/scheduler routes refuse Moxy accounts and leave the non-Moxy account alone', not extra_bad, extra_bad[:5])

    # manual operator send to a taken-over fan on a Moxy account, and a manual send on the non-Moxy account
    man_bad = []
    for c in CREATORS:
        k = next((c.account, f) for i, f in enumerate(c.fans) if scenario(i) == 'takeover_pre')
        try:
            iid = authority.prepare(c.tenant, c.account, k[1], command_key='operator-1', expected_revision=states[k].revision,
                                    body='Operator here, hello (%s-%d)' % (c.tag, k[1]), origin='manual', actor='fixture-operator')
            wire = SyntheticTextTransport(c.account, message_id=ctx.pid(c.account, iid))
            res = authority.dispatch(c.tenant, c.account, iid, wire)
            if res != 'accepted' or len(wire.calls) != 1:
                man_bad.append((c.tag, res, len(wire.calls)))
        except Exception as exc:  # noqa: BLE001
            man_bad.append((c.tag, repr(exc)[:200]))
    try:
        with Session(engine) as s:
            rev = s.get(OfConversationState, (FREE_ACCOUNT, 9001)).revision
        iid = authority.prepare(FREE_TENANT, FREE_ACCOUNT, 9001, command_key='free-1', expected_revision=rev,
                                body='hello from the operator', origin='manual', actor='fixture-operator')
        wire = SyntheticTextTransport(FREE_ACCOUNT, message_id=ctx.pid(FREE_ACCOUNT, iid))
        if authority.dispatch(FREE_TENANT, FREE_ACCOUNT, iid, wire) != 'accepted' or len(wire.calls) != 1:
            man_bad.append(('free', 'not accepted'))
    except Exception as exc:  # noqa: BLE001
        man_bad.append(('free', repr(exc)[:200]))
    check('manual operator sends still work: taken-over fan on each Moxy account, and the non-Moxy account', not man_bad, man_bad)

    # cross-tenant isolation at the service boundary
    from onlyfans_runtime.store import Refused as R2
    iso = []
    try:
        service.add_event('tenant-third', CREATORS[0].account, CREATORS[0].fans[0], 'turn', 'cross-tenant', {})
        iso.append('add_event accepted a foreign tenant')
    except R2:
        pass
    check('wrong tenant cannot add an event to another creator\'s account', not iso, iso)

    # summary line for the report
    sc = {}
    for s_ in scn_of.values():
        sc[s_] = sc.get(s_, 0) + 1
    summary = {'creators': 3, 'fans_per_creator': N_FANS, 'pool': POOL, 'scenarios_per_creator_sum': sc,
               'moxy_events': len(events), 'intents': len(intents), 'accepted_sends': len(accepted),
               'wire_calls': len(all_calls), 'model_calls': len(ctx.model_calls), 'purchase_webhooks': len(webhook['results']),
               'setup_s': round(setup_s, 1), 'phase_a_s': round(t1, 1), 'phase_b_s': round(t2, 1),
               'transient_history_gate_refusals': len(transient), 'fence_jobs_moxy': n_moxy_jobs, 'fence_jobs_free': n_free_jobs, 'real_model': False, 'real_provider': False}
    print('\nLOAD SUMMARY ' + json.dumps(summary, sort_keys=True))
    for name, ok, detail in results:
        print('%s  %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok else '  -> ' + detail))
    asyncio.run(jobs_engine.dispose())
    store.close()
    failed = [n for n, ok, _ in results if not ok]
    assert not failed, failed


def test_two_workers_cannot_double_admit_one_intent(database, monkeypatch):
    """Minimal repro for concurrent dispatch of the SAME queued intent (two workers / two dispatcher loops).

    Exactly one worker may reserve and send; the rest must see a settled intent and do nothing. Policy
    accounting (used_units / queued_count) and the fan's unresolved-attempt pointer must end clean."""
    url, engine = database
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('DATABASE_URL', url)
    from twin_bridge.store import ensure_runtime_schema
    ensure_runtime_schema(url)
    store = Store(url, pool_size=POOL)
    service = MoxyService(store)
    authority = service.authority
    ctx = Ctx(engine, store, service, authority)
    c = CREATORS[0]
    build_creator(ctx, url, engine, c, {'amelia': load('amelia').creator})
    fans = c.fans[:8]
    intents = {}
    for f in fans:
        ctx.inbound(c, f, ROUND1['chat'])
    store.drain(c.tenant, c.account)
    for f in fans:
        ev = add_event(ctx, c, f, 'race-%d' % f)
        service.prepare(c.tenant, c.account, ev, make_stub(ctx, c))
        intents[f] = service.queue(c.tenant, c.account, ev)
    outcomes, errors, wires = {f: [] for f in fans}, [], []
    for f in fans:
        barrier = threading.Barrier(RACERS)

        def worker(f=f, barrier=barrier):
            wire = SyntheticTextTransport(c.account, message_id=ctx.pid(c.account, intents[f]))
            barrier.wait()
            try:
                outcomes[f].append(authority.dispatch(c.tenant, c.account, intents[f], wire))
            except Exception as exc:  # noqa: BLE001
                errors.append((f, repr(exc)[:200]))
            if wire.calls:
                wires.append((f, len(wire.calls)))
        ts = [threading.Thread(target=worker) for _ in range(RACERS)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    with Session(engine) as s:
        reasons = sorted({(i.status, i.reason) for i in s.scalars(select(OfTextIntent))}, key=str)
        print('RACE intent (status, reason):', reasons)
        st = {i.fan_id: i.status for i in s.scalars(select(OfTextIntent))}
        policy = s.get(OfDeliveryPolicy, c.account)
        pending_attempts = [r.fan_id for r in s.scalars(select(OfConversationState).where(
            OfConversationState.account_id == c.account, OfConversationState.unresolved_attempt_id.is_not(None)))]
        out_rows = s.scalar(select(func.count()).select_from(Message).where(Message.account_id == c.account, Message.direction == 'out'))
        used, queued = policy.used_units, policy.queued_count
    print('RACE outcomes', {f: sorted(v) for f, v in outcomes.items()}, 'errors', errors, 'wire_calls', sum(n for _, n in wires),
          'statuses', sorted(set(st.values())), 'used_units', used, 'queued_count', queued, 'unresolved_attempts', pending_attempts)
    store.close()
    assert sum(n for _, n in wires) == len(fans) and sorted(set(st.values())) == ['accepted'] and out_rows == len(fans)
    assert not errors
    assert used == len(fans) and queued == 0 and not pending_attempts


@pytest.mark.xfail(strict=True, reason='FINDING: any other fan\'s pending ingress event makes admit() permanently refuse an unrelated fan\'s '
                   'ready Moxy reply (history_not_current), and nothing re-creates it')
def test_other_fans_pending_inbound_must_not_permanently_refuse_a_ready_reply(database, monkeypatch):
    """Minimal repro. Fan A's reply is queued. Fan B's inbound event is recorded but not yet drained (the normal
    few-millisecond window in a live account). A's dispatch is refused terminally instead of deferred."""
    url, engine = database
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('DATABASE_URL', url)
    from twin_bridge.store import ensure_runtime_schema
    ensure_runtime_schema(url)
    store = Store(url, pool_size=POOL)
    service = MoxyService(store)
    ctx = Ctx(engine, store, service, service.authority)
    c = CREATORS[0]
    build_creator(ctx, url, engine, c, {'amelia': load('amelia').creator})
    a, b = c.fans[0], c.fans[1]
    ctx.inbound(c, a, ROUND1['chat'])
    store.drain(c.tenant, c.account)
    ev = add_event(ctx, c, a, 'repro-a')
    service.prepare(c.tenant, c.account, ev, make_stub(ctx, c))
    intent = service.queue(c.tenant, c.account, ev)
    ctx.inbound(c, b, ROUND1['chat'])          # recorded, deliberately NOT drained yet
    wire = SyntheticTextTransport(c.account, message_id=ctx.pid(c.account, intent))
    result = service.authority.dispatch(c.tenant, c.account, intent, wire)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (c.account, intent))
        status, reason = row.status, row.reason
    store.close()
    print('REPRO dispatch result', result, 'intent', status, reason)
    assert result != 'refused' and status in ('queued', 'accepted')
