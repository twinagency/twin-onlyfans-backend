"""Offline simulation runner: scripted fan journey through the REAL engine path.

ingress (signed Fans webhook) -> plan -> sequence -> queue -> dispatch, with the
synthetic Fans provider as the only transport. Nothing leaves the process: no
model call, no Fans API call, no network except the throwaway test Postgres.

Run (inside the offline image, see docs in the commit message / report):
    python -m tools.sim_journey run --provider A
    python -m tools.sim_journey side-by-side --out /tmp/out
    python -m tools.sim_journey run --model real --provider C    # when A6 is wired

The model is pluggable (ModelPort). `FixtureModel` replays the replies stored in
the script file; `RealModelAdapter` is the seam for A6's ModelClient.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import textwrap
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
for _p in (SERVICE, SERVICE / 'tests'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_SCRIPT = Path(__file__).resolve().parent / 'sim_scripts' / 'journey30.json'
TENANT, ACCOUNT, FAN = 'tenant-a', 'acc-a', 101
SECRET = b'sim-secret'
RETRYABLE = ('unsupported', 'a pitch must use', 'sales are paused', 'invalid synthetic proposal',
             'explicit buying scope', 'offer authorization', 'approved')


# --------------------------------------------------------------------------- model port

@dataclass
class StepContext:
    n: int
    step: dict
    provider: str
    attempt: int = 1
    rejection: str | None = None


@dataclass
class ModelOutput:
    """What a model proposes for one step, in script vocabulary (item keys, not ids)."""
    parts: tuple = ()
    offer: str | None = None          # catalogue key; the runner creates the grant
    signal: str | None = None         # 'decline' | 'reopen'
    scope: tuple = ()                 # catalogue keys for a reopen
    pitch: bool = False
    price_claims: tuple = ()          # cents
    purchase_claim: bool = False


class ModelPort:
    """Interface. propose() returns ModelOutput (or an engine SyntheticProposal)."""
    name = 'port'

    def propose(self, ctx: StepContext, snapshot: dict):
        raise NotImplementedError


class FixtureModel(ModelPort):
    """Scripted replies from the script file: step['fixture'][provider]."""
    name = 'fixture'

    def propose(self, ctx, snapshot):
        fx = (ctx.step.get('fixture') or {}).get(ctx.provider)
        if fx is None:
            raise LookupError(f'no fixture for step {ctx.n} provider {ctx.provider}')
        if ctx.attempt == 1 and fx.get('first_attempt'):
            fx = {**fx, **fx['first_attempt']}
        return ModelOutput(parts=tuple(fx['parts']), offer=fx.get('offer'), signal=fx.get('signal'),
                           scope=tuple(fx.get('scope', ())), pitch=bool(fx.get('pitch')),
                           price_claims=tuple(fx.get('price_claims', ())),
                           purchase_claim=bool(fx.get('purchase_claim')))


class NotWired(RuntimeError):
    pass


class RealRuntimePort(ModelPort):
    """Marker: the Journey builds A6's ModelRuntime itself (model_transport.build_model_runtime)."""
    name = 'real'

    def propose(self, ctx, snapshot):
        raise NotWired('real model runs through EngineService(store, ModelRuntime), not the shim')


