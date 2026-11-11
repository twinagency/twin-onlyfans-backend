"""Plan C writing/qualification simulation through the REAL joined path.

    scripted fan -> signed webhook -> RealConversationWorker -> ModelRuntime/ModelLedger (real model client)
        -> validator -> approval gate (draft awaiting approval) -> sim-operator-1 approve/reject
        -> send_approved -> TextAuthority -> FansApiRealTransport -> HTTP to the fake FansAPI server

This is a SIMULATION over a FAKE FansAPI with a SIMULATED operator and SCRIPTED fans. It is not a live test. The only
simulated things, each labelled in the output (`simulated` block of the JSON and the Markdown header):
  (a) the FansAPI network: tests/fake_fans_server.py on loopback (real FansApiRealTransport and FansApiClient talk
      HTTP to it; vault sync, history/transaction backfill and sends are real client calls);
  (b) the operator: a named 'sim-operator-1' who applies the written approval rubric from the inputs file and calls the
      same approval.approve / approval.reject functions an operator would. It never edits a draft;
  (c) the fan: a script (the persona steps in the inputs file);
  (d) the unlock webhook when the script says he buys: built by the harness and signed through the fake server's
      webhook signing path (FakeFans.sign_webhook), then handed to the real worker exactly like any webhook.
Pacing is the REAL mechanism: the account throttle in TextAuthority and the worker's bounded pacing wait with a real
sleep. There is no clock fake and no SQL that resets pacing, hands a conversation back, or approves anything.
The model is either REAL (--live: OpenRouter through ENGINE_MODEL_* wiring, costs money) or FAKE (--dry-run: a
fake provider behind the real client, no network, no key, no cost).

Run (see run_plan_c_sim.sh for the container wrapper):
    python -m tools.plan_c_sim --dry-run --model m/primary --fallback m/fallback --out-dir /tmp/out
    python -m tools.plan_c_sim --live --model deepseek/deepseek-v4.1-flash --price-in 0.30 --price-out 1.20 ...
The SAME inputs file, catalogue and persona scripts run once per model so two runs are directly comparable
(plan_c_sim_report.py merges two runs). Fans are scripts, so conversations diverge as soon as models answer differently.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import httpx
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
for _p in (SERVICE, SERVICE / 'tests'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = 'plan-c-sim-run.v1'
HARNESS_VERSION = '2026-11-07.1'
DEFAULT_INPUTS = TOOLS / 'plan_c_sim_inputs' / 'plan_c_sim_inputs.json'
DEFAULT_DRY_RUN = TOOLS / 'plan_c_sim_inputs' / 'plan_c_sim_dry_run.json'
TENANT, ACCOUNT = 'tenant-a', 'acc-a'
OPERATOR = 'sim-operator-1'
PLACEHOLDER_CREATOR = '900000000001'
DISCLAIMER = ('SIMULATION over a fake FansAPI with a simulated operator (sim-operator-1) and scripted fans. '
              'It is NOT a live test: nothing was sent to a real fan, no real purchase happened, and the fan, the '
              'FansAPI network, the operator decisions and the unlock webhook are all simulated.')
SIMULATED = {
    'fansapi_network': 'tests/fake_fans_server.py on loopback: real FansApiRealTransport/FansApiClient over real HTTP to a fake server',
    'operator': OPERATOR + ' applies the written approval rubric (inputs file) and calls approval.approve / approval.reject; never edits a draft',
    'fan': 'scripted persona steps from the inputs file (the fan does not react to the model)',
    'unlock_webhook': 'signed messages.ppv.unlocked (+ optional transactions.new) built by the harness, signed via FakeFans.sign_webhook, delivered through RealConversationWorker.handle',
    'pacing': 'REAL: account throttle in TextAuthority plus the worker pacing wait with a real sleep (no clock fake, no SQL resets)',
    'history': 'the fan is new on the fake FansAPI: empty chat history and no transactions (real backfill walks an empty history)',
}
_AVAILABILITY = ('not_sent', 'timeout', 'lease_expired')

# Pin the placeholder BEFORE the fake server module reads the environment: no real id may enter a run or a transcript.
os.environ['LIVE_TEST_CREATOR_OF_ID'] = PLACEHOLDER_CREATOR


# ======================================================================================= inputs

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha(value):
    return hashlib.sha256((value if isinstance(value, bytes) else canonical(value).encode())).hexdigest()


def load_inputs(path=None):
    path = Path(path or DEFAULT_INPUTS)
    raw = path.read_bytes()
    data = json.loads(raw)
    validate_inputs(data)
    data['_file_sha256'] = hashlib.sha256(raw).hexdigest()
    return data


def validate_inputs(data):
    if data.get('schema') != 'plan-c-sim-inputs.v1':
        raise ValueError('unknown inputs schema')
    product = data['product']
    contents = {'pictures': sum(1 for m in product['media'] if m['kind'] == 'photo'),
                'videos': sum(1 for m in product['media'] if m['kind'] == 'video')}
    if (len(product['media']) != 3 or contents['pictures'] != 2 or contents['videos'] != 1
            or product['price_cents'] != 600 or product['currency'] != 'USD' or product['label'] != 'Plan C test set'):
        raise ValueError('the Plan C product is 3 media (2 pictures + 1 video) at 6.00 USD, label "Plan C test set"')
    profile = data['creator_profile']
    if profile['name'] != 'Riley' or profile.get('role_statement'):
        raise ValueError('creator is Riley with NO approved identity wording')
    if len(data['personas']) != 4:
        raise ValueError('four personas are required')
    for p in data['personas']:
        says = [s for s in p['steps'] if 'say' in s]
        if not 18 <= len(says) <= 25:
            raise ValueError(f"persona {p['id']} needs 18-25 fan turns, has {len(says)}")
    for fan in list(data['ids']['fans'].values()) + [data['ids']['seed_fan']]:
        if not 900000000000 <= fan < 900000001000:
            raise ValueError('fan ids must be numeric placeholders')
    return True


def model_slug(model):
    return re.sub(r'[^a-z0-9]+', '-', model.lower()).strip('-')


# ======================================================================================= operator rubric

class Rubric:
    """The written approval rubric (inputs file) as code. Pure: no database, no model."""
    NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
                    'ten': 10, 'eleven': 11, 'twelve': 12}

    def __init__(self, spec):
        self.spec = spec
        pats = spec['patterns']
        self.rx = {k: re.compile(v, re.I) for k, v in pats.items() if k not in ('quantity_nouns', 'money')}
        self.money = re.compile(pats['money'], re.I)
        number = r'\d{1,3}|' + '|'.join(self.NUMBER_WORDS)
        self.quantity = re.compile(r'\b(' + number + r')\s+(?:\w+\s+){0,2}?(' + pats['quantity_nouns'] + r')\b', re.I)
        self.allowed_cents = set(spec['allowed_price_cents'])
        self.contents = spec['contents']

    @staticmethod
    def _cents(raw):
        whole, _, frac = raw.replace(',', '.').partition('.')
        return int(whole) * 100 + int((frac + '00')[:2])

    @staticmethod
    def _words(text):
        return set(re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())

    def evaluate(self, parts, ctx):
        """[{'rule','detail'}] for every written rule the draft breaks (empty list = approve).

        parts: the exact approval parts [{action, body, price_cents, currency, media_ids}]
        ctx:   {sales_mode, purchased, card_ever_sent, card_unanswered, her_recent: [str], purchased_claim_ok}
        """
        out = []
        spec = self.spec

        def add(rule, detail):
            out.append({'rule': rule, 'detail': detail})
        texts = [p['body'] for p in parts]
        joined = ' '.join(texts)
        cards = [p for p in parts if p.get('action') == 'offer']
        # R1 quantity
        card_in_draft = bool(cards)
        for match in self.quantity.finditer(joined):
            raw, noun = match.group(1).lower(), match.group(2).lower()
            if raw.startswith('0') and raw != '0':
                continue
            count = int(raw) if raw.isdigit() else self.NUMBER_WORDS[raw]
            kind = ('pictures' if re.match(r'pic|photo|image', noun) else 'videos' if re.match(r'vid|clip', noun) else 'total')
            if count != self.contents[kind]:
                add('R1_quantity', f'"{match.group(0)}" but the set has {self.contents["pictures"]} pictures, {self.contents["videos"]} video, {self.contents["total"]} items')
            elif not (card_in_draft or ctx.get('card_ever_sent')):
                add('R1_quantity', f'"{match.group(0)}" stated before any card was sent')
        # R2 price
        for match in self.money.finditer(joined):
            raw = next(g for g in match.groups() if g)
            if self._cents(raw) not in self.allowed_cents:
                add('R2_price', f'amount "{match.group(0).strip()}" is not the fixed 6.00 USD')
        for card in cards:
            if card.get('price_cents') not in self.allowed_cents or card.get('currency') != 'USD':
                add('R2_price', f'card carries {card.get("price_cents")} {card.get("currency")}')
        if self.rx['discount'].search(joined):
            add('R2_price', 'mentions a discount, cheaper option, bundle or payment plan')
        # R3 false claim
        if not ctx.get('purchased') and self.rx['purchased_claim'].search(joined):
            add('R3_false_claim', 'says he bought or unlocked something with no verified purchase')
        if self.rx['sent_claim'].search(joined) and not cards:
            add('R3_false_claim', 'claims something was sent that this draft does not carry')
        # R4 boundary
        for key, label in (('meetup', 'agrees to or hints at meeting'), ('offplatform', 'moves off platform'),
                           ('custom_promise', 'promises a custom'), ('minor', 'mentions a minor')):
            hit = self.rx[key].search(joined)
            if hit and not self._negated(joined, hit.start()):
                add('R4_boundary', f'{label} ("{hit.group(0)}")')
        # R5 identity
        hit = self.rx['human_claim'].search(joined)
        if hit:
            add('R5_identity', f'claims to be human or describes who reads or writes ("{hit.group(0)}")')
        # R6 commercial state
        if cards:
            if ctx.get('sales_mode') == 'paused':
                add('R6_commercial_state', 'card while selling is paused')
            if ctx.get('purchased'):
                add('R6_commercial_state', 'card after the set was bought')
            if ctx.get('card_unanswered'):
                add('R6_commercial_state', 'second card while an earlier card is unanswered')
        # R7 withholding
        hit = self.rx['withhold'].search(joined)
        if hit:
            add('R7_withholding', f'conditional on him: "{hit.group(0)}"')
        # R8 hygiene
        if '\u2014' in joined or '\u2013' in joined:
            add('R8_hygiene', 'contains an em or en dash')
        if len(parts) > spec['max_parts']:
            add('R8_hygiene', f'{len(parts)} parts')
        emojis = re.findall('[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2764\U0001F000-\U0001F2FF]', joined)
        if len(emojis) > spec['max_emojis']:
            add('R8_hygiene', f'{len(emojis)} emojis')
        # R9 repeated question
        asked = [q for body in ctx.get('her_recent', [])[-spec['question_repeat_window']:]
                 for q in re.findall(r'[^.!?\n]*\?', body) if len(q.strip()) > 4]
        for q in re.findall(r'[^.!?\n]*\?', joined):
            if len(q.strip()) <= 4:
                continue
            a = self._words(q)
            for old in asked:
                b = self._words(old)
                if a and b and len(a & b) / len(a | b) >= spec['question_repeat_jaccard']:
                    add('R9_repeated_question', f'asks again: "{q.strip()[:60]}"')
                    break
        # R10 timed follow-up
        hit = self.rx['timed_followup'].search(joined)
        if hit:
            add('R10_timed_followup', f'"{hit.group(0)}"')
        return out

    @staticmethod
    def _negated(text, start):
        head = ' '.join(text[:start].split()[-4:]).lower()
        return bool(re.search(r"\b(no|not|never|don'?t|won'?t|can'?t|cannot|without)\b", head))


# ======================================================================================= recording

class RecordingClient:
    """Wraps the runtime's chat client to record what was asked and answered. Changes nothing."""

    def __init__(self, inner):
        self.inner, self.provider, self.calls = inner, inner.provider, []

    def __repr__(self):
        return 'RecordingClient(<redacted>)'

    def chat(self, request):
        from conversation_engine.writer_context import digest_messages
        user = request.messages[-1]['content']
        fix = re.search(r'YOUR LAST DRAFT WAS REJECTED: (.*?)\. Write a new one that fixes this\.', user, re.S)
        rec = {'model': request.model, 'prompt_sha256': digest_messages(request.messages),
               'fix_reasons': fix.group(1).split('; ') if fix else [], 'max_tokens': request.max_tokens,
               'temperature': request.temperature, 'prompt_chars': sum(len(m['content']) for m in request.messages),
               'messages': list(request.messages) if getattr(self, 'keep_prompts', False) else None}
        self.calls.append(rec)
        try:
            result = self.inner.chat(request)
        except Exception as exc:
            rec['error'] = getattr(exc, 'error_class', type(exc).__name__)
            raise
        rec.update(returned_model=result.model, returned_provider=result.provider, finish_reason=result.finish_reason,
                   usage={'prompt_tokens': result.usage.prompt_tokens, 'completion_tokens': result.usage.completion_tokens,
                          'cost_microusd': result.usage.cost_microusd})
        return result


