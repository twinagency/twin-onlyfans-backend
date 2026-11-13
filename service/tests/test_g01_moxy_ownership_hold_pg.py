"""G01-02: one fan's unreadable ownership evidence holds only that fan's Moxy event; the account's step continues.

Real PostgreSQL (throwaway), synthetic model/transport; no network. Declared bypass: one paid `messages` row with a
malformed `media_ids` payload inserted by ORM (stand-in for corrupt ingested PPV history). Synthetic ids only.
"""
import pytest
from sqlalchemy.orm import Session

from sqlalchemy import func, select

from db.models import Fan, Message, OfConversationState
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow
from test_moxy_integrated_pg import decision, model, setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT

HEALTHY, CORRUPT = 201, 202


@pytest.mark.parametrize('stage', ['pending', 'ready'])
def test_g01_02_corrupt_ownership_for_one_fan_holds_only_that_fans_event(database, monkeypatch, stage):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    with Session(engine) as s, s.begin():
        for fan in (HEALTHY, CORRUPT):
            s.add(Fan(account_id=ACCOUNT, fan_id=fan)); s.flush()
            s.add(OfConversationState(account_id=ACCOUNT, fan_id=fan, mode='automated', revision=0,
                                      input_revision=0, takeover_state='none'))
    bad = service.add_event(TENANT, ACCOUNT, CORRUPT, 'scheduled', 'g01-corrupt', {})
    if stage == 'ready':  # prepared on clean evidence, corrupted before the step re-checks its snapshot
        service.prepare(TENANT, ACCOUNT, bad, model(decision()))
        with Session(engine) as s:
            assert s.get(MoxyEvent, (ACCOUNT, bad)).status == 'ready'
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=CORRUPT, message_id=990101, direction='out', media_ids='[7101,',
                      price_cents=7500, is_paid=True, created_at=utcnow()))
    good = service.add_event(TENANT, ACCOUNT, HEALTHY, 'scheduled', 'g01-healthy', {})
    assert service.step(TENANT, ACCOUNT, model(decision())) == 1
    assert service.step(TENANT, ACCOUNT, model(decision())) == 0  # next tick: no retry of the held event
    with Session(engine) as s:
        held = s.get(MoxyEvent, (ACCOUNT, bad))
        assert (held.status, held.reason) == ('held', 'ownership evidence unavailable')
        assert stage == 'ready' or held.model_call_id is None  # held before any model call
        assert s.get(MoxyEvent, (ACCOUNT, good)).status == 'queued'
        cases = s.scalars(select(OfOperatorCase).where(OfOperatorCase.account_id == ACCOUNT,
                                                       OfOperatorCase.fan_id == CORRUPT)).all()
        assert [c.category for c in cases] == ['moxy_technical']  # surfaced once, to an operator
        assert s.scalar(select(func.count()).select_from(OfOperatorCase).where(
            OfOperatorCase.account_id == ACCOUNT, OfOperatorCase.fan_id == HEALTHY)) == 0
    store.close()
