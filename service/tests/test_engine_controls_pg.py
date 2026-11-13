"""New-engine mechanical controls do not invoke legacy conversation policy."""
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import Fan, FanSafetyFlag, OfConversationState, SkipList
from db.onlyfans_case_models import OfOperatorCase
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused, utcnow


def setup(database):
    store, authority = configured(database, mode='automated')
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_controls_fixture', 'creator-fixture')
    model = SyntheticModel({'Are you a bot?': SyntheticProposal('I am an AI assistant.')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'Are you a bot?')
    return store, authority, engine, engine.plan(TENANT, ACCOUNT, FAN, 1)


def test_new_engine_does_not_run_legacy_detector_or_policy(database, monkeypatch):
    store, authority, engine, decision = setup(database)
    from onlyfans_runtime import operator_cases, conversation_policy
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('legacy')
        raise AssertionError('legacy conversation policy was invoked')
    monkeypatch.setattr(operator_cases, 'scan', forbidden)
    monkeypatch.setattr(conversation_policy, 'decide', forbidden)
    monkeypatch.setattr(conversation_policy, 'action_of', forbidden)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(message_id=9501)
    assert authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)) == 'accepted'
    assert calls == [] and len(provider.calls) == 1
    store.close()


def test_new_engine_honours_ladder_stop_without_legacy_policy(database, monkeypatch):
    # W3TSA-02: ladder_stop is in runtime_stop.STRONGER, so the engine hold (controls.reason) refuses it too.
    store, authority, engine, decision = setup(database)
    from onlyfans_runtime import operator_cases, conversation_policy
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy conversation policy was invoked')
    monkeypatch.setattr(operator_cases, 'scan', forbidden)
    monkeypatch.setattr(conversation_policy, 'decide', forbidden)
    monkeypatch.setattr(conversation_policy, 'action_of', forbidden)
    with Session(database[1]) as s, s.begin():
        s.add(SkipList(account_id=ACCOUNT, fan_id=FAN, reason='ladder_stop'))
    with pytest.raises(Refused, match='fan_restricted'):
        engine.queue(TENANT, ACCOUNT, decision)
    store.close()


@pytest.mark.parametrize('kind', ['muted', 'no_contact', 'platform_restriction', 'critical_flag', 'operator_case'])
def test_new_engine_retains_explicit_contact_and_safety_holds(database, kind):
    store, authority, engine, decision = setup(database)
    with Session(database[1]) as s, s.begin():
        if kind == 'muted':
            s.get(Fan, (ACCOUNT, FAN)).is_muted = True
        elif kind == 'no_contact':
            s.get(OfConversationState, (ACCOUNT, FAN)).mode = 'no_contact'
        elif kind == 'platform_restriction':
            s.add(SkipList(account_id=ACCOUNT, fan_id=FAN, reason='of_restricted'))
        elif kind == 'critical_flag':
            s.add(FanSafetyFlag(account_id=ACCOUNT, fan_id=FAN, flag_type='fixture', severity='critical', flagged_by='human'))
        else:
            s.add(OfOperatorCase(account_id=ACCOUNT, fan_id=FAN, case_id='a'*36,
                category='other', scope='all', status='open', reason='operator_hold', evidence_json='{}',
                origin='operator', created_at=utcnow(), updated_at=utcnow()))
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, decision)
    store.close()


def test_hold_added_after_queue_is_checked_before_wire(database):
    store, authority, engine, decision = setup(database)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    with Session(database[1]) as s, s.begin():
        s.add(FanSafetyFlag(account_id=ACCOUNT, fan_id=FAN, flag_type='fixture', severity='critical', flagged_by='human'))
    provider = SyntheticFansProvider()
    assert authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)) in {'refused', 'cancelled'}
    assert provider.calls == []
    store.close()


def test_explicit_fan_opt_out_is_audited_without_clearing_unknown(database, monkeypatch):
    from db.onlyfans_models import OfControlAudit
    from db.onlyfans_engine_models import OfEngineRequest
    from onlyfans_runtime import operator_cases
    store, authority, engine, decision = setup(database)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(('unknown',))
    assert authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)) == 'unknown'
    incoming(store, 2, 'Please stop messaging me.')
    monkeypatch.setattr(operator_cases, 'scan', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('legacy detector invoked')))
    calls = len(engine.model.calls)
    with pytest.raises(Refused, match='no contact'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    assert len(engine.model.calls) == calls and len(provider.calls) == 1
    with Session(database[1]) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert state.mode == 'no_contact' and state.unresolved_attempt_id is not None
        audit = s.query(OfControlAudit).filter_by(account_id=ACCOUNT, fan_id=FAN, action='mark_no_contact').one()
        assert 'message 2' in audit.reason
        request = s.query(OfEngineRequest).filter_by(account_id=ACCOUNT, fan_id=FAN, source_message_id=2).one()
        assert request.status == 'declined' and request.next_step == 'no_contact'
    store.close()


def test_stop_detection_does_not_treat_a_quoted_question_as_consent(database):
    from conversation_engine.controls import explicit_stop
    assert explicit_stop('Do not contact me again!')
    assert not explicit_stop('Did you say stop messaging me?')
    assert not explicit_stop('Please stop messaging me about that one topic')


def test_contact_stop_retries_a_concurrent_control_revision(database, monkeypatch):
    store, authority, engine, decision = setup(database)
    incoming(store, 2, 'Do not contact me again!')
    original = store.control
    fired = False
    def concurrent(*args, **kwargs):
        nonlocal fired
        if kwargs.get('synthetic_engine_only') and not fired:
            fired = True
            original(TENANT, ACCOUNT, FAN, mode='paused', expected_revision=0,
                command_key='race-pause', actor='synthetic-operator', reason='test race', action='pause_automation')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'control', concurrent)
    with pytest.raises(Refused, match='no contact'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    with Session(database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).revision == 2
    store.close()


def test_contact_stop_does_not_change_a_switched_live_policy(database, monkeypatch):
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    store, authority, engine, decision = setup(database)
    incoming(store, 2, 'Please stop messaging me.')
    original = store.control
    def switch(*args, **kwargs):
        with Session(database[1]) as s, s.begin():
            s.get(OfDeliveryPolicy, ACCOUNT).transport_kind = 'native'
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'control', switch)
    with pytest.raises(Refused, match='synthetic engine control scope'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    with Session(database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
    store.close()