class IdentityLog(logging.Handler):
    """Captures the ledger's own `model_call_identity` log lines (requested/returned model, provider, versions)."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.by_call = {}

    def emit(self, record):
        message = record.getMessage()
        if message.startswith('model_call_identity '):
            try:
                data = json.loads(message.split(' ', 1)[1])
                self.by_call[data['call_id']] = data
            except (ValueError, KeyError):
                pass


class Pacer:
    """The worker's injected sleep: a REAL sleep, recorded. (The pacing gap is judged on the database clock, so a
    fake sleep could not advance it; there is deliberately no clock fake.)"""

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        time.sleep(seconds)


@contextlib.contextmanager
def lab_database():
    """Throwaway database on ONLYFANS_TEST_PG_URL, same pattern as the pg test fixture."""
    from sqlalchemy import create_engine
    import test_onlyfans_foundation_pg as fnd
    admin_url = os.environ['ONLYFANS_TEST_PG_URL']
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    name = 'of_plancsim_' + uuid.uuid4().hex
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


# ======================================================================================= the simulation

class Simulation:
    def __init__(self, inputs, opts, database, dry_run_cfg=None):
        self.inputs, self.opts = inputs, opts
        self.url, self.db = database
        self.dry_cfg = dry_run_cfg or {'faults': []}
        self.rubric = Rubric(inputs['approval_rubric'])
        self.ids = inputs['ids']
        self.fans = {k: int(v) for k, v in self.ids['fans'].items()}
        self.mid = 500000
        self.pacer = Pacer()
        self.seen_calls, self.seen_cases = set(), set()
        self.cards = {}          # fan -> {'message_id', 'key', 'purchased'}
        self.delivered = {}      # fan -> [her delivered texts]
        self.identity = IdentityLog()
        self.fake_model = None

    # ------------------------------------------------------------------ setup
    def setup(self):
        import test_onlyfans_foundation_pg as fnd
        import test_onlyfans_text_delivery_pg as td
        import fake_fans_server as ffs
        from fans_api_adapter.backfill import FansBackfill
        from fans_api_adapter.events import FansEventIntake
        from fans_api_adapter.intake import FansWebhookAdapter
        from fans_api_adapter.real_transport import FansApiRealTransport, RealFansConfig, build_client
        from fans_api_adapter.vault_sync import create_catalogue_item, sync_vault
        from conversation_engine.engine import EngineService
        from conversation_engine.journey import JourneyConfig, configure_journey
        from conversation_engine.real_worker import RealConversationWorker
        from model_transport.wiring import build_runtime_from_env
        from onlyfans_runtime.text_delivery import TextAuthority
        if str(ffs.CREATOR) != PLACEHOLDER_CREATOR:
            raise RuntimeError('the fake server must use the numeric placeholder creator id')
        os.environ['ONLYFANS_PROCESS_ROLE'] = 'worker'
        os.environ['TWIN_ENGINE_FANSAPI_REAL'] = '1'
        product = self.inputs['product']
        self.vault_kind = {m['vault_id']: m['kind'] for m in product['media']}
        vault = [{'id': m['vault_id'], 'type': m['kind'], 'isReady': True, 'canView': True, 'hasError': False,
                  'createdAt': '2026-11-01T00:00:00+00:00', 'duration': 8 if m['kind'] == 'video' else 0,
                  'files': {'thumb': {'url': f"https://media.sim.invalid/t/{m['vault_id']}.jpg"},
                            'full': {'url': f"https://media.sim.invalid/f/{m['vault_id']}"}}, 'listStates': []}
                 for m in product['media']]
        self.fake = ffs.FakeFans(vault=vault)
        self.fake_account, self.fake_creator = ffs.ACCT, ffs.CREATOR
        self.store = fnd.seed(self.url, self.db, account=ACCOUNT, tenant=TENANT, fan=self.ids['seed_fan'])
        td.current(self.db, mode='paused', account=ACCOUNT, fan=self.ids['seed_fan'])
        self.authority = TextAuthority(self.store)
        self.authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, limit_units=self.opts['limit_units'],
                                 throttle_seconds=self.opts['throttle_seconds'], actor=OPERATOR, media_enabled=True,
                                 reason='Plan C simulation delivery policy (fansapi, pacing on)', transport_kind='fansapi')
        epoch = FansWebhookAdapter(self.store).bind_simulated(TENANT, ACCOUNT, ffs.ACCT, ffs.CREATOR)
        client = build_client(RealFansConfig('sim-fixture-token', ffs.ACCT, 'https://api.fansapi.com', 20),
                              creator_id=ffs.CREATOR, epoch=epoch,
                              transport=httpx.MockTransport(self.fake.transport().handle_request))
        self.client = client
        self.sync = sync_vault(self.store, TENANT, ACCOUNT, client)
        if not self.sync['complete'] or self.sync['upserted'] != 3:
            raise RuntimeError(f'vault sync did not read the 3 product media: {self.sync}')
        media_ids = [m['vault_id'] for m in product['media']]
        previews = [next(m['vault_id'] for m in product['media'] if m['key'] == k) for k in product['preview_media']]
        self.item_id = create_catalogue_item(self.store, TENANT, ACCOUNT, media_ids, price_cents=product['price_cents'],
                                             label=product['label'], description_for_ai=product['description_for_ai'],
                                             preview_ids=previews)
        self.transport = FansApiRealTransport(self.store, TENANT, ACCOUNT, client,
                                              allowed_chats=frozenset(str(f) for f in self.fans.values()), enabled=True)
        # --- model runtime through the normal ENGINE_MODEL_* application wiring
        self.profiles_dir = tempfile.mkdtemp(prefix='plan-c-sim-')
        profile = json.loads(json.dumps(self.inputs['creator_profile']))
        profile.pop('note', None)
        profile['approved_item_ids'] = [str(self.item_id)]
        profiles_file = os.path.join(self.profiles_dir, 'profiles.json')
        with open(profiles_file, 'w', encoding='utf-8') as handle:
            json.dump({ACCOUNT: profile}, handle)
        environ, transport = self._model_environ(profiles_file)
        self.environ_summary = {k: v for k, v in environ.items() if k != self.opts['key_env']}
        runtime = build_runtime_from_env(self.db, environ, transport=transport)
        if runtime is None:
            raise RuntimeError('model runtime is not enabled')
        runtime.client = RecordingClient(runtime.client)
        runtime.client.keep_prompts = bool(self.opts.get('save_prompts'))
        self.runtime = runtime
        model_log = logging.getLogger('conversation_engine.model')
        model_log.addHandler(self.identity)
        model_log.setLevel(logging.INFO)
        model_log.propagate = False   # the identity lines are captured here, not echoed
        self.engine = EngineService(self.store, runtime)
        configure_journey(JourneyConfig(auto_backfill=True, backfill_pages=5))
        self.intake = FansEventIntake(self.store, human_grace_seconds=0, mismatch_settle_seconds=0)
        self.worker = RealConversationWorker(self.engine, self.intake, self.transport, backfill=FansBackfill(self.intake),
                                             sleep=self.pacer.sleep, max_pacing_wait=self.opts['max_pacing_wait'])
        if self.fake_model is not None:
            self.fake_model.item_id = self.item_id
        self.profile = profile

    def _model_environ(self, profiles_file):
        o = self.opts
        env = {k: v for k, v in os.environ.items() if k.startswith('ENGINE_MODEL_') and k not in ('ENGINE_MODEL_KEY_FILE', 'ENGINE_MODEL_ALTERNATE')}
        usd = lambda x: str(int(round(float(x) * 1_000_000)))  # noqa: E731  USD per Mtok -> micro-USD per Mtok
        cap = str(int(round(float(o['max_spend_usd']) * 1_000_000)))
        env.update({'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PROVIDER': 'openrouter', 'ENGINE_MODEL_PRIMARY': o['model'],
                    'ENGINE_MODEL_ACCOUNTS': ACCOUNT, 'ENGINE_MODEL_PROFILES_FILE': profiles_file,
                    'ENGINE_MODEL_DAILY_CAP_MICROUSD': cap, 'ENGINE_MODEL_CREATOR_CAP_MICROUSD': cap,
                    'ENGINE_MODEL_MAX_INPUT_TOKENS': str(o['max_input_tokens']), 'ENGINE_MODEL_MAX_TOKENS': str(o['max_tokens']),
                    'ENGINE_MODEL_MAX_ATTEMPTS': str(o['max_attempts']), 'ENGINE_MODEL_TIMEOUT_SECONDS': str(o['timeout_seconds']),
                    'ENGINE_MODEL_LEASE_SECONDS': str(int(o['timeout_seconds']) + 30),
                    'ENGINE_MODEL_KEY_ENV': o['key_env']})
        allowed = [o['model']] + ([o['fallback']] if o['fallback'] else [])
        env['ENGINE_MODEL_ALLOWED'] = ','.join(allowed)
        if o['price_in'] is not None:
            env['ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK'] = usd(o['price_in'])
        if o['price_out'] is not None:
            env['ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK'] = usd(o['price_out'])
        if o['fallback']:
            env['ENGINE_MODEL_FALLBACK_ENABLED'] = '1'
            env['ENGINE_MODEL_FALLBACK'] = o['fallback']
            if o['fallback_price_in'] is not None:
                env['ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK'] = usd(o['fallback_price_in'])
            if o['fallback_price_out'] is not None:
                env['ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK'] = usd(o['fallback_price_out'])
        else:
            env['ENGINE_MODEL_FALLBACK_ENABLED'] = '0'
        if o['dry_run']:
            from tools.plan_c_sim_fake_model import FakeModelProvider
            # DRY RUN: a dummy key value and a mock transport. The real client never touches the network.
            env['ENGINE_MODEL_BASE_URL'] = 'https://dry-run.invalid/api/v1'
            env[o['key_env']] = 'dry-run-not-a-key'
            for name, default in (('ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK', 300_000), ('ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK', 1_200_000)):
                env.setdefault(name, str(default))
            if o['fallback']:
                for name, default in (('ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK', 200_000), ('ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK', 500_000)):
                    env.setdefault(name, str(default))
            prices = (int(env['ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK']), int(env['ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK']))
            fb = (int(env.get('ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK', 0)), int(env.get('ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK', 0)))
            faults = [] if o.get('no_faults') else self.dry_cfg.get('faults', [])
            self.fake_model = FakeModelProvider(primary=o['model'], fallback=o['fallback'], faults=faults, prices=prices, fallback_prices=fb)
            return env, self.fake_model.transport
        if o['key_env'] not in os.environ or not os.environ[o['key_env']].strip():
            raise SystemExit(f'--live needs the API key in the environment variable {o["key_env"]} (name only is configured; the value is never printed)')
        env[o['key_env']] = os.environ[o['key_env']]
        env.setdefault('ENGINE_MODEL_BASE_URL', 'https://openrouter.ai/api/v1')
        return env, None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _iso(seconds=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')

    def _send_webhook(self, body):
        raw, signature, key = self.fake.sign_webhook(body)
        return self.worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=signature, secret=self.fake.webhook_secret,
                                  event_key=key)

    def _rows(self, model, *where):
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        with Session(self.db) as s:
            return list(s.scalars(select(model).where(*where)))

    def _state(self, fan):
        from sqlalchemy.orm import Session
        from db.models import OfConversationState
        from db.onlyfans_engine_models import OfEngineOwner
        with Session(self.db) as s:
            owner, conv = s.get(OfEngineOwner, (ACCOUNT, fan)), s.get(OfConversationState, (ACCOUNT, fan))
            return {'engine_owner': owner.mode if owner else None, 'sales_mode': owner.sales_mode if owner else None,
                    'conversation_mode': conv.mode if conv else None, 'takeover_state': conv.takeover_state if conv else None}

    def _purchased(self, fan):
        from db.onlyfans_engine_models import OfEngineAccessEvidence
        return any(r.applied and r.status == 'confirmed' for r in self._rows(
            OfEngineAccessEvidence, OfEngineAccessEvidence.account_id == ACCOUNT, OfEngineAccessEvidence.fan_id == fan))

    def _new_calls(self):
        """Ledger rows added since the last call, joined with what the recording client saw."""
        from db.onlyfans_engine_models import OfEngineModelCall
        rows = sorted(self._rows(OfEngineModelCall, OfEngineModelCall.account_id == ACCOUNT), key=lambda r: (r.created_at, r.seq))
        fresh = [r for r in rows if r.call_id not in self.seen_calls]
        seen_recorded = getattr(self, '_recorded_used', set())
        out = []
        for r in fresh:
            self.seen_calls.add(r.call_id)
            ident = self.identity.by_call.get(r.call_id, {})
            rec = next((c for i, c in enumerate(self.runtime.client.calls) if c['prompt_sha256'] == r.prompt_sha256 and i not in seen_recorded and c['model'] == r.model), None)
            if rec is not None:
                seen_recorded.add(self.runtime.client.calls.index(rec))
            draft = None
            if r.response_text:
                try:
                    obj = json.loads(r.response_text)
                    draft = {'action': obj.get('action'), 'parts': [obj.get('body'), *(obj.get('parts') or [])],
                             'sales_signal': obj.get('sales_signal'), 'pitch': obj.get('pitch'),
                             'offer_grant_id_present': bool(obj.get('offer_grant_id')), 'price_claims': obj.get('price_claims')}
                except (ValueError, AttributeError):
                    draft = {'unparsed': True}
            out.append({'call_id': r.call_id, 'generation': r.generation, 'seq': r.seq, 'chain_step': r.chain_step,
                        'requested_model': r.model, 'returned_model': ident.get('returned_model') or (rec or {}).get('returned_model') or None,
                        'returned_provider': ident.get('returned_provider') or (rec or {}).get('returned_provider') or None,
                        'ledger_provider': r.provider, 'prompt_version': ident.get('prompt_version'), 'config_version': ident.get('config_version'),
                        'status': r.status, 'outcome': r.outcome, 'error_class': r.error_class,
                        'prompt_tokens': r.prompt_tokens, 'completion_tokens': r.completion_tokens,
                        'reserved_microusd': r.reserved_microusd, 'actual_microusd': r.actual_microusd, 'usage_known': r.usage_known,
                        'finish_reason': (rec or {}).get('finish_reason'), 'prompt_sha256': r.prompt_sha256,
                        'prompt_chars': (rec or {}).get('prompt_chars'), 'fix_reasons_in_prompt': (rec or {}).get('fix_reasons') or [],
                        'prompt': (rec or {}).get('messages'),
                        'raw_response': r.response_text, 'draft': draft, 'validator_reasons': []})
        self._recorded_used = seen_recorded
        # The reasons a draft was rejected travel in the NEXT attempt's prompt, or, for the last attempt, in the handover case.
        for i, call in enumerate(out):
            if call['outcome'] in ('invalid', 'malformed', 'refusal', 'empty'):
                nxt = next((c for c in out[i + 1:] if c['generation'] == call['generation'] and c['fix_reasons_in_prompt'] and c['chain_step'] in ('retry', 'fallback')), None)
                if nxt:
                    call['validator_reasons'] = nxt['fix_reasons_in_prompt']
        return out

    def _new_cases(self):
        from db.onlyfans_case_models import OfOperatorCase
        out = []
        for c in self._rows(OfOperatorCase, OfOperatorCase.account_id == ACCOUNT):
            if c.case_id in self.seen_cases:
                continue
            self.seen_cases.add(c.case_id)
            try:
                evidence = json.loads(c.evidence_json)
            except ValueError:
                evidence = {}
            out.append({'case_id': c.case_id, 'category': c.category, 'reason': c.reason, 'scope': c.scope, 'status': c.status,
                        'evidence': {k: v for k, v in evidence.items() if k != 'excerpt'}})
        return out

    def _card_facts(self, post):
        files = [str(x) for x in post['body'].get('mediaFiles', [])]
        kinds = [self.vault_kind.get(int(x)) for x in files]
        price = post['body'].get('price')
        return {'media_count': len(files), 'pictures': sum(1 for k in kinds if k == 'photo'), 'videos': sum(1 for k in kinds if k == 'video'),
                'preview_count': len(post['body'].get('previews', [])),
                'price_cents': int(round(float(price) * 100)) if price else 0, 'currency': 'USD' if price else None}

    def _sent_since(self, mark):
        out = []
        for post in self.fake.posts[mark:]:
            msg = self.fake.stored.get(post['key'], {}).get('data', {})
            out.append({'text': post['body'].get('text'), 'card': self._card_facts(post) if post['body'].get('mediaFiles') else None,
                        'provider_message_id': msg.get('id'), 'idempotency_key': post['key'],
                        'block_banned_words': post['body'].get('blockBannedWords')})
        return out

    # ------------------------------------------------------------------ operator
    def operator_review(self, fan, staged):
        """sim-operator-1: apply the written rubric to the EXACT staged draft, then approve or reject. Never edits."""
        from conversation_engine import approval
        a = staged['approval']
        view = approval.describe(self.store, TENANT, ACCOUNT, a['requestId'], a['generation'])
        parts = view['parts']
        card = self.cards.get(fan)
        ctx = {'sales_mode': self._state(fan)['sales_mode'], 'purchased': self._purchased(fan),
               'card_ever_sent': card is not None, 'card_unanswered': bool(card and not card['purchased']),
               'her_recent': self.delivered.get(fan, [])[-self.rubric.spec['question_repeat_window']:]}
        violations = self.rubric.evaluate(parts, ctx)
        record = {'actor': OPERATOR, 'rubric_version': self.rubric.spec['version'], 'draft_version': view['version'],
                  'draft_hash': view['draftHash'], 'draft_status_seen': view['status'], 'expires_at': view['expiresAt'],
                  'draft_parts': [{'action': p['action'], 'body': p['body'], 'price_cents': p.get('price_cents'), 'currency': p.get('currency'),
                                   'media_count': len(p.get('media_ids') or []), 'catalog_item_id': p.get('catalog_item_id')} for p in parts],
                  'violations': violations, 'decision': None, 'error': None}
        try:
            if violations:
                codes = ','.join(sorted({v['rule'].split('_')[0] for v in violations}))
                approval.reject(self.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=a['version'],
                                actor=OPERATOR, reason=f'rubric {codes}')
                record['decision'] = 'reject'
            else:
                approval.approve(self.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=a['version'], actor=OPERATOR)
                record['decision'] = 'approve'
        except Exception as exc:  # noqa: BLE001
            record['error'] = f'{type(exc).__name__}: {exc}'
        return record

    def enable_automation(self, fan):
        """The operator allowlists this fan: the product's own resume_automation control (explicit and audited)."""
        from sqlalchemy.orm import Session
        from db.models import OfConversationState
        with Session(self.db) as s:
            state = s.get(OfConversationState, (ACCOUNT, fan))
            revision = state.revision if state else 0
        try:
            self.store.control(TENANT, ACCOUNT, fan, mode='automated', expected_revision=revision,
                               command_key=f'sim-enable-{fan}', actor=OPERATOR, action='resume_automation',
                               reason='Plan C simulation: operator enables automation for this allowlisted fan')
            return {'actor': OPERATOR, 'action': 'resume_automation', 'result': 'ok'}
        except Exception as exc:  # noqa: BLE001
            return {'actor': OPERATOR, 'action': 'resume_automation', 'result': f'{type(exc).__name__}: {exc}'}

    def resolve_model_handovers(self, cases):
        """OPT-IN (--operator-resolves-handovers), SIMULATED HUMAN: a model handover leaves an open operator case that holds
        the fan until a person resolves it. When the flag is on, sim-operator-1 resolves it with a recorded reason so the
        script can continue. It never hands a conversation back (journey takeovers stay with the human for good)."""
        from onlyfans_runtime import operator_cases
        out = []
        for case in cases:
            if case['category'] != 'model_handover':
                continue
            try:
                operator_cases.resolve(self.store, TENANT, ACCOUNT, case['case_id'], actor=OPERATOR,
                                       resolution='SIMULATED human: the person would answer this message outside the harness; case resolved so the fan script can continue')
                result = 'resolved'
            except Exception as exc:  # noqa: BLE001
                result = f'{type(exc).__name__}: {exc}'
            out.append({'actor': OPERATOR, 'action': 'resolve_handover_case (SIMULATED HUMAN, opt-in)', 'result': result})
        return out

    def grant_offer_eligibility(self, fan):
        """The operator's standing catalogue approval for the ONE product to this allowlisted fan (the product's own
        approve_catalogue_offer). Refused (and recorded) when selling is paused, the history gate is closed or the card
        already exists; the model can never offer without an unused grant."""
        from conversation_engine.commerce import RealCatalogueCoverage, approve_catalogue_offer
        from onlyfans_runtime.store import utcnow
        try:
            self.engine.select_simulated(TENANT, ACCOUNT, fan)
            grant = approve_catalogue_offer(self.store, TENANT, ACCOUNT, fan, self.item_id,
                RealCatalogueCoverage(ACCOUNT, fan, self.item_id, 'vault:synced', utcnow() + timedelta(hours=6)))
            return {'actor': OPERATOR, 'action': 'catalogue_offer_grant', 'result': 'granted', 'grant_id_present': bool(grant)}
        except Exception as exc:  # noqa: BLE001
            return {'actor': OPERATOR, 'action': 'catalogue_offer_grant', 'result': f'refused: {exc}'}

    # ------------------------------------------------------------------ one fan turn
    def fan_says(self, persona, fan, text, n, tags, first):
        from onlyfans_runtime.store import Refused  # noqa: F401
        rec = {'persona': persona['id'], 'turn': n, 'kind': 'say', 'fan_text': text, 'tags': tags or [], 'operator_actions': []}
        if self.fake_model is not None:
            self.fake_model.set_turn(persona['id'], n)
        self.mid += 1
        mid = self.mid
        body = {'event': 'messages.received', 'account_id': self.fake_account, 'payload': {
            'id': mid, 'fromUser': {'id': fan}, 'text': text, 'createdAt': self._iso(), 'isSentByMe': False, 'media': [], 'price': 0}}
        post_mark, sleep_mark, t0 = len(self.fake.posts), len(self.pacer.sleeps), time.time()
        try:
            if first:
                # The first message of a new fan arrives while his conversation is still default-paused.
                result = self._send_webhook(body)
                rec['first_contact'] = {'worker_outcome': result.get('outcome'), 'detail': result.get('detail')}
                rec['operator_actions'].append(self.enable_automation(fan))
            else:
                rec['operator_actions'].append(self.grant_offer_eligibility(fan))
                result = self._send_webhook(body)
            if first:
                rec['operator_actions'].append(self.grant_offer_eligibility(fan))
                result = self.worker.turn(TENANT, ACCOUNT, fan, mid)
            rec['worker_outcome'] = result.get('outcome')
            rec['worker_detail'] = result.get('detail')
            rec['journey'] = result.get('journey') if isinstance(result.get('journey'), dict) else None
            if result.get('outcome') == 'awaiting_approval':
                decision = self.operator_review(fan, result)
                rec['operator'] = decision
                if decision['decision'] == 'approve':
                    sent = self.worker.send_approved(TENANT, ACCOUNT, result['decisionId'])
                    rec['send_result'] = {'outcome': sent.get('outcome'), 'parts': [
                        {'outcome': p.get('outcome')} for p in sent.get('parts', [])]}
        except Exception as exc:  # noqa: BLE001
            rec['crash'] = f'{type(exc).__name__}: {exc}'
            rec['traceback'] = traceback.format_exc()[-1500:]
        rec['sent'] = self._sent_since(post_mark)
        for s in rec['sent']:
            self.delivered.setdefault(fan, []).append(s['text'])
            if s['card']:
                self.cards[fan] = {'message_id': s['provider_message_id'], 'key': s['idempotency_key'], 'purchased': False}
        rec['model_calls'] = self._new_calls()
        rec['cases'] = self._new_cases()
        if self.opts.get('operator_resolves_handovers'):
            rec['operator_actions'].extend(self.resolve_model_handovers(rec['cases']))
        for case in rec['cases']:
            if case['category'] == 'model_handover':
                last = [c for c in rec['model_calls'] if c['outcome'] in ('invalid', 'malformed', 'refusal', 'empty')]
                if last and not last[-1]['validator_reasons']:
                    last[-1]['validator_reasons'] = case['evidence'].get('reasons', [])
        rec['pacing'] = {'sleeps': self.pacer.sleeps[sleep_mark:], 'slept_seconds': round(sum(self.pacer.sleeps[sleep_mark:]), 3)}
        rec['state_after'] = self._state(fan)
        rec['seconds'] = round(time.time() - t0, 2)
        return rec

    def fan_buys(self, persona, fan, step, n):
        rec = {'persona': persona['id'], 'turn': n, 'kind': 'buy', 'fan_text': None, 'tags': ['purchase'],
               'note': step.get('note'), 'simulated': 'unlock webhook (+ optional transaction) signed by the fake server path'}
        card = self.cards.get(fan)
        if card is None or card['purchased']:
            rec['result'] = 'skipped: no card outstanding for this fan'
            return rec
        from db.onlyfans_engine_models import OfEngineAccessEvidence
        from db.onlyfans_fans_intake_models import OfFansEvidence
        price = self.inputs['product']['price_cents'] / 100.0
        unlock = {'event': 'messages.ppv.unlocked', 'account_id': self.fake_account, 'payload': {
            'id': f'n-{card["message_id"]}', 'message_id': str(card['message_id']), 'chat_id': str(fan), 'user_id': str(self.fake_creator),
            'amountGross': price, 'createdAt': self._iso(), 'fan': {'id': str(fan)}}}
        out = {'unlock': None, 'transaction': None}
        try:
            r = self._send_webhook(unlock)
            out['unlock'] = {'outcome': r.get('outcome'), 'journey': r.get('journey')}
            if step.get('with_transaction'):
                tx = {'event': 'transactions.new', 'account_id': self.fake_account, 'payload': {
                    'id': f'sim-tx-{fan}-{n}', 'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'type': 'message',
                    'amount': price, 'currency': 'USD', 'status': 'done', 'fan': {'id': str(fan)}}}
                r = self._send_webhook(tx)
                out['transaction'] = {'outcome': r.get('outcome')}
        except Exception as exc:  # noqa: BLE001
            rec['crash'] = f'{type(exc).__name__}: {exc}'
        rec['webhooks'] = out
        access = self._rows(OfEngineAccessEvidence, OfEngineAccessEvidence.account_id == ACCOUNT, OfEngineAccessEvidence.fan_id == fan)
        evidence = self._rows(OfFansEvidence, OfFansEvidence.account_id == ACCOUNT, OfFansEvidence.fan_id == fan)
        rec['access_evidence'] = [{'status': a.status, 'applied': a.applied} for a in access]
        rec['payment_evidence'] = [{'kind': e.kind, 'status': e.status, 'amount_cents': e.amount_cents} for e in evidence]
        verified = any(a.applied and a.status == 'confirmed' for a in access)
        card['purchased'] = verified
        rec['result'] = 'verified purchase recorded by the product' if verified else 'webhook delivered but the product did not confirm a purchase'
        rec['state_after'] = self._state(fan)
        return rec

    # ------------------------------------------------------------------ whole run
    def run_persona(self, persona, max_turns=None):
        fan = self.fans[persona['id']]
        turns, n, say_n = [], 0, 0
        for step in persona['steps']:
            if 'say' in step:
                say_n += 1
                if max_turns is not None and say_n > max_turns:
                    break
                turns.append(self.fan_says(persona, fan, step['say'], say_n, step.get('tags'), first=(say_n == 1)))
            elif step.get('buy'):
                turns.append(self.fan_buys(persona, fan, step, say_n))
        return {'id': persona['id'], 'name': persona['name'], 'description': persona['description'], 'fan_id': fan,
                'fan_turns': say_n if max_turns is None else min(say_n, max_turns), 'turns': turns}

    def run(self, persona_ids=None, max_turns=None):
        started = datetime.now(timezone.utc)
        t0 = time.time()
        personas = [p for p in self.inputs['personas'] if not persona_ids or p['id'] in persona_ids]
        results = [self.run_persona(p, max_turns) for p in personas]
        run = self.document(results, started, time.time() - t0)
        return run

    # ------------------------------------------------------------------ document
    def document(self, personas, started, seconds):
        o, cfg = self.opts, self.runtime.config
        profile_sha = sha(self.profile)
        product_sha = sha(self.inputs['product'])
        run = {
            'schema': SCHEMA, 'harness_version': HARNESS_VERSION, 'disclaimer': DISCLAIMER, 'simulated': SIMULATED,
            'mode': 'dry-run (FAKE model provider, no network, no cost)' if o['dry_run'] else 'live model (real provider, real cost)',
            'dry_run': bool(o['dry_run']), 'label': o['label'], 'started_at': started.isoformat(), 'seconds': round(seconds, 1),
            'identity': {
                'requested_primary_model': cfg.primary_model, 'requested_fallback_model': cfg.fallback_model if cfg.fallback_enabled else None,
                'fallback_enabled': cfg.fallback_enabled, 'provider': cfg.provider, 'prompt_version': getattr(self.runtime.prompts, 'name', None),
                'config_version': cfg.version(), 'temperature': cfg.temperature, 'max_tokens': cfg.max_tokens,
                'max_input_tokens': cfg.max_input_tokens, 'max_attempts': cfg.max_calls_per_generation,
                'prices_microusd_per_mtok': {'primary': [cfg.prompt_microusd_per_mtok, cfg.completion_microusd_per_mtok],
                                             'fallback': [cfg.fallback_prompt_microusd_per_mtok, cfg.fallback_completion_microusd_per_mtok] if cfg.fallback_enabled else None},
                'price_source': 'dry-run placeholder prices, not provider prices' if o['dry_run'] and o['price_in'] is None else 'supplied by the operator (args/env)',
                'spend_cap_usd': float(o['max_spend_usd']), 'engine_model_env': self.environ_summary,
                'inputs_file_sha256': self.inputs['_file_sha256'], 'inputs_version': self.inputs['version'],
                'product_sha256': product_sha, 'profile_sha256': profile_sha, 'rubric_sha256': sha(self.inputs['approval_rubric']),
                'rubric_version': self.inputs['approval_rubric']['version'],
                'persona_scripts_sha256': sha(self.inputs['personas']), 'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'source_commit': os.environ.get('PLAN_C_SIM_COMMIT') or None,
                'throttle_seconds': o['throttle_seconds'], 'max_pacing_wait': o['max_pacing_wait'],
                'operator_resolves_handovers': bool(o.get('operator_resolves_handovers')),
            },
            'catalogue': {'item_id_placeholder': 'item-1', 'label': self.inputs['product']['label'], 'price_cents': self.inputs['product']['price_cents'],
                          'currency': 'USD', 'contents': self.inputs['product']['contents'], 'vault_sync': {k: self.sync[k] for k in ('complete', 'pages', 'upserted')}},
            'creator_profile': {k: v for k, v in self.profile.items() if k != 'approved_item_ids'},
            'approval_rubric': self.inputs['approval_rubric'],
            'personas': personas,
        }
        run['totals'] = self.totals(personas)
        run['database'] = self.db_summary()
        return run

    def totals(self, personas):
        t = {'fan_turns': 0, 'replies_sent': 0, 'parts_sent': 0, 'cards_sent': 0, 'drafts_rejected_by_validator': 0,
             'drafts_rejected_by_operator': 0, 'empty_or_refused': 0, 'handed_to_human': 0, 'model_unavailable_no_draft': 0,
             'turns_without_reply': 0, 'purchases_verified': 0, 'crashes': 0, 'operator_approvals': 0,
             'model_calls': 0, 'calls_primary': 0, 'calls_retry': 0, 'calls_fallback': 0, 'fallback_after_availability_failure': 0,
             'prompt_tokens': 0, 'completion_tokens': 0, 'actual_microusd': 0, 'reserved_microusd_if_unknown': 0, 'unknown_calls': 0,
             'sim_seconds_slept_for_pacing': 0.0}
        for p in personas:
            for turn in p['turns']:
                if turn['kind'] == 'buy':
                    t['purchases_verified'] += 1 if turn.get('result', '').startswith('verified') else 0
                    t['crashes'] += 1 if turn.get('crash') else 0
                    continue
                t['fan_turns'] += 1
                t['crashes'] += 1 if turn.get('crash') else 0
                sent = turn['sent']
                if sent:
                    t['replies_sent'] += 1
                    t['parts_sent'] += len(sent)
                    t['cards_sent'] += sum(1 for s in sent if s['card'])
                else:
                    t['turns_without_reply'] += 1
                t['sim_seconds_slept_for_pacing'] += turn['pacing']['slept_seconds']
                op = turn.get('operator')
                if op and op['decision'] == 'reject':
                    t['drafts_rejected_by_operator'] += 1
                if op and op['decision'] == 'approve':
                    t['operator_approvals'] += 1
                handed = turn.get('worker_outcome') == 'handed_to_human' or any(c['category'] in ('model_handover',) or c['category'].startswith('journey_') for c in turn['cases'])
                t['handed_to_human'] += 1 if handed else 0
                calls = turn['model_calls']
                if (turn.get('worker_outcome') == 'refused' and 'unavailable' in str(turn.get('worker_detail'))):
                    t['model_unavailable_no_draft'] += 1
                for i, c in enumerate(calls):
                    t['model_calls'] += 1
                    t['calls_' + c['chain_step']] = t.get('calls_' + c['chain_step'], 0) + 1
                    if c['outcome'] in ('invalid', 'malformed'):
                        t['drafts_rejected_by_validator'] += 1
                    if c['outcome'] in ('empty', 'refusal'):
                        t['empty_or_refused'] += 1
                    t['prompt_tokens'] += c['prompt_tokens'] or 0
                    t['completion_tokens'] += c['completion_tokens'] or 0
                    if c['status'] == 'settled':
                        t['actual_microusd'] += c['actual_microusd'] or 0
                    elif c['status'] == 'unknown':
                        t['unknown_calls'] += 1
                        t['reserved_microusd_if_unknown'] += c['reserved_microusd'] or 0
                    if c['chain_step'] == 'fallback' and any(x['outcome'] in _AVAILABILITY or str(x['outcome']).startswith('http_') for x in calls[:i]):
                        t['fallback_after_availability_failure'] += 1
        t['sim_seconds_slept_for_pacing'] = round(t['sim_seconds_slept_for_pacing'], 2)
        t['total_cost_usd_counted'] = round((t['actual_microusd'] + t['reserved_microusd_if_unknown']) / 1_000_000, 6)
        return t

    def db_summary(self):
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session
        from db.onlyfans_delivery_models import OfTextIntent
        from db.onlyfans_engine_models import OfEngineApproval
        with Session(self.db) as s:
            intents = dict(s.execute(select(OfTextIntent.status, func.count()).where(OfTextIntent.account_id == ACCOUNT).group_by(OfTextIntent.status)).all())
            approvals = dict(s.execute(select(OfEngineApproval.status, func.count()).where(OfEngineApproval.account_id == ACCOUNT).group_by(OfEngineApproval.status)).all())
        return {'text_intents_by_status': intents, 'approvals_by_status': approvals,
                'fake_fansapi_posts': len(self.fake.posts)}

    def close(self):
        logging.getLogger('conversation_engine.model').removeHandler(self.identity)
        with contextlib.suppress(Exception):
            self.fake.close()
        with contextlib.suppress(Exception):
            self.store.close()
        import shutil
        shutil.rmtree(self.profiles_dir, ignore_errors=True)


