"""V5 section 6, N6: provider-request exhaustion DURING a tick, and the daily text-unit boundary, in the AUTONOMOUS text-only
pilot (stopped_text -> text_no_offers), signed HTTP intake, normal pilot.tick(), real PostgreSQL, fake model, fake FansAPI.

Question per case: reservations retained (never refunded, never double counted), ONLY the appropriate work held, the work that
was already safe still completed, and REQUIRED RECOVERY (saved accepted receipt -> transcript, no POST) still allowed.
The limits come from the durable pilot scope (limits.provider_requests, limits.daily_send_units) exactly as an operator would
configure them; nothing is seeded into a ledger.
"""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import Message
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineRequest
from onlyfans_runtime.store import Refused
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig  # noqa: F401
from test_plan_c_q2_n2_crash_recovery_pilot_pg import expire_dispatching, facts, restart
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401


@pytest.fixture
def tight(monkeypatch, request):
    """Tighten the durable pilot scope BEFORE it is installed: the same config_for the other stopped_text suites use."""
    import test_plan_c_text_activation_pg as activation
    real, box = activation.config_for, dict(getattr(request, 'param', {}))

    def wrapped(w, env, **kw):
        cfg = real(w, env, **kw)
        cfg['limits'].update(box)
        return cfg
    monkeypatch.setattr(activation, 'config_for', wrapped)
    return box


def ledger(rig):
    d = pilot_scope.describe(rig.w.store, TENANT, ACCOUNT)
    return d['providerRequestsUsed'], d['providerCreditsReserved'], d['config']['limits']


def tick_all(rig, n):
    """A refusal out of tick is an expected outcome here; any other exception is a defect and propagates."""
    out = []
    for _ in range(n):
        try:
            out.append(('ok', rig.pilot.tick()))
        except Refused as exc:
            out.append(('refused', str(exc)))
    return out


def served(rig):
    return {fan for fan in (FAN, FAN2) if any(f'/{fan}/' in p['path'] for p in rig.w.fans.posts)}


def intents_for(rig, fan):
    with Session(rig.w.database[1]) as s:
        return [(i.status, i.reason, i.wire_started_at is not None, i.reservation_held)
                for i in s.scalars(select(OfTextIntent).where(OfTextIntent.fan_id == fan).order_by(OfTextIntent.created_at))]


def out_messages(rig, fan):
    with Session(rig.w.database[1]) as s:
        return s.scalar(select(func.count()).select_from(Message).where(Message.fan_id == fan, Message.direction == 'out'))


@pytest.mark.parametrize('tight', [{'provider_requests': 2}], indirect=True)
def test_request_budget_exhausted_inside_a_tick_holds_only_the_unsent_fan_and_never_refunds_the_reservation(tight, rig):
    used, credits, limits = ledger(rig)
    assert used == 1 and limits['provider_requests'] == 2, 'activation spent one request; exactly one is left for the whole tick'
    per_request = limits['provider_credits_per_request']
    rig.fans_in()
    first = tick_all(rig, 1)
    who = served(rig)
    assert len(who) == 1 and len(rig.w.fans.posts) == 1, f'exactly one send fits the last request: {first}'
    held = ({FAN, FAN2} - who).pop()
    assert ledger(rig)[:2] == (2, credits + per_request), 'the one spent request is reserved at its full worst-case credit cost'
    calls = len(rig.seen)
    assert calls == 2, 'both fans were planned before the budget ran out'

    rounds = tick_all(rig, 4)
    assert all(kind in ('ok', 'refused') for kind, _ in rounds)
    assert ledger(rig)[:2] == (2, credits + per_request), 'a refused attempt reserves nothing and nothing is refunded'
    assert len(rig.w.fans.posts) == 1 and served(rig) == who, 'no further POST for anyone'
    assert len(rig.seen) == calls, 'exhaustion is not a reason to spend a model call per tick'

    # Only the appropriate work is held: the served fan is complete; the held fan keeps its request, was never wired, never unknown.
    done = rig.requests()
    assert done[next(iter(who))] == 'completed' and done[held] != 'completed', done
    assert out_messages(rig, next(iter(who))) == 1 and out_messages(rig, held) == 0
    expire_dispatching(rig)
    tick_all(rig, 3)
    mine, theirs = intents_for(rig, next(iter(who))), intents_for(rig, held)
    assert [i[0] for i in mine] == ['accepted']
    # The guard refused before any HTTP request was made, so the attempt is PROVED not accepted (terminal, unit released), not unknown.
    assert [(i[0], i[1], i[3]) for i in theirs] == [('not_accepted', 'proved_not_accepted', False)], f'{theirs}'
    assert len([p for p in rig.w.fans.posts if f'/{held}/' in p['path']]) == 0, 'no request reached the provider for the held fan'
    after = facts(rig)
    assert after['used'] == 1 and after['queued'] == 0, 'units: only the accepted send is counted, nothing leaked'
    assert after['unresolved'] == {FAN: False, FAN2: False}
    assert ledger(rig)[:2] == (2, credits + per_request)


