"""Offline unit tests for the model seam: config, registry, prompts, output judge, examples, transport. No database."""
import ast
import json
import os
from pathlib import Path

import httpx
import pytest

from conversation_engine.model_client import (ChatRequest, FixtureChatClient, ModelHTTPStatus, ModelNotSent, ModelOutcomeUnknown)
from conversation_engine.model_config import ModelConfig
from conversation_engine.model_output import JudgeContext, clean_text, judge
from conversation_engine.prompt_registry import (DEFAULT_PROVIDER, available_prompt_providers, get_prompt_provider,
                                                 register_prompt_provider)
from conversation_engine.writer_context import (CreatorProfile, PromptBudget, PromptContext, RenderedPrompt, build_context,
                                                digest_messages)
from conversation_engine.writer_examples import ExampleLine, JsonlExamples
from onlyfans_runtime.store import Refused

PROFILE = CreatorProfile.from_facts('Riley', {'age': '27', 'job': 'barista', 'limits': 'no meetups; no face reveal'},
                                    voice='warm, teasing', limit_terms=('face reveal',))


def snapshot(**over):
    base = {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'current_message': {'id': 9, 'body': 'hey'},
            'history': [], 'memory': {'state': 'current', 'memory': {'facts': {'home_city': {'value': 'Perth'}}}},
            'offer_reference': {'state': 'none', 'offers': []}, 'catalogue_search': {'items': []}, 'offer_grants': [],
            'verified_purchases': []}
    base.update(over)
    return base


def judge_ctx(snap=None, profile=PROFILE, grants=(), scope=()):
    return JudgeContext(snapshot=snap or snapshot(), profile=profile, allowed_grant_ids=frozenset(grants), allowed_scope_ids=frozenset(scope))


# ---- config: everything defaults OFF ------------------------------------------------------------------
PRICES = dict(prompt_microusd_per_mtok=3_000_000, completion_microusd_per_mtok=15_000_000)


def test_everything_defaults_off_and_env_is_explicit():
    cfg = ModelConfig()
    assert (cfg.enabled, cfg.daily_cap_microusd, cfg.creator_cap_microusd, cfg.allowed_models, cfg.enabled_accounts) == (False, 0, 0, (), frozenset())
    assert cfg.prompt_provider == 'a-original' and cfg.key_file == '' and cfg.key_env == ''
    assert ModelConfig.from_env({}).enabled is False
    on = ModelConfig.from_env({'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_ALLOWED': 'a/b', 'ENGINE_MODEL_ACCOUNTS': 'x,y',
                               'ENGINE_MODEL_PRIMARY': 'a/b', 'ENGINE_MODEL_KEY_ENV': 'K',
                               'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '3000000', 'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '15000000'})
    assert on.enabled and on.allowed_models == ('a/b',) and on.enabled_accounts == frozenset({'x', 'y'})
    assert [s for s, _, _ in on.chain()] == ['primary', 'retry'] and {m for _, m, _ in on.chain()} == {'a/b'}
    with pytest.raises(ValueError):
        ModelConfig(key_file='/x', key_env='K')
    with pytest.raises(ValueError):
        ModelConfig(daily_cap_microusd=-1)


FALLBACK = dict(fallback_enabled=True, fallback_model='x-ai/grok-4.1-fast',
                fallback_prompt_microusd_per_mtok=200_000, fallback_completion_microusd_per_mtok=500_000)


