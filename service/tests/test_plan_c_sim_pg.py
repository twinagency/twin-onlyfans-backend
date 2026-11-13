"""Plan C simulation harness (tools/plan_c_sim.py), dry-run only: FAKE model provider, FAKE FansAPI, no network.

One full dry run (every persona, fallback on, injected faults, handover cases resolved by the labelled opt-in) feeds most
assertions; a second, small STRICT run (no fallback, no resolving) proves the holds. Nothing here calls a real model or the
real FansAPI.
"""
import ast
import contextlib
import copy
import json
import re
import socket
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tools import plan_c_sim, plan_c_sim_report
from tools.plan_c_sim import ACCOUNT, OPERATOR, Rubric, default_opts, load_inputs, lab_database, run_simulation
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfDeliveryPolicyChange, OfTextIntent
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision
from db.onlyfans_models import OfControlAudit

PRIMARY, FALLBACK = 'dry/primary-model', 'dry/fallback-model'
AVAILABILITY = {'not_sent', 'timeout', 'lease_expired', 'http_429', 'http_408'}
CONTENT_FAILURES = {'refusal', 'empty', 'invalid', 'malformed'}
TOOLS_DIR = Path(plan_c_sim.__file__).resolve().parent


def turns(run, persona=None, kind='say'):
    for p in run['personas']:
        if persona and p['id'] != persona:
            continue
        for t in p['turns']:
            if t['kind'] == kind:
                yield t


def turn(run, persona, n):
    return next(t for t in turns(run, persona) if t['turn'] == n)


@pytest.fixture(scope='module')
def full():
    """One complete dry run; the lab database stays open for DB assertions. Every socket must stay on loopback."""
    seen = []
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        seen.append(address)
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in ('127.0.0.1', 'localhost', '::1', None):
            raise OSError(f'dry run attempted a lookup of {host!r}')
        return real_getaddrinfo(host, *a, **k)
    with contextlib.ExitStack() as stack, pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, 'connect', connect)
        mp.setattr(socket, 'getaddrinfo', getaddrinfo)
        db = stack.enter_context(lab_database())
        opts = default_opts(model=PRIMARY, fallback=FALLBACK, dry_run=True, label='test-full', operator_resolves_handovers=True)
        run = run_simulation(opts, database=db)
        yield run, db, seen


@pytest.fixture(scope='module')
def strict():
    """Strict run: NO fallback configured, handover cases are NOT resolved. p1 (rate limit at turn 4), p2 (refusal at turn 3)."""
    with lab_database() as db:
        opts = default_opts(model=PRIMARY, fallback='', dry_run=True, label='test-strict', personas=['p1', 'p2'], max_turns=6)
        yield run_simulation(opts, database=db), db


# ------------------------------------------------------------------------------------------ inputs
def test_inputs_file_is_complete_and_uses_placeholders():
    inputs = load_inputs()
    product = inputs['product']
    assert product['label'] == 'Plan C test set' and product['price_cents'] == 600 and product['currency'] == 'USD'
    assert [m['kind'] for m in product['media']].count('photo') == 2 and [m['kind'] for m in product['media']].count('video') == 1
    assert inputs['creator_profile']['name'] == 'Riley' and not inputs['creator_profile']['role_statement']
    assert {'no minors, ever', 'no meet-ups'} <= set(inputs['creator_profile']['limits'])
    ids = [p['id'] for p in inputs['personas']]
    assert ids == ['p1', 'p2', 'p3', 'p4']
    texts, tags = [], set()
    for p in inputs['personas']:
        says = [s for s in p['steps'] if 'say' in s]
        assert 18 <= len(says) <= 25
        for s in p['steps']:
            texts.append(s.get('say', ''))
            tags.update(s.get('tags', []))
    assert not any('—' in t or '–' in t for t in texts)
    assert {'regression:six_pictures_vs_three_media', 'regression:same_question_repeated', 'regression:withholding_until_earned',
            'compliment_after_purchase', 'refusal:not_now', 'refusal:explicit_stop_offering', 'identity_question'} <= tags
    assert sum(1 for p in inputs['personas'] for s in p['steps'] if s.get('buy')) >= 1
    assert all(m.startswith('9') for m in re.findall(r'\d{12,}', json.dumps(inputs)))
    for fan in inputs['ids']['fans'].values():
        assert str(fan).startswith('9000000')


