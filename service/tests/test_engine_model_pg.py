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
from db.models import OfConversationState
from db.onlyfans_engine_models import OfEngineDecision, OfEngineModelCall, OfEngineRequest
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused, utcnow

PROFILE = CreatorProfile(name='Riley', voice='warm, teasing', facts=(('job', 'barista'),), limits=('no meetups',))
IDENT = 'This chat is written by an AI assistant that works for her team.'
PRICES = dict(prompt_microusd_per_mtok=1_000_000, completion_microusd_per_mtok=2_000_000)
FALLBACK = dict(fallback_enabled=True, fallback_model='m/fallback', allowed_models=('m/primary', 'm/fallback'),
                fallback_prompt_microusd_per_mtok=2_000_000, fallback_completion_microusd_per_mtok=4_000_000)
FALLBACK_BOUND = 17200  # 8000 input tokens at $2/Mtok + 300 output tokens at $4/Mtok
# upper bound per call: 8000 input tokens at $1/Mtok + 300 output tokens at $2/Mtok = 8600 micro-USD; 2 attempts per generation
BOUND = 8600


class Clock:
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset


def config(**overrides):
    values = dict(enabled=True, provider='fixture', primary_model='m/primary', allowed_models=('m/primary',),
                  enabled_accounts=frozenset({ACCOUNT}), daily_cap_microusd=1_000_000, creator_cap_microusd=1_000_000,
                  max_input_tokens=8000, max_tokens=300, timeout_seconds=20.0, lease_seconds=90, **PRICES)
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
    (config(provider='openrouter'), None, 'provider'),
    (config(), StaticCreatorProfiles({}), 'creator facts'),
    (config(), StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Riley')}), 'boundaries'),
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
    # the daily cap must cover THIS call and the retry allowance (2 attempts x 8600), each at the upper bound
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(daily_cap_microusd=2 * BOUND - 1))
    incoming(store, 1, 'hello')
    with pytest.raises(ModelBudgetExhausted, match='daily'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    store.close()


def test_cap_exactly_covering_the_retry_allowance_lets_the_call_through(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('fits')], cfg=config(daily_cap_microusd=2 * BOUND, creator_cap_microusd=2 * BOUND))
    incoming(store, 1, 'hello')
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) and len(client.requests) == 1
    [row] = rows(database)
    assert row.reserved_microusd == BOUND and row.actual_microusd == 1000  # reserved at the bound, settled at the actual cost
    store.close()


def test_reservation_is_computed_from_tokens_and_prices_not_a_fixed_estimate(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(
        max_input_tokens=9000, max_tokens=400, prompt_microusd_per_mtok=3_000_000, completion_microusd_per_mtok=15_000_000))
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert rows(database)[0].reserved_microusd == 9000 * 3 + 400 * 15  # 33_000
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
        database, [ok('first'), ok('second'), ok('third')], cost=7000, cfg=config(daily_cap_microusd=7000 * 2 + 2 * BOUND - 1))
    incoming(store, 1, 'hello')
    run(store, authority, engine, 1, SyntheticFansProvider(message_id=9001))  # needs 17200, settles at 7000
    unthrottle(database[1]); incoming(store, 2, 'next')
    run(store, authority, engine, 2, SyntheticFansProvider(message_id=9002))  # 7000 + 17200 fits
    assert runtime.ledger.committed(day=utcnow().date()) == 14000
    unthrottle(database[1]); incoming(store, 3, 'third')
    with pytest.raises(ModelBudgetExhausted, match='daily'):  # 14000 + 17200 > cap
        engine.plan(TENANT, ACCOUNT, FAN, 3)
    assert len(client.requests) == 2
    store.close()


def test_provider_cost_above_the_bound_is_kept_and_flagged(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('pricey')], cost=BOUND + 500)
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    [row] = rows(database)
    assert (row.actual_microusd, row.error_class) == (BOUND + 500, 'cost_over_bound')  # never hidden by clamping to the bound
    store.close()


