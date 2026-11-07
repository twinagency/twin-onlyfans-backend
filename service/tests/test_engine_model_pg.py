"""Real model invocation inside the engine, offline: fake chat client and httpx.MockTransport only. No network."""
from datetime import timedelta
import json
import threading

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, unthrottle
from test_engine_memory_pg import setup, correct
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService, SyntheticModel
from conversation_engine.model_client import (ChatResult, FixtureChatClient, ModelBudgetExhausted, ModelBusy,
                                              ModelHTTPStatus, ModelNotSent, ModelOutcomeUnknown, ModelUnavailable, Usage)
from conversation_engine.model_config import ModelConfig
from conversation_engine.model_runtime import ModelRuntime
from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles
from conversation_engine.writer_examples import ExampleLine
from db.onlyfans_engine_models import OfEngineDecision, OfEngineModelCall, OfEngineRequest
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused, utcnow

PROFILE = CreatorProfile(name='Riley', voice='warm, teasing', facts=(('job', 'barista'),), limits=('no meetups',))


class Clock:
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset


def config(**overrides):
    values = dict(enabled=True, provider='fixture', primary_model='m/primary', allowed_models=('m/primary', 'm/alt'),
                  enabled_accounts=frozenset({ACCOUNT}), daily_cap_microusd=1_000_000, creator_cap_microusd=1_000_000,
                  turn_estimate_microusd=5000, timeout_seconds=20.0, lease_seconds=90)
    values.update(overrides)
    return ModelConfig(**values)


def ok(body='hey you, how was your day', **extra):
    return json.dumps({'body': body, **extra})


def with_model(database, script, *, clock=None, cfg=None, profiles=None, examples=None, cost=1000):
    store, authority, engine, model, memory = setup(database)
    client = FixtureChatClient(script, cost_microusd=cost)
    runtime = ModelRuntime(database[1], client, cfg or config(), profiles or StaticCreatorProfiles({ACCOUNT: PROFILE}),
                           clock=clock, examples=examples)
    engine.model = runtime
    return store, authority, engine, runtime, client, memory


def rows(database):
    with Session(database[1]) as s:
        return list(s.scalars(select(OfEngineModelCall).order_by(OfEngineModelCall.generation, OfEngineModelCall.seq)))


def run(store, authority, engine, mid, provider):
    decision = engine.plan(TENANT, ACCOUNT, FAN, mid)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    return decision, authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider))


def system_of(request):
    return request.messages[0]['content']


# ---- happy path, locks, budget bookkeeping -------------------------------------------------------
def test_plan_queue_dispatch_with_model_outside_any_transaction(database):
    seen = []

    def call(request):
        with database[1].connect() as c:
            seen.append(c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND state LIKE 'idle in transaction%' AND pid<>pg_backend_pid()")).scalar())
        return ok('hello babe, long day?')
    store, authority, engine, runtime, client, _ = with_model(database, [call])
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    decision, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted' and provider.calls[0]['body']['text'] == 'hello babe, long day?'
    assert seen == [0]  # no database transaction was open during the provider call
    [row] = rows(database)
    assert (row.status, row.outcome, row.actual_microusd, row.usage_known, row.generation, row.seq) == ('settled', 'ok', 1000, True, 0, 0)
    assert runtime.ledger.committed(day=row.cost_day) == 1000
    assert runtime.ledger.committed(day=row.cost_day, account=ACCOUNT) == 1000
    system = system_of(client.requests[0])
    assert 'Riley' in system and 'barista' in system and 'no meetups' in system
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) == decision and len(client.requests) == 1  # replay: no second call
    store.close()