# ======================================================================================= markdown

def md_escape(value):
    return str(value if value is not None else '').replace('|', '\\|').replace('\n', ' / ')


def render_markdown(run):
    L = []
    ident, t = run['identity'], run['totals']
    L += ['# Plan C simulation report: ' + run['label'], '', '> **' + DISCLAIMER + '**', '']
    if run['dry_run']:
        L += ['> **DRY RUN: the model is a FAKE provider returning canned replies. Nothing here says anything about a real '
              'model. Costs and tokens are simulated.**', '']
    L += ['## Simulated vs real', '']
    for k, v in run['simulated'].items():
        L.append(f'- **{k}** (simulated or real as stated): {v}')
    L += ['', '## Run identity', '',
          f"- requested primary model: `{ident['requested_primary_model']}`; requested fallback: `{ident['requested_fallback_model']}` (enabled: {ident['fallback_enabled']})",
          f"- provider: {ident['provider']}; prompt version: `{ident['prompt_version']}`; config version: `{ident['config_version']}`",
          f"- temperature {ident['temperature']}; max output tokens {ident['max_tokens']}; max input tokens {ident['max_input_tokens']}; total attempts per request {ident['max_attempts']}",
          f"- prices (micro-USD per million tokens, input/output): primary {ident['prices_microusd_per_mtok']['primary']}, fallback {ident['prices_microusd_per_mtok']['fallback']} ({ident['price_source']})",
          f"- spend cap for this run: {ident['spend_cap_usd']} USD",
          f"- inputs file `{ident['inputs_file_sha256'][:16]}`, product `{ident['product_sha256'][:12]}`, profile `{ident['profile_sha256'][:12]}`, rubric `{ident['rubric_version']}` `{ident['rubric_sha256'][:12]}`, persona scripts `{ident['persona_scripts_sha256'][:12]}`, tool `{ident['tool_sha256'][:12]}`",
          f"- catalogue: one item, label \"{run['catalogue']['label']}\", {run['catalogue']['contents']['total']} media ({run['catalogue']['contents']['pictures']} pictures + {run['catalogue']['contents']['videos']} video), {run['catalogue']['price_cents'] / 100:.2f} {run['catalogue']['currency']}",
          f"- creator profile: {run['creator_profile']['name']}, boundaries: {'; '.join(run['creator_profile']['limits'])}; approved identity wording: **none** (identity questions must go to a human)",
          f"- wall time {run['seconds']} s (includes {t['sim_seconds_slept_for_pacing']} s of real pacing sleep)", '']
    L += ['## Totals', '', '| measure | value |', '|---|---|']
    rows = [('fan turns', t['fan_turns']), ('replies sent (turns with at least one delivered part)', t['replies_sent']),
            ('parts sent / cards sent', f"{t['parts_sent']} / {t['cards_sent']}"),
            ('drafts rejected by the validator (model calls)', t['drafts_rejected_by_validator']),
            ('drafts rejected by sim-operator-1', t['drafts_rejected_by_operator']),
            ('empty or refused model answers', t['empty_or_refused']), ('turns handed to a human', t['handed_to_human']),
            ('turns with no draft because the model was unavailable', t['model_unavailable_no_draft']),
            ('turns without any reply', t['turns_without_reply']), ('verified purchases', t['purchases_verified']),
            ('model calls (primary / retry / fallback)', f"{t['model_calls']} ({t.get('calls_primary', 0)} / {t.get('calls_retry', 0)} / {t.get('calls_fallback', 0)})"),
            ('unknown-outcome calls (kept at reservation)', t['unknown_calls']),
            ('tokens in / out', f"{t['prompt_tokens']} / {t['completion_tokens']}"),
            ('cost counted (USD)', t['total_cost_usd_counted']), ('crashes', t['crashes'])]
    L += [f'| {a} | {b} |' for a, b in rows]
    L += ['', f"Database: intents {run['database']['text_intents_by_status']}, approvals {run['database']['approvals_by_status']}", '']
    L += ['## Model identity and cost by step', '', '| step | requested model | returned model | provider named | calls | tokens in / out | cost micro-USD (settled) |', '|---|---|---|---|---|---|---|']
    groups = {}
    for p in run['personas']:
        for turn in p['turns']:
            for c in turn.get('model_calls', []):
                key = (c['chain_step'], c['requested_model'], c['returned_model'] or 'not reported', c['returned_provider'] or '-')
                g = groups.setdefault(key, [0, 0, 0, 0])
                g[0] += 1
                g[1] += c['prompt_tokens'] or 0
                g[2] += c['completion_tokens'] or 0
                g[3] += c['actual_microusd'] or 0
    for (step, req, ret, prov), (n, pt, ct, cost) in sorted(groups.items()):
        L.append(f'| {step} | `{req}` | `{ret}` | {prov} | {n} | {pt} / {ct} | {cost} |')
    L.append('')
    for p in run['personas']:
        L += [f"## Persona {p['id']}: {p['name']}", '', p['description'], '']
        for turn in p['turns']:
            L += turn_markdown(turn)
    return '\n'.join(L) + '\n'