# ------------------------------------------------------------------------------------------ rubric (pure)
CTX = {'sales_mode': 'open', 'purchased': False, 'card_ever_sent': True, 'card_unanswered': False, 'her_recent': []}


def part(body, action='reply', price=None, media=0):
    return {'action': action, 'body': body, 'price_cents': price, 'currency': 'USD' if price else None, 'media_ids': list(range(media))}


@pytest.mark.parametrize('body,rule', [
    ('it is six pictures and a video', 'R1_quantity'),
    ('you get 3 pics in the set', 'R1_quantity'),
    ('the set is $9.00', 'R2_price'),
    ('i can do half price for you', 'R2_price'),
    ('thanks for buying it, enjoy', 'R3_false_claim'),
    ('you already unlocked it', 'R3_false_claim'),
    ('sure, we can meet up on friday', 'R4_boundary'),
    ('add me on whatsapp', 'R4_boundary'),
    ("i'm a real person, promise", 'R5_identity'),
    ('i read every message myself', 'R5_identity'),
    ('tell me first and then i will show you', 'R7_withholding'),
    ('you have to earn it', 'R7_withholding'),
    ('goodnight, we will talk tomorrow', 'R10_timed_followup'),
    ('good night, we’ll talk tomorrow', None),
])
def test_rubric_rejects_violations_and_approves_clean_drafts(body, rule):
    rubric = Rubric(load_inputs()['approval_rubric'])
    found = {v['rule'] for v in rubric.evaluate([part(body)], CTX)}
    if rule is None:
        assert not found or found == {'R10_timed_followup'}   # a curly apostrophe is not matched by the written pattern
    else:
        assert rule in found, (body, found)


def test_rubric_card_and_state_rules_and_clean_application_card_line():
    rubric = Rubric(load_inputs()['approval_rubric'])
    clean = part('since you asked, take a look\n\nPlan C test set, USD 6.00.', 'offer', 600, 3)
    assert rubric.evaluate([clean], CTX) == []
    assert any(v['rule'] == 'R2_price' for v in rubric.evaluate([part('x\n\nPlan C test set, USD 9.00.', 'offer', 900, 3)], CTX))
    assert any(v['rule'] == 'R6_commercial_state' for v in rubric.evaluate([clean], {**CTX, 'sales_mode': 'paused'}))
    assert any(v['rule'] == 'R6_commercial_state' for v in rubric.evaluate([clean], {**CTX, 'purchased': True}))
    assert any(v['rule'] == 'R6_commercial_state' for v in rubric.evaluate([clean], {**CTX, 'card_unanswered': True}))
    assert any(v['rule'] == 'R1_quantity' for v in rubric.evaluate([part('i have 2 pictures')], {**CTX, 'card_ever_sent': False}))
    assert rubric.evaluate([part('i have 2 pictures and 1 video in it')], CTX) == []
    assert any(v['rule'] == 'R9_repeated_question' for v in rubric.evaluate(
        [part('what do you like to do on a lazy sunday?')], {**CTX, 'her_recent': ['so what do you like to do on a lazy sunday?']}))
    assert any(v['rule'] == 'R8_hygiene' for v in rubric.evaluate([part('well — hello')], CTX))
    assert any(v['rule'] == 'R3_false_claim' for v in rubric.evaluate([part('i just sent you something')], CTX))


# ------------------------------------------------------------------------------------------ the full dry run
def test_every_persona_completes_end_to_end(full):
    run, db, seen = full
    assert run['dry_run'] is True and run['totals']['crashes'] == 0
    assert [p['id'] for p in run['personas']] == ['p1', 'p2', 'p3', 'p4']
    for p in run['personas']:
        assert 18 <= p['fan_turns'] <= 25
        says = [t for t in p['turns'] if t['kind'] == 'say']
        assert len(says) == p['fan_turns'] and not any(t.get('crash') for t in p['turns'])
        assert any(t['sent'] for t in says), p['id']          # each persona got at least one delivered reply
    assert run['totals']['replies_sent'] >= 40 and run['totals']['cards_sent'] >= 1
    buy = next(turns(run, 'p1', kind='buy'))
    assert buy['result'].startswith('verified purchase') and buy['access_evidence'] == [{'status': 'confirmed', 'applied': True}]
    # the unlock never links the separate transaction by amount/time: it stays unlinked evidence
    assert {e['kind'] for e in buy['payment_evidence']} >= {'unlock'}
    after = next(t for t in turns(run, 'p1') if 'compliment_after_purchase' in t['tags'])
    assert after['sent'] and all(s['card'] is None for s in after['sent'])
    assert run['totals']['purchases_verified'] >= 1