class RealModelAdapter(ModelPort):
    """Seam for A6's ModelClient (branch claude/engine-model-20261107). NOT WIRED YET.

    A6's interface was not present when this was written (the branch only had
    DB model/migration changes), so this is a documented stub with the contract
    the runner needs. To wire it, give `client` any object with
        client.generate(snapshot: dict, *, provider: str) -> SyntheticProposal | str
    where a str is one structured-proposal JSON (parsed with
    conversation_engine.model_contract.parse_proposal). Then adjust
    `_from_a6_registry` to import A6's real client + PromptProvider registry.
    Provider names 'A' and 'C' are passed through unchanged as `provider`.

    Known gaps for wiring (not solved here): EngineService only accepts
    SyntheticModel/StructuredFixtureModel instances (the runner satisfies this by
    wrapping the port in a SyntheticModel); the engine builds no model call
    ledger rows for this path; a real model cannot know offer grant ids, so it
    must pick a catalogue key which the runner turns into a grant (ModelOutput.offer).
    """
    name = 'real'

    def __init__(self, client=None):
        self.client = client if client is not None else self._from_a6_registry()

    @staticmethod
    def _from_a6_registry():
        for mod in ('conversation_engine.model_client', 'conversation_engine.model_registry'):
            try:
                __import__(mod)
            except ImportError:
                continue
            raise NotWired(f'{mod} exists but RealModelAdapter._from_a6_registry is not written against it')
        raise NotWired("A6's ModelClient is not importable in this tree; pass client=... or wire _from_a6_registry")

    def propose(self, ctx, snapshot):
        raw = self.client.generate(snapshot, provider=ctx.provider)
        if isinstance(raw, str):
            from conversation_engine.model_contract import parse_proposal
            return parse_proposal(raw)
        return raw


# --------------------------------------------------------------------------- lab database

@contextmanager
def lab_database():
    """Throwaway DB on ONLYFANS_TEST_PG_URL, same pattern as the pg test fixture."""
    from sqlalchemy import create_engine
    import test_onlyfans_foundation_pg as fnd
    admin_url = os.environ['ONLYFANS_TEST_PG_URL']
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    name = 'of_sim_' + uuid.uuid4().hex
    template = fnd._empty_template(admin, admin_url) if os.environ.get('ONLYFANS_TEST_PG_EMPTY_TEMPLATE') == '1' else None
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{template or "template0"}" ENCODING \'UTF8\'')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, connect_args={'options': '-c timezone=UTC'})
    try:
        if template is None:
            with engine.begin() as c:
                fnd.bootstrap(c)
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


# --------------------------------------------------------------------------- flags

def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def opener(text):
    w = _words(text)
    return ' '.join(w[:2]) if w else ''


_USD = re.compile(r'(?:usd\s*\$?\s*|\$\s*)(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:usd|dollars?|bucks)\b', re.I)


def text_prices(text):
    out = set()
    for m in _USD.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(',', '.')
        whole, _, frac = raw.partition('.')
        out.add(int(whole) * 100 + int((frac + '00')[:2]))
    return out


def analyse(rec, history, catalogue_prices, *, human_hold_before):
    """Return a list of flag dicts for one step record. Pure function (unit tested)."""
    flags = []
    bubbles = rec['bubbles']

    def flag(kind, detail):
        flags.append({'flag': kind, 'detail': detail})

    if rec['kind'] == 'fan' and not bubbles and not rec['handover']['hold_active']:
        flag('no_reply', rec.get('no_reply_reason') or 'no bubble dispatched')
    if len(bubbles) > 2:
        flag('more_than_2_bubbles', f'{len(bubbles)} bubbles')
    if bubbles:
        recent = [opener(b['text']) for h in history[-5:] for b in h['bubbles'][:1] if not b.get('card')]
        first = next((b for b in bubbles if not b.get('card')), None)
        if first and opener(first['text']) and opener(first['text']) in recent:
            flag('repeated_opener', f"'{opener(first['text'])}' opened an earlier reply")
    allowed = set(catalogue_prices)
    for b in bubbles:
        found = set(text_prices(b['text']))
        if b.get('card'):
            found.add(b['card']['price_cents'])
        bad = sorted(found - allowed)
        if bad:
            flag('price_not_in_catalogue', f'{[c / 100 for c in bad]} in "{b["text"][:40]}"')
    cards = [b for b in bubbles if b.get('card')]
    if cards and rec['sales']['mode_before'] == 'paused' and rec['decision'].get('sales_signal') != 'reopen':
        flag('card_after_refusal', 'card sent while selling was paused')
    if cards and rec['decision'].get('sales_signal') == 'decline':
        flag('card_after_refusal', 'card sent in the same turn as a refusal')
    keys = [b['idempotency_key'] for b in bubbles]
    if len(keys) != len(set(keys)):
        flag('duplicate_dispatch', 'same idempotency key sent twice')
    texts = [b['text'] for b in bubbles]
    if len(texts) != len(set(texts)):
        flag('duplicate_dispatch', 'identical bubble twice in one turn')
    prior_keys = {b['idempotency_key'] for h in history for b in h['bubbles']}
    if prior_keys & set(keys):
        flag('duplicate_dispatch', 'idempotency key reused from an earlier turn')
    if bubbles and (human_hold_before or rec['handover']['hold_active']):
        flag('reply_while_human_holds', 'bubble dispatched while a human holds the chat')
    return flags