def test_real_client_code_path_with_mock_transport(database, monkeypatch):
    from model_transport.openrouter import OpenRouterClient
    monkeypatch.setenv('A6_TEST_KEY', 'sk-test-0000')
    seen = {}

    def handler(request):
        seen['auth'] = request.headers.get('authorization')
        seen['body'] = json.loads(request.content)
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [{'message': {'content': ok('hi you')}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 321, 'completion_tokens': 12, 'cost': 0.000417}})
    store, authority, engine, _, _, _ = with_model(database, ['unused'])
    client = OpenRouterClient(base_url='https://openrouter.test/api/v1', key_env='A6_TEST_KEY', transport=httpx.MockTransport(handler))
    runtime = ModelRuntime(database[1], client, config(provider='openrouter'), StaticCreatorProfiles({ACCOUNT: PROFILE}))
    engine.model = runtime
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    _, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted' and provider.calls[0]['body']['text'] == 'hi you'
    assert seen['auth'] == 'Bearer sk-test-0000' and seen['body']['model'] == 'm/primary'
    assert seen['body']['response_format'] == {'type': 'json_object'} and 'sk-test' not in json.dumps(seen['body'])
    [row] = rows(database)
    assert (row.status, row.actual_microusd, row.prompt_tokens, row.completion_tokens, row.provider) == ('settled', 417, 321, 12, 'openrouter')
    assert 'sk-test' not in repr(client) and 'sk-test' not in (row.response_text or '')
    store.close()


# ---- refused BEFORE any network call -------------------------------------------------------------
@pytest.mark.parametrize('cfg,profiles,match', [
    (config(enabled=False), None, 'not enabled'),
    (config(enabled_accounts=frozenset({'other'})), None, 'not enabled for this account'),
    (config(primary_model='m/rogue'), None, 'approved list'),
    (config(alternate_model='m/rogue2'), None, 'approved list'),
    (config(provider='openrouter'), None, 'provider'),
    (config(), StaticCreatorProfiles({}), 'creator facts'),
    (config(lease_seconds=10), None, 'lease'),
])
def test_wrong_model_scope_or_profile_refused_before_any_network_call(database, cfg, profiles, match):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=cfg, profiles=profiles)
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match=match):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    store.close()


def test_owner_change_after_snapshot_is_refused_before_any_network_call(database):
    class Flip:
        def retrieve(self, account_id, query, **_):
            from db.onlyfans_engine_models import OfEngineOwner
            with Session(database[1]) as s, s.begin():
                s.get(OfEngineOwner, (ACCOUNT, FAN)).mode = 'paused'
            return ()
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], examples=Flip())
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='changed during generation'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    store.close()


# ---- budget ---------------------------------------------------------------------------------------
def test_budget_caps_stop_the_call_before_it_is_made(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(daily_cap_microusd=4000))
    incoming(store, 1, 'hello')
    with pytest.raises(ModelBudgetExhausted, match='daily'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    store.close()


def test_creator_cap_and_zero_default_cap(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(creator_cap_microusd=1000))
    incoming(store, 1, 'hello')
    with pytest.raises(ModelBudgetExhausted, match='creator'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == []
    store.close()
    assert ModelConfig().daily_cap_microusd == 0 and ModelConfig().creator_cap_microusd == 0 and ModelConfig().enabled is False


def test_actual_spend_counts_and_the_next_call_hits_the_cap(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ok('first'), ok('second'), ok('third')], cost=7000, cfg=config(daily_cap_microusd=12000))
    incoming(store, 1, 'hello')
    run(store, authority, engine, 1, SyntheticFansProvider(message_id=9001))  # reserve 5000, settle at 7000
    unthrottle(database[1]); incoming(store, 2, 'next')
    run(store, authority, engine, 2, SyntheticFansProvider(message_id=9002))  # 7000 + 5000 estimate = 12000 fits exactly
    assert runtime.ledger.committed(day=utcnow().date()) == 14000
    unthrottle(database[1]); incoming(store, 3, 'third')
    with pytest.raises(ModelBudgetExhausted, match='daily'):
        engine.plan(TENANT, ACCOUNT, FAN, 3)
    assert len(client.requests) == 2
    store.close()


# ---- timeouts, unknown usage ----------------------------------------------------------------------
def test_timeout_is_unknown_usage_counted_and_falls_to_alternate(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelOutcomeUnknown('timeout'), ok('from the alternate')], cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    _, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted' and provider.calls[0]['body']['text'] == 'from the alternate'
    first, second = rows(database)
    assert (first.model, first.status, first.outcome, first.actual_microusd) == ('m/primary', 'unknown', 'timeout', None)
    assert (second.model, second.status) == ('m/alt', 'settled')
    assert runtime.ledger.committed(day=first.cost_day) == 5000 + 1000  # unknown counts at the reservation
    assert [r.model for r in client.requests] == ['m/primary', 'm/alt']  # same model is not hammered with a prefix retry
    store.close()


def test_transport_failure_without_alternate_keeps_request_open_and_retries_later(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelOutcomeUnknown('timeout'), ok('back again')], clock=clock)
    incoming(store, 1, 'hello')
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    with Session(database[1]) as s:
        assert s.scalar(select(OfEngineRequest)).status == 'open'
        assert s.scalar(select(func.count()).select_from(OfEngineDecision)) == 0
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # inside the retry window: nothing is re-sent
    assert len(client.requests) == 1
    clock.offset = timedelta(seconds=45)
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert decision and len(client.requests) == 2
    assert [r.status for r in rows(database)] == ['unknown', 'settled']
    store.close()