def test_dry_run_stays_on_loopback_and_never_sees_a_key(full):
    run, db, seen = full
    assert seen and all(str(a[0]) in ('127.0.0.1', '::1') for a in seen if isinstance(a, tuple)), seen
    blob = json.dumps(run, default=str)
    assert 'dry-run-not-a-key' not in blob and not re.search(r'sk-[A-Za-z0-9]{8}', blob)
    assert run['identity']['engine_model_env']['ENGINE_MODEL_BASE_URL'] == 'https://dry-run.invalid/api/v1'
    assert all(m.startswith('9') for m in re.findall(r'(?<![0-9a-f-])\d{12,}(?![0-9a-f-])', blob))   # numeric placeholders only
    assert plan_c_sim.DISCLAIMER in run['disclaimer'] and set(run['simulated']) >= {'fansapi_network', 'operator', 'fan', 'unlock_webhook', 'pacing'}


def test_no_automatic_handback_and_no_sql_pacing_reset(full):
    run, db, _ = full
    # static: the harness has no SQL writes, no pacing column access and no hand-back control
    for name in ('plan_c_sim.py', 'plan_c_sim_fake_model.py', 'plan_c_sim_report.py'):
        source = (TOOLS_DIR / name).read_text(encoding='utf-8')
        for token in ('next_dispatch_at', 'release_takeover', 'unthrottle', 'no_throttle', 'handback', 'UPDATE ', 'DELETE FROM'):
            assert token not in source, (name, token)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('sqlalchemy'):
                assert not {a.name for a in node.names} & {'text', 'update', 'delete', 'insert'}, (name, node.names)
    source = (TOOLS_DIR / 'plan_c_sim.py').read_text(encoding='utf-8')
    controls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'control']
    assert len(controls) == 1                                  # the only control call is the initial resume_automation
    # runtime: the audit trail has one initial enabling per fan by the named operator and no hand-back, ever
    with Session(db[1]) as s:
        audits = list(s.scalars(select(OfControlAudit).where(OfControlAudit.account_id == ACCOUNT).order_by(OfControlAudit.created_at)))
        changes = list(s.scalars(select(OfDeliveryPolicyChange).where(OfDeliveryPolicyChange.account_id == ACCOUNT)))
    assert {a.action for a in audits} <= {'resume_automation', 'request_takeover'}
    resumes = [a for a in audits if a.action == 'resume_automation']
    assert len(resumes) == 4 and {a.actor for a in resumes} == {OPERATOR} and all(a.previous_revision == 0 for a in resumes)
    for takeover in (a for a in audits if a.action == 'request_takeover'):
        assert takeover.actor == 'engine-journey' and any(r.fan_id == takeover.fan_id and r.created_at <= takeover.created_at for r in resumes)
    p3 = [t for t in turns(run, 'p3')]
    assert p3[-1]['state_after']['conversation_mode'] == 'human_controlled'      # still with the human at the end
    assert len(changes) == 1 and changes[0].actor == OPERATOR                     # the policy was configured once, never touched again
    # pacing was the real mechanism: multi-part replies waited for the account gap (real sleep), nothing reset it
    multi = [t for t in turns(run) if len(t['sent']) > 1]
    assert multi and all(t['pacing']['sleeps'] and all(0 < x <= 2.5 for x in t['pacing']['sleeps']) for t in multi)