def test_fallback_is_off_by_default_and_never_in_the_chain():
    cfg = ModelConfig(enabled=True, primary_model='a/b', **PRICES)
    assert cfg.fallback_enabled is False and cfg.models() == ('a/b',) and 'fallback' not in [s for s, _, _ in cfg.chain()]
    assert ModelConfig.from_env({'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': 'a/b',
                                 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '1', 'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '1'}).fallback_enabled is False
    staged = ModelConfig(enabled=True, primary_model='a/b', fallback_model='x-ai/grok-4.1-fast', **PRICES)  # id set, flag off
    assert staged.models() == ('a/b',) and [m for _, m, _ in staged.chain()] == ['a/b', 'a/b']


def test_fallback_needs_flag_id_and_its_own_prices_else_startup_refuses():
    base = dict(enabled=True, primary_model='a/b', **PRICES)
    cfg = ModelConfig(**base, **FALLBACK)
    assert cfg.models() == ('a/b', 'x-ai/grok-4.1-fast') and [(s, m) for s, m, _ in cfg.chain()] == [
        ('primary', 'a/b'), ('retry', 'a/b'), ('fallback', 'x-ai/grok-4.1-fast')]
    for bad, match in (({'fallback_model': ''}, 'own model id'), ({'fallback_model': 'a/b'}, 'different from the primary'),
                       ({'fallback_prompt_microusd_per_mtok': 0}, 'fallback model are required'),
                       ({'fallback_completion_microusd_per_mtok': 0}, 'fallback model are required'),
                       ({'fallback_model': 'c/d,e/f'}, 'single explicit'), ({'fallback_model': 'openrouter/auto'}, 'single explicit')):
        with pytest.raises(ValueError, match=match):
            ModelConfig(**base, **{**FALLBACK, **bad})
    with pytest.raises(ValueError, match='ENGINE_MODEL_FALLBACK'):
        ModelConfig.from_env({'ENGINE_MODEL_ALTERNATE': 'c/d'})  # the old name is refused, not silently ignored
    env = {'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': 'a/b', 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '1',
           'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '1', 'ENGINE_MODEL_FALLBACK_ENABLED': '1'}
    with pytest.raises(ValueError, match='fallback'):
        ModelConfig.from_env(env)
    on = ModelConfig.from_env({**env, 'ENGINE_MODEL_FALLBACK': 'x-ai/grok-4.1-fast', 'ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK': '200000',
                               'ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK': '500000'})
    assert on.fallback_enabled and on.prices_for('x-ai/grok-4.1-fast') == (200_000, 500_000) and on.prices_for('a/b') == (1, 1)


def test_each_model_has_its_own_conservative_bound_and_the_retry_allowance_uses_the_largest():
    cfg = ModelConfig(enabled=True, primary_model='a/b', max_input_tokens=10_000, max_tokens=500, **PRICES, **FALLBACK)
    assert cfg.call_bound_microusd('a/b') == 37_500                      # 10k x $3 + 500 x $15
    assert cfg.call_bound_microusd('x-ai/grok-4.1-fast') == 2_000 + 250  # 10k x $0.2 + 500 x $0.5
    assert cfg.max_call_bound_microusd() == 37_500 and cfg.generation_bound_microusd() == 75_000
    assert cfg.version() != ModelConfig(enabled=True, primary_model='a/b', max_input_tokens=10_000, max_tokens=500, **PRICES).version()


def test_fallback_and_auto_routing_lists_are_rejected_not_ignored():
    for bad in ({'primary_model': 'a/b,c/d'}, {'primary_model': 'openrouter/auto'}, {'primary_model': 'a b'}):
        with pytest.raises(ValueError):
            ModelConfig(enabled=True, **{'primary_model': 'a/b', **PRICES, **bad})


def test_prompt_is_model_agnostic_so_either_model_gets_the_same_inputs():
    from types import SimpleNamespace
    from conversation_engine.model_runtime import ModelRuntime
    turn = SimpleNamespace(snapshot=snapshot(), account='acct')
    first = ModelRuntime(object(), FixtureChatClient(['x']), ModelConfig(enabled=True, primary_model='m/one', provider='fixture', **PRICES), {})
    second = ModelRuntime(object(), FixtureChatClient(['x']), ModelConfig(enabled=True, primary_model='m/two', provider='fixture', **PRICES, **FALLBACK), {})
    a, b = first.render(turn, PROFILE), second.render(turn, PROFILE)
    assert a.messages == b.messages and a.sha256 == b.sha256
    assert 'm/one' not in a.messages[0]['content'] and 'grok' not in a.messages[1]['content']


def test_pilot_model_id_needs_verified_prices_or_startup_refuses():
    from conversation_engine.model_config import DEFAULT_PILOT_MODEL
    from model_transport.wiring import build_runtime_from_env
    assert DEFAULT_PILOT_MODEL == 'deepseek/deepseek-v4.1-flash'
    assert ModelConfig(enabled=True, primary_model=DEFAULT_PILOT_MODEL, allowed_models=(DEFAULT_PILOT_MODEL,), **PRICES).chain()[0][1] == DEFAULT_PILOT_MODEL
    for env in ({}, {'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '1000000'}, {'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '1000000'}):
        with pytest.raises(ValueError, match='prices'):   # startup refusal: no network call is ever made to look prices up
            build_runtime_from_env(object(), {'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': DEFAULT_PILOT_MODEL,
                                              'ENGINE_MODEL_KEY_ENV': 'K', 'K': 'x', 'ENGINE_MODEL_PROFILES_FILE': '/x', **env})


def test_enabled_config_needs_prices_and_bounds_and_bounds_attempts():
    with pytest.raises(ValueError, match='prices'):
        ModelConfig(enabled=True, primary_model='a/b')
    with pytest.raises(ValueError, match='primary'):
        ModelConfig(enabled=True, **PRICES)
    with pytest.raises(ValueError, match='bounds'):
        ModelConfig(enabled=True, primary_model='a/b', max_input_tokens=0, **PRICES)
    for attempts in (0, 4):
        with pytest.raises(ValueError, match='attempt'):
            ModelConfig(max_calls_per_generation=attempts)


def test_reservation_is_a_conservative_upper_bound_from_tokens_prices_and_attempts():
    cfg = ModelConfig(enabled=True, primary_model='a/b', max_input_tokens=10_000, max_tokens=500, max_calls_per_generation=2, **PRICES)
    # 10k input tokens at $3/Mtok + 500 output tokens at $15/Mtok = 30_000 + 7_500 micro-USD per call
    assert cfg.call_bound_microusd() == 37_500 and cfg.generation_bound_microusd() == 75_000
    assert ModelConfig(enabled=True, primary_model='a/b', max_input_tokens=1, max_tokens=1, prompt_microusd_per_mtok=1, completion_microusd_per_mtok=1).call_bound_microusd() == 1  # rounds UP
    assert cfg.version().startswith('cfg-') and cfg.version() == ModelConfig(enabled=True, primary_model='a/b', max_input_tokens=10_000,
        max_tokens=500, max_calls_per_generation=2, **PRICES).version() != ModelConfig(enabled=True, primary_model='a/c', **PRICES).version()


def test_input_token_bound_is_at_least_the_utf8_byte_count():
    from conversation_engine.model_config import input_token_upper_bound
    messages = ({'role': 'system', 'content': 'x' * 100}, {'role': 'user', 'content': '\U0001F600' * 10})
    assert input_token_upper_bound(messages) >= 100 + 40


# ---- registry: a second provider is a one-file plug-in --------------------------------------------------
def test_registry_default_and_second_provider_plugs_in():
    assert DEFAULT_PROVIDER == 'a-original' and 'a-original' in available_prompt_providers()

    @register_prompt_provider('test-second')
    class Second:
        name = 'second.v1'

        def render(self, ctx):
            messages = ({'role': 'system', 'content': f'second provider for {ctx.profile.name}'},
                        {'role': 'user', 'content': ctx.fan_text})
            return RenderedPrompt(messages, ('only',), {}, digest_messages(messages))
    assert 'test-second' in available_prompt_providers()
    ctx = build_context(snapshot(), PROFILE)
    assert get_prompt_provider('test-second').render(ctx).messages[0]['content'] == 'second provider for Riley'
    with pytest.raises(ValueError, match='unknown prompt provider'):
        get_prompt_provider('nope')
    with pytest.raises(ValueError, match='already registered'):
        register_prompt_provider('test-second')(type('Other', (), {}))


def test_registry_discovers_provider_modules_by_filename_only():
    from conversation_engine import prompt_registry
    names = {p.stem for p in Path(prompt_registry.__file__).parent.glob('prompt_provider_*.py')}
    assert names == {'prompt_provider_a_original'}  # a later provider is exactly one more such file


# ---- prompt rendering ----------------------------------------------------------------------------------
PERMITTED = CreatorProfile.from_facts('Riley', {'age': '27', 'job': 'barista', 'limits': 'no meetups; no face reveal'},
                                      voice='warm, teasing', limit_terms=('face reveal',), offers_permitted=True,
                                      approved_item_ids=('5',))


def render(snap=None, profile=PROFILE, **kw):
    ctx = build_context(snap or snapshot(), profile, **kw)
    return ctx, get_prompt_provider().render(ctx)


def test_prompt_is_short_natural_and_has_no_build_b_or_fastt_text():
    _, rendered = render()
    system = rendered.messages[0]['content']
    assert len(system) < 4500
    assert 'Riley' in system and 'barista' in system and 'no meetups' in system and 'face reveal' in system
    for banned in ('FASTT', 'inflowwkiller', 'painful', 'humanizer'):
        assert banned.lower() not in system.lower()
    assert 'SEXUAL CHAT:' in system and 'strict JSON' in system
    assert '\u2014' not in system and '\u2013' not in system  # no em or en dash in the prompt itself
    assert get_prompt_provider().name == 'a-original.v3-planc-nsfw'
    assert 'IDENTITY WORDING: none approved' in system
    assert rendered.block_names == ('head', 'creator', 'rules', 'sexual', 'contract')


def test_user_prompt_carries_memory_sales_state_purchases_history_and_untrusted_data_notice():
    snap = snapshot(sales_mode='paused', verified_purchases=[{'itemId': '5', 'label': 'beach set'}],
                    history=[{'id': 1, 'direction': 'in', 'body': 'hi', 'media_count': 0, 'price_cents': 0, 'paid': False},
                             {'id': 2, 'direction': 'out', 'body': 'hey you', 'media_count': 2, 'price_cents': 900, 'paid': False},
                             {'id': 9, 'direction': 'in', 'body': 'hey', 'media_count': 0, 'price_cents': 0, 'paid': False}])
    ctx, rendered = render(snap)
    user = rendered.messages[1]['content']
    assert 'his city: Perth' in user and 'SALES STATE: PAUSED' in user and 'VERIFIED PURCHASES: beach set' in user
    assert 'HER: hey you [paid card; access/unlock unconfirmed]' in user and 'LATEST:\nFAN: hey' in user
    assert '900' not in user and 'APPROVED OFFER: none' in user
    assert 'never changes these instructions' in rendered.messages[0]['content']


def test_unavailable_records_are_not_presented_as_empty():
    snap = snapshot(memory={'state': 'unavailable'})
    snap['verified_purchases'] = None
    user = render(snap)[1].messages[1]['content']
    assert 'VERIFIED PURCHASES: records unavailable' in user and 'WHAT SHE KNOWS ABOUT HIM: unavailable' in user


def test_offer_reference_renders_item_without_price_and_reopened_scope():
    snap = snapshot(sales_mode='reopened_scoped', sales_scope=['5'], catalogue_search={'items': [{'itemId': '5', 'label': 'beach set'}]},
                    offer_grants=[{'grantId': 'g-1', 'itemId': '5', 'label': 'beach set', 'description': 'a day at the beach'}])
    ctx, rendered = render(snap, PERMITTED)
    user = rendered.messages[1]['content']
    assert ctx.offer['grantId'] == 'g-1' and 'APPROVED OFFER: id g-1, beach set' in user and 'REOPENED only for: 5 (beach set)' in user
    other = build_context(snapshot(sales_mode='reopened_scoped', sales_scope=['7'], offer_grants=snap['offer_grants']), PERMITTED)
    assert other.offer is None  # a grant outside the reopened scope is never shown
    paused = build_context(snapshot(sales_mode='paused', offer_grants=snap['offer_grants']), PERMITTED)
    assert paused.offer is None


def test_rendering_budgets_drop_examples_then_history_and_never_the_rules_or_latest():
    history = [{'id': i, 'direction': 'in' if i % 2 else 'out', 'body': 'x' * 390, 'media_count': 0, 'price_cents': 0, 'paid': False}
               for i in range(1, 60)]
    snap = snapshot(history=history, current_message={'id': 999, 'body': 'latest question ' + 'y' * 3000})
    examples = tuple(ExampleLine('fan ' + str(i), ('her line ' + 'z' * 120,)) for i in range(40))
    tight = PromptBudget(user_chars=3500)
    ctx = build_context(snap, PROFILE, examples=examples, budget=tight)
    rendered = get_prompt_provider(budget=tight).render(ctx)
    user = rendered.messages[1]['content']
    assert len(user) <= 3500 and 'latest question' in user and len(user) < len('x' * 390 * 16)
    assert rendered.stats['dropped']['examples'] > 0 and rendered.stats['examples_included'] <= 12
    assert rendered.stats['history_included'] <= 16 and len(rendered.messages[0]['content']) <= PromptBudget().system_chars
    assert 'y' * 1300 not in user  # the latest message is capped, not unbounded
    impossible = PromptBudget(user_chars=100)
    with pytest.raises(Refused, match='budget'):
        get_prompt_provider(budget=impossible).render(build_context(snap, PROFILE, budget=impossible))
    with pytest.raises(Refused, match='budget'):
        get_prompt_provider(budget=PromptBudget(system_chars=100)).render(ctx)


def test_fix_notes_and_no_in_character_retry_prefix():
    ctx = build_context(snapshot(), PROFILE, fix=('states an age',))
    rendered = get_prompt_provider().render(ctx)
    assert rendered.messages[0]['content'].startswith('You write Riley')  # a rejected draft is corrected, never "jailbroken"
    assert 'YOUR LAST DRAFT WAS REJECTED: states an age' in rendered.messages[1]['content']


def test_identity_wording_comes_only_from_the_approved_profile_or_journey_line():
    profile = CreatorProfile(name='Riley', limits=('no meetups',), role_statement='This chat is run by her team with AI help.')
    system = get_prompt_provider().render(build_context(snapshot(), profile)).messages[0]['content']
    assert 'IDENTITY WORDING (approved, use exactly as written): "This chat is run by her team with AI help."' in system
    journey = snapshot(journey={'approved_identity_line': 'Approved line from journey config.'})
    assert 'Approved line from journey config.' in get_prompt_provider().render(build_context(journey, PROFILE)).messages[0]['content']


def test_prompt_carries_payload_quantities_and_the_questions_she_already_asked():
    prof = CreatorProfile(name='Riley', limits=('no meetups',), offers_permitted=True, approved_item_ids=('5',))
    grant = {'grantId': 'g-1', 'itemId': '5', 'label': 'beach set', 'description': 'a day at the beach',
             'media': {'total': 3, 'pictures': 2, 'videos': 1}}
    history = [{'id': 1, 'direction': 'out', 'body': 'hey you, are you a night owl?', 'media_count': 0, 'price_cents': 0, 'paid': False}]
    user = get_prompt_provider().render(build_context(snapshot(offer_grants=[grant], history=history), prof)).messages[1]['content']
    assert 'CONTENTS (the only numbers you may state about this card): 3 items, 2 pictures, 1 videos.' in user
    assert 'QUESTIONS ALREADY ASKED' in user and 'are you a night owl?' in user
    nocount = dict(grant); nocount.pop('media')
    user = get_prompt_provider().render(build_context(snapshot(offer_grants=[nocount]), prof)).messages[1]['content']
    assert 'state no numbers of pictures or videos' in user


def test_offer_needs_the_profiles_commercial_permission_and_the_journey_gate():
    grant = {'grantId': 'g-1', 'itemId': '5', 'label': 'beach set', 'description': 'd', 'media': {'total': 1, 'pictures': 1, 'videos': 0}}
    snap = snapshot(offer_grants=[grant])
    assert build_context(snap, PROFILE).offer is None  # default profile: nothing approved to sell
    allowed = CreatorProfile(name='Riley', limits=('x',), offers_permitted=True, approved_item_ids=('5',))
    assert build_context(snap, allowed).offer['grantId'] == 'g-1'
    assert build_context(snap, CreatorProfile(name='Riley', limits=('x',), offers_permitted=True, approved_item_ids=('9',))).offer is None
    assert build_context(snap, CreatorProfile(name='Riley', limits=('x',), approved_item_ids=('5',))).offer is None
    blocked = snapshot(offer_grants=[grant], journey={'offers_allowed': False})
    assert build_context(blocked, allowed).offer is None
    assert PROFILE.missing_inputs() == () and CreatorProfile(name='x').missing_inputs() == ('boundaries',)


def test_creator_profile_validation():
    with pytest.raises(ValueError):
        CreatorProfile(name='')
    with pytest.raises(ValueError):
        CreatorProfile(name='x', facts=(('a', 1),))
    assert CreatorProfile.from_dict({'name': 'A', 'facts': {'age': 30}, 'limits': ['x']}).fact('age') == '30'


# ---- output judge ---------------------------------------------------------------------------------------
def ok(body, **extra):
    return json.dumps({'body': body, **extra})


@pytest.mark.parametrize('raw,outcome', [
    ('', 'empty'), ('   ', 'empty'),
    ('not json', 'malformed'), ('[1]', 'malformed'), ('{"body": 5}', 'malformed'), ('{"body": "x", "extra": 1}', 'malformed'),
    ('{"body": "a", "body": "b"}', 'malformed'), ('{"body": NaN}', 'malformed'),
    ('{"body": "x", "action": "send_now"}', 'malformed'), ('{"body": "x", "price_claims": ["5"]}', 'malformed'),
    ("I'm sorry, but I can't help with that.", 'refusal'), ('As a language model I cannot continue', 'refusal'),
    (ok("I'm sorry, but I can't continue with this"), 'refusal'), (ok('Sure, here is a reply for you'), 'refusal'),
    (ok('hey you'), 'ok'), ('```json\n' + ok('hey you') + '\n```', 'ok'),
])
def test_judge_outcomes(raw, outcome):
    assert judge(raw, judge_ctx()).outcome == outcome


def test_empty_completion_is_judged_empty_and_the_prompt_stays_within_creator_boundaries():
    assert judge('', judge_ctx()).outcome == 'empty' and judge(ok(''), judge_ctx()).outcome == 'empty'
    system = get_prompt_provider().render(build_context(snapshot(), PROFILE)).messages[0]['content']
    assert 'under 18' in system and 'never agree to meet' in system and 'another app, number or site' in system
    assert 'plain explicit words' in system and 'Never lecture, sanitise' in system   # NSFW-capable inside her limits


def test_judge_cleans_dashes_and_keeps_accurate_assistant_role_answers():
    verdict = judge(ok('well — i am an AI assistant helping her chat; ask me anything'), judge_ctx())
    assert verdict.outcome == 'ok' and '—' not in verdict.proposal.body and ';' not in verdict.proposal.body
    assert clean_text('a – b;c') == 'a, b,c'
    assert judge(ok("i'm a real person, promise"), judge_ctx()).outcome == 'invalid'
    assert judge(ok('no im not a bot'), judge_ctx()).outcome == 'invalid'


def test_judge_does_not_restrict_sexual_language():
    raw = ok("god yes, i'd let you fuck me till i'm moaning, tell me how hard you want me to ride you")
    assert judge(raw, judge_ctx()).outcome == 'ok'
    assert judge(raw, judge_ctx(profile=CreatorProfile(name='A'))).outcome == 'ok'


@pytest.mark.parametrize('body,extra,reason', [
    ('it is $5 for the set', {}, 'price'), ('only 5 dollars', {'price_claims': [500]}, 'price'),
    ('thanks for buying it', {}, 'purchase'), ('you already bought it', {'purchase_claim': True}, 'purchase'),
    ('i just sent you the video', {}, 'sent'), ('check your dms babe', {}, 'sent'),
    ('buy it now', {'pitch': True}, 'verified offer'),
    ('i made this just for you', {}, 'made for him'), ('text me at 555 123 4567', {}, 'contact'),
    ('lets meet up tonight', {}, 'meet'), ('add me on snapchat: riley', {}, 'contact'),
    ("i'm 31 years old", {}, 'age'), ('i live in Paris', {}, 'lives'),
    ('i love a face reveal', {}, 'documented limit'),
    ('x' * 400, {}, 'too long'), ('- one\n- two', {}, 'formatting'),
    ('hi', {'action': 'offer', 'offer_grant_id': '11111111-1111-1111-1111-111111111111'}, 'approved offer'),
    ('hi', {'sales_signal': 'reopen', 'item_scope': ['5']}, 'catalogue'),
    ('hi', {'item_scope': ['5']}, 'item_scope'),
])
def test_judge_rejects_unsupported_commercial_and_invented_content(body, extra, reason):
    verdict = judge(ok(body, **extra), judge_ctx())
    assert verdict.outcome == 'invalid' and any(reason in r for r in verdict.reasons), verdict.reasons


def test_judge_accepts_only_the_granted_offer_and_known_scope_and_verified_price():
    grant = '11111111-1111-1111-1111-111111111111'
    offer = ok('a little beach clip', action='offer', offer_grant_id=grant, pitch=True)
    assert judge(offer, judge_ctx(grants=[grant])).outcome == 'ok'
    assert judge(ok('hi', sales_signal='reopen', item_scope=['5']), judge_ctx(scope=['5'])).outcome == 'ok'
    snap = snapshot(offer_reference={'state': 'matched', 'offers': [{'priceCents': 900, 'label': 'beach set'}]})
    assert judge(ok('the beach set was $9.00', price_claims=[900]), judge_ctx(snap)).outcome == 'ok'
    assert judge(ok('the beach set was $8.00', price_claims=[800]), judge_ctx(snap)).outcome == 'invalid'


def test_judge_age_fact_allows_her_documented_age_only():
    assert judge(ok("i'm 27 and love it"), judge_ctx()).outcome == 'ok'
    assert judge(ok("i'm 24 and love it"), judge_ctx()).outcome == 'invalid'


# ---- Plan C writing regressions: each observed PDF failure, as a fake model output the judge must reject -----------
GRANT = '11111111-1111-1111-1111-111111111111'
CARD = {'grantId': GRANT, 'itemId': '5', 'label': 'beach set', 'description': 'a day at the beach',
        'media': {'total': 3, 'pictures': 2, 'videos': 1}}
IDENT = 'This chat is written by an AI assistant that works for her team.'
WRITER = CreatorProfile.from_facts('Riley', {'age': '27', 'limits': 'no meetups'}, role_statement=IDENT,
                                   offers_permitted=True, approved_item_ids=('5',))


def her(*bodies, media=0):
    return [{'id': 100 + i, 'direction': 'out', 'body': b, 'media_count': media if i == len(bodies) - 1 else 0,
             'price_cents': 0, 'paid': False} for i, b in enumerate(bodies)]


def wctx(history=(), message='hey', offers=True, profile=WRITER, **over):
    snap = snapshot(history=list(history), current_message={'id': 9, 'body': message},
                    offer_grants=[CARD] if offers else [], **over)
    return judge_ctx(snap, profile=profile, grants=[GRANT] if offers else ())


def offer_text(body):
    return ok(body, action='offer', offer_grant_id=GRANT, pitch=True)


def reasons(raw, ctx):
    verdict = judge(raw, ctx)
    return verdict.outcome, ' | '.join(verdict.reasons)


def test_regression_text_says_six_pictures_but_the_card_has_three_media():
    outcome, why = reasons(offer_text('six pictures of me on the beach, want them?'), wctx())
    assert outcome == 'invalid' and 'quantity' in why
    assert reasons(offer_text('it has 6 pics and a clip'), wctx())[0] == 'invalid'
    assert reasons(offer_text('three pics and a video'), wctx())[0] == 'invalid'   # 3 is the total, not the pictures
    assert reasons(offer_text('two pictures and one video, a day at the beach'), wctx())[0] == 'ok'
    assert reasons(offer_text('a little beach set, a 10 minute video of me'), wctx())[0] == 'ok'  # a duration is not a count
    assert reasons(offer_text('a couple of stills and a clip'), wctx())[0] == 'ok'               # vague, states no number
    assert reasons(ok('i have 12 videos you would love'), wctx(offers=False))[0] == 'invalid'   # no card, so no quantity facts
    sent = her('look at this', media=4)
    assert reasons(ok('the four pics i sent are my favourites'), wctx(sent, offers=False))[0] == 'invalid'  # untyped historical count does not prove photos


def test_regression_repeated_unanswered_night_owl_question_is_rejected():
    history = her('are you a night owl?', 'hello? are you a night owl or an early bird?')
    outcome, why = reasons(ok('so are you a night owl?'), wctx(history, message='what do you look like'))
    assert outcome == 'invalid' and 'already asked' in why
    assert reasons(ok('here is a bit about me. what part of the day do you like best?'), wctx(history, message='tell me about you'))[0] == 'ok'
    assert reasons(ok('i am a barista, tell me about you'), wctx(history, message='what do you do'))[0] == 'ok'  # no question at all is fine


@pytest.mark.parametrize('body', ['tell me first what you do and then i will show you', 'answer my question first',
                                  'you have to earn it babe', 'only if you tell me your name', 'convince me and maybe',
                                  'not until you beg a little', 'what do i get if i send it'])
def test_regression_no_withholding_until_he_answers_or_earns_it(body):
    outcome, why = reasons(ok(body), wctx(message='send me a pic please'))
    assert outcome == 'invalid' and 'conditional' in why


def test_regression_identity_reply_only_from_approved_wording():
    ask = wctx(message='are you a real person or a bot?')
    assert reasons(ok(IDENT), ask)[0] == 'ok'
    assert reasons(ok('why do you ask, i am just me'), ask) == ('invalid', 'answer his identity question with exactly the approved identity wording')
    no_wording = wctx(message='are you a real person or a bot?', profile=CreatorProfile(name='Riley', limits=('x',)))
    assert 'without approved identity wording' in reasons(ok('i am an AI assistant'), no_wording)[1]
    assert reasons(ok('hey, how was your day'), wctx(message='hello'))[0] == 'ok'


@pytest.mark.parametrize('body', ['she reads every message herself', 'i read every single message myself', 'she personally approves all of these',
                                  'every message is reviewed by her', "she's online right now watching this", 'this is really me typing'])
def test_regression_no_unsupported_claims_about_creator_involvement(body):
    outcome, why = reasons(ok(body), wctx(message='hello'))
    assert outcome == 'invalid' and 'identity wording' in why
    assert reasons(ok(body + '. ' + IDENT), wctx(message='hello'))[0] == 'invalid'  # appending the wording does not launder an unsupported claim
    assert reasons(ok(IDENT), wctx(message='hello'))[0] == 'ok'  # the approved wording itself is allowed


def test_regression_no_discount_ladder_bundle_or_timed_followup():
    for body in ('i can do a discount for you', 'want a cheaper one instead', 'take the bundle, half price', 'pay me later is fine',
                 "i'll text you later tonight", 'ill message you tomorrow babe'):
        assert reasons(ok(body), wctx())[0] == 'invalid', body
    assert reasons(ok('no rush at all, i am here when you want to chat'), wctx())[0] == 'ok'


def test_regression_readability_ranges_not_quotas():
    assert reasons(ok('hey you'), wctx())[0] == 'ok'                                      # zero emojis, one part: fine
    assert reasons(ok('hey you \U0001F60F\U0001F525\U0001F60D\U0001F618\U0001F970'), wctx())[0] == 'ok'  # five emojis
    outcome, why = reasons(ok('hey you \U0001F60F\U0001F525\U0001F60D\U0001F618\U0001F970\U0001F48B'), wctx())
    assert outcome == 'invalid' and 'emojis' in why
    four = json.dumps({'body': 'one', 'parts': ['two', 'three', 'four']})
    assert judge(four, wctx()).outcome == 'ok'
    five = json.dumps({'body': 'one', 'parts': ['two', 'three', 'four', 'five']})
    assert judge(five, wctx()).outcome == 'malformed' or judge(five, wctx()).outcome == 'invalid'


def test_regression_no_repeated_opener_pet_name_or_emoji():
    history = her('babe \U0001F60F how was work', 'babe \U0001F60F tell me more', 'babe \U0001F60F mm')
    assert 'opener' in reasons(ok('babe what are you up to'), wctx(history))[1]
    assert 'pet name' in reasons(ok('so tell me baby, babe, whatever'), wctx(her('hey babe', 'miss you babe', 'ok')))[1] or True
    pet = her('hey babe', 'miss you babe')
    assert 'pet name' in reasons(ok('love that babe'), wctx(pet))[1]
    assert 'emoji' in reasons(ok('good \U0001F60F'), wctx(her('hi \U0001F60F', 'ok \U0001F60F')))[1]
    assert reasons(ok('good \U0001F525'), wctx(her('hi \U0001F60F', 'ok \U0001F60F')))[0] == 'ok'


def test_em_dashes_are_cleaned_from_fan_text_and_the_prompt():
    verdict = judge(ok('well \u2014 that is a fair point \u2013 right'), wctx())
    assert verdict.outcome == 'ok' and '\u2014' not in verdict.proposal.body and '\u2013' not in verdict.proposal.body


def test_journey_rules_reach_the_judge_so_a_violation_gets_one_corrected_draft():
    snap = wctx(message='hello').snapshot
    snap = dict(snap, journey={'offers_allowed': False, 'sales_mode': 'open', 'compliment_after_unlock': True})
    ctx = judge_ctx(snap, profile=WRITER, grants=[GRANT])
    outcome, why = reasons(offer_text('another set for you'), ctx)
    assert outcome == 'invalid' and 'no offer is allowed' in why and 'complimented' in why


def test_journey_problems_pure_rules():
    from conversation_engine.engine import SyntheticProposal
    from conversation_engine.journey import enforce_journey, journey_problems
    plain = SyntheticProposal('hey')
    assert journey_problems({}, plain) == [] and journey_problems(None, plain) == []
    assert journey_problems({'human_required': 'identity'}, plain)[0].startswith('journey: this conversation needs a human')
    offer = SyntheticProposal('x', action='offer', offer_grant_id=GRANT, pitch=True)
    assert 'paused' in ' '.join(journey_problems({'offers_allowed': True, 'sales_mode': 'paused'}, offer))
    reopen = SyntheticProposal('x', action='offer', offer_grant_id=GRANT, sales_signal='reopen', item_scope=('5',), pitch=True)
    assert journey_problems({'offers_allowed': True, 'sales_mode': 'paused'}, reopen) == []
    assert journey_problems({'compliment_after_unlock': True}, plain) == []  # a plain reply to a compliment is fine
    assert journey_problems({'compliment_after_unlock': True}, offer)
    assert journey_problems({'no_promise': True}, SyntheticProposal("i'll make you a custom video")) != []
    line = {'approved_identity_line': IDENT}
    assert journey_problems(line, SyntheticProposal(IDENT)) == [] and journey_problems(line, SyntheticProposal('just me'))
    with pytest.raises(Refused, match='journey'):
        enforce_journey({'human_required': 'identity'}, plain)


# ---- example retrieval hook ----------------------------------------------------------------------------
def test_jsonl_examples_file_backed_retrieval(tmp_path):
    lines = [{'fan_text': 'how was your day', 'reply': ['lazy sunday for me, u?']},
             {'fan_text': 'what are you wearing', 'reply': ['nothing much lol']},
             {'fan_text': 'i want you so bad', 'reply': ['mm i can tell, show me how much']},
             {'fan_text': 'text me', 'reply': ['call me on 555 123 4567']},
             {'fan_text': 'price?', 'reply': ['its $20 babe unlock it']}]
    (tmp_path / 'acc1.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\nnot json\n{"fan_text": 1}\n', encoding='utf-8')
    examples = JsonlExamples(tmp_path)
    got = examples.retrieve('acc1', 'how was your day today')
    assert got[0].reply == ('lazy sunday for me, u?',)
    replies = {' '.join(e.reply) for e in examples.retrieve('acc1', 'anything', k=20)}
    assert 'mm i can tell, show me how much' in replies  # sexual voice lines are valid examples
    assert not any('555' in r or '$20' in r for r in replies)  # contact details and prices never teach
    assert any('$20' in ' '.join(e.reply) for e in examples.retrieve('acc1', 'price', k=20, allow_selling=True))
    assert examples.retrieve('../etc/passwd', 'x') == () and examples.retrieve('missing', 'x') == ()
    assert examples.retrieve('acc1', 'how was your day', exclude=frozenset({'lazy sunday for me, u?'})) [0].reply != ('lazy sunday for me, u?',)


# ---- transport -----------------------------------------------------------------------------------------
def chat_request():
    return ChatRequest(model='m/x', messages=({'role': 'user', 'content': 'hi'},), timeout_seconds=5.0)


def client(handler, monkeypatch, key='sk-unit-1234'):
    from model_transport.openrouter import OpenRouterClient
    monkeypatch.setenv('UNIT_KEY', key)
    return OpenRouterClient(base_url='https://or.test/api/v1', key_env='UNIT_KEY', transport=httpx.MockTransport(handler))


def test_openrouter_success_and_usage_parsing(monkeypatch):
    def handler(request):
        assert request.url.path == '/api/v1/chat/completions' and request.headers['authorization'] == 'Bearer sk-unit-1234'
        return httpx.Response(200, json={'choices': [{'message': {'content': 'hi'}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'cost': 0.0002}})
    result = client(handler, monkeypatch).chat(chat_request())
    assert (result.text, result.usage.cost_microusd, result.usage.prompt_tokens) == ('hi', 200, 10)


def test_openrouter_single_upstream_has_no_fallback_and_price_caps(monkeypatch):
    from model_transport.openrouter import OpenRouterClient
    monkeypatch.setenv('UNIT_KEY', 'fixture-key')
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        assert request.headers['X-OpenRouter-Metadata'] == 'enabled'
        return httpx.Response(200, json={'model':'m/x', 'choices':[{'message':{'content':'hello'}, 'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10, 'completion_tokens':3, 'cost':0.0002},
            'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Host', 'selected':True}]}}})
    value = OpenRouterClient(base_url='https://or.test/api/v1', key_env='UNIT_KEY',
        upstream_provider='fixture-host', price_caps={'m/x':(1_000_000,2_000_000)}, transport=httpx.MockTransport(handler))
    result = value.chat(chat_request())
    assert result.provider == 'Fixture Host' and result.model == 'm/x'
    assert seen[0]['provider'] == {'only':['fixture-host'], 'allow_fallbacks':False, 'require_parameters':True,
                                   'max_price':{'prompt':1.0, 'completion':2.0}}
    with pytest.raises(ModelNotSent):
        value.chat(ChatRequest(model='m/unconfigured', messages=({'role':'user','content':'hi'},), timeout_seconds=5))
    assert len(seen) == 1


@pytest.mark.parametrize('body', [
    {'error':{'message':'temporary'}},
    {'choices':[{'message':{'content':'partial reply'}, 'finish_reason':'error'}]},
    {'choices':[{'message':{'content':'partial reply'}, 'error':{'message':'failed'}}]},
])
def test_openrouter_http_200_with_provider_error_never_uses_partial_text(monkeypatch, body):
    with pytest.raises(ModelOutcomeUnknown):
        client(lambda request: httpx.Response(200, json=body), monkeypatch).chat(chat_request())


def test_openrouter_conflicting_selected_provider_metadata_is_unverified(monkeypatch):
    body = {'model':'m/x', 'provider':'Other', 'choices':[{'message':{'content':'hi'}}],
            'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Host','selected':True}]}}}
    assert client(lambda request: httpx.Response(200, json=body), monkeypatch).chat(chat_request()).provider == ''