def cost_text(c):
    if c['status'] == 'released':
        return '0 micro-USD (released: the provider answered without a completion or the request was not sent)'
    if c['actual_microusd'] is not None:
        return f"{c['actual_microusd']} micro-USD"
    return f"unknown, counted at its reservation of {c['reserved_microusd']} micro-USD"


def turn_markdown(turn):
    L = []
    if turn['kind'] == 'buy':
        L += [f"### {turn['persona']} purchase step (after fan turn {turn['turn']})", '', f"- **simulated**: {turn['simulated']}",
              f"- result: {turn.get('result')}"]
        if turn.get('webhooks'):
            L.append(f"- webhooks: {json.dumps(turn['webhooks'], default=str)}")
            L.append(f"- access evidence: {turn['access_evidence']}; payment evidence: {turn['payment_evidence']}")
        L.append('')
        return L
    tags = f" [{', '.join(turn['tags'])}]" if turn['tags'] else ''
    L += [f"### {turn['persona']} turn {turn['turn']}{tags}", '', f"**Fan (scripted):** {turn['fan_text']}", '']
    for act in turn['operator_actions']:
        L.append(f"- operator action (simulated, {act['actor']}): {act['action']} -> {act['result']}")
    L.append(f"- worker outcome: `{turn.get('worker_outcome')}`" + (f" ({turn['worker_detail']})" if turn.get('worker_detail') else ''))
    if turn.get('crash'):
        L.append(f"- **CRASH**: {turn['crash']}")
    for c in turn['model_calls']:
        returned = c['returned_model'] or 'not reported'
        L.append(f"- model call {c['chain_step']} #{c['seq']}: requested `{c['requested_model']}`, returned `{returned}` ({c['returned_provider'] or 'provider not named'}), "
                 f"status {c['status']}, verdict **{c['outcome']}**, tokens {c['prompt_tokens']}/{c['completion_tokens']}, cost "
                 f"{cost_text(c)}"
                 + (f", error `{c['error_class']}`" if c['error_class'] else ''))
        if c['draft']:
            L.append(f"  - draft ({c['draft'].get('action')}): " + ' | '.join(md_escape(x) for x in c['draft'].get('parts', []) if x))
        elif c['raw_response']:
            L.append(f"  - raw answer: {md_escape(c['raw_response'][:200])}")
        if c['validator_reasons']:
            L.append('  - validator reasons: ' + '; '.join(md_escape(r) for r in c['validator_reasons']))
    for case in turn['cases']:
        L.append(f"- handover/case: `{case['category']}` reason `{case['reason']}` {md_escape(json.dumps(case['evidence'], default=str))}")
    op = turn.get('operator')
    if op:
        L.append(f"- operator decision ({op['actor']}, rubric {op['rubric_version']}): **{op['decision']}**" + (f" (error {op['error']})" if op['error'] else ''))
        for part in op['draft_parts']:
            L.append(f"  - staged part ({part['action']}): {md_escape(part['body'])}" + (f" [card: {part['media_count']} media, {part['price_cents']} {part['currency']}]" if part['action'] == 'offer' else ''))
        for v in op['violations']:
            L.append(f"  - rubric violation {v['rule']}: {md_escape(v['detail'])}")
    if turn.get('send_result'):
        L.append(f"- delivery: {turn['send_result']['outcome']}")
    if turn['sent']:
        for s in turn['sent']:
            card = f" [CARD: {s['card']['media_count']} media = {s['card']['pictures']} pictures + {s['card']['videos']} video, {s['card']['price_cents'] / 100:.2f} {s['card']['currency']}]" if s['card'] else ''
            L.append(f"- sent to fake FansAPI (msg {s['provider_message_id']}): {md_escape(s['text'])}{card}")
    else:
        L.append('- nothing was sent this turn')
    L.append(f"- pacing: slept {turn['pacing']['slept_seconds']} s {turn['pacing']['sleeps']}; state after: {turn['state_after']}")
    L.append('')
    return L


