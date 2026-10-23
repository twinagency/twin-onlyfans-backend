"""Twin's projection of an accepted send stamps who sent it, for FASTT (PostgreSQL).

A human-typed send gets the Twin-staff sentinel when an operator has created it;
an AI send keeps its automation_kind and is never stamped as human; with no
sentinel the column stays NULL exactly as before. Disposable DB, synthetic wire.
"""
import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, accepted, configured, prepare, unthrottle
from db.models import Employee, Message
from onlyfans_runtime.staff_sentinels import AUTOMATION_NAME, HUMAN_NAME
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def send_manual(authority, engine, key, message_id):
    unthrottle(engine)
    intent = prepare(authority, key)
    dispatched = authority.admit(TENANT, ACCOUNT, intent)
    authority.record_accepted(TENANT, ACCOUNT, intent, dispatched.token, accepted(dispatched, message_id=message_id))
    assert authority.materialize(TENANT, ACCOUNT, intent) is True


def test_human_projection_is_stamped_only_when_the_sentinel_exists(database):
    _, engine = database
    store, authority = configured(database)
    send_manual(authority, engine, "before", 9001)
    with Session(engine) as s, s.begin():
        s.add(Employee(display_name=AUTOMATION_NAME, is_active=False))
        s.add(Employee(display_name=HUMAN_NAME, is_active=False))
    # nothing is rewritten retroactively
    send_manual(authority, engine, "after", 9002)
    with Session(engine) as s:
        human = s.query(Employee.id).filter_by(display_name=HUMAN_NAME).scalar()
        before, after = s.get(Message, (ACCOUNT, FAN, 9001)), s.get(Message, (ACCOUNT, FAN, 9002))
        assert (before.automation_kind, before.sent_by_employee_id) == (None, None)
        assert (after.automation_kind, after.sent_by_employee_id) == (None, human)
    store.close()


def test_ai_projection_keeps_its_kind_and_is_never_stamped_as_human(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    with Session(engine) as s, s.begin():
        s.add(Employee(display_name=HUMAN_NAME, is_active=False))
    store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT); due(engine)
    claim = store.claim(TENANT)
    intent = prepare(authority, origin="automated", work=claim)
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == "accepted"
    with Session(engine) as s:
        message = s.get(Message, (ACCOUNT, FAN, 9001))
        assert message.automation_kind == "onlyfans_text" and message.sent_by_employee_id is None
    store.close()