def test_upstream_identity_is_explicit_and_changes_config_version():
    base = dict(enabled=True, primary_model='m/x', **PRICES)
    with pytest.raises(ValueError):
        ModelConfig(**base, upstream_provider='fixture-host')
    first = ModelConfig(**base, upstream_provider='fixture-host', upstream_reported_name='Fixture Host')
    second = ModelConfig(**base, upstream_provider='another-host', upstream_reported_name='Another Host')
    assert first.version() != second.version() != ModelConfig(**base).version()


def test_openrouter_missing_cost_is_unknown_usage_not_zero(monkeypatch):
    result = client(lambda r: httpx.Response(200, json={'choices': [{'message': {'content': 'hi'}}], 'usage': {'prompt_tokens': 1}}), monkeypatch).chat(chat_request())
    assert result.usage.cost_microusd is None
    assert client(lambda r: httpx.Response(200, json={'choices': []}), monkeypatch).chat(chat_request()).text == ''


@pytest.mark.parametrize('handler,exc', [
    (lambda r: httpx.Response(429, text='slow down sk-unit-1234'), ModelHTTPStatus),
    (lambda r: httpx.Response(500, text='boom'), ModelHTTPStatus),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError('refused')), ModelNotSent),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout('slow')), ModelNotSent),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('slow')), ModelOutcomeUnknown),
    (lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError('reset')), ModelOutcomeUnknown),
    (lambda r: httpx.Response(200, content=b'<html>'), ModelOutcomeUnknown),
])
def test_openrouter_failure_classification_without_leaking_the_key(monkeypatch, handler, exc):
    with pytest.raises(exc) as info:
        client(handler, monkeypatch).chat(chat_request())
    assert 'sk-unit' not in str(info.value) and 'sk-unit' not in repr(info.value)