# ---- budget and lease races ---------------------------------------------------------------------------
def _two_requests(database, engine, runtime, client, store):
    incoming(store, 1, 'hello'); first = _make_request(database, engine, runtime, client)
    unthrottle(database[1]); incoming(store, 2, 'second'); 
    engine.model = ModelRuntime(database[1], client, config(enabled=False), StaticCreatorProfiles({ACCOUNT: PROFILE}))
    with pytest.raises(Refused, match='not enabled'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    engine.model = runtime
    with Session(database[1]) as s:
        second = s.scalar(select(OfEngineRequest.request_id).where(OfEngineRequest.source_message_id == 2))
    return first, second


def _reserve_for(runtime, database, request_id, worker, results, key, barrier, model='m/primary', step='primary'):
    from db.onlyfans_engine_models import OfEngineOwner
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        epoch, binding = owner.epoch, owner.binding_id
    barrier.wait(20)
    try:
        results[key] = runtime.ledger.reserve(account=ACCOUNT, request_id=request_id, generation=0, fan_id=FAN, binding_id=binding,
            owner_epoch=epoch, chain_pos=0, chain_step=step, model=model, prefixed=False, snapshot_sha256='0' * 64,
            prompt_sha256='1' * 64, worker_id=worker, provider='fixture')
    except Refused as exc:
        results[key] = exc


def test_cap_race_two_workers_on_two_requests_only_one_reservation_fits(database):
    # each reservation needs its call bound plus one more bound for the retry allowance (17200): the cap, one unit short of
    # both together (8600 held + 17200), lets exactly one of two simultaneous reservations through
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(
        daily_cap_microusd=3 * BOUND - 1, creator_cap_microusd=1_000_000))
    first, second = _two_requests(database, engine, runtime, client, store)
    results, barrier = {}, threading.Barrier(2)
    threads = [threading.Thread(target=_reserve_for, args=(runtime, database, rid, f'w{i}', results, i, barrier))
               for i, rid in enumerate((first, second))]
    [t.start() for t in threads]; [t.join(30) for t in threads]
    won = [v for v in results.values() if not isinstance(v, Exception)]
    lost = [v for v in results.values() if isinstance(v, ModelBudgetExhausted)]
    assert len(won) == 1 and len(lost) == 1 and len(rows(database)) == 1
    assert runtime.ledger.committed(day=utcnow().date()) == BOUND  # the winner holds exactly one upper-bound reservation
    store.close()


def test_lease_race_two_workers_on_one_request_only_one_reservation(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()])
    incoming(store, 1, 'hello')
    request_id = _make_request(database, engine, runtime, client)
    results, barrier = {}, threading.Barrier(2)
    threads = [threading.Thread(target=_reserve_for, args=(runtime, database, request_id, f'w{i}', results, i, barrier)) for i in range(2)]
    [t.start() for t in threads]; [t.join(30) for t in threads]
    assert sorted(type(v).__name__ for v in results.values()) == ['ModelBusy', 'Reservation'] and len(rows(database)) == 1
    store.close()


def test_a_lease_that_lapses_before_the_wire_is_refused_and_released_not_charged(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], clock=clock)
    incoming(store, 1, 'hello')
    request_id = _make_request(database, engine, runtime, client)
    reservation = _reserve(runtime, database, request_id, worker='slow')
    clock.offset = timedelta(seconds=91)  # the 90 second lease lapsed before the worker reached the wire
    with pytest.raises(Refused, match='lease expired'):
        runtime.ledger.mark_calling(ACCOUNT, reservation.call_id, 'slow')
    [row] = rows(database)
    assert (row.status, row.outcome) == ('released', 'lease_expired') and client.requests == []
    assert runtime.ledger.committed(day=row.cost_day) == 0
    with pytest.raises(Refused, match='lease'):
        runtime.ledger.settle(ACCOUNT, reservation.call_id, 'other-worker', text_value='x', usage=Usage(1, 1, 1))
    store.close()


# ---- timeouts, unknown usage ----------------------------------------------------------------------
def test_timeout_is_unknown_usage_and_keeps_its_full_reservation_until_reconciled(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ModelOutcomeUnknown('timeout'), ok('second try')])
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='outcome is unknown; request handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # handover proof: test_w3_trace_ledger_pg; here: the ledger bound + provider-evidence settle
    [first] = rows(database)
    assert (first.status, first.outcome, first.actual_microusd) == ('unknown', 'timeout', None)
    assert runtime.ledger.committed(day=first.cost_day) == BOUND  # the whole upper bound stays counted
    assert [r.call_id for r in runtime.ledger.unreconciled()] == [first.call_id]
    from conversation_engine.model_ledger import ModelLedger
    with Session(database[1]) as s, s.begin():  # the provider-evidence path (model_transport.reconciliation)
        row = s.get(OfEngineModelCall, (ACCOUNT, first.call_id))
        with pytest.raises(Refused, match='only unknown usage'):
            ModelLedger.apply_verified_usage(row, -1, now=utcnow(), retain_unknown_outcome=True)
        ModelLedger.apply_verified_usage(row, 2500, now=utcnow(), retain_unknown_outcome=True)
    # the verified cost replaces the bound; the unanswered outcome stays held (never 'settled', never re-asked)
    assert runtime.ledger.committed(day=first.cost_day) == 2500
    assert [r.call_id for r in runtime.ledger.unreconciled()] == [first.call_id]
    with Session(database[1]) as s, s.begin():
        row = s.get(OfEngineModelCall, (ACCOUNT, first.call_id))
        with pytest.raises(Refused, match='only unknown usage'):
            ModelLedger.apply_verified_usage(row, 1, now=utcnow(), retain_unknown_outcome=True)  # never rewritten
    store.close()