# --------------------------------------------------------------------------- runner

class Journey:
    def __init__(self, script, port, provider, database):
        self.script, self.port, self.provider = script, port, provider
        self.url, self.engine_db = database
        self.catalogue = {c['key']: c for c in script['catalogue']}
        self.prices = {c['price_cents'] for c in script['catalogue']}
        self.item_ids, self.key_by_id = {}, {}
        self.offers = {}            # key -> dict(message_id, intent_id, price, media, paid)
        self.records, self.spend_cents = [], 0
        self.next_inbound = 1
        self.ctx = None

    # setup ---------------------------------------------------------------
    def setup(self):
        from sqlalchemy.orm import Session
        import test_onlyfans_foundation_pg as fnd
        import test_onlyfans_text_delivery_pg as td
        from db.models import CatalogItem, VaultItem
        from onlyfans_runtime.store import utcnow
        from onlyfans_runtime.text_delivery import TextAuthority
        from conversation_engine.engine import EngineService, SyntheticModel
        from fans_api_adapter.intake import FansWebhookAdapter
        from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
        self.store = fnd.seed(self.url, self.engine_db)
        td.current(self.engine_db, mode='automated')
        self.authority = TextAuthority(self.store)
        self.authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, media_enabled=True,
            limit_units=1000, throttle_seconds=1, actor='sim-operator', reason='Offline simulation lab')
        with Session(self.engine_db) as s, s.begin():
            for c in self.script['catalogue']:
                for mid in c['media']:
                    s.add(VaultItem(account_id=ACCOUNT, media_id=mid, kind='image', created_at=utcnow(),
                                    content_hash=f'sim-file-{mid}'))
                item = CatalogItem(account_id=ACCOUNT, kind=c['kind'], label=c['label'],
                    description_for_ai=c['description'], media_ids=json.dumps(c['media']),
                    preview_media_ids=json.dumps(c['previews']), price_cents=c['price_cents'], enabled=True)
                s.add(item); s.flush()
                self.item_ids[c['key']] = item.id
                self.key_by_id[str(item.id)] = c['key']
        adapter = FansWebhookAdapter(self.store)
        adapter.bind_simulated(TENANT, ACCOUNT, 'acct_sim', 'creator-sim')
        self.adapter = adapter
        self.real = self.port.name == 'real'
        if self.real:
            from conversation_engine.model_config import ModelConfig
            from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles
            from model_transport import build_model_runtime
            profile = CreatorProfile.from_facts('Maile', {
                'age': 'private: never say or hint at her age',
                'location': 'private: never say where she lives',
                'pet_names': 'babe, baby, hun',
                'explicit_adult_chat': 'allowed within her limits',
                'limits': 'no explicit videos (no moaning, no sex acts on video)'},
                voice='flirty, teasing, a little cheeky; short, texty replies',
                limit_terms=('moaning', 'sex act'))
            runtime = build_model_runtime(self.engine_db, ModelConfig.from_env(os.environ),
                                          StaticCreatorProfiles({ACCOUNT: profile}))
            if runtime is None:
                raise NotWired('ENGINE_MODEL_ENABLED is not 1')
            self.engine = EngineService(self.store, runtime)
        else:
            shim = SyntheticModel({})
            shim.generate = self._generate          # EngineService type-gates on SyntheticModel
            self.engine = EngineService(self.store, shim)
        self.engine.select_simulated(TENANT, ACCOUNT, FAN)
        self.provider_fx = SyntheticFansProvider(('accepted',) * 400, message_id=tuple(range(9000, 9400)))
        self.transport = FansApiSyntheticTransport(self.store, TENANT, ACCOUNT, self.provider_fx)
        os.environ['ONLYFANS_PROCESS_ROLE'] = 'worker'
        from onlyfans_runtime.commerce import CommerceIntake
        self.commerce = CommerceIntake(self.store)
        self.snapshots = []

    # model shim -----------------------------------------------------------
    def _generate(self, snapshot):
        from conversation_engine.commerce import SyntheticCoverage, approve_simulated_offer
        from conversation_engine.engine import SyntheticProposal
        from onlyfans_runtime.store import utcnow
        self.snapshots.append(snapshot)
        out = self.port.propose(self.ctx, snapshot)
        if isinstance(out, SyntheticProposal):
            return out
        parts = list(out.parts)
        item_scope = tuple(str(self.item_ids[k]) for k in out.scope)
        grant = None
        if out.offer:
            item_id = self.item_ids[out.offer]
            media = tuple(self.catalogue[out.offer]['media'])
            try:
                grant = approve_simulated_offer(self.store, TENANT, ACCOUNT, FAN, item_id,
                    SyntheticCoverage(ACCOUNT, FAN, item_id, media, 'synthetic:sim-coverage', utcnow() + timedelta(minutes=5)))
            except Exception as exc:       # noqa: BLE001 - surfaced as a validation rejection
                from onlyfans_runtime.store import Refused
                raise Refused(f'offer authorization unavailable: {exc}') from None
        signal = out.signal or ('reopen' if item_scope else 'none')
        if grant:
            return SyntheticProposal(body=parts[0], sales_signal=signal, item_scope=item_scope, action='offer',
                offer_grant_id=grant, pitch=out.pitch, price_claims=tuple(out.price_claims),
                purchase_claim=out.purchase_claim)
        return SyntheticProposal(body=parts[0], sales_signal=signal, item_scope=item_scope, parts=tuple(parts[1:]),
            pitch=out.pitch, price_claims=tuple(out.price_claims), purchase_claim=out.purchase_claim)

    # ingress ---------------------------------------------------------------
    def _ingest(self, text):
        mid = self.next_inbound; self.next_inbound += 1
        payload = {'event': 'messages.received', 'account_id': 'acct_sim',
            'payload': {'id': mid, 'fromUser': {'id': FAN}, 'text': text,
                        'createdAt': datetime.now(timezone.utc).isoformat(), 'media': [], 'price': 0}}
        raw = json.dumps(payload, separators=(',', ':')).encode()
        sig = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        result = self.adapter.accept(raw, sig, SECRET, f'evt_sim_{mid}', expected_tenant=TENANT, expected_account=ACCOUNT)
        self.store.drain(TENANT, ACCOUNT)
        return mid, result

    def _state(self):
        from sqlalchemy.orm import Session
        from db.models import OfConversationState
        from db.onlyfans_engine_models import OfEngineOwner
        with Session(self.engine_db) as s:
            st = s.get(OfConversationState, (ACCOUNT, FAN))
            ow = s.get(OfEngineOwner, (ACCOUNT, FAN))
            scope = [self.key_by_id.get(x, x) for x in json.loads(ow.sales_scope_json)]
            return {'conversation_mode': st.mode, 'takeover_state': st.takeover_state, 'revision': st.revision,
                    'sales_mode': ow.sales_mode, 'sales_scope': scope}

    def _control(self, mode, action, key):
        st = self._state()
        self.store.control(TENANT, ACCOUNT, FAN, mode=mode, expected_revision=st['revision'],
                           command_key=key, actor='sim-operator', reason='Simulated operator ' + action, action=action)

    # step execution --------------------------------------------------------
    def run(self):
        self.setup()
        hold = False
        for step in self.script['steps']:
            if hold and step['kind'] == 'fan':
                self._control('automated', 'release_takeover', f"rel-{step['n']}")
                hold = False
            rec = self._step(step, hold)
            hold = rec['handover']['hold_active']
            self.records.append(rec)
        return self.result()

    def _step(self, step, hold_before):
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
        from onlyfans_runtime.store import Refused
        from onlyfans_runtime.text_delivery import TextAuthority
        n, kind = step['n'], step['kind']
        before = self._state()
        calls0 = len(self.provider_fx.calls)
        mc0 = self._model_calls() if self.real else (0, 0, 0)
        rec = {'n': n, 'kind': kind, 'note': step.get('note', ''), 'fan': None, 'event': None,
               'attempts': [], 'rejections': [], 'decision': {'kind': 'none'}, 'bubbles': [],
               'sales': {'mode_before': before['sales_mode'], 'mode': before['sales_mode'], 'scope': before['sales_scope']},
               'handover': {'hold_active': False, 'requested': False}, 'flags': []}
        if kind == 'event' and step['event'] in ('purchase', 'spend_milestone'):
            self._business_event(step, rec)
        else:
            if kind == 'fan':
                rec['fan'] = step['text']
                text = step['text']
            else:
                rec['event'] = step['event']
                text = '[event: new subscriber]'
            mid, intake = self._ingest(text)
            if step.get('handover'):
                self._control('human_controlled', 'request_takeover', f'take-{n}')
                rec['handover'].update(requested=True, reason=step.get('note') or 'handover requested')
                try:
                    self.engine.plan(TENANT, ACCOUNT, FAN, mid)
                except Refused as exc:
                    rec['no_reply_reason'] = f'human holds chat ({exc})'
                rec['decision'] = {'kind': 'held_for_human'}
            else:
                self._plan_and_send(step, mid, rec)
        rec['bubbles'] = self._bubbles(calls0)
        after = self._state()
        if self.real:
            mc1 = self._model_calls()
            rec['model_calls'] = mc1[0] - mc0[0]
            rec['model_microusd'] = mc1[1] - mc0[1]
            if rec['decision'].get('kind') not in ('none', 'rejected', 'held_for_human'):
                rec['decision']['sales_signal'] = ('decline' if after['sales_mode'] == 'paused' and before['sales_mode'] != 'paused'
                                                   else 'reopen' if after['sales_mode'] != before['sales_mode'] else 'none')
        rec['sales'].update(mode=after['sales_mode'], scope=after['sales_scope'])
        rec['handover'].update(conversation_mode=after['conversation_mode'], takeover_state=after['takeover_state'],
                               hold_active=after['conversation_mode'] != 'automated')
        if kind == 'event' and step['event'] == 'spend_milestone':
            rec['handover'].update(requested=True, reason='whale: spend milestone')
        rec['flags'] = analyse(rec, self.records, self.prices, human_hold_before=hold_before)
        return rec

    def _model_calls(self):
        from sqlalchemy import text
        with self.engine_db.begin() as c:
            row = c.execute(text("SELECT count(*), coalesce(sum(actual_microusd),0), coalesce(sum(reserved_microusd) "
                                 "FILTER (WHERE actual_microusd IS NULL),0) FROM of_engine_model_call")).one()
        return int(row[0]), int(row[1]), int(row[2])

    def _approve_all_grants(self):
        """A real model cannot mint grants: pre-approve each enabled item the sales state permits (best effort)."""
        from conversation_engine.commerce import SyntheticCoverage, approve_simulated_offer
        from onlyfans_runtime.store import utcnow
        for key, item_id in self.item_ids.items():
            try:
                approve_simulated_offer(self.store, TENANT, ACCOUNT, FAN, item_id, SyntheticCoverage(
                    ACCOUNT, FAN, item_id, tuple(self.catalogue[key]['media']), 'synthetic:sim-coverage',
                    utcnow() + timedelta(minutes=5)))
            except Exception:       # noqa: BLE001 - sales paused / already owned / already granted
                pass

    def _plan_and_send(self, step, mid, rec):
        if self.real:
            self._approve_all_grants()
        from onlyfans_runtime.store import Refused
        from onlyfans_runtime.text_delivery import TextAuthority
        from sqlalchemy.orm import Session
        from db.onlyfans_engine_models import OfEngineDecision
        decision, rejection = None, None
        for attempt in (1, 2):
            self.ctx = StepContext(step['n'], step, self.provider, attempt, rejection)
            try:
                decision = self.engine.plan(TENANT, ACCOUNT, FAN, mid)
                rec['attempts'].append({'attempt': attempt, 'result': 'accepted'})
                break
            except Refused as exc:
                rejection = str(exc)
                rec['attempts'].append({'attempt': attempt, 'result': 'rejected', 'reason': rejection})
                rec['rejections'].append({'attempt': attempt, 'reason': rejection})
                if not any(token in rejection for token in RETRYABLE):
                    break
            except LookupError as exc:
                rec['rejections'].append({'attempt': attempt, 'reason': f'model: {exc}'})
                break
            except Exception as exc:   # noqa: BLE001 - ModelBusy / ModelUnavailable etc: record, keep the run going
                rejection = f'{type(exc).__name__}: {exc}'
                rec['attempts'].append({'attempt': attempt, 'result': 'error', 'reason': rejection})
                rec['rejections'].append({'attempt': attempt, 'reason': rejection})
                break
        if decision is None:
            rec['no_reply_reason'] = 'rejected: ' + (rejection or 'no proposal')
            rec['decision'] = {'kind': 'rejected'}
            return
        sequence = self.engine.sequence(TENANT, ACCOUNT, decision)
        with Session(self.engine_db) as s:
            d0 = s.get(OfEngineDecision, (ACCOUNT, decision))
            rec['decision'] = {'kind': d0.action if d0.status != 'held' else 'held', 'decision_id': decision,
                               'status': d0.status, 'parts': len(sequence), 'sales_signal': self._signal(step)}
        if rec['decision']['kind'] == 'held':
            rec['no_reply_reason'] = 'decision held (offer evidence)'
            rec['rejections'].append({'attempt': len(rec['attempts']), 'reason': 'offer held for review'})
            return
        for part in sequence:
            intent = self.engine.queue(TENANT, ACCOUNT, part)
            self._unthrottle()
            outcome = TextAuthority(self.store).dispatch(TENANT, ACCOUNT, intent, self.transport)
            if outcome != 'accepted':
                rec['no_reply_reason'] = f'dispatch {outcome}'
                break

    def _signal(self, step):
        fx = (step.get('fixture') or {}).get(self.provider) or {}
        return fx.get('signal') or ('reopen' if fx.get('scope') else 'none')

    def _unthrottle(self):
        from sqlalchemy import text
        with self.engine_db.begin() as c:
            c.execute(text("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'"))

    def _bubbles(self, calls0):
        out = []
        for call in self.provider_fx.calls[calls0:]:
            body = call['body']
            card = None
            if body.get('mediaFiles'):
                price = int((Decimal(str(body.get('price', 0))) * 100).to_integral_value())
                label = next((self.catalogue[k]['label'] for k in self.catalogue
                              if self.catalogue[k]['label'] in body['text']), None)
                key = next((k for k in self.catalogue if self.catalogue[k]['label'] == label), None)
                card = {'item': key, 'label': label, 'price_cents': price, 'media_count': len(body['mediaFiles']),
                        'previews': len(body.get('previews', []))}
                if key:
                    self.offers[key] = {'intent_id': call['idempotency_key'], 'price': price, 'paid': False,
                        'media': tuple(int(m) for m in body['mediaFiles'])}
            out.append({'text': body['text'], 'card': card, 'idempotency_key': call['idempotency_key']})
        return out

    def _business_event(self, step, rec):
        from sqlalchemy.orm import Session
        from conversation_engine.commerce import SyntheticAccessObservation, record_simulated_access
        from db.onlyfans_delivery_models import OfTextIntent
        from onlyfans_runtime.commerce import SyntheticCommerceEvent
        if step['event'] == 'purchase':
            key = step['item']
            offer = self.offers.get(key)
            if offer is None or offer['paid']:
                rec['event'] = f'purchase of {key} (no open offer: skipped)'
                return
            with Session(self.engine_db) as s:
                message_id = s.get(OfTextIntent, (ACCOUNT, offer['intent_id'])).provider_message_id
            now = datetime.now(timezone.utc)
            ev = SyntheticCommerceEvent(account_id=ACCOUNT, fan_id=FAN, message_id=message_id,
                event_key=f'sim-purchase-{step["n"]}', provider_transaction_id=f'sim-tx-{step["n"]}',
                provider_revision=1, kind='purchase', amount_cents=offer['price'], currency='USD',
                occurred_at=(now + timedelta(seconds=1)).isoformat(), evidence_ref='synthetic:sim-purchase')
            self.commerce.record(TENANT, ACCOUNT, ev)
            record_simulated_access(self.store, TENANT, ACCOUNT, SyntheticAccessObservation(ACCOUNT, FAN,
                offer['intent_id'], message_id, offer['media'], f'sim-access-{step["n"]}', 'synthetic:sim-access',
                'confirmed', now + timedelta(seconds=2)))
            offer['paid'] = True
            self.spend_cents += offer['price']
            rec['event'] = f'purchase {key} ${offer["price"] / 100:.2f} (lifetime ${self.spend_cents / 100:.2f}); engine has no reply trigger for a purchase'
        else:
            self.spend_cents = max(self.spend_cents, step['cents'])
            self._control('human_controlled', 'request_takeover', f'whale-{step["n"]}')
            rec['event'] = f'spend milestone ${step["cents"] / 100:.0f}: whale hold (runner policy, not an engine rule)'

    def result(self):
        flags = [dict(f, n=r['n']) for r in self.records for f in r['flags']]
        return {'script': self.script['name'], 'provider': self.provider, 'model': self.port.name,
                'steps': self.records, 'flags': flags,
                'totals': {'steps': len(self.records), 'bubbles': sum(len(r['bubbles']) for r in self.records),
                           'cards': sum(1 for r in self.records for b in r['bubbles'] if b['card']),
                           'flags': len(flags), 'spend_cents': self.spend_cents,
                           'provider_calls': len(self.provider_fx.calls),
                           **({'model_calls': sum(r.get('model_calls', 0) for r in self.records),
                               'model_microusd': sum(r.get('model_microusd', 0) for r in self.records)}
                              if self.real else {})}}

    def close(self):
        self.store.close()


