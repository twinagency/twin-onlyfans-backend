"""R0 operator status follows durable request/deadline evidence, never inferred retry policy.

Real scheduler/runtime/ledger and authenticated operator HTTP, synthetic transports only.
"""
from dataclasses import replace
from datetime import timedelta

import pytest

from test_plan_c_r0_acceptance_pg import (make_world, database, world, configured_draft, stopped_text,
                                         post, request_item)
from plan_c_worker import _work_counts


def configure_cooldown(w, seconds):
    model = w.pilot.worker.engine.model
    model.config = replace(model.config, transport_retry_seconds=seconds)
    model.ledger.config = model.config
    # Reconstruct the frozen pilot with the same worker and approved durable model identity.
    # All ordinary validate/require_model checks remain active.
    w.pilot = replace(w.pilot, model_config=model.config)


@pytest.mark.parametrize('retry_after', [None, '120', '7200'])
def test_nondefault_cooldown_is_scheduled_until_actual_handover(make_world, monkeypatch, retry_after):
    script = [429 if retry_after is None else (429, {'Retry-After': retry_after})]
    w = make_world(script)
    configure_cooldown(w, 7200)
    post(w, 94001)
    w.ticks(1)
    [call] = w.calls()
    assert call.retry_not_before - call.updated_at == timedelta(seconds=7200)
    assert w.request(94001)[0] == 'open'
    before = (call.call_id, call.status, call.outcome, call.retry_not_before, call.failure_detail_json)
    shown = request_item(w, 94001, monkeypatch)
    assert shown['recoveryState'] == 'scheduled_retry' and shown['operatorAlert']
    assert shown['recoveryReason'] == 'long_retry_wait'
    assert shown['recoveryNextStep'] == 'wait_until_retry_due'
    assert shown['nextRetryAt'] == call.retry_not_before.isoformat() + 'Z'
    assert _work_counts(w.pilot)['modelRetryDelayed'] == 1
    assert request_item(w, 94001, monkeypatch) == shown
    assert w.provider.calls == [429] and w.w.fans.posts == []
    unchanged = w.calls()[0]
    assert before == (unchanged.call_id, unchanged.status, unchanged.outcome,
                      unchanged.retry_not_before, unchanged.failure_detail_json)
    w.advance(3601)
    w.ticks(3)
    assert w.provider.calls == [429] and w.w.fans.posts == []
    w.advance(7201)
    w.ticks(3)
    assert w.provider.calls == [429, 'ok']
    assert w.request(94001)[0] == 'completed' and len(w.w.fans.posts) == 1


@pytest.mark.parametrize('cooldown', [30, 120, 7200])
def test_legacy_missing_deadline_is_explicitly_unknown_without_changing_recovery(
        make_world, monkeypatch, cooldown):
    w = make_world([429])
    configure_cooldown(w, cooldown)
    ledger = w.pilot.worker.engine.model.ledger
    release = ledger.release

    def old_shape_release(*args, **kwargs):
        # A row written by 0129 had no durable deadline/details. Build that old shape
        # through the real ledger rather than disabling immutable evidence triggers.
        kwargs.pop('retry_delay_seconds', None)
        kwargs.pop('failure_detail', None)
        return release(*args, **kwargs)

    monkeypatch.setattr(ledger, 'release', old_shape_release)
    post(w, 94002)
    w.ticks(1)
    [call] = w.calls()
    assert call.status == 'released' and call.outcome == 'http_429' and call.retry_not_before is None
    shown = request_item(w, 94002, monkeypatch)
    assert shown['recoveryState'] == 'retry_timing_unknown' and shown['operatorAlert']
    assert shown['nextRetryAt'] is None and shown['recoveryNextStep'] == 'review_retry_timing'
    assert request_item(w, 94002, monkeypatch) == shown
    assert w.provider.calls == [429] and w.w.fans.posts == []
    assert _work_counts(w.pilot)['staleUnplannedRequests'] == 0
    w.advance(cooldown + 1)
    w.ticks(3)
    assert w.provider.calls == [429, 'ok']
    assert w.request(94002)[0] == 'completed' and len(w.w.fans.posts) == 1


@pytest.mark.parametrize('delay', ['7200', '9' * 100])
def test_crash_before_handover_keeps_long_wait_visible_without_stopping_other_fan(
        make_world, monkeypatch, delay):
    from test_plan_c_r0_acceptance_pg import FAN2
    w = make_world([(429, {'Retry-After': delay})])
    ledger = w.pilot.worker.engine.model.ledger
    release = ledger.release

    def crash_after_release(*args, **kwargs):
        release(*args, **kwargs)
        raise RuntimeError('injected crash after durable release')

    with monkeypatch.context() as m:
        m.setattr(ledger, 'release', crash_after_release)
        post(w, 94003)
        with pytest.raises(RuntimeError, match='injected crash after durable release'):
            w.ticks(1)
    assert w.request(94003)[0] == 'open'
    shown = request_item(w, 94003, monkeypatch)
    assert shown['recoveryState'] == 'scheduled_retry' and shown['operatorAlert']
    assert shown['recoveryReason'] == 'long_retry_wait'
    assert shown['recoveryNextStep'] == 'wait_until_retry_due'
    assert w.provider.calls == [429] and w.w.fans.posts == []
    post(w, 94004, fan=FAN2)
    w.ticks(4)
    assert w.request(94004)[0] == 'completed' and len(w.w.fans.posts) == 1
    assert w.request(94003)[0] == 'open' and w.provider.calls == [429, 'ok']
    if delay == '7200':
        w.advance(7201)
        w.ticks(3)
        assert w.request(94003)[0] == 'handover_pending'
        shown = request_item(w, 94003, monkeypatch)
        assert shown['recoveryState'] == 'deferred_operator' and shown['operatorAlert']
        assert shown['recoveryReason'] == 'model_retry_deferred'
        assert w.provider.calls == [429, 'ok'] and len(w.w.fans.posts) == 1


@pytest.mark.parametrize('next_step', ['review_changed_conversation', 'review_delivery_capacity'])
def test_existing_review_work_is_visible_without_repeating_planning_gate(make_world, monkeypatch, next_step):
    from sqlalchemy.orm import Session
    from db.onlyfans_engine_models import OfEngineRequest
    from conversation_engine import pilot_scope
    w = make_world([429])
    post(w, 94005)
    w.ticks(1)
    with Session(w.w.database[1]) as s, s.begin():
        rows=list(s.query(OfEngineRequest).filter_by(account_id=w.pilot.account, source_message_id=94005))
        assert len(rows)==1
        rows[0].next_step=next_step
    def cannot_plan(*a, **kw):
        raise AssertionError('status reader repeated full planning gate for an existing review hold')
    monkeypatch.setattr(pilot_scope, 'planning_block', cannot_plan)
    shown=request_item(w, 94005, monkeypatch)
    assert shown['recoveryState']=='review_hold' and shown['recoveryNextStep']==next_step
    assert not shown['operatorAlert']
    assert _work_counts(w.pilot)['staleUnplannedRequests']==0
    assert w.provider.calls==[429] and w.w.fans.posts==[]