def test_missing_usage_keeps_the_reservation_not_zero(database):
    nothing = ChatResult(text=ok('quiet'), usage=Usage(None, None, None), model='m/primary')
    store, authority, engine, runtime, client, _ = with_model(database, [nothing])
    incoming(store, 1, 'hello')
    assert engine.plan(TENANT, ACCOUNT, FAN, 1)
    [row] = rows(database)
    assert (row.status, row.outcome, row.usage_known, row.actual_microusd, row.error_class) == ('unknown', 'ok', False, None, 'usage_missing')
    assert runtime.ledger.committed(day=row.cost_day) == BOUND
    store.close()


def test_token_counts_without_a_provider_cost_are_priced_from_the_configured_prices(database):
    counted = ChatResult(text=ok('quiet'), usage=Usage(2000, 100, None), model='m/primary')
    store, authority, engine, runtime, client, _ = with_model(database, [counted])
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert rows(database)[0].actual_microusd == 2000 * 1 + 100 * 2  # 2200 micro-USD
    store.close()


def test_not_sent_and_http_refusals_release_the_reservation_and_attempts_stay_bounded(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(database, [ModelNotSent('x'), ModelHTTPStatus(429), ok('finally')], clock=clock)
    incoming(store, 1, 'hello')
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    got = rows(database)
    assert [(r.status, r.outcome) for r in got] == [('released', 'not_sent')]
    assert runtime.ledger.committed(day=got[0].cost_day) == 0
    clock.offset = timedelta(seconds=45)
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # second (and last) allowed attempt: refused with 429
    assert [(r.status, r.outcome) for r in rows(database)] == [('released', 'not_sent'), ('released', 'http_429')]
    clock.offset = timedelta(seconds=120)
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # total attempts are bounded: no third call, a human gets the request
    assert len(client.requests) == 2 and len(rows(database)) == 2
    with Session(database[1]) as s:
        assert s.scalar(select(OfEngineRequest)).status == 'handover_pending'
    store.close()


# ---- malformed output, refusals, validation, the chain --------------------------------------------
def _cases(database):
    from db.onlyfans_case_models import OfOperatorCase
    with Session(database[1]) as s:
        return [(c.category, c.scope, c.status, c.reason, json.loads(c.evidence_json)) for c in s.scalars(select(OfOperatorCase))]


def test_invalid_model_output_becomes_visible_human_work_after_one_corrected_attempt(database):
    store, authority, engine, runtime, client, _ = with_model(database, ['not json at all', '{"body": 12}', '{"body": "x", "surprise": 1}'])
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary', 'm/primary']  # one regeneration, no fallback, no third call
    assert 'Reply only with the JSON' not in system_of(client.requests[1])    # no in-character "jailbreak" prefix either
    assert 'YOUR LAST DRAFT WAS REJECTED' in client.requests[1].messages[1]['content']
    assert [r.outcome for r in rows(database)] == ['malformed'] * 2
    with Session(database[1]) as s:
        request = s.scalar(select(OfEngineRequest))
        assert (request.status, request.handover_target, request.next_step) == ('handover_pending', 'operator', 'await_handover_acceptance')
        assert s.scalar(select(func.count()).select_from(OfEngineDecision)) == 0  # no canned line, no decision
    [(category, scope, status, reason, evidence)] = _cases(database)  # and it is visible work for an operator
    assert (category, scope, status, reason) == ('model_handover', 'all', 'open', 'model_rejected')
    assert evidence['outcomes'] == ['malformed', 'malformed'] and evidence['request_id'] == request.request_id and evidence['reasons']
    with Session(database[1]) as s:
        assert s.scalar(select(OfConversationState)).requires_attention is True
    with pytest.raises(Refused, match='with a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 2  # the handover is final: no more calls
    store.close()


def test_refusal_is_never_re_asked_it_goes_straight_to_a_human(database):
    refusal = ok("I'm sorry, but I can't continue with this conversation")
    store, authority, engine, runtime, client, _ = with_model(database, [refusal, 'I cannot help with that request', ok('so what are you wearing rn')])
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 1 and [r.outcome for r in rows(database)] == ['refusal']  # no retry to dodge the refusal
    [(category, _, status, reason, evidence)] = _cases(database)
    assert (category, status, reason, evidence['outcomes']) == ('model_handover', 'open', 'model_refused', ['refusal'])
    with pytest.raises(Refused, match='with a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 1
    store.close()


def test_refusal_on_the_corrected_attempt_also_stops_there(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('it is $5 for the set'), 'As a language model I cannot continue'])
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.outcome for r in rows(database)] == ['invalid', 'refusal'] and len(client.requests) == 2
    store.close()


def test_invalid_commercial_output_is_regenerated_once_with_the_reason_then_accepted(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('it is only $5 for the set'), ok('what are you up to tonight')])
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    assert [r.outcome for r in rows(database)] == ['invalid', 'ok']
    assert 'YOUR LAST DRAFT WAS REJECTED' in client.requests[1].messages[1]['content']
    assert 'price' in client.requests[1].messages[1]['content']
    store.close()


