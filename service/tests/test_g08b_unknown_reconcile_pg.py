"""G08b: an unknown model-call outcome is settled only from provider evidence or handed to a human, never re-asked."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from conversation_engine.model_ledger import ModelLedger
from db.onlyfans_engine_models import OfEngineModelCall, OfEnginePilotScope, OfEngineRequest
from db.onlyfans_case_models import OfOperatorCase
from onlyfans_runtime.store import utcnow
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import world  # noqa: F401
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_supervised_pg import supervised, stage  # noqa: F401


@pytest.mark.parametrize('reactivated', [False, True])
@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_crash_recovered_unknown_call_is_handed_to_an_operator_case(supervised, monkeypatch, reactivated):
    # reactivated: the call predates the current activation (pilot stopped/re-activated after the crash; Codex G08b).
    world, _, _, seen, pilot = supervised
    ledger = pilot.worker.engine.model.ledger
    original = ledger.mark_calling
    def crash_after_marker(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('offline post-marker crash')
    monkeypatch.setattr(ledger, 'mark_calling', crash_after_marker)
    with pytest.raises(RuntimeError, match='offline post-marker crash'):
        stage(pilot, 18081, 'hello there')
    monkeypatch.setattr(ledger, 'mark_calling', original)
    with Session(world.database[1]) as s, s.begin():
        row = s.scalar(select(OfEngineModelCall))
        assert row.status == 'calling'
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        request_id, reserved = row.request_id, row.reserved_microusd
        assert reserved > 0
        if reactivated:  # a later activation; the scope guard only allows that via stop/activate, so bypass it here
            s.execute(text('SET LOCAL session_replication_role = replica'))
            s.scalar(select(OfEnginePilotScope)).active_since = utcnow() + timedelta(seconds=1)
    for _ in range(3):
        pilot.tick()
    assert not seen and not world.fans.posts  # never re-asked, never sent
    with Session(world.database[1]) as s:
        [call] = s.scalars(select(OfEngineModelCall)).all()
        assert (call.status, call.usage_known, call.actual_microusd) == ('unknown', False, None)
        assert call.reserved_microusd == reserved  # reservation kept, still counted against the day
        request = s.get(OfEngineRequest, (call.account_id, request_id))
        assert request.status == 'handover_pending', 'crash-recovered unknown call must reach a human'
        cases = s.scalars(select(OfOperatorCase).where(OfOperatorCase.category == 'model_handover')).all()
        assert len(cases) == 1 and cases[0].reason == 'model_outcome_unknown'


def test_ledger_has_no_unaudited_cost_setter_that_reopens_unknown_calls():
    # The only settle route for an unknown call is provider evidence (model_transport.reconciliation), which keeps an
    # unknown OUTPUT held. A bare operator-supplied cost that flips the call to 'settled' would make the request look
    # resolved to the runtime and the pilot scheduler, i.e. eligible for a re-ask after an unknown outcome.
    assert not hasattr(ModelLedger, 'reconcile_unknown')