def test_every_sent_part_has_a_matching_named_approval_row(full):
    run, db, _ = full
    with Session(db[1]) as s:
        intents = list(s.scalars(select(OfTextIntent).where(OfTextIntent.account_id == ACCOUNT, OfTextIntent.status == 'accepted')))
        assert intents and len(intents) == run['totals']['parts_sent'] == run['database']['fake_fansapi_posts']
        for intent in intents:
            decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == ACCOUNT, OfEngineDecision.intent_id == intent.intent_id))
            assert decision is not None and decision.status == 'completed'
            versions = list(s.scalars(select(OfEngineApproval).where(OfEngineApproval.account_id == ACCOUNT,
                OfEngineApproval.request_id == decision.request_id, OfEngineApproval.generation == decision.generation)
                .order_by(OfEngineApproval.version)))
            assert len(versions) == 1                                   # never edited: one version per draft
            row = versions[0]
            assert row.status == 'consumed' and row.actor == OPERATOR and row.created_by == 'engine' and row.decided_at is not None
            match = [p for p in json.loads(row.parts_json) if p['decision_id'] == decision.decision_id]
            assert len(match) == 1 and match[0]['body'] == intent.body and match[0]['payload_hash'] == decision.payload_hash
        # no approval was ever created or decided by anything but the engine (staging) and the named simulated operator
        everything = list(s.scalars(select(OfEngineApproval).where(OfEngineApproval.account_id == ACCOUNT)))
        assert {r.actor for r in everything} <= {OPERATOR, None} and {r.created_by for r in everything} == {'engine'}
        assert all(r.version == 1 for r in everything)


def test_fallback_never_after_refusal_and_only_after_availability_failure(full):
    run, db, _ = full
    fallback_calls = 0
    for t in turns(run):
        calls = t['model_calls']
        for i, c in enumerate(calls):
            if c['chain_step'] != 'fallback':
                continue
            fallback_calls += 1
            before = calls[:i]
            assert before and all(b['outcome'] in AVAILABILITY or (b['error_class'] or '') in {'http_429', 'unknown_outcome'} for b in before), (t['persona'], t['turn'], before)
            assert not any(b['outcome'] in CONTENT_FAILURES for b in before)
        # once a content failure (refusal, empty, invalid) is on record, no fallback call follows it
        for i, c in enumerate(calls):
            if c['outcome'] in {'refusal', 'empty'}:
                assert not [x for x in calls[i + 1:] if x['chain_step'] == 'fallback']
    assert fallback_calls == run['totals']['calls_fallback'] >= 2
    # refusal on the primary: ONE call, no fallback, no retry, handed to a human
    refusal = turn(run, 'p2', 3)
    assert [(c['chain_step'], c['outcome']) for c in refusal['model_calls']] == [('primary', 'refusal')] and not refusal['sent']
    assert any(c['category'] == 'model_handover' and c['reason'] == 'model_refused' for c in refusal['cases'])
    empty = turn(run, 'p3', 2)
    assert [(c['chain_step'], c['outcome']) for c in empty['model_calls']] == [('primary', 'empty')]
    assert any(c['reason'] == 'model_empty' for c in empty['cases']) and not empty['sent']
    # rate limit then refusal on the fallback: exactly two calls, then a human
    both = turn(run, 'p4', 9)
    assert [(c['chain_step'], c['outcome']) for c in both['model_calls']] == [('primary', 'http_429'), ('fallback', 'refusal')]
    assert not both['sent'] and any(c['category'] == 'model_handover' for c in both['cases'])
    # rate limit, fallback answers; unknown outcome keeps its reservation and goes to a human, no fallback (G08)
    limited = turn(run, 'p1', 4)
    assert [(c['chain_step'], c['outcome'], c['status']) for c in limited['model_calls']] == [('primary', 'http_429', 'released'), ('fallback', 'ok', 'settled')]
    assert limited['sent']
    unknown = turn(run, 'p2', 9)
    assert [(c['chain_step'], c['status']) for c in unknown['model_calls']] == [('primary', 'unknown')] and not unknown['sent']
    assert any(c['reason'] == 'model_outcome_unknown' for c in unknown['cases'])
    assert unknown['model_calls'][0]['reserved_microusd'] > 0 and unknown['model_calls'][0]['actual_microusd'] is None
    assert run['totals']['unknown_calls'] == 1 and run['totals']['reserved_microusd_if_unknown'] == unknown['model_calls'][0]['reserved_microusd']