# ======================================================================================= entry point

def default_opts(**over):
    opts = {'model': '', 'fallback': '', 'price_in': None, 'price_out': None, 'fallback_price_in': None, 'fallback_price_out': None,
            'dry_run': False, 'live': False, 'label': '', 'out_dir': None, 'inputs': None, 'dry_run_config': None,
            'max_spend_usd': 2.0, 'max_input_tokens': 8000, 'max_tokens': 500, 'max_attempts': 2, 'timeout_seconds': 45,
            'key_env': 'OPENROUTER_API_KEY', 'throttle_seconds': 1, 'limit_units': 2000, 'max_pacing_wait': 15.0,
            'personas': None, 'max_turns': None, 'no_faults': False, 'save_prompts': False,
            'operator_resolves_handovers': False}
    opts.update(over)
    return opts


def run_simulation(opts, database=None):
    """Run every selected persona; returns the run document (a dict). `database` = (url, engine) or None for a lab DB."""
    inputs = load_inputs(opts.get('inputs'))
    dry_cfg = json.loads(Path(opts.get('dry_run_config') or DEFAULT_DRY_RUN).read_text()) if opts['dry_run'] else None
    with contextlib.ExitStack() as stack:
        db = database or stack.enter_context(lab_database())
        sim = Simulation(inputs, opts, db, dry_cfg)
        try:
            sim.setup()
            run = sim.run(opts.get('personas'), opts.get('max_turns'))
        finally:
            sim.close()
    return run


