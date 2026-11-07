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
def test_everything_defaults_off_and_env_is_explicit():
    cfg = ModelConfig()
    assert (cfg.enabled, cfg.daily_cap_microusd, cfg.creator_cap_microusd, cfg.allowed_models, cfg.enabled_accounts) == (False, 0, 0, (), frozenset())
    assert cfg.prompt_provider == 'a-original' and cfg.key_file == '' and cfg.key_env == ''
    assert ModelConfig.from_env({}).enabled is False
    on = ModelConfig.from_env({'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_ALLOWED': 'a/b, c/d', 'ENGINE_MODEL_ACCOUNTS': 'x,y',
                               'ENGINE_MODEL_PRIMARY': 'a/b', 'ENGINE_MODEL_ALTERNATE': 'c/d', 'ENGINE_MODEL_KEY_ENV': 'K'})
    assert on.enabled and on.allowed_models == ('a/b', 'c/d') and on.enabled_accounts == frozenset({'x', 'y'})
    assert [s for s, _, _ in on.chain()] == ['primary', 'retry', 'alternate']
    with pytest.raises(ValueError):
        ModelConfig(key_file='/x', key_env='K')
    with pytest.raises(ValueError):
        ModelConfig(daily_cap_microusd=-1)


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
def render(snap=None, **kw):
    ctx = build_context(snap or snapshot(), PROFILE, **kw)
    return ctx, get_prompt_provider().render(ctx)


def test_prompt_is_short_natural_and_has_no_build_b_or_fastt_text():
    _, rendered = render()
    system = rendered.messages[0]['content']
    assert len(system) < 4500
    assert 'Riley' in system and 'barista' in system and 'no meetups' in system and 'face reveal' in system
    for banned in ('FASTT', 'inflowwkiller', 'painful', 'humanizer'):
        assert banned.lower() not in system.lower()
    assert "don't refuse, sanitise or steer away" in system and 'strict JSON' in system
    assert rendered.block_names == ('head', 'creator', 'rules', 'contract')


def test_user_prompt_carries_memory_sales_state_purchases_history_and_untrusted_data_notice():
    snap = snapshot(sales_mode='paused', verified_purchases=[{'itemId': '5', 'label': 'beach set'}],
                    history=[{'id': 1, 'direction': 'in', 'body': 'hi', 'media_count': 0, 'price_cents': 0, 'paid': False},
                             {'id': 2, 'direction': 'out', 'body': 'hey you', 'media_count': 2, 'price_cents': 900, 'paid': False},
                             {'id': 9, 'direction': 'in', 'body': 'hey', 'media_count': 0, 'price_cents': 0, 'paid': False}])
    ctx, rendered = render(snap)
    user = rendered.messages[1]['content']
    assert 'his city: Perth' in user and 'SALES STATE: PAUSED' in user and 'VERIFIED PURCHASES: beach set' in user
    assert 'HER: hey you [a locked card, not opened]' in user and 'LATEST:\nFAN: hey' in user
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
    ctx, rendered = render(snap)
    user = rendered.messages[1]['content']
    assert ctx.offer['grantId'] == 'g-1' and 'APPROVED OFFER: id g-1, beach set' in user and 'REOPENED only for: 5 (beach set)' in user
    other = build_context(snapshot(sales_mode='reopened_scoped', sales_scope=['7'], offer_grants=snap['offer_grants']), PROFILE)
    assert other.offer is None  # a grant outside the reopened scope is never shown
    paused = build_context(snapshot(sales_mode='paused', offer_grants=snap['offer_grants']), PROFILE)
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


def test_in_character_retry_prefix_and_fix_notes():
    ctx = build_context(snapshot(), PROFILE, prefixed=True, fix=('states an age',))
    rendered = get_prompt_provider().render(ctx)
    assert rendered.messages[0]['content'].startswith("You are writing Riley's next text")
    assert 'YOUR LAST DRAFT WAS REJECTED: states an age' in rendered.messages[1]['content']


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
    cfg = ModelConfig(enabled=True, primary_model='m/x', allowed_models=('m/x',), key_env='FACTORY_KEY')
    runtime = build_model_runtime(object(), cfg, profiles)
    assert type(runtime).__name__ == 'ModelRuntime' and runtime.client.provider == 'openrouter'
    with pytest.raises(ValueError):
        build_model_runtime(object(), ModelConfig(enabled=True, provider='other'), profiles)


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
    new_engine_files = ['model_client.py', 'model_config.py', 'model_ledger.py', 'model_output.py', 'model_runtime.py',
                        'prompt_registry.py', 'prompt_provider_a_original.py', 'writer_context.py', 'writer_examples.py']
    for name in new_engine_files:
        for module in _imports(root / 'conversation_engine' / name):
            assert not any(module == f or module.startswith(f + '.') for f in forbidden), (name, module)
    for path in (root / 'model_transport').glob('*.py'):
        used = {m for m in _imports(path) if any(m == f or m.startswith(f + '.') for f in forbidden)}
        assert used == ({'httpx'} if path.name == 'openrouter.py' else set()), (path.name, used)