def test_requested_and_returned_models_versions_usage_and_costs_are_recorded(full):
    run, db, _ = full
    ident = run['identity']
    assert ident['requested_primary_model'] == PRIMARY and ident['requested_fallback_model'] == FALLBACK
    assert ident['prompt_version'] and ident['config_version'].startswith('cfg-') and ident['inputs_file_sha256']
    total_cost = 0
    for t in turns(run):
        for c in t['model_calls']:
            assert c['requested_model'] in (PRIMARY, FALLBACK) and (c['requested_model'] == FALLBACK) == (c['chain_step'] == 'fallback')
            if c['status'] == 'settled':
                assert c['prompt_version'] == ident['prompt_version'] and c['config_version'] == ident['config_version']
                assert c['returned_model'] and c['returned_provider'] == 'DryRunProvider'
                assert c['prompt_tokens'] > 0 and c['completion_tokens'] > 0 and c['usage_known'] and c['actual_microusd'] >= 0
                total_cost += c['actual_microusd']
    assert run['totals']['actual_microusd'] == total_cost > 0
    substituted = turn(run, 'p1', 10)['model_calls'][0]                      # the provider answered with another model id
    assert substituted['returned_model'] != substituted['requested_model'] and substituted['returned_model'].startswith(PRIMARY)
    assert (substituted['error_class'] or '').startswith('ret_model:')       # the ledger flags the mismatch itself


def test_validator_rejections_carry_their_reasons_and_one_corrected_draft(full):
    run, db, _ = full
    t = turn(run, 'p4', 5)
    assert [(c['chain_step'], c['outcome']) for c in t['model_calls']] == [('primary', 'invalid'), ('retry', 'ok')]
    assert t['model_calls'][0]['validator_reasons'] and any('six' in r or 'quantity' in r for r in t['model_calls'][0]['validator_reasons'])
    assert t['model_calls'][0]['draft']['parts'][0].startswith('I made this set') and t['sent']
    assert run['totals']['drafts_rejected_by_validator'] >= 1
    handed = turn(run, 'p2', 3)['model_calls'][0]
    assert handed['validator_reasons']                                       # the refusal's reason is on the record too


def test_operator_rejects_a_rubric_violation_without_editing_and_nothing_is_sent(full):
    run, db, _ = full
    t = turn(run, 'p4', 21)
    assert t['operator']['decision'] == 'reject' and {v['rule'] for v in t['operator']['violations']} == {'R10_timed_followup'}
    assert not t['sent'] and t['operator']['draft_parts'][0]['body'] == "good night, we'll talk tomorrow"   # exactly what the model wrote
    with Session(db[1]) as s:
        rows = list(s.scalars(select(OfEngineApproval).where(OfEngineApproval.account_id == ACCOUNT, OfEngineApproval.status == 'rejected')))
    assert len(rows) == run['totals']['drafts_rejected_by_operator'] >= 1
    assert rows[0].actor == OPERATOR and rows[0].invalidation_reason.startswith('rejected: rubric R10') and rows[0].version == 1


def test_identity_question_goes_to_a_human_and_the_chat_stays_held(full):
    run, db, _ = full
    t = turn(run, 'p3', 13)
    assert t['worker_outcome'] == 'handed_to_human' and t['model_calls'] == [] and not t['sent']
    assert any(c['category'] == 'journey_identity' for c in t['cases'])
    for n in range(14, 21):
        later = turn(run, 'p3', n)
        assert not later['sent'] and later['model_calls'] == [] and later['worker_outcome'] == 'not_ready'
    # meet-up, off-platform and custom pushes are answered inside her boundaries, never agreed to
    for n in (3, 4, 5, 6, 9):
        t = turn(run, 'p3', n)
        assert t['operator']['violations'] == []