def run_journey(script, port=None, provider='A', database=None):
    """Run one provider over one script. `database` = (url, engine) or None for a throwaway DB."""
    port = port or FixtureModel()
    if database is not None:
        j = Journey(script, port, provider, database)
        try:
            return j.run()
        finally:
            j.close()
    with lab_database() as db:
        j = Journey(script, port, provider, db)
        try:
            return j.run()
        finally:
            j.close()


# --------------------------------------------------------------------------- rendering

def load_script(path=None):
    return json.loads(Path(path or DEFAULT_SCRIPT).read_text(encoding='utf-8'))


def step_lines(r):
    lines = []
    head = f"[{r['n']:02d}] "
    if r['fan'] is not None:
        lines.append(head + f"FAN: {r['fan']}" + (f"   ({r['note']})" if r['note'] else ''))
    else:
        lines.append(head + f"EVENT: {r['event']}" + (f"   ({r['note']})" if r['note'] else ''))
    for b in r['bubbles']:
        lines.append(f"     HER: {b['text'].replace(chr(10), ' / ')}")
        if b['card']:
            c = b['card']
            lines.append(f"     CARD: {c['label']} ${c['price_cents'] / 100:.2f}, {c['media_count']} media ({c['previews']} preview)")
    if not r['bubbles']:
        lines.append('     HER: (no reply)' + (f" - {r['no_reply_reason']}" if r.get('no_reply_reason') else ''))
    d = r['decision']
    lines.append(f"     decision: {d['kind']}" + (f" parts={d['parts']} status={d['status']}" if 'parts' in d else ''))
    lines.append(f"     sales: {r['sales']['mode']}" + (f" scope={r['sales']['scope']}" if r['sales']['scope'] else ''))
    h = r['handover']
    lines.append(f"     handover: {'HOLD' if h['hold_active'] else 'none'}" +
                 (f" ({h.get('reason')})" if h.get('requested') else ''))
    if 'model_calls' in r:
        lines.append(f"     model calls: {r['model_calls']} (${r['model_microusd'] / 1e6:.4f})")
    for x in r['rejections']:
        lines.append(f"     REJECTED (attempt {x['attempt']}): {x['reason']}")
    for f in r['flags']:
        lines.append(f"     FLAG {f['flag']}: {f['detail']}")
    return lines