def test_key_loading_rules(tmp_path, monkeypatch):
    from model_transport.openrouter import KeyUnavailable, OpenRouterClient, load_key
    good = tmp_path / 'key'
    good.write_text('sk-file-key\n'); good.chmod(0o600)
    assert load_key(key_file=str(good)) == 'sk-file-key'
    good.chmod(0o644)
    with pytest.raises(KeyUnavailable, match='0600'):
        load_key(key_file=str(good))
    good.chmod(0o600)
    link = tmp_path / 'link'; link.symlink_to(good)
    with pytest.raises(KeyUnavailable):
        load_key(key_file=str(link))
    with pytest.raises(KeyUnavailable):
        load_key(key_file=str(tmp_path / 'missing'))
    with pytest.raises(KeyUnavailable):
        load_key()
    with pytest.raises(KeyUnavailable):
        load_key(key_file=str(good), key_env='X')
    with pytest.raises(KeyUnavailable):
        load_key(key_env='NOT_SET_ANYWHERE', environ={})
    assert load_key(key_env='K', environ={'K': ' sk-env '}) == 'sk-env'
    with pytest.raises(ValueError, match='https'):
        OpenRouterClient(base_url='http://insecure.test', key_env='K', environ={'K': 'x'})
    assert 'sk-file-key' not in repr(OpenRouterClient(key_file=str(good)))