def test_card_facts_match_the_payload_and_totals_add_up(full):
    run, db, _ = full
    cards = [s for t in turns(run) for s in t['sent'] if s['card']]
    assert cards and all(c['card'] == {'media_count': 3, 'pictures': 2, 'videos': 1, 'preview_count': 1, 'price_cents': 600, 'currency': 'USD'} for c in cards)
    assert all('USD 6.00' in c['text'] for c in cards)
    totals = run['totals']
    assert totals['parts_sent'] == sum(len(t['sent']) for t in turns(run))
    assert totals['replies_sent'] == sum(1 for t in turns(run) if t['sent'])
    assert totals['fan_turns'] == len(list(turns(run))) == sum(p['fan_turns'] for p in run['personas'])
    assert totals['handed_to_human'] >= 3 and totals['empty_or_refused'] >= 3
    # one card per fan at most (one unresolved offer), and no card after refusal/pause or after the set was bought
    for p in run['personas']:
        assert sum(1 for t in p['turns'] if t['kind'] == 'say' for s in t['sent'] if s['card']) <= 1
    for t in turns(run, 'p4'):
        if t['turn'] >= 13:
            assert all(s['card'] is None for s in t['sent'])


def test_markdown_report_states_it_is_a_simulation_and_labels_what_is_simulated(full):
    run, db, _ = full
    text = plan_c_sim.render_markdown(run)
    head = '\n'.join(text.splitlines()[:6])
    assert 'SIMULATION over a fake FansAPI with a simulated operator' in head and 'NOT a live test' in head
    assert 'DRY RUN' in text and 'sim-operator-1' in text and 'SIMULATED HUMAN' in text
    for needle in ('requested `' + PRIMARY, 'returned `', 'validator reasons', 'operator decision', 'sent to fake FansAPI', 'CARD: 3 media = 2 pictures + 1 video, 6.00 USD'):
        assert needle in text, needle


def test_comparison_report_merges_two_runs_and_never_claims_a_controlled_ab_when_inputs_differ(full):
    run, db, _ = full
    other = copy.deepcopy(run)
    other['label'] = 'test-other'
    other['identity']['requested_primary_model'] = 'dry/other-model'
    other['identity']['config_version'] = 'cfg-other'
    same = plan_c_sim_report.render(run, other, 'Two dry runs')
    head = '\n'.join(same.splitlines()[:6])
    assert 'SIMULATION' in head and 'NOT a live test' in head and 'DRY RUN' in same
    assert '## Persona p1: horny_regular' in same and '## Persona p4: sweet_slow_talker' in same and 'Regression and policy cases at a glance' in same
    assert same.count('| 1 |') >= 4 and 'A: ' + PRIMARY in same and 'B: dry/other-model' in same
    assert '**NOT a controlled A/B comparison' not in same and 'Still **not** a controlled A/B of model quality' in same
    other['identity']['product_sha256'] = 'f' * 64
    other['identity']['temperature'] = 0.2
    differ = plan_c_sim_report.render(run, other, 'Different inputs')
    assert '**NOT a controlled A/B comparison: the inputs differ.**' in differ and 'product/catalogue' in differ and 'temperature' in differ
    assert 'Do not read any difference below as a model win.' in differ


# ------------------------------------------------------------------------------------------ strict run
def test_strict_run_holds_after_a_handover_and_without_fallback_a_rate_limit_leaves_the_request_open(strict):
    run, db = strict
    assert run['totals']['crashes'] == 0 and run['totals']['calls_fallback'] == 0
    limited = turn(run, 'p1', 4)                 # 429 and NO fallback configured: no draft, the request stays open, nothing sent
    assert [(c['chain_step'], c['outcome']) for c in limited['model_calls']] == [('primary', 'http_429')]
    assert limited['worker_outcome'] == 'refused' and 'unavailable' in limited['worker_detail'] and not limited['sent']
    assert run['totals']['model_unavailable_no_draft'] >= 1
    refused = turn(run, 'p2', 3)                 # refusal -> a human; no resolving: the fan stays held
    assert any(c['category'] == 'model_handover' for c in refused['cases']) and not refused['sent']
    assert not any(a['action'].startswith('resolve_handover') for t in turns(run) for a in t['operator_actions'])
    for n in (4, 5, 6):
        held = turn(run, 'p2', n)
        assert not held['sent'] and held['send_result']['outcome'] == 'superseded:operator_hold'
    with Session(db[1]) as s:
        open_cases = list(s.scalars(select(OfOperatorCase).where(OfOperatorCase.status.in_(('open', 'acknowledged')))))
    assert any(c.category == 'model_handover' for c in open_cases)