def test_two_invalid_drafts_hand_over_with_the_reasons_recorded(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('it is only $5 for the set'), ok('thanks for buying it earlier')])
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    [(_, _, _, reason, evidence)] = _cases(database)
    assert reason == 'model_rejected' and evidence['outcomes'] == ['invalid', 'invalid'] and any('purchase' in r for r in evidence['reasons'])
    store.close()


def test_permitted_explicit_conversation_is_not_blocked_by_any_layer(database):
    explicit = ok("mmm i want you so bad rn, tell me exactly what you'd do to me and i'll tell you how hard i'd ride you")
    store, authority, engine, runtime, client, _ = with_model(database, [explicit])
    incoming(store, 1, 'i am so horny, want to fuck you tonight')
    provider = SyntheticFansProvider(message_id=9001)
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    assert 'ride you' in provider.calls[0]['body']['text'] and len(client.requests) == 1
    assert "Never lecture, sanitise, deflect" in system_of(client.requests[0])
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


def _reserve(runtime, database, request_id, *, calling=False, worker='crashed', generation=0):
    ledger = runtime.ledger
    with Session(database[1]) as s:
        from db.onlyfans_engine_models import OfEngineOwner
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        epoch, binding = owner.epoch, owner.binding_id
    r = ledger.reserve(account=ACCOUNT, request_id=request_id, generation=generation, fan_id=FAN, binding_id=binding, owner_epoch=epoch,
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


def test_crash_after_the_wire_started_counts_as_unknown_and_hands_over(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(database, [ok('later')], clock=clock)
    incoming(store, 1, 'hello')
    request_id = _make_request(database, engine, runtime, client)
    _reserve(runtime, database, request_id, calling=True)
    _expire(database)
    clock.offset = timedelta(seconds=1)
    with pytest.raises(Refused, match='outcome is unknown; request handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # may have reached the wire: never a second paid call
    assert client.requests == []
    [row] = rows(database)
    assert (row.status, row.outcome) == ('unknown', 'lease_expired')
    assert runtime.ledger.committed(day=row.cost_day) == BOUND  # conservatively counted at the upper bound
    [(category, _, _, reason, evidence)] = _cases(database)
    assert (category, reason, evidence['outcomes']) == ('model_handover', 'model_outcome_unknown', ['lease_expired'])
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


@pytest.mark.parametrize('returned_model,returned_provider', [('', 'Fixture Host'), ('m/other', 'Fixture Host'),
                                                              ('m/primary', ''), ('m/primary', 'Other Host')])
def test_pinned_model_requires_verified_returned_identity(database, returned_model, returned_provider):
    cfg = config(upstream_provider='fixture-host', upstream_reported_name='Fixture Host')
    answer = ChatResult(text=ok('hello'), usage=Usage(100, 10, 500), model=returned_model, provider=returned_provider)
    store, authority, engine, runtime, client, _ = with_model(database, [answer], cfg=cfg)
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='identity is unverified'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    [row] = rows(database)
    assert row.status == 'settled' and row.actual_microusd == 500 and row.outcome == 'identity_unverified'
    assert len(client.requests) == 1
    with Session(database[1]) as s:
        assert s.scalar(select(OfEngineRequest.status)) == 'handover_pending'
        assert s.scalar(select(func.count()).select_from(OfEngineDecision)) == 0
    store.close()


def test_pinned_model_crash_before_identity_verification_never_reuses_answer(database):
    cfg = config(upstream_provider='fixture-host', upstream_reported_name='Fixture Host')
    answer = ChatResult(text=ok('stored answer'), usage=Usage(100, 10, 500), model='m/primary', provider='Fixture Host')
    store, authority, engine, runtime, client, _ = with_model(database, [answer], cfg=cfg)
    incoming(store, 1, 'hello')
    original = engine._model_recheck
    calls = {'n':0}
    def crash_after_settle(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('simulated crash before identity verification')
        return original(*args, **kwargs)
    engine._model_recheck = crash_after_settle
    with pytest.raises(RuntimeError):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    engine._model_recheck = original
    assert rows(database)[0].outcome == 'identity_pending' and len(client.requests) == 1
    with pytest.raises(Refused, match='identity is unverified'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 1
    store.close()


def test_pinned_verified_answer_is_reused_after_crash_before_decision(database):
    cfg = config(upstream_provider='fixture-host', upstream_reported_name='Fixture Host')
    answer = ChatResult(text=ok('stored answer'), usage=Usage(100, 10, 500), model='m/primary', provider='Fixture Host')
    store, authority, engine, runtime, client, _ = with_model(database, [answer], cfg=cfg)
    incoming(store, 1, 'hello')
    original = runtime.propose
    def crash_after_verification(turn):
        original(turn)
        raise RuntimeError('simulated crash after verification')
    runtime.propose = crash_after_verification
    with pytest.raises(RuntimeError):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    runtime.propose = original
    assert rows(database)[0].outcome == 'verified_ok'
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) and len(client.requests) == 1
    store.close()


def test_changed_upstream_does_not_reuse_prior_verified_answer(database):
    from dataclasses import replace
    cfg = config(upstream_provider='first-host', upstream_reported_name='First Host')
    answers = [ChatResult(text=ok('stored answer'), usage=Usage(100, 10, 500), model='m/primary', provider='First Host'),
               ChatResult(text=ok('hello babe, how was your day?'), usage=Usage(100, 10, 500), model='m/primary', provider='Second Host')]
    store, authority, engine, runtime, client, _ = with_model(database, answers, cfg=cfg)
    incoming(store, 1, 'hello')
    original = runtime.propose
    def crash_after_verification(turn):
        original(turn)
        raise RuntimeError('simulated crash after verification')
    runtime.propose = crash_after_verification
    with pytest.raises(RuntimeError):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    runtime.propose = original
    runtime.config = replace(cfg, upstream_provider='second-host', upstream_reported_name='Second Host')
    runtime.ledger.config = runtime.config
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert decision and len(client.requests) == 2
    with Session(database[1]) as s:
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).body == 'hello babe, how was your day?'
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
    from dataclasses import replace
    runtime.profiles = StaticCreatorProfiles({ACCOUNT: replace(PROFILE, offers_permitted=True, approved_item_ids=(str(item_id),))})
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


_OUTSIDE = lambda r: ok('here you go', action='offer', offer_grant_id='00000000-0000-4000-8000-000000000000', pitch=True)
_PRICED = lambda r: ok('it is USD 1.00 for the set', action='offer', offer_grant_id=_grant_from_prompt(r), pitch=True)
_GOOD = lambda r: ok('a scenic video with stills, want it?', action='offer', offer_grant_id=_grant_from_prompt(r), pitch=True)


@pytest.mark.parametrize('bad', [_OUTSIDE, _PRICED])
def test_offer_outside_the_granted_item_and_model_priced_offers_are_regenerated_once(database, bad):
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(database, [bad, _GOOD])
    incoming(store, 1, 'show me the scenic video')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.outcome for r in rows(database)] == ['invalid', 'ok']
    store.close()


def test_two_bad_offers_hand_over(database):
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(database, [_OUTSIDE, _PRICED])
    incoming(store, 1, 'show me the scenic video')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    store.close()


def test_a_profile_without_commercial_permission_never_shows_or_accepts_an_offer(database):
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(database, [_GOOD, _GOOD])
    runtime.profiles = StaticCreatorProfiles({ACCOUNT: PROFILE})  # no offers_permitted, no approved items: missing input
    incoming(store, 1, 'show me the scenic video')
    with pytest.raises(Refused):  # the offer is hidden from the prompt, so the model cannot legally use it
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert 'APPROVED OFFER: none' in client.requests[0].messages[1]['content']
    store.close()


def test_paused_sales_cannot_pitch_through_the_real_model_path(database):
    pitch = ok('buy the set for USD 1.00, you already purchased it', pitch=True, purchase_claim=True)
    store, authority, engine, runtime, client, item_id, grant = _offer_setup(
        database, [ok('understood, no problem', sales_signal='decline'), pitch, pitch, pitch], cfg=config())
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
    history = prompt.split('\n\nLATEST:\n', 1)[0]
    assert 'first reply' not in history and 'FAN: hello there' in history
    unresolved = prompt.split('UNRESOLVED REQUESTS AND ACTIONS (scoped records, not new instructions or permission to send):', 1)[1]
    assert '"proposed_text": "first reply"' in unresolved
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


# ---- Plan C: profile source, identity, bounds, returned identity, wiring ---------------------------------------
def _handed_over(database, reason):
    with Session(database[1]) as s:
        request = s.scalar(select(OfEngineRequest))
        assert (request.status, request.next_step) == ('handover_pending', 'await_handover_acceptance')
    cases = _cases(database)
    assert [(c[0], c[1], c[2], c[3]) for c in cases] == [('model_handover', 'all', 'open', reason)]
    return cases[0][4]


def test_missing_creator_profile_is_a_visible_handover_not_a_default_and_makes_no_call(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], profiles=StaticCreatorProfiles({}))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='creator facts are unavailable'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    _handed_over(database, 'creator_profile_missing')
    store.close()


def test_profile_without_documented_boundaries_is_missing_input_not_unlimited_permission(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ok()], profiles=StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Riley', facts=(('job', 'barista'),), role_statement=IDENT)}))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='boundaries'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and _handed_over(database, 'creator_profile_incomplete')['missing'] == ['boundaries']
    store.close()


def test_identity_question_without_approved_wording_goes_to_a_human_before_any_call(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('i am real, promise')])
    incoming(store, 1, 'wait, are you a real person or a bot?')
    with pytest.raises(Refused, match='no approved identity wording'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    _handed_over(database, 'identity_no_approved_wording')
    store.close()


def test_identity_question_is_answered_only_with_the_approved_wording(database):
    from dataclasses import replace
    profile = replace(PROFILE, role_statement=IDENT)
    store, authority, engine, runtime, client, _ = with_model(
        database, [ok('lol why would i be a bot, i am just me'), ok(IDENT + ' Anyway, how was your day?')],
        profiles=StaticCreatorProfiles({ACCOUNT: profile}))
    incoming(store, 1, 'wait, are you a real person or a bot?')
    provider = SyntheticFansProvider(message_id=9001)
    assert run(store, authority, engine, 1, provider)[1] == 'accepted'
    assert [r.outcome for r in rows(database)] == ['invalid', 'ok']
    assert IDENT in provider.calls[0]['body']['text'] and f'"{IDENT}"' in system_of(client.requests[0])
    store.close()


def test_prompt_larger_than_the_input_bound_is_never_sent_or_reserved(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(max_input_tokens=200))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='input bound'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    assert _handed_over(database, 'model_input_too_large')['limit'] == 200
    store.close()


def test_oversize_answer_is_truncated_in_storage_rejected_and_handed_over(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('x' * 5000)], cfg=config(max_output_bytes=600))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    got = rows(database)
    assert [r.outcome for r in got] == ['invalid', 'invalid'] and all(len(r.response_text.encode()) <= 600 for r in got)
    assert any('output bound' in r for r in _cases(database)[0][4]['reasons'])
    store.close()


