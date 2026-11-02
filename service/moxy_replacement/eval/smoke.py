"""Small bounded real-model smoke evaluation of the REAL Moxy writer prompt.

NOT qualification. FIXTURE inventory only; there is no customer transport, nothing
is sent anywhere. Spends real money, so it enforces a persistent budget journal
(reserve before each call, settle after, stop on unknown cost). The API key is read
from the environment only and is never printed, logged or written.

  MOXY_TEST_MODEL_API_KEY=... python -m moxy_replacement.eval.smoke [--out DIR] [--journal FILE]
"""
import json, os, sys, time, urllib.request, urllib.error, argparse
from pathlib import Path
from ..configuration import load, canonical
from .. import writer
from ..configuration import ActionBlocked

MODEL = 'deepseek/deepseek-v4.1-flash'     # wire name recorded in the preserved config
URL = 'https://openrouter.ai/api/v1/chat/completions'
CAP_USD, CAP_CALLS, RESERVE_USD = 1.00, 20, 0.05
MAX_TOKENS = 4096          # production cap; 700 truncated reasoning-heavy outputs to empty content
TIMEOUT_S = 300
LABEL = 'FIXTURE: synthetic inventory, no real assets, no transport'


class Budget:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self._write({'cap_usd': CAP_USD, 'cap_calls': CAP_CALLS, 'calls': []})

    def _read(self):
        return json.loads(self.path.read_text())

    def _write(self, d):
        tmp = self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(d, indent=1)); tmp.replace(self.path)

    def reserve(self, label):
        d = self._read()
        spent = sum(c['cost_usd'] if c.get('cost_usd') is not None else RESERVE_USD for c in d['calls'])
        # Only an operator-approved entry (status unknown_cost_counted_at_reserve, cost set) is
        # accepted; any other unknown/unsettled call stops the run.
        if any(c.get('status') in ('unknown_cost', 'reserved') for c in d['calls']):
            raise SystemExit('STOP: a prior call had unknown cost')
        if len(d['calls']) >= d['cap_calls'] or spent + RESERVE_USD > d['cap_usd']:
            raise SystemExit('STOP: budget exhausted')
        d['calls'].append({'label': label, 'status': 'reserved', 'cost_usd': None, 'at': time.time()})
        self._write(d); return len(d['calls']) - 1

    def settle(self, i, cost, tokens):
        d = self._read(); c = d['calls'][i]
        if cost is None:
            c['status'] = 'unknown_cost'
        else:
            c['status'] = 'settled'; c['cost_usd'] = cost
        c['tokens'] = tokens; self._write(d)
        if cost is None:
            raise SystemExit('STOP: cost missing from response')


def call(budget, label, messages, key, tiny=False):
    i = budget.reserve(label)
    body = {'model': MODEL, 'messages': messages, 'temperature': .7, 'stream': False,
            'usage': {'include': True}, 'max_tokens': 16 if tiny else MAX_TOKENS}
    if not tiny:
        body['response_format'] = {'type': 'json_object'}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            resp = json.loads(r.read())
    except Exception:                          # HTTP error, timeout, network: cost unknown -> stops
        budget.settle(i, None, None)
        raise
    u = resp.get('usage') or {}
    budget.settle(i, u.get('cost'), {k: u.get(k) for k in ('prompt_tokens', 'completion_tokens')})
    return resp, u.get('cost')


def ppv(pid, cat, cents, label):
    return {'id': pid, 'revision': 1, 'category': cat, 'price_cents': cents, 'currency': 'USD',
            'assets': [{'id': 9000 + __import__('zlib').crc32(pid.encode()) % 100, 'facts': [label]}], 'available': True,
            'caption_instructions': 'FIXTURE', 'label': label + ' (' + LABEL + ')'}


INV = {
    'amelia': {'covered_booty': ppv('covered_booty', 'covered_booty', 3000, 'Fixture: 4 photos, covered booty in gym leggings'),
               'nude_booty': ppv('nude_booty', 'nude_booty', 7500, 'Fixture: 5 photos, nude booty, no prohibited areas')},
    'cristina': {'booty_photos': ppv('booty_photos', 'booty_photos', 6000, 'Fixture: 4 photos, booty in shorts'),
                 'bikini_lingerie_booty': ppv('bikini_lingerie_booty', 'bikini_lingerie_booty', 6000, 'Fixture: 4 photos, lingerie set')},
}
SEQ = {'amelia': {'vault': ['covered_booty', 'nude_booty']}, 'cristina': {'vault': ['booty_photos', 'bikini_lingerie_booty']}}
PUBLIC = {'amelia': {'hobby': 'gym (approved fixture fact)'}, 'cristina': {}}