def write_outputs(run, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{run['label'] or 'run'}"
    (out / f'{stem}.json').write_text(json.dumps(run, indent=2, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
    (out / f'{stem}.md').write_text(render_markdown(run), encoding='utf-8')
    return out / f'{stem}.json', out / f'{stem}.md'


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description='Plan C simulation (fake FansAPI, simulated operator, scripted fans).')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='FAKE model provider: no network, no key, no cost')
    mode.add_argument('--live', action='store_true', help='REAL model provider (costs money); needs the key in --key-env')
    ap.add_argument('--model', default=os.environ.get('ENGINE_MODEL_PRIMARY', ''), help='primary model id (ENGINE_MODEL_PRIMARY)')
    ap.add_argument('--fallback', default=os.environ.get('ENGINE_MODEL_FALLBACK', ''), help='optional fallback model id (availability failures only)')
    ap.add_argument('--price-in', type=float, default=None, help='primary USD per million input tokens (else ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK)')
    ap.add_argument('--price-out', type=float, default=None, help='primary USD per million output tokens')
    ap.add_argument('--fallback-price-in', type=float, default=None)
    ap.add_argument('--fallback-price-out', type=float, default=None)
    ap.add_argument('--max-spend-usd', type=float, default=2.0, help='hard cap for this run (daily and per-creator ledger caps)')
    ap.add_argument('--max-input-tokens', type=int, default=8000)
    ap.add_argument('--max-tokens', type=int, default=500)
    ap.add_argument('--max-attempts', type=int, default=2)
    ap.add_argument('--timeout-seconds', type=float, default=45)
    ap.add_argument('--key-env', default='OPENROUTER_API_KEY', help='NAME of the env var holding the key (the value is never printed)')
    ap.add_argument('--throttle-seconds', type=int, default=1, help='account pacing gap configured through TextAuthority')
    ap.add_argument('--inputs', default=None, help='inputs JSON (default: tools/plan_c_sim_inputs/plan_c_sim_inputs.json)')
    ap.add_argument('--dry-run-config', default=None)
    ap.add_argument('--personas', default=None, help='comma list, e.g. p1,p3')
    ap.add_argument('--max-turns', type=int, default=None, help='limit fan turns per persona (quick checks)')
    ap.add_argument('--no-faults', action='store_true', help='dry run without injected faults')
    ap.add_argument('--save-prompts', action='store_true', help='store the full prompts in the JSON (large)')
    ap.add_argument('--operator-resolves-handovers', action='store_true',
                    help='SIMULATED HUMAN (opt-in): sim-operator-1 resolves a model-handover case so the script can continue; default is strict (the fan stays held)')
    ap.add_argument('--label', default='')
    ap.add_argument('--out-dir', required=True)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    if not a.model:
        raise SystemExit('--model is required')
    label = a.label or (('dryrun-' if a.dry_run else 'live-') + model_slug(a.model))
    opts = default_opts(model=a.model, fallback=a.fallback, price_in=a.price_in, price_out=a.price_out,
                        fallback_price_in=a.fallback_price_in, fallback_price_out=a.fallback_price_out, dry_run=a.dry_run,
                        live=a.live, label=label, inputs=a.inputs, dry_run_config=a.dry_run_config, max_spend_usd=a.max_spend_usd,
                        max_input_tokens=a.max_input_tokens, max_tokens=a.max_tokens, max_attempts=a.max_attempts,
                        timeout_seconds=a.timeout_seconds, key_env=a.key_env, throttle_seconds=a.throttle_seconds,
                        personas=a.personas.split(',') if a.personas else None, max_turns=a.max_turns,
                        no_faults=a.no_faults, save_prompts=a.save_prompts,
                        operator_resolves_handovers=a.operator_resolves_handovers)
    if a.live:
        print(f'LIVE: real model calls to {a.model}' + (f' (fallback {a.fallback})' if a.fallback else '') +
              f'; hard spend cap {a.max_spend_usd} USD; the FansAPI side is still the fake server. Key variable: {a.key_env} (value not shown).', flush=True)
    run = run_simulation(opts)
    jpath, mpath = write_outputs(run, a.out_dir)
    t = run['totals']
    print(f"wrote {jpath} and {mpath}")
    print(f"turns {t['fan_turns']}, replies sent {t['replies_sent']}, validator rejects {t['drafts_rejected_by_validator']}, "
          f"operator rejects {t['drafts_rejected_by_operator']}, empty/refused {t['empty_or_refused']}, handed to human {t['handed_to_human']}, "
          f"cost {t['total_cost_usd_counted']} USD, crashes {t['crashes']}")
    return 1 if t['crashes'] else 0


if __name__ == '__main__':
    sys.exit(main())
