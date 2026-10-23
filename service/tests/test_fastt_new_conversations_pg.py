"""Account-policy admission for FASTT intake, on disposable PostgreSQL only.

No provider, credentials or production account is used. The shared fixture creates
and drops a uniquely named scratch database from ONLYFANS_TEST_PG_URL.
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_fastt_controls_pg import database, template  # noqa: F401 -- pytest fixtures
from test_onlyfans_foundation_pg import seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from db.models import Fan, Message, OfConversationState, OfCreatorBinding, SkipList
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_models import OfControlAudit, OfRuntime
from onlyfans_runtime.store import utcnow

NEW_FAN = FAN + 100


@pytest.fixture
def prepared(database):
    url, engine = database
    store = seed(url, engine)
    now = utcnow()
    with Session(engine) as session, session.begin():
        session.add(Fan(account_id=ACCOUNT, fan_id=NEW_FAN))
        session.flush()
        session.add(Message(account_id=ACCOUNT, fan_id=NEW_FAN, message_id=7001,
                            direction="in", body="Synthetic new fan input",
                            created_at=now - timedelta(minutes=4)))
        session.add(OfProviderConnection(
            account_id=ACCOUNT, binding_id="binding-" + ACCOUNT, owner_id=176774528,
            revision=3, sealed_config="synthetic-config-never-opened", status="verified",
            verified_at=now - timedelta(minutes=6), expires_at=now + timedelta(hours=1),
            updated_at=now, automate_new_conversations=True,
            automation_starts_at=now - timedelta(minutes=5)))
    try:
        yield store, engine, now
    finally:
        store.close()


def evidence(engine):
    with Session(engine) as session:
        state = session.get(OfConversationState, (ACCOUNT, NEW_FAN))
        projection = None if state is None else (
            state.mode, state.takeover_state, state.revision, state.requires_attention)
        audits = session.scalars(select(OfControlAudit).where(
            OfControlAudit.account_id == ACCOUNT, OfControlAudit.fan_id == NEW_FAN)).all()
        return projection, [(row.action, row.actor, row.consent_ref,
                             row.previous_revision, row.revision) for row in audits]


def test_policy_off_leaves_the_new_fan_unactivated(prepared):
    store, engine, _ = prepared
    with Session(engine) as session, session.begin():
        session.get(OfProviderConnection, ACCOUNT).automate_new_conversations = False
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert evidence(engine) == (None, [])


def test_an_armed_fresh_conversation_is_enabled_once_with_owner_policy_audit(prepared):
    store, engine, _ = prepared
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is True
    assert evidence(engine) == (("automated", "none", 1, False), [
        ("initial_automation", "account-policy:3", "connection:3", 0, 1)])
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert len(evidence(engine)[1]) == 1
    # Admission itself never sends a message or activates another conversation.
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Message).where(
            Message.account_id == ACCOUNT, Message.direction == "out")) == 0
        assert session.get(OfConversationState, (ACCOUNT, FAN)).revision == 0


def test_pre_arm_history_cannot_be_treated_as_a_new_conversation(prepared):
    store, engine, now = prepared
    with Session(engine) as session, session.begin():
        session.add(Message(account_id=ACCOUNT, fan_id=NEW_FAN, message_id=7000,
                            direction="out", body="Synthetic older history",
                            created_at=now - timedelta(minutes=6)))
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert evidence(engine) == (None, [])


@pytest.mark.parametrize("mode,takeover,stop", [
    ("paused", "none", None),
    ("human_controlled", "effective", None),
    ("no_contact", "none", "manual_restrict"),
    ("paused", "none", "of_restricted"),
])
def test_existing_operator_state_and_stops_are_preserved(prepared, mode, takeover, stop):
    store, engine, _ = prepared
    with Session(engine) as session, session.begin():
        session.add(OfConversationState(account_id=ACCOUNT, fan_id=NEW_FAN,
                    mode=mode, takeover_state=takeover, revision=7, requires_attention=True))
        if stop:
            session.add(SkipList(account_id=ACCOUNT, fan_id=NEW_FAN, reason=stop))
    before = evidence(engine)
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert evidence(engine) == before
    with Session(engine) as session:
        row = session.get(SkipList, (ACCOUNT, NEW_FAN))
        assert (row.reason if row else None) == stop


@pytest.mark.parametrize("failure", ["expired", "binding", "unverified", "not_armed"])
def test_connection_fences_refuse_activation(prepared, failure):
    store, engine, now = prepared
    with Session(engine) as session, session.begin():
        connection = session.get(OfProviderConnection, ACCOUNT)
        if failure == "expired":
            connection.expires_at = now - timedelta(seconds=1)
        elif failure == "binding":
            session.add(OfCreatorBinding(id="old-binding", tenant_id=TENANT,
                        creator_id="old-creator", creator_name="Synthetic previous binding",
                        account_id=ACCOUNT, handle="synthetic-old", channel="onlyfans",
                        revoked_at=now - timedelta(hours=1)))
            session.flush()
            connection.binding_id = "old-binding"
        elif failure == "unverified":
            connection.verified_at = None
        else:
            connection.automation_starts_at = None
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert evidence(engine) == (None, [])


@pytest.mark.parametrize("failure", ["runtime_off", "muted", "no_history"])
def test_runtime_and_fan_prerequisites_fail_closed(prepared, failure):
    store, engine, _ = prepared
    with Session(engine) as session, session.begin():
        if failure == "runtime_off":
            session.get(OfRuntime, ACCOUNT).enabled = False
        elif failure == "muted":
            session.get(Fan, (ACCOUNT, NEW_FAN)).is_muted = True
        else:
            session.query(Message).filter_by(account_id=ACCOUNT, fan_id=NEW_FAN).delete()
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert evidence(engine) == (None, [])