def test_not_sent_and_http_refusals_release_the_reservation(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelNotSent('x'), ModelHTTPStatus(429), ok('finally')], cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'hello')
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    got = rows(database)
    assert [(r.status, r.outcome) for r in got] == [('released', 'not_sent'), ('released', 'http_429')]
    assert runtime.ledger.committed(day=got[0].cost_day) == 0
    store.close()


# ---- malformed output, refusals, validation, the chain --------------------------------------------
def test_malformed_output_goes_through_the_chain_then_hands_to_a_human_without_a_canned_line(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, ['not json at all', '{"body": 12}', '{"body": "x", "surprise": 1}'], cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary', 'm/primary', 'm/alt']
    assert 'Reply only with the JSON' in system_of(client.requests[1]) and 'Reply only with the JSON' not in system_of(client.requests[0])
    assert [r.outcome for r in rows(database)] == ['malformed'] * 3
    with Session(database[1]) as s:
        request = s.scalar(select(OfEngineRequest))
        assert (request.status, request.handover_target, request.next_step) == ('handover_pending', 'operator', 'await_handover_acceptance')
        assert s.scalar(select(func.count()).select_from(OfEngineDecision)) == 0  # no canned line, no decision
    with pytest.raises(Refused, match='with a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 3  # the handover is final: no more calls
    store.close()


def test_refusal_primary_then_in_character_retry_then_alternate(database):
    refusal = ok("I'm sorry, but I can't continue with this conversation")
    store, authority, engine, runtime, client, _ = with_model(
        database, [refusal, 'I cannot help with that request', ok('so what are you wearing rn')], cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    _, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted' and provider.calls[0]['body']['text'] == 'so what are you wearing rn'
    assert [r.outcome for r in rows(database)] == ['refusal', 'refusal', 'ok']
    assert [(r.model, 'Reply only with the JSON' in system_of(r)) for r in client.requests] == [
        ('m/primary', False), ('m/primary', True), ('m/alt', True)]
    store.close()


def test_invalid_commercial_output_is_regenerated_with_the_reason_then_accepted(database):
    bad_price = ok('it is only $5 for the set')
    bad_claim = ok('thanks for buying it earlier', purchase_claim=False)
    store, authority, engine, runtime, client, _ = with_model(
        database, [bad_price, bad_claim, ok('what are you up to tonight')], cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    assert [r.outcome for r in rows(database)] == ['invalid', 'invalid', 'ok']
    assert 'YOUR LAST DRAFT WAS REJECTED' in client.requests[1].messages[1]['content']
    assert 'price' in client.requests[1].messages[1]['content']
    store.close()


def test_permitted_explicit_conversation_is_not_blocked_by_any_layer(database):
    explicit = ok("mmm i want you so bad rn, tell me exactly what you'd do to me and i'll tell you how hard i'd ride you")
    store, authority, engine, runtime, client, _ = with_model(database, [explicit])
    incoming(store, 1, 'i am so horny, want to fuck you tonight')
    provider = SyntheticFansProvider(message_id=9001)
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    assert 'ride you' in provider.calls[0]['body']['text'] and len(client.requests) == 1
    assert "don't refuse, sanitise" in system_of(client.requests[0])
    store.close()


def test_documented_limit_terms_are_enforced_in_code(database):
    profile = CreatorProfile(name='Riley', limits=('no meetups',), limit_terms=('feet',))
    store, authority, engine, runtime, client, _ = with_model(
        database, [ok('send me your feet pics'), ok('what would you wear for me')], profiles=StaticCreatorProfiles({ACCOUNT: profile}))
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    assert [r.outcome for r in rows(database)] == ['invalid', 'ok']
    store.close()


# ---- leases, crashes, two workers -------------------------------------------------------------------
def _make_request(database, engine, runtime, client):
    """plan() creates the durable request before it reaches the model; a disabled config stops it there."""
    engine.model = ModelRuntime(database[1], client, config(enabled=False), StaticCreatorProfiles({ACCOUNT: PROFILE}))
    with pytest.raises(Refused, match='not enabled'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    engine.model = runtime
    return _request_id(database)


def _reserve(runtime, database, request_id, *, calling=False, worker='crashed'):
    ledger = runtime.ledger
    with Session(database[1]) as s:
        from db.onlyfans_engine_models import OfEngineOwner
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        epoch, binding = owner.epoch, owner.binding_id
    r = ledger.reserve(account=ACCOUNT, request_id=request_id, generation=0, fan_id=FAN, binding_id=binding, owner_epoch=epoch,
        chain_pos=0, chain_step='primary', model='m/primary', prefixed=False, snapshot_sha256='0' * 64, prompt_sha256='1' * 64,
        worker_id=worker, provider='fixture')
    if calling:
        ledger.mark_calling(ACCOUNT, r.call_id, worker)
    return r


def _request_id(database):
    with Session(database[1]) as s:
        return s.scalar(select(OfEngineRequest.request_id))


def _expire(database):
    with database[1].begin() as c:
        c.execute(text("UPDATE of_engine_model_call SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'"))


def test_crash_holding_a_lease_then_restart_releases_and_calls_once(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('after restart')])
    incoming(store, 1, 'hello')
    request_id = _make_request(database, engine, runtime, client)
    dead = _reserve(runtime, database, request_id)  # worker A reserves and dies before the wire
    with pytest.raises(ModelBusy):  # the lease is still live: a second worker must not call
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == []
    _expire(database)  # the lease lapses; the restarted worker recovers
    provider = SyntheticFansProvider(message_id=9001)
    _, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted' and len(client.requests) == 1
    old, new = rows(database)
    assert (old.call_id, old.status, old.outcome) == (dead.call_id, 'released', 'lease_expired')
    assert (new.status, new.seq) == ('settled', 1)
    assert runtime.ledger.committed(day=new.cost_day) == 1000  # the dead reservation was released, not charged
    store.close()


def test_crash_after_the_wire_started_counts_as_unknown_and_waits_before_retrying(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(database, [ok('later')], clock=clock)
    incoming(store, 1, 'hello')
    request_id = _make_request(database, engine, runtime, client)
    _reserve(runtime, database, request_id, calling=True)
    _expire(database)
    clock.offset = timedelta(seconds=1)
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # unknown outcome: no immediate second paid call
    assert client.requests == []
    [row] = rows(database)
    assert (row.status, row.outcome) == ('unknown', 'lease_expired')
    assert runtime.ledger.committed(day=row.cost_day) == 5000  # conservatively counted
    clock.offset = timedelta(seconds=120)
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) and len(client.requests) == 1
    store.close()


def test_two_workers_produce_one_invocation(database):
    started, release = threading.Event(), threading.Event()

    def slow(request):
        started.set()
        assert release.wait(20)
        return ok('only one of us answers')
    store, authority, engine1, runtime1, client1, _ = with_model(database, [slow])
    client2 = FixtureChatClient([ok('second worker must never get here')])
    engine2 = EngineService(store, SyntheticModel({}))
    runtime2 = ModelRuntime(database[1], client2, config(), StaticCreatorProfiles({ACCOUNT: PROFILE}))
    engine2.model = runtime2
    incoming(store, 1, 'hello')
    result = {}
    thread = threading.Thread(target=lambda: result.update(decision=engine1.plan(TENANT, ACCOUNT, FAN, 1)))
    thread.start()
    assert started.wait(20)
    with pytest.raises(ModelBusy):
        engine2.plan(TENANT, ACCOUNT, FAN, 1)
    release.set(); thread.join(30)
    assert result['decision'] and client2.requests == [] and len(client1.requests) == 1
    assert engine2.plan(TENANT, ACCOUNT, FAN, 1) == result['decision']  # second worker reuses the finished work
    assert len(rows(database)) == 1
    store.close()


def test_stored_answer_is_reused_after_a_crash_between_settle_and_decision(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('stored answer')])
    incoming(store, 1, 'hello')
    boom = {'n': 0}
    original = engine._model_recheck

    def crash_once(*a, **k):
        boom['n'] += 1
        if boom['n'] == 2:  # the post-call recheck: the process dies after settling
            raise RuntimeError('simulated crash')
        return original(*a, **k)
    engine._model_recheck = crash_once
    with pytest.raises(RuntimeError):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    engine._model_recheck = original
    assert len(client.requests) == 1 and rows(database)[0].status == 'settled'
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert decision and len(client.requests) == 1  # no second paid call: the stored settled answer was judged again and used
    store.close()


# ---- stale generation is never sent -----------------------------------------------------------------
def test_new_fan_message_during_the_call_means_the_stale_answer_is_not_planned(database):
    holder = {}

    def call(request):
        incoming(holder['store'], 2, 'actually one more thing')
        return ok('answer to the old message')
    store, authority, engine, runtime, client, _ = with_model(database, [call])
    holder['store'] = store
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='changed during generation'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEngineDecision)) == 0
    assert rows(database)[0].status == 'settled'  # the spend is recorded even though the answer was discarded
    store.close()


def test_memory_correction_during_the_call_discards_the_answer(database):
    holder = {}

    def call(request):
        correct(holder['memory'], value='Perth', key='mid-call')
        return ok('stale answer')
    store, authority, engine, runtime, client, memory = with_model(database, [call])
    holder['memory'] = memory
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='memory changed'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    store.close()


def test_conversation_change_after_a_refusal_stops_the_chain_before_the_next_paid_call(database):
    holder = {}

    def refuse(request):
        incoming(holder['store'], 2, 'new message while she was refusing')
        return ok("I'm sorry, but I can't continue")
    store, authority, engine, runtime, client, _ = with_model(database, [refuse, ok('never used')])
    holder['store'] = store
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='changed during generation'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 1
    store.close()


# ---- offers: the model proposes, the engine decides -------------------------------------------------
def _offer_setup(database, script, **kw):
    from test_onlyfans_media_pg import shelf, media_policy
    from conversation_engine.commerce import SyntheticCoverage, approve_simulated_offer
    store, authority, engine, runtime, client, memory = with_model(database, script, **kw)
    item_id = shelf(database[1]); media_policy(authority)
    coverage = SyntheticCoverage(ACCOUNT, FAN, item_id, (11, 12, 13), 'synthetic:complete-ownership', utcnow() + timedelta(minutes=5))
    grant = approve_simulated_offer(store, TENANT, ACCOUNT, FAN, item_id, coverage)
    return store, authority, engine, runtime, client, item_id, grant


def _grant_from_prompt(request):
    text_ = request.messages[1]['content']
    return text_.split('APPROVED OFFER: id ')[1].split(',')[0]


def test_offer_turn_uses_only_the_approved_grant_and_keeps_answer_and_engine_rendered_price(database):
    def good(request):
        return ok('it is a short scenic video with a couple of stills', action='offer', offer_grant_id=_grant_from_prompt(request), pitch=True)
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(database, [good])
    incoming(store, 1, 'show me the scenic video')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    prompt = client.requests[0].messages[1]['content']
    assert f'APPROVED OFFER: id {grant}' in prompt and 'USD' not in prompt and '1000' not in prompt and '10.00' not in prompt
    with Session(database[1]) as s:
        row = s.get(OfEngineDecision, (ACCOUNT, decision))
        assert row.action == 'offer' and row.offer_grant_id == grant
        assert row.body.startswith('it is a short scenic video') and row.body.endswith('USD 10.00.')
    store.close()


def test_offer_outside_the_granted_item_and_model_priced_offers_are_rejected(database):
    script = [
        lambda r: ok('here you go', action='offer', offer_grant_id='00000000-0000-4000-8000-000000000000', pitch=True),
        lambda r: ok('it is USD 1.00 for the set', action='offer', offer_grant_id=_grant_from_prompt(r), pitch=True),
        lambda r: ok('a scenic video with stills, want it?', action='offer', offer_grant_id=_grant_from_prompt(r), pitch=True)]
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(database, script, cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'show me the scenic video')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.outcome for r in rows(database)] == ['invalid', 'invalid', 'ok']
    store.close()


