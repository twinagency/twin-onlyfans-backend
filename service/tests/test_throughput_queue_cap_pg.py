"""B4 (Twin text path): at the delivery queue's 1000 cap the behaviour is BACKPRESSURE, never a drop or an overflow.

``of_delivery_policy.queued_count`` is CHECK-capped at 1000. A request at the cap is refused with the fixed reason
"delivery queue full" (native_planning keeps the ready plan for the next pass), writes no intent and leaves the count
alone; one place below the cap it is accepted and fills the queue exactly; once places free up it is accepted again.
Runs on a throwaway PostgreSQL (ONLYFANS_TEST_PG_URL) through the existing fixtures."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 -- pytest fixture
from test_onlyfans_text_delivery_pg import configured, prepare, ACCOUNT
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.store import Refused


def count(engine):
    with Session(engine) as s:
        return s.get(OfDeliveryPolicy, ACCOUNT).queued_count, s.query(OfTextIntent).filter_by(account_id=ACCOUNT).count()


def test_queue_cap_is_backpressure_not_loss(database):
    url, engine = database
    store, authority = configured(database)
    with engine.begin() as c:
        c.execute(text("UPDATE of_delivery_policy SET queued_count=1000"))
    assert count(engine) == (1000, 0)
    with pytest.raises(Refused, match="delivery queue full"):
        prepare(authority, "full-1")
    assert count(engine) == (1000, 0), "refused: no intent written, count unchanged"
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("UPDATE of_delivery_policy SET queued_count=1001"))      # the database cap itself holds
    with engine.begin() as c:
        c.execute(text("UPDATE of_delivery_policy SET queued_count=999"))
    prepare(authority, "last-place")
    assert count(engine) == (1000, 1), "the last place is taken exactly"
    with pytest.raises(Refused, match="delivery queue full"):
        prepare(authority, "over-1")
    assert count(engine) == (1000, 1)
    with engine.begin() as c:
        c.execute(text("UPDATE of_delivery_policy SET queued_count=998"))           # places free up
    prepare(authority, "after-release")
    assert count(engine) == (999, 2), "accepted again once a place is free: the earlier refusal lost nothing that was ever accepted"