def render_text(result):
    out = [f"SIM {result['script']}  provider={result['provider']}  model={result['model']}", '=' * 78]
    for r in result['steps']:
        out.extend(step_lines(r)); out.append('')
    t = result['totals']
    out.append(f"TOTALS steps={t['steps']} bubbles={t['bubbles']} cards={t['cards']} flags={t['flags']} "
               f"spend=${t['spend_cents'] / 100:.2f} provider_calls={t['provider_calls']}")
    for f in result['flags']:
        out.append(f"  flag step {f['n']:02d} {f['flag']}: {f['detail']}")
    return '\n'.join(out) + '\n'


def render_side_by_side(ra, rc, width=60):
    out = [f"SIDE BY SIDE  script={ra['script']}  A model={ra['model']}  C model={rc['model']}",
           f"{'A':<{width}} | C", '-' * (width * 2 + 3)]
    for a, c in zip(ra['steps'], rc['steps']):
        la = [w for line in step_lines(a) for w in (textwrap.wrap(line, width, subsequent_indent='       ', replace_whitespace=False) or [''])]
        lc = [w for line in step_lines(c) for w in (textwrap.wrap(line, width, subsequent_indent='       ', replace_whitespace=False) or [''])]
        for i in range(max(len(la), len(lc))):
            out.append(f"{(la[i] if i < len(la) else ''):<{width}} | {lc[i] if i < len(lc) else ''}")
        out.append('')
    out.append(f"A flags={ra['totals']['flags']} bubbles={ra['totals']['bubbles']} cards={ra['totals']['cards']}   "
               f"C flags={rc['totals']['flags']} bubbles={rc['totals']['bubbles']} cards={rc['totals']['cards']}")
    return '\n'.join(out) + '\n'


