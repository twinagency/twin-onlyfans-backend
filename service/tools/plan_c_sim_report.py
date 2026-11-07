"""Merge two Plan C simulation runs (plan_c_sim.py JSON) into one side-by-side Markdown comparison for a PDF.

    python -m tools.plan_c_sim_report RUN_A.json RUN_B.json --out comparison.md [--title "DeepSeek vs Grok"]

Pure JSON in, Markdown out: no database, no network, no model. Every cell is built only from what the two runs
recorded. The comparison states up front that it is a simulation and says exactly which inputs were identical and
which were not; it never claims a controlled A/B when the inputs differ (and says why it is still not one when they
do not: scripted fans do not react, sampling is random, one run per model).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DISCLAIMER = ('SIMULATION over a fake FansAPI with a simulated operator (sim-operator-1) and scripted fans. It is NOT a '
              'live test: nothing was sent to a real fan and no real purchase happened.')
SAME_KEYS = (('inputs_file_sha256', 'inputs file'), ('product_sha256', 'product/catalogue'), ('profile_sha256', 'creator profile'),
             ('rubric_sha256', 'operator approval rubric'), ('persona_scripts_sha256', 'persona/fan scripts'),
             ('prompt_version', 'writer prompt version'), ('temperature', 'temperature'), ('max_tokens', 'max output tokens'),
             ('max_input_tokens', 'max input tokens'), ('max_attempts', 'attempts per request'),
             ('throttle_seconds', 'pacing gap'), ('operator_resolves_handovers', 'handover resolution mode'),
             ('tool_sha256', 'harness code'))


def load(path):
    run = json.loads(Path(path).read_text(encoding='utf-8'))
    if run.get('schema') != 'plan-c-sim-run.v1':
        raise SystemExit(f'{path}: not a plan-c-sim-run.v1 file')
    return run


def esc(text):
    return str(text if text is not None else '').replace('|', '\\|').replace('\n', ' / ').strip()


def short(text, n=170):
    text = esc(text)
    return text if len(text) <= n else text[:n - 3] + '...'


def comparability(a, b):
    ia, ib = a['identity'], b['identity']
    same, diff = [], []
    for key, label in SAME_KEYS:
        (same if ia.get(key) == ib.get(key) else diff).append((label, ia.get(key), ib.get(key)))
    return same, diff


def cell(turn):
    """One compact cell for a fan turn."""
    if turn['kind'] == 'buy':
        return 'PURCHASE STEP: ' + esc(turn.get('result'))
    calls = turn['model_calls']
    op = turn.get('operator')
    sent = turn['sent']
    chain = ' > '.join(f"{c['chain_step']}:{c['outcome'] or c['status']}" for c in calls) or 'no model call'
    if turn.get('crash'):
        status = 'CRASH'
    elif sent:
        status = 'SENT'
    elif op and op['decision'] == 'reject':
        status = 'OPERATOR REJECTED'
    elif any(c['category'].startswith('journey_') for c in turn['cases']) or turn.get('worker_outcome') == 'handed_to_human':
        status = 'HANDED TO HUMAN (journey rule)'
    elif any(c['category'] == 'model_handover' for c in turn['cases']):
        status = 'HANDED TO HUMAN (model answer unusable)'
    elif turn.get('worker_outcome') == 'not_ready':
        status = 'HELD (conversation is with a human)'
    elif turn.get('send_result') and str(turn['send_result']['outcome']).startswith(('superseded', 'not_authorized')):
        status = 'HELD (' + esc(turn['send_result']['outcome']) + ')'
    elif turn.get('worker_outcome') == 'refused':
        status = 'NO DRAFT (' + short(turn.get('worker_detail'), 60) + ')'
    else:
        status = esc(turn.get('worker_outcome'))
    parts = []
    for s in sent:
        card = f" [CARD {s['card']['media_count']} media = {s['card']['pictures']} pic + {s['card']['videos']} vid, {s['card']['price_cents'] / 100:.2f} {s['card']['currency']}]" if s['card'] else ''
        parts.append(short(s['text']) + card)
    if not sent and op and op['draft_parts']:
        parts.append('draft not sent: ' + ' // '.join(short(p['body'], 100) for p in op['draft_parts']))
    reasons = []
    for c in calls:
        if c['validator_reasons'] and c['outcome'] not in ('ok',):
            reasons.append(f"{c['chain_step']} rejected: " + short('; '.join(c['validator_reasons']), 110))
    if op and op['violations']:
        reasons.append('operator: ' + short('; '.join(f"{v['rule']} {v['detail']}" for v in op['violations']), 110))
    for c in calls:
        if c['returned_model'] and c['returned_model'] != c['requested_model']:
            reasons.append(f"returned model {c['returned_model']} (asked {c['requested_model']})")
    bits = [f'**{status}**'] + (['"' + ' // '.join(parts) + '"'] if parts else []) + [f'calls: {chain}'] + reasons
    return ' · '.join(esc(b) for b in bits)


def align(pa, pb):
    ta, tb = pa['turns'], pb['turns']
    n = max(len(ta), len(tb))
    return [(ta[i] if i < len(ta) else None, tb[i] if i < len(tb) else None) for i in range(n)]


def render(a, b, title=None):
    ia, ib = a['identity'], b['identity']
    na, nb = f"A: {ia['requested_primary_model']}", f"B: {ib['requested_primary_model']}"
    same, diff = comparability(a, b)
    L = [f'# {title or "Plan C simulation comparison"}', '', '> **' + DISCLAIMER + '**', '']
    if a['dry_run'] or b['dry_run']:
        L += ['> **At least one of these runs is a DRY RUN: its model is a FAKE provider returning canned replies. A dry run says nothing about any real model.**', '']
    L += ['## Runs compared', '', '| | ' + na + ' | ' + nb + ' |', '|---|---|---|']
    rows = [('label', a['label'], b['label']), ('mode', a['mode'], b['mode']),
            ('requested primary', ia['requested_primary_model'], ib['requested_primary_model']),
            ('requested fallback', ia['requested_fallback_model'], ib['requested_fallback_model']),
            ('prices micro-USD/Mtok (in, out)', ia['prices_microusd_per_mtok']['primary'], ib['prices_microusd_per_mtok']['primary']),
            ('fallback prices', ia['prices_microusd_per_mtok']['fallback'], ib['prices_microusd_per_mtok']['fallback']),
            ('config version', ia['config_version'], ib['config_version']), ('prompt version', ia['prompt_version'], ib['prompt_version']),
            ('started', a['started_at'], b['started_at']), ('wall seconds', a['seconds'], b['seconds'])]
    L += [f'| {k} | {esc(x)} | {esc(y)} |' for k, x, y in rows]
    L += ['', '## Is this a controlled comparison?', '']
    if diff:
        L += ['**NOT a controlled A/B comparison: the inputs differ.** Differences:', '']
        L += [f'- {label}: A `{x}` vs B `{y}`' for label, x, y in diff]
        L += ['', 'Do not read any difference below as a model win.']
    else:
        L += ['The two runs used the **same** ' + ', '.join(label for label, _, _ in same) + '.', '',
              'Still **not** a controlled A/B of model quality: the fans are scripts that do not react to what each model says, '
              'conversations diverge as soon as the answers differ (one model may be held or handed over while the other continues), '
              'sampling is random (one run per model), and the operator rubric and validator are heuristics. '
              'It shows how each model behaves on identical inputs, not which is better.']
    L += ['', '## Totals side by side', '', '| measure | ' + na + ' | ' + nb + ' |', '|---|---|---|']
    keys = [('fan_turns', 'fan turns'), ('replies_sent', 'replies sent'), ('parts_sent', 'parts sent'), ('cards_sent', 'cards sent'),
            ('drafts_rejected_by_validator', 'drafts rejected by the validator'), ('drafts_rejected_by_operator', 'drafts rejected by sim-operator-1'),
            ('empty_or_refused', 'empty or refused model answers'), ('handed_to_human', 'turns handed to a human'),
            ('model_unavailable_no_draft', 'no draft: model unavailable'), ('turns_without_reply', 'turns without any reply'),
            ('purchases_verified', 'verified purchases'), ('model_calls', 'model calls'), ('calls_fallback', 'fallback calls'),
            ('unknown_calls', 'unknown-outcome calls'), ('prompt_tokens', 'prompt tokens'), ('completion_tokens', 'completion tokens'),
            ('total_cost_usd_counted', 'cost counted (USD)'), ('crashes', 'crashes')]
    L += [f"| {label} | {a['totals'].get(k)} | {b['totals'].get(k)} |" for k, label in keys]
    # persona by persona
    pb_by_id = {p['id']: p for p in b['personas']}
    tagged = []
    for pa in a['personas']:
        pb = pb_by_id.get(pa['id'])
        L += ['', f"## Persona {pa['id']}: {pa['name']}", '', pa['description'], '']
        if pb is None:
            L += ['(not present in run B)']
            continue
        L += ['| # | Fan (scripted) | ' + esc(na) + ' | ' + esc(nb) + ' |', '|---|---|---|---|']
        for ta, tb in align(pa, pb):
            ref = ta or tb
            fan = ref['fan_text'] if ref['kind'] == 'say' else '(fan buys the card)'
            tags = ref.get('tags') or []
            label = f"{ref['turn']}" + ('b' if ref['kind'] == 'buy' else '')
            if tags:
                label += ' ' + ','.join(t.split(':')[-1] for t in tags)
                tagged.append((pa['id'], label, fan, ta, tb))
            L.append(f"| {label} | {esc(fan)} | {cell(ta) if ta else 'n/a'} | {cell(tb) if tb else 'n/a'} |")
    L += ['', '## Regression and policy cases at a glance', '',
          'Cases from the review (six pictures vs three media, repeated unanswered question, withholding until earned) and the '
          'refusal, identity, boundary and purchase cases.', '', '| persona/turn | Fan | ' + esc(na) + ' | ' + esc(nb) + ' |', '|---|---|---|---|']
    for pid, label, fan, ta, tb in tagged:
        L.append(f"| {pid} {esc(label)} | {esc(fan)} | {cell(ta) if ta else 'n/a'} | {cell(tb) if tb else 'n/a'} |")
    L += ['', '## Notes', '',
          '- Fan messages, product, creator profile and operator rubric are inputs; everything else is recorded behaviour.',
          '- "SENT" means delivered to the FAKE FansAPI after a sim-operator-1 approval; nothing reached a real fan.',
          '- "HELD" turns are conversations stopped by the product (human takeover or an open handover case); the harness never hands a conversation back.',
          '- The returned model id is what the provider said answered; a mismatch with the requested id is shown in the cell.', '']
    return '\n'.join(L) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('run_a')
    ap.add_argument('run_b')
    ap.add_argument('--out', required=True)
    ap.add_argument('--title', default=None)
    args = ap.parse_args(argv)
    text = render(load(args.run_a), load(args.run_b), args.title)
    Path(args.out).write_text(text, encoding='utf-8')
    print(f'wrote {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