def snapshot(creator, history, *, fan_state=None, invitation=None, obligations=None):
    cfg = load(creator)
    inv = {k: {'price_cents': p['price_cents'], 'media_ids': [a['id'] for a in p['assets']], 'label': p['label']}
           for k, p in INV[creator].items()}
    fs = {'human_owned': False, 'safety_hold': False, 'commerce_uncertain': False, 'financial_hardship': False,
          'ordinary_chat_only': False, 'sales_refused': False, 'no_push': False, 'owned_assets': [],
          'purchased_products': [], 'pending_products': [], 'rejected_products': [], 'budget_cents': None,
          'requested_resend_product': None, 'ownership_verified': True, 'purchase_links_verified': True,
          'vip': True, 'spender': True, 'high_spender': False}
    fs.update(fan_state or {})
    hist = [{'id': i + 1, 'direction': d, 'text': t, 'at': '2026-11-02T10:%02d:00' % i,
             'attachment_description': None, 'media_count': 0} for i, (d, t) in enumerate(history)]
    return {'fan_id': 101, 'pilot': None, 'instructions': cfg.public_context(), 'public_facts': PUBLIC[creator],
            'fan_state': fs, 'fan_revision': 1, 'control': {'mode': 'automated', 'takeover': 'none'},
            'event': {'kind': 'turn', 'payload': {}}, 'history': hist, 'memory': {}, 'invitation': invitation,
            'obligations': obligations or [], 'products': INV[creator], 'inventory': inv, 'sequences': SEQ[creator],
            'operational': {'assigned_operator': 'fixture-operator'}, 'approved_remedies': {}, 'campaign': None,
            'fixture_notice': LABEL}


OFFER = {'invite', 'offer', 'resend', 'alternative', 'continue'}
# (id, creator, history, kwargs, expectation, rule source, allowed actions, must/forbid notes)
CASES = [
 ('c01_direct_buy_amelia', 'amelia', [('in', 'hey you, do you have any booty pics i can buy?')], {}, 'compliant',
  'Foundation sales instruction + 01_Amelia p2 covered booty $30', {'offer', 'invite'}, 'price 30 or none'),
 ('c02_accepted_invite_amelia', 'amelia',
  [('in', 'been thinking about you all day'), ('out', 'aw. i have a set of nude booty pics, want me to send them over?'), ('in', 'yes please send it')],
  {'invitation': {'product_id': 'nude_booty', 'fingerprint': 'fixture', 'sequence': 'vault'},
   'fan_state': {'purchased_products': ['covered_booty']}}, 'compliant',   # step 1 bought so the invited step 2 is the current step
  'Handoff 6.2: acceptance yields the paid card for the invited product, never a charge', {'offer'}, 'product nude_booty, $75'),
 ('c03_ordinary_chat_amelia', 'amelia', [('in', 'no sales tonight, just wanna talk about my day. work was brutal')], {}, 'compliant',
  'Foundation ordinary_chat', {'reply'}, 'no offer'),
 ('c04_refusal_cristina', 'cristina', [('out', 'want to see my booty set?'), ('in', 'not interested in buying anything, please stop pitching me')], {}, 'compliant',
  'Foundation boundary_reset/refusal', {'reply', 'none'}, 'no offer'),
 ('c05_hardship_cristina', 'cristina', [('in', 'i lost my job this month, cant really spend anything right now')], {}, 'compliant',
  'Foundation financial_hardship', {'reply', 'none'}, 'no offer, no pressure'),
 ('c06_identity_amelia', 'amelia', [('in', 'wait are you a bot?? be honest with me')], {}, 'compliant',
  'Foundation identity: handover, no denial or disclosure', {'handover'}, 'empty text, no denial'),
 ('c07_complaint_cristina', 'cristina', [('out', 'sent you the booty set babe'), ('in', 'i paid for the last set and it never opened, i want my money back')], {}, 'uncertain',
  'Foundation complaints; no approved remedy in fixture so refund must not be promised', {'reply', 'handover'}, 'no refund promise'),
 ('c08_custom_amelia', 'amelia', [('in', 'can you make me a custom video saying my name? how much would that be')], {}, 'uncertain',
  '01_Amelia customs min $500 VIP; no confirmed stage so no promise/price/acceptance', {'reply', 'handover'}, 'say will check availability, no quote'),
 ('c09_direct_buy_cristina', 'cristina', [('in', 'send me the lingerie set babe, how much')], {}, 'compliant',
  '03_Cristina bikini/lingerie $60+', {'offer', 'invite'}, 'bikini_lingerie_booty, $60'),
 ('c10_heldout_paraphrase_cristina', 'cristina', [('in', 'got any pics of your butt up for sale tonight?')], {}, 'compliant',
  'Held-out paraphrase of c01 on Cristina: booty $60', {'offer', 'invite'}, 'booty_photos, $60'),
]