def crash_after_acceptance(rig, monkeypatch):
    rig.post(inbound(79601, 'hello, how was your day?', fan=FAN), key='n6-a')
    authority = rig.pilot.worker.authority
    original = authority.materialize

    def crash(*args, **kwargs):
        raise RuntimeError('crash after the accepted receipt committed')
    monkeypatch.setattr(authority, 'materialize', crash)
    with pytest.raises(RuntimeError, match='crash after the accepted receipt'):
        rig.pilot.tick()
    monkeypatch.setattr(authority, 'materialize', original)
    assert len(rig.w.fans.posts) == 1 and ledger(rig)[0] == ledger(rig)[2]['provider_requests'], 'the last request was spent on that send'
    assert [(i[1], i[4]) for i in facts(rig)['intents']] == [('accepted', False)], 'accepted receipt saved, not yet recorded'


@pytest.mark.parametrize('tight', [{'provider_requests': 2}], indirect=True)
def test_saved_accepted_receipt_is_recorded_in_process_with_the_request_budget_exhausted_and_the_other_fan_stays_held(tight, rig, monkeypatch):
    used, credits, limits = ledger(rig)
    crash_after_acceptance(rig, monkeypatch)
    rig.post(inbound(79602, 'hey are you there?', fan=FAN2), key='n6-b')
    rounds = tick_all(rig, 5)
    final = facts(rig)
    # Required recovery is allowed with no budget left: it needs no provider request.
    assert [(i[0], i[1], i[4]) for i in final['intents'] if i[0] == FAN] == [(FAN, 'accepted', True)], 'recorded exactly once'
    assert out_messages(rig, FAN) == 1 and final['requests'][FAN] == 'completed'
    assert len(rig.w.fans.posts) == 1, 'no replay of the accepted send and no send for the other fan (no request left)'
    assert ledger(rig)[:2] == (limits['provider_requests'], credits + limits['provider_credits_per_request'])
    # ...while the unsent fan is the only thing held.
    assert final['requests'].get(FAN2) != 'completed' and out_messages(rig, FAN2) == 0
    assert not any(i[0] == FAN2 and i[1] in ('accepted', 'unknown') for i in final['intents']), 'FAN2 was never sent and is not unknown'
    assert final['unresolved'] == {FAN: False, FAN2: False}
    assert all(kind in ('ok', 'refused') for kind, _ in rounds)


@pytest.mark.parametrize('tight', [{'provider_requests': 2}], indirect=True)
def test_cold_restart_with_the_request_budget_exhausted_fails_closed_and_loses_the_saved_receipt_nowhere(tight, rig, monkeypatch):
    """CHARACTERISATION of a finding (see v5-s6 findings): the worker's start-up identity check needs one provider request. With the
    ledger exactly exhausted, a cold-built replacement worker cannot start, so a saved accepted receipt cannot be recorded until
    the operator renews the scope with a larger request budget. Nothing is lost or sent; it is fail-closed, not a safe in-place
    recovery. If this is changed (e.g. start-up identity exempt from the pilot request ledger), this test must be updated on purpose."""
    used, credits, limits = ledger(rig)
    crash_after_acceptance(rig, monkeypatch)
    from model_transport.pilot_supervised import build_supervised_pilot
    with pytest.raises(Refused, match='pilot provider request budget exhausted'):
        build_supervised_pilot(rig.w.store, rig.env, model_transport=rig.model, fans_transport=rig.w.fans.transport)
    after = facts(rig)
    assert [(i[1], i[4]) for i in after['intents']] == [('accepted', False)], 'the saved receipt is intact, awaiting a worker'
    assert len(rig.w.fans.posts) == 1 and ledger(rig)[:2] == (limits['provider_requests'], credits + limits['provider_credits_per_request'])
    # The accepted-but-unrecorded attempt keeps FAN's conversation held (unresolved marker) until a worker records it; FAN2 is untouched.
    assert after['out_messages'] == 0 and after['unresolved'] == {FAN: True, FAN2: False}


@pytest.mark.parametrize('tight', [{'daily_send_units': 1}], indirect=True)
def test_daily_unit_boundary_holds_the_second_fan_until_the_window_rolls_and_loses_nothing(tight, rig):
    with Session(rig.w.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert policy.daily_limit_units == 1 and policy.limit_units > 1, 'one unit a day, many in total'
    rig.fans_in()
    rig.ticks(4)
    who = served(rig)
    assert len(who) == 1 and len(rig.w.fans.posts) == 1, 'the daily boundary lets exactly one reply out today'
    held = ({FAN, FAN2} - who).pop()
    assert out_messages(rig, held) == 0 and rig.requests()[held] != 'completed', 'the other fan is held, not completed or dropped'
    assert not any(i[0] == 'unknown' for i in facts(rig)['intents']), 'a daily hold is not an unknown send'
    with Session(rig.w.database[1]) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    # Roll the 24h window; the held reply (or a fresh plan) is then allowed, once.
    with rig.w.database[1].begin() as c:
        c.execute(text("UPDATE of_text_intent SET dispatched_at = dispatched_at - interval '25 hours', "
                       "created_at = created_at - interval '25 hours'"))
    rig.ticks(5)
    assert served(rig) == {FAN, FAN2} and len(rig.w.fans.posts) == 2, 'after the window rolls the held fan is answered exactly once'
    assert rig.requests() == {FAN: 'completed', FAN2: 'completed'}
    assert facts(rig)['used'] == 2
