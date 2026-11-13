"""G-DS: a provider safety stop (fans-webhook `_disable_sends`) is never lost.

A concurrent policy writer cannot make the stop give up silently, and a stop that cannot apply raises a distinct
error and holds the projection for an operator (sibling of test_g02_01_provider_disable_never_refused_pg)."""
import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_fans_intake_models import OfFansLink, OfFansProjection
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority
from test_engine_real_worker_pg import PROVIDER
from test_onlyfans_text_delivery_pg import ACCOUNT
from test_plan_c_draft_app_pg import signed
from test_plan_c_supervised_pg import supervised, configured_draft, database, world  # noqa: F401


def _auth_failed(pilot, key):
    raw, headers = signed({'event': 'accounts.authentication_failed', 'account_id': PROVIDER, 'payload': {}}, key=key)
    return lambda: pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'])


def test_concurrent_policy_writer_cannot_make_the_stop_give_up(supervised, monkeypatch):
    world, _, _, _, pilot = supervised
    engine, original, bumps = world.database[1], TextAuthority._configure_in_session, []

    def racing(self, s, runtime, **kw):
        # Another writer edits the enabled policy between the stop's read and its write, every time it can.
        with engine.connect() as c:
            try:
                c.execute(text("SET LOCAL lock_timeout = '300ms'"))
                c.execute(update(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == ACCOUNT)
                          .values(revision=OfDeliveryPolicy.revision + 1))
                c.commit()
                bumps.append(1)
            except OperationalError:
                c.rollback()   # the stop holds the policy row: the writer waits, it cannot move the revision
        return original(self, s, runtime, **kw)

    monkeypatch.setattr(TextAuthority, '_configure_in_session', racing)
    try:
        _auth_failed(pilot, 'gds-race')()
    except Refused as exc:   # the pilot re-validates after intake; an applied stop makes that refuse
        assert 'enabled fansapi account policy' in str(exc)
    with Session(engine) as s:
        assert s.query(OfFansLink).one().send_blocked is True
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is False, f'stop lost after {len(bumps)} concurrent edits'


def test_stop_that_cannot_apply_raises_and_holds_the_projection(supervised, monkeypatch):
    world, _, _, _, pilot = supervised

    def broken(self, s, runtime, **kw):
        raise Refused('policy owner unavailable')

    monkeypatch.setattr(TextAuthority, '_configure_in_session', broken)
    with pytest.raises(Refused) as failed:
        _auth_failed(pilot, 'gds-broken')()
    assert type(failed.value).__name__ == 'SendStopFailed'   # distinct from an ordinary refusal
    with Session(world.database[1]) as s:
        assert s.query(OfFansLink).one().send_blocked is True
        assert s.get(OfDeliveryPolicy, ACCOUNT).enabled is True   # honest: the stop did NOT apply ...
        row = s.query(OfFansProjection).filter_by(account_id=ACCOUNT, event_key='gds-broken').one()
        assert (row.status, row.detail) == ('held', 'send_stop_failed')   # ... and an operator can see that
