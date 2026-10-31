"""Account-policy admission for FASTT intake, on disposable PostgreSQL only.

New fans are auto-enabled ONLY by the explicit per-account "future conversations" setting (default off, set by an
administrator command). ``store.NEW_CONVERSATION_AUTOMATION_LOCKED`` remains as a deployment-level kill constant:
the lock tests below pin it on (``locked``) against a connection row that is still armed and must write nothing.
The admission tests run with it off (the default) so the guard conditions behind the setting stay covered and
prove the lock tests are not passing for an unrelated reason.

No provider, credentials or production account is used. The shared fixture creates
and drops a uniquely named scratch database from ONLYFANS_TEST_PG_URL.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_fastt_controls_pg import database, template  # noqa: F401 -- pytest fixtures
from test_onlyfans_foundation_pg import seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from db.models import Fan, Message, OfConversationState, OfCreatorBinding, SkipList
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_models import OfControlAudit, OfRuntime
from onlyfans_runtime import store as store_module
from onlyfans_runtime.store import Event, utcnow

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


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(store_module, "NEW_CONVERSATION_AUTOMATION_LOCKED", True)


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(store_module, "NEW_CONVERSATION_AUTOMATION_LOCKED", False)


def evidence(engine):
    with Session(engine) as session:
        state = session.get(OfConversationState, (ACCOUNT, NEW_FAN))
        projection = None if state is None else (
            state.mode, state.takeover_state, state.revision, state.requires_attention)
        audits = session.scalars(select(OfControlAudit).where(
            OfControlAudit.account_id == ACCOUNT, OfControlAudit.fan_id == NEW_FAN)).all()
        return projection, [(row.action, row.actor, row.consent_ref,
                             row.previous_revision, row.revision) for row in audits]


def test_lock_is_on_and_a_still_armed_connection_never_enables_a_new_fan(prepared, locked):
    store, engine, _ = prepared
    assert store_module.NEW_CONVERSATION_AUTOMATION_LOCKED is True
    with Session(engine) as session:
        armed = session.get(OfProviderConnection, ACCOUNT)
        assert armed.automate_new_conversations is True and armed.automation_starts_at is not None
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False      # FASTT path
    assert evidence(engine) == (None, [])
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Message).where(
            Message.account_id == ACCOUNT, Message.direction == "out")) == 0


def live_inbound(store, fan, mid):
    store.record(TENANT, ACCOUNT, Event(fan, mid, datetime.now(timezone.utc).isoformat(), "in", "Synthetic live hello"), source="ws")
    assert store.drain(TENANT, ACCOUNT) == 1


def test_worker_intake_never_enables_a_brand_new_fan_while_locked(prepared, locked):
    store, engine, _ = prepared
    brand_new = NEW_FAN + 1
    live_inbound(store, brand_new, 7101)                                         # worker path (_apply)
    with Session(engine) as session:
        state = session.get(OfConversationState, (ACCOUNT, brand_new))
        assert (state.mode, state.revision, state.requires_attention) == ("paused", 0, True)
        assert session.scalars(select(OfControlAudit).where(
            OfControlAudit.account_id == ACCOUNT, OfControlAudit.fan_id == brand_new)).all() == []


def test_worker_intake_control_enables_only_when_the_lock_is_lifted(prepared, unlocked):
    store, engine, _ = prepared
    brand_new = NEW_FAN + 2
    live_inbound(store, brand_new, 7201)
    with Session(engine) as session:
        assert session.get(OfConversationState, (ACCOUNT, brand_new)).mode == "automated"


def test_lifting_the_lock_is_the_only_thing_that_admits_via_fastt(prepared, unlocked):
    store, engine, _ = prepared
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is True


def test_policy_off_leaves_the_new_fan_unactivated(prepared, unlocked):
    store, engine, _ = prepared
    with Session(engine) as session, session.begin():
        session.get(OfProviderConnection, ACCOUNT).automate_new_conversations = False
    assert store.admit_new_conversation(TENANT, ACCOUNT, NEW_FAN) is False
    assert evidence(engine) == (None, [])


def test_unlocked_admission_enables_once_with_owner_policy_audit(prepared, unlocked):
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


def test_pre_arm_history_cannot_be_treated_as_a_new_conversation(prepared, unlocked):
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
def test_existing_operator_state_and_stops_are_preserved(prepared, mode, takeover, stop, unlocked):
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
def test_connection_fences_refuse_activation(prepared, failure, unlocked):
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
def test_runtime_and_fan_prerequisites_fail_closed(prepared, failure, unlocked):
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