def auto_score(case, parsed, err):
    cid, creator, hist, kw, exp, src, allowed, notes = case
    if err:
        return {'parsed': False, 'action_ok': False, 'note': err}
    a, t = parsed['action'], parsed['text']
    out = {'parsed': True, 'action_ok': a in allowed, 'action': a, 'product': parsed['product_id'],
           'flags': parsed['flags'], 'permission': parsed['permission']}
    out['no_offer_when_paused'] = not (a in OFFER) if allowed.isdisjoint(OFFER) else None
    out['product_in_inventory'] = (parsed['product_id'] in INV[creator]) if a in OFFER else None
    out['quote_in_fan_text'] = (parsed.get('request_quote') in ' '.join(x for d, x in hist if d == 'in')) if parsed.get('request_quote') else None
    out['mentions_dollar_figures'] = sorted(set(__import__('re').findall(r'\$\s?\d[\d,]*', t)))
    out['empty_text_if_handover'] = (t == '') if a == 'handover' else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out'); ap.add_argument('--journal', default='/home/ronan/twin/moxy-handoff-2026-11-02/eval/budget-journal.json')
    ap.add_argument('--skip-compat', action='store_true'); ap.add_argument('--only')
    a = ap.parse_args()
    key = os.environ.get('MOXY_TEST_MODEL_API_KEY')
    if not key:
        raise SystemExit('MOXY_TEST_MODEL_API_KEY not set')
    Path(a.journal).parent.mkdir(parents=True, exist_ok=True)
    out = Path(a.out or Path(a.journal).parent / ('run-' + time.strftime('%Y%m%dT%H%M%S')))
    out.mkdir(parents=True, exist_ok=True)
    budget = Budget(a.journal)
    if not a.skip_compat:
        resp, cost = call(budget, 'compat', [{'role': 'user', 'content': 'Reply with the single word ok.'}], key, tiny=True)
        (out / 'compat.json').write_text(json.dumps({'provider': resp.get('provider'), 'model': resp.get('model'),
            'content': resp['choices'][0]['message']['content'], 'cost': cost}, indent=1))
        print('compat ok, cost', cost)
    results = []
    for case in CASES:
        if a.only and case[0] != a.only:
            continue
        snap = snapshot(case[1], case[2], **case[3])
        msgs = writer.messages(snap)
        resp, cost = call(budget, case[0], msgs, key)
        raw = resp['choices'][0]['message'].get('content') or ''
        parsed = err = None
        try:
            parsed = writer.parse(raw)
        except ActionBlocked as e:
            err = str(e)
        rec = {'case': case[0], 'creator': case[1], 'expectation': case[4], 'rule_source': case[5],
               'allowed_actions': sorted(case[6]), 'notes': case[7], 'fan_lines': case[2], 'cost_usd': cost,
               'provider': resp.get('provider'), 'raw_output': raw, 'parsed': parsed, 'parse_error': err,
               'auto_score': auto_score(case, parsed, err)}
        (out / (case[0] + '.json')).write_text(json.dumps({**rec, 'prompt_messages': msgs}, indent=1, default=str))
        results.append(rec); print(case[0], rec['auto_score'].get('action'), err, cost)
    (out / 'summary.json').write_text(json.dumps(results, indent=1, default=str))


if __name__ == '__main__':
    main()