def test_returned_model_prompt_and_config_identity_are_recorded(database, caplog):
    import logging
    drifted = ChatResult(text=ok('hey you'), usage=Usage(100, 20, 900), model='m/primary-2026-11-01', provider='AcmeHost')
    store, authority, engine, runtime, client, _ = with_model(database, [drifted])
    incoming(store, 1, 'hello')
    with caplog.at_level(logging.INFO, logger='conversation_engine.model'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    [row] = rows(database)
    assert row.model == 'm/primary' and row.error_class == 'ret_model:m/primary-2026-11-01'  # a DIFFERENT returned model is flagged
    [line] = [r.getMessage() for r in caplog.records if 'model_call_identity' in r.getMessage()]
    identity = json.loads(line.split(' ', 1)[1])
    assert (identity['requested_model'], identity['returned_model'], identity['returned_provider']) == ('m/primary', 'm/primary-2026-11-01', 'AcmeHost')
    assert identity['prompt_version'] == 'a-original.v3-planc-nsfw' and identity['config_version'] == config().version()
    assert (identity['call_id'], row.prompt_tokens, row.completion_tokens, row.actual_microusd) == (row.call_id, 100, 20, 900)
    assert len(row.prompt_sha256) == 64
    store.close()


def test_same_returned_model_is_clean_and_an_unnamed_model_is_flagged_not_assumed(database):
    same = ChatResult(text=ok('hey you'), usage=Usage(100, 20, 900), model='m/primary')
    silent = ChatResult(text=ok('hi again'), usage=Usage(100, 20, 900), model='')
    store, authority, engine, runtime, client, _ = with_model(database, [same, silent])
    incoming(store, 1, 'hello')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert rows(database)[0].error_class is None
    unthrottle(database[1]); incoming(store, 2, 'again')
    engine.plan(TENANT, ACCOUNT, FAN, 2)
    assert rows(database)[1].error_class == 'ret_model:not_reported'
    store.close()


def test_fallback_is_off_by_default_so_an_availability_failure_never_reaches_another_model(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ModelHTTPStatus(503), ok('x')])
    assert runtime.config.fallback_enabled is False and runtime.config.models() == ('m/primary',)
    incoming(store, 1, 'hello')
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary'] and {r.model for r in rows(database)} == {'m/primary'}
    store.close()