def _port(name):
    if name == 'fixture':
        return FixtureModel()
    if name == 'real':
        return RealRuntimePort()
    raise SystemExit(f'unknown model {name}')


def main(argv=None):
    p = argparse.ArgumentParser(prog='sim_journey')
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'side-by-side'):
        q = sub.add_parser(name)
        q.add_argument('--script', default=None)
        q.add_argument('--model', default='fixture', choices=['fixture', 'real'])
        q.add_argument('--out', default='.', help='output directory')
        if name == 'run':
            q.add_argument('--provider', default='A', choices=['A', 'C'])
    args = p.parse_args(argv)
    script = load_script(args.script)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if args.cmd == 'run':
        res = run_journey(script, _port(args.model), args.provider)
        tag = args.provider + ('_real' if args.model == 'real' else '')
        (out / f'sim_{tag}.txt').write_text(render_text(res), encoding='utf-8')
        (out / f'sim_{tag}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
        print(render_text(res))
    else:
        ra = run_journey(script, _port(args.model), 'A')
        rc = run_journey(script, _port(args.model), 'C')
        (out / 'sim_A.json').write_text(json.dumps(ra, indent=1, ensure_ascii=False), encoding='utf-8')
        (out / 'sim_C.json').write_text(json.dumps(rc, indent=1, ensure_ascii=False), encoding='utf-8')
        (out / 'sim_A.txt').write_text(render_text(ra), encoding='utf-8')
        (out / 'sim_C.txt').write_text(render_text(rc), encoding='utf-8')
        text = render_side_by_side(ra, rc)
        (out / 'sim_side_by_side.txt').write_text(text, encoding='utf-8')
        print(text)


if __name__ == '__main__':
    main()