def test_paused_sales_cannot_pitch_through_the_real_model_path(database):
    pitch = ok('buy the set for USD 1.00, you already purchased it', pitch=True, purchase_claim=True)
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(
        database, [ok('understood, no problem', sales_signal='decline'), pitch, pitch, pitch], cfg=config(alternate_model='m/alt'))
    incoming(store, 1, 'no thanks')
    provider = SyntheticFansProvider(('accepted',), message_id=(9001,))
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    unthrottle(database[1]); incoming(store, 2, 'hello again')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    assert len(provider.calls) == 1  # nothing commercial reached the wire
    assert 'SALES STATE: PAUSED' in client.requests[1].messages[1]['content']
    assert 'APPROVED OFFER: none' in client.requests[1].messages[1]['content']
    store.close()


def test_history_in_the_prompt_is_delivered_messages_only(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('first reply'), ok('second reply')])
    incoming(store, 1, 'hello there')
    engine.plan(TENANT, ACCOUNT, FAN, 1)  # planned but NOT delivered
    unthrottle(database[1]); incoming(store, 2, 'you there?')
    engine.plan(TENANT, ACCOUNT, FAN, 2)
    prompt = client.requests[1].messages[1]['content']
    assert 'first reply' not in prompt and 'FAN: hello there' in prompt
    store.close()


def test_corrected_memory_reaches_the_prompt_and_examples_are_style_only(database):
    class Examples:
        def retrieve(self, account_id, query, **kw):
            return (ExampleLine('how was your day', ('lazy sunday for me, u?',)),)
    store, authority, engine, runtime, client, memory = with_model(database, [ok('hey')], examples=Examples())
    correct(memory, value='Brisbane', key='fix-city')
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    prompt = client.requests[0].messages[1]['content']
    assert 'his city: Brisbane' in prompt and 'lazy sunday for me, u?' in prompt and 'style only' in prompt
    assert 'VERIFIED PURCHASES: none on record' in prompt
    store.close()