# ---- the fallback (Ronan decision 2026-11-07): availability failures only ---------------------------------------------
@pytest.mark.parametrize('failure', [ModelNotSent('connect'), ModelHTTPStatus(429), ModelHTTPStatus(503),
    ModelHTTPStatus(500)])  # a timeout may have been charged: never a fallback (test_w3_trace_ledger_pg)
def test_fallback_runs_after_an_availability_failure_of_the_primary(database, failure):
    store, authority, engine, runtime, client, _ = with_model(database, [failure, ok('from the fallback')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(message_id=9001)
    _, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted' and provider.calls[0]['body']['text'] == 'from the fallback'
    first, second = rows(database)
    assert (first.model, first.chain_step, first.status) == ('m/primary', 'primary', 'released')
    assert (second.model, second.chain_step, second.status, second.outcome) == ('m/fallback', 'fallback', 'settled', 'ok')
    assert [r.model for r in client.requests] == ['m/primary', 'm/fallback']
    assert client.requests[0].messages == client.requests[1].messages  # the SAME inputs went to either model
    assert runtime.ledger.committed(day=first.cost_day) == 1000
    assert (first.reserved_microusd, second.reserved_microusd) == (BOUND, FALLBACK_BOUND)  # each model has its own bound
    store.close()


@pytest.mark.parametrize('first,reason,outcomes', [
    (ok("I'm sorry, but I can't continue with this conversation"), 'model_refused', ['refusal']),
    ('I cannot help with that request', 'model_refused', ['refusal']),
    (ChatResult(text='', usage=Usage(10, 0, 100), model='m/primary'), 'model_empty', ['empty'])])
def test_refusal_or_empty_completion_never_falls_back(database, first, reason, outcomes):
    store, authority, engine, runtime, client, _ = with_model(database, [first, ok('fallback would happily write it')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary'] and [r.outcome for r in rows(database)] == outcomes
    [(category, _, _, got_reason, evidence)] = _cases(database)
    assert (category, got_reason, evidence['outcomes']) == ('model_handover', reason, outcomes)
    store.close()


def test_validator_rejection_corrects_on_the_primary_and_never_falls_back(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ok('it is only $5 for the set'), ok('thanks for buying it earlier'), ok('the fallback would pass')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary', 'm/primary']  # the corrected attempt, then a human
    assert [r.outcome for r in rows(database)] == ['invalid', 'invalid']
    store.close()


def test_malformed_output_never_falls_back_either(database):
    store, authority, engine, runtime, client, _ = with_model(database, ['not json', 'still not json', ok('x')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert {r.model for r in client.requests} == {'m/primary'}
    store.close()


def test_primary_unavailable_then_fallback_rejected_is_human_work_with_no_third_call(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelHTTPStatus(503), ok('it is only $5 for the set'), ok('third call must never happen')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary', 'm/fallback']
    assert [(r.chain_step, r.outcome) for r in rows(database)] == [('primary', 'http_503'), ('fallback', 'invalid')]
    store.close()


@pytest.mark.parametrize('failure', [ModelHTTPStatus(400), ModelHTTPStatus(401), ModelHTTPStatus(403), ModelHTTPStatus(404)])
def test_failures_that_are_not_availability_never_fall_back(database, failure):
    store, authority, engine, runtime, client, _ = with_model(database, [failure, ok('fallback')], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    # R0 2026-11-11: a provider HTTP refusal (4xx) is visible human work instead of a silently open request.
    # Unclassified client errors and timeouts: test_w3_trace_ledger_pg.
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert [r.model for r in client.requests] == ['m/primary']
    store.close()


def test_total_attempts_stay_bounded_with_a_fallback(database):
    clock = Clock()
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelHTTPStatus(503), ModelHTTPStatus(503), ok('never')], clock=clock, cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(ModelUnavailable):
        engine.plan(TENANT, ACCOUNT, FAN, 1)          # primary unavailable, fallback unavailable too
    assert [r.model for r in client.requests] == ['m/primary', 'm/fallback']
    assert [r.status for r in rows(database)] == ['released', 'released']
    clock.offset = timedelta(seconds=300)
    with pytest.raises(Refused, match='call limit reached; request handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)          # two attempts is the bound: a human gets the request
    assert len(client.requests) == 2
    store.close()


def test_one_global_cap_covers_both_models_and_the_whole_chain(database):
    # primary reserve needs its own bound plus the largest bound for the one remaining attempt
    need = BOUND + FALLBACK_BOUND
    store, authority, engine, runtime, client, _ = with_model(
        database, [ModelHTTPStatus(503), ok('fits exactly')], cfg=config(daily_cap_microusd=need + 0, **FALLBACK))
    incoming(store, 1, 'hello')
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) and len(client.requests) == 2  # primary reserve 8600 + remaining 17200 = cap
    store.close()


def test_cap_one_unit_short_refuses_before_any_call_with_two_models(database):
    need = BOUND + FALLBACK_BOUND
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(daily_cap_microusd=need - 1, **FALLBACK))
    incoming(store, 1, 'hello')
    with pytest.raises(ModelBudgetExhausted, match='daily'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    store.close()


def test_cap_race_between_a_primary_call_and_a_fallback_call_only_one_fits(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(daily_cap_microusd=40_000, **FALLBACK))
    first, second = _two_requests(database, engine, runtime, client, store)
    results, barrier = {}, threading.Barrier(2)
    threads = [threading.Thread(target=_reserve_for, args=(runtime, database, first, 'wa', results, 'primary', barrier, 'm/primary', 'primary')),
               threading.Thread(target=_reserve_for, args=(runtime, database, second, 'wb', results, 'fallback', barrier, 'm/fallback', 'fallback'))]
    [t.start() for t in threads]; [t.join(30) for t in threads]
    won = [k for k, v in results.items() if not isinstance(v, Exception)]
    assert len(won) == 1 and sum(isinstance(v, ModelBudgetExhausted) for v in results.values()) == 1
    assert runtime.ledger.committed(day=utcnow().date()) == (BOUND if won == ['primary'] else FALLBACK_BOUND)
    store.close()


def test_fallback_call_records_requested_returned_model_and_that_it_was_the_fallback(database, caplog):
    import logging
    served = ChatResult(text=ok('hey you'), usage=Usage(100, 20, 900), model='x-ai/grok-4.1-fast-20261101', provider='xAI')
    store, authority, engine, runtime, client, _ = with_model(database, [ModelHTTPStatus(429), served], cfg=config(**FALLBACK))
    incoming(store, 1, 'hello')
    with caplog.at_level(logging.INFO, logger='conversation_engine.model'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    primary, fallback = rows(database)
    assert (primary.model, primary.chain_step, primary.outcome) == ('m/primary', 'primary', 'http_429')
    assert (fallback.model, fallback.chain_step, fallback.error_class) == ('m/fallback', 'fallback', 'ret_model:x-ai/grok-4.1-fast-20261101'[:64])
    [line] = [r.getMessage() for r in caplog.records if 'model_call_identity' in r.getMessage()]
    identity = json.loads(line.split(' ', 1)[1])
    assert (identity['requested_model'], identity['returned_model'], identity['returned_provider']) == ('m/fallback', 'x-ai/grok-4.1-fast-20261101', 'xAI')
    assert identity['is_fallback'] is True and identity['chain_step'] == 'fallback' and identity['config_version'] == config(**FALLBACK).version()
    store.close()


def test_fallback_model_must_be_on_the_allow_list_before_any_call(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok()], cfg=config(**{**FALLBACK, 'allowed_models': ('m/primary',)}))
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='approved list'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == []
    store.close()


def test_engine_service_accepts_the_real_runtime_selected_by_application_configuration(database, tmp_path):
    from model_transport.wiring import build_engine_service
    from conversation_engine.writer_context import set_active_profiles
    store, authority, engine, model, memory = setup(database)
    profiles = tmp_path / 'profiles.json'
    profiles.write_text(json.dumps({ACCOUNT: {'name': 'Riley', 'facts': {'job': 'barista'}, 'limits': ['no meetups'], 'role_statement': IDENT}}))
    env = {'ENGINE_MODEL_ENABLED': '1', 'ENGINE_MODEL_PRIMARY': 'm/primary', 'ENGINE_MODEL_ALLOWED': 'm/primary',
           'ENGINE_MODEL_ACCOUNTS': ACCOUNT, 'ENGINE_MODEL_KEY_ENV': 'PLANC_KEY', 'PLANC_KEY': 'sk-planc-0000',
           'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK': '1000000', 'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK': '2000000',
           'ENGINE_MODEL_MAX_INPUT_TOKENS': '8000', 'ENGINE_MODEL_MAX_TOKENS': '300', 'ENGINE_MODEL_TIMEOUT_SECONDS': '20',
           'ENGINE_MODEL_DAILY_CAP_MICROUSD': '1000000', 'ENGINE_MODEL_CREATOR_CAP_MICROUSD': '1000000',
           'ENGINE_MODEL_PROFILES_FILE': str(profiles)}
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [{'message': {'content': ok('hi there you')}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 500, 'completion_tokens': 10, 'cost': 0.0007}})
    try:
        assert build_engine_service(store, {}) is None and build_engine_service(store, {**env, 'ENGINE_MODEL_ENABLED': '0'}) is None
        draft_service = build_engine_service(store, env, transport=httpx.MockTransport(handler), draft_only=True)
        assert draft_service.draft_only is True and seen == {}
        service = build_engine_service(store, env, transport=httpx.MockTransport(handler))
        assert type(service.model).__name__ == 'ModelRuntime' and service.model.client.provider == 'openrouter'
        incoming(store, 1, 'hello')
        decision = service.plan(TENANT, ACCOUNT, FAN, 1)
        with Session(database[1]) as s:
            assert s.get(OfEngineDecision, (ACCOUNT, decision)).body == 'hi there you'
        assert seen['body']['model'] == 'm/primary' and not {'models', 'route'} & set(seen['body'])
        [row] = rows(database)
        assert (row.provider, row.model, row.actual_microusd, row.error_class) == ('openrouter', 'm/primary', 700, None)
        assert 'sk-planc' not in json.dumps(seen['body']) and 'sk-planc' not in repr(service.model.client)
    finally:
        set_active_profiles(None)
        store.close()


def test_empty_completion_is_a_silent_refusal_one_call_then_visible_human_work(database):
    silent = ChatResult(text='', usage=Usage(300, 0, 400), model='deepseek/deepseek-v4.1-flash', provider='SomeHost')
    store, authority, engine, runtime, client, _ = with_model(database, [silent, ok('a different answer on a second try')])
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 1                       # no repeat call to dodge the silence
    [row] = rows(database)
    assert (row.outcome, row.status, row.actual_microusd) == ('empty', 'settled', 400)  # the spend is recorded
    [(category, _, status, reason, evidence)] = _cases(database)
    assert (category, status, reason, evidence['outcomes']) == ('model_handover', 'open', 'model_empty', ['empty'])
    with pytest.raises(Refused, match='with a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(client.requests) == 1
    store.close()