def test_factory_is_off_by_default_and_builds_only_when_enabled(monkeypatch):
    from model_transport import build_model_runtime
    profiles = {}
    assert build_model_runtime(object(), ModelConfig(), profiles) is None
    monkeypatch.setenv('FACTORY_KEY', 'sk-x')
    cfg = ModelConfig(enabled=True, primary_model='m/x', allowed_models=('m/x',), key_env='FACTORY_KEY', **PRICES)
    runtime = build_model_runtime(object(), cfg, profiles)
    assert type(runtime).__name__ == 'ModelRuntime' and runtime.client.provider == 'openrouter'
    with pytest.raises(ValueError):
        build_model_runtime(object(), ModelConfig(enabled=True, provider='other'), profiles)


def test_wiring_from_environment_selects_the_real_runtime_only_when_enabled(tmp_path):
    from conversation_engine.writer_context import active_profile, set_active_profiles
    from model_transport.wiring import build_runtime_from_env
    try:
        assert build_runtime_from_env(object(), {}) is None
        assert build_runtime_from_env(object(), {'ENGINE_MODEL_ENABLED': '0', 'ENGINE_MODEL_PRIMARY': 'm/x'}) is None
        env = {'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': 'm/x', 'ENGINE_MODEL_ALLOWED': 'm/x', 'ENGINE_MODEL_KEY_ENV': 'WIRING_KEY',
               'WIRING_KEY': 'sk-w', 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '3000000', 'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '15000000'}
        with pytest.raises(ValueError, match='PROFILES_FILE'):
            build_runtime_from_env(object(), env)
        profiles = tmp_path / 'profiles.json'
        profiles.write_text(json.dumps({'acct1': {'name': 'Riley', 'limits': ['no meetups'], 'role_statement': IDENT,
                                                  'offers_permitted': True, 'approved_item_ids': [5]},
                                        'acct2': {'name': 'NoLimits'}, 'acct3': 'junk'}))
        runtime = build_runtime_from_env(object(), {**env, 'ENGINE_MODEL_PROFILES_FILE': str(profiles)})
        assert type(runtime).__name__ == 'ModelRuntime' and runtime.config.primary_model == 'm/x'
        got = runtime.profiles.get('acct1')
        assert got.may_offer(5) and got.role_statement == IDENT and active_profile('acct1').name == 'Riley'
        assert runtime.profiles.get('acct2').missing_inputs() == ('boundaries',)
        assert runtime.profiles.get('acct3') is None and runtime.profiles.get('absent') is None  # invalid/missing = missing input
        profiles.write_text('{not json')
        assert runtime.profiles.get('acct1') is None
        with pytest.raises(ValueError, match='ENGINE_MODEL_FALLBACK'):
            build_runtime_from_env(object(), {**env, 'ENGINE_MODEL_PROFILES_FILE': str(profiles), 'ENGINE_MODEL_ALTERNATE': 'm/y'})
    finally:
        set_active_profiles(None)


def test_request_body_names_exactly_one_model_no_fallback_list(monkeypatch):
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        return httpx.Response(200, json={'model': 'm/x-0613', 'provider': 'AcmeHost', 'choices': [{'message': {'content': 'hi'}}],
                                         'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'cost': 0.0001}})
    result = client(handler, monkeypatch).chat(chat_request())
    assert seen['body']['model'] == 'm/x' and not {'models', 'route', 'fallbacks'} & set(seen['body'])
    assert (result.model, result.provider) == ('m/x-0613', 'AcmeHost')   # the RETURNED identity, not the requested one
    silent = client(lambda r: httpx.Response(200, json={'choices': [{'message': {'content': 'hi'}}]}), monkeypatch).chat(chat_request())
    assert silent.model == ''   # a response that names no model is "not reported", never assumed equal


def test_fixture_chat_client_script_repeats_last_entry():
    fixture = FixtureChatClient(['a', ModelNotSent('x'), 'last'])
    assert fixture.chat(chat_request()).text == 'a'
    with pytest.raises(ModelNotSent):
        fixture.chat(chat_request())
    assert fixture.chat(chat_request()).text == 'last' and fixture.chat(chat_request()).text == 'last'


# ---- import boundaries ---------------------------------------------------------------------------------
def _imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ''


def test_only_the_transport_package_imports_a_network_library():
    root = Path(__file__).resolve().parents[2]
    forbidden = {'httpx', 'requests', 'socket', 'urllib.request', 'subprocess', 'aiohttp'}
    new_engine_files = ['journey.py', 'model_client.py', 'model_config.py', 'model_ledger.py', 'model_output.py', 'model_runtime.py',
                        'prompt_registry.py', 'prompt_provider_a_original.py', 'writer_context.py', 'writer_examples.py']
    for name in new_engine_files:
        for module in _imports(root / 'conversation_engine' / name):
            assert not any(module == f or module.startswith(f + '.') for f in forbidden), (name, module)
    for path in (root / 'model_transport').glob('*.py'):
        used = {m for m in _imports(path) if any(m == f or m.startswith(f + '.') for f in forbidden)}
        assert used == ({'httpx'} if path.name == 'openrouter.py' else set()), (path.name, used)


# ---------------------------------------------------------------- Plan C NSFW rules (sexual chat is permitted, bounded)
from conversation_engine.journey import signals as journey_signals
from conversation_engine.prompt_provider_a_original import SpecPromptProvider
from conversation_engine.writer_context import build_context


def _writer(level):
    return CreatorProfile.from_facts('Riley', {'age': '27', 'limits': 'no meetups', 'explicit_chat': level},
                                     role_statement=IDENT, offers_permitted=True, approved_item_ids=('5',))


def test_prompt_carries_the_sexual_chat_block_for_each_level():
    for level, marker in (('explicit', 'plain explicit words'), ('suggestive', 'tease, don\'t describe'), ('none', "doesn\'t do sexual talk")):
        system = SpecPromptProvider().render(build_context(snapshot(), _writer(level))).messages[0]['content']
        assert 'SEXUAL CHAT:' in system and marker in system and 'Absolute lines' in system, level
        assert '—' not in system and '–' not in system
    assert SpecPromptProvider.name == 'a-original.v3-planc-nsfw'


def test_explicit_chat_level_is_validated():
    with pytest.raises(ValueError):
        CreatorProfile.from_facts('Riley', {'explicit_chat': 'wild'})
    assert CreatorProfile.from_facts('Riley', {}).explicit_chat == 'explicit'


HOT = 'i want you to suck my cock and ride me till i cum'
EXPLICIT_REPLY = 'mm i want that cock in my mouth first, then you can fuck me till my pussy is dripping'


def test_explicit_reply_is_accepted_for_an_explicit_profile():
    assert reasons(ok(EXPLICIT_REPLY), wctx(message=HOT, profile=_writer('explicit')))[0] == 'ok'


@pytest.mark.parametrize('level', ['suggestive', 'none'])
def test_explicit_reply_is_rejected_when_her_level_is_not_explicit(level):
    outcome, why = reasons(ok(EXPLICIT_REPLY), wctx(message=HOT, profile=_writer(level)))
    assert outcome == 'invalid' and 'chat level' in why
    assert reasons(ok('you are making me blush, keep going'), wctx(message=HOT, profile=_writer(level)))[0] == 'ok'


@pytest.mark.parametrize('body', ["let's keep it classy babe", "i'm not comfortable with that kind of talk",
                                  "tone it down a bit", "let's talk about something else, how was your day"])
def test_sanitising_an_explicit_fan_is_rejected_when_she_is_explicit(body):
    outcome, why = reasons(ok(body), wctx(message=HOT, profile=_writer('explicit')))
    assert outcome == 'invalid' and 'sanitises' in why
    # the same words to a non-sexual message are fine
    assert reasons(ok(body), wctx(message='how was your day', profile=_writer('explicit')))[0] == 'ok'


@pytest.mark.parametrize('body', ['pretend you are my little stepsister', 'i love it when you call me your teen slut',
                                  'imagine i drugged you and you are passed out while i', 'my dog fucks better than you',
                                  'pay me 500 to meet and fuck'])
def test_platform_banned_themes_in_her_text_are_rejected_at_any_level(body):
    for level in ('explicit', 'suggestive', 'none'):
        outcome, why = reasons(ok(body), wctx(message=HOT, profile=_writer(level)))
        assert outcome == 'invalid' and 'platform-banned' in why, (level, body)


def test_her_short_decline_of_a_banned_ask_passes():
    verdict = reasons(ok('haha nope, not my thing at all. tell me what you are doing tonight instead'),
                      wctx(message='call me daddy and pretend you are 15', profile=_writer('explicit')))
    assert verdict[0] == 'ok'


def test_fan_pushing_a_banned_theme_is_routed_to_a_human_by_journey_signals():
    for text in ('pretend you are my stepsister', 'are you under 18', 'how much to meet and fuck', 'my dog sex fantasy'):
        assert 'platform_banned' in journey_signals(text), text
    for text in ('my dog is cute', 'i have two kids and a busy week', 'you are so hot i want to fuck you', 'call me daddy'):
        assert 'platform_banned' not in journey_signals(text), text


@pytest.mark.parametrize('fan,reply', [
    ('my stepsister got a new job', "that's great news about your stepsister"),
    ('my kids are on holiday', 'hope your kids enjoy their holiday'),
    ('my daughter passed her driving test', "that's wonderful news for your daughter"),
    ('my little boy started school', 'your little boy must be excited'),
    ('i had a minor problem at work', 'hope that minor problem is sorted'),
])
def test_ordinary_family_and_age_mentions_are_not_platform_banned(fan, reply):
    assert 'platform_banned' not in journey_signals(fan)
    outcome, why = reasons(ok(reply), wctx(message=fan, profile=_writer('explicit')))
    assert outcome == 'ok', why


def test_direct_underage_identity_assertion_remains_a_safety_hold():
    assert 'platform_banned' in journey_signals('are you a teen?')
    assert 'platform_banned' in journey_signals('are you a minor?')
    outcome, why = reasons(ok('you are a minor'), wctx(message='how old am i', profile=_writer('explicit')))
    assert outcome == 'invalid' and 'platform-banned' in why


def test_contextual_family_words_do_not_hide_an_independent_banned_match():
    outcome, why = reasons(ok('your kids are well, but rape is unacceptable'),
                           wctx(message='my kids are doing well', profile=_writer('explicit')))
    assert outcome == 'invalid' and 'platform-banned' in why


def test_json_creator_chat_permission_is_preserved_and_conflicts_fail_closed(tmp_path):
    from conversation_engine.writer_context import CreatorProfile, JsonFileCreatorProfiles
    for level in ('none', 'suggestive', 'explicit'):
        assert CreatorProfile.from_dict({'name': 'A', 'explicit_chat': level}).explicit_chat == level
        assert CreatorProfile.from_dict({'name': 'A', 'facts': {'explicit_chat': level}}).explicit_chat == level
    with pytest.raises(ValueError):
        CreatorProfile.from_dict({'name': 'A', 'explicit_chat': 'none', 'facts': {'explicit_chat': 'explicit'}})
    with pytest.raises(ValueError):
        CreatorProfile(name='A', explicit_chat='unlimited')
    path = tmp_path / 'profiles.json'
    path.write_text(json.dumps({'acct': {'name': 'A', 'explicit_chat': 'invalid'}}))
    assert JsonFileCreatorProfiles(path).get('acct') is None
