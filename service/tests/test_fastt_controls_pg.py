"""Twin's dashboard controls drive FASTT's one durable per-fan stop (PostgreSQL).

`skip_list(reason='manual_restrict')` is the enforced runtime stop; Twin's
`of_conversation_state` is its display projection, committed in the SAME
transaction. Disposable `s5_` databases only (ONLYFANS_TEST_PG_URL is the admin
URL); synthetic data, no provider. The ai_chatter side (pause during generation,
pacing and later bubbles; explicit stop beyond the 1h cooldown) is
tests/test_fastt_wire_stop.py.
"""
import hashlib
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from test_onlyfans_text_delivery_pg import (ACCOUNT, FAN, TENANT, accepted, configured, current,
                                            prepare, rejected)
from db.models import Fan, Message, OfConversationState, OfMessageDelivery, SkipList
from db.onlyfans_models import OfControlAudit
from db.onlyfans_delivery_models import OfTextIntent
from db.schema import bootstrap
from onlyfans_commands.api import router
from onlyfans_commands.settings import ACTIONS, HEADER, SCHEMA
from onlyfans_read.contract import conversation_id_for
from onlyfans_runtime import runtime_stop
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import SyntheticTextTransport

STAFF = "op-session:" + "a" * 64


@pytest.fixture(scope="module")
def template():
    admin = create_engine(os.environ["ONLYFANS_TEST_PG_URL"], isolation_level="AUTOCOMMIT")
    name = "s5_tpl_" + uuid.uuid4().hex[:16]
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\'')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, connect_args={"options": "-c timezone=UTC"})
    with engine.begin() as c:
        bootstrap(c)
    engine.dispose()
    yield admin, name
    with admin.connect() as c:
        c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.dispose()


@pytest.fixture
def database(template):
    admin, tpl = template
    name = "s5_" + uuid.uuid4().hex[:20]
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{tpl}"')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, connect_args={"options": "-c timezone=UTC"})
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def command(store, action, revision, key=None, *, actor=STAFF):
    return store.control(TENANT, ACCOUNT, FAN, mode=ACTIONS[action], action=action,
        expected_revision=revision, command_key=key or f"s5-{action}-{revision}",
        actor=actor, reason="Dashboard operator: " + action)


def stop_row(engine, account=ACCOUNT, fan=FAN):
    with Session(engine) as s:
        return s.scalar(select(SkipList.reason).where(
            SkipList.account_id == runtime_stop.to_fastt_account_id(account), SkipList.fan_id == fan))


def display(engine):
    with Session(engine) as s:
        st = s.get(OfConversationState, (ACCOUNT, FAN))
        return st.mode, st.takeover_state, st.revision


def set_row(engine, reason, account=ACCOUNT, fan=FAN):
    with Session(engine) as s, s.begin():
        s.merge(SkipList(account_id=runtime_stop.to_fastt_account_id(account), fan_id=fan, reason=reason))


def test_account_id_translation_is_one_function():
    f = runtime_stop.to_fastt_account_id
    assert f("of-176774528") == "176774528" and f("176774528") == "176774528" and f("acc-a") == "acc-a"
    assert f("of-abc") == "of-abc"


def test_pause_and_resume_move_the_runtime_stop_and_the_display_together(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    assert stop_row(engine) is None
    assert command(store, "pause_automation", 0) == 1
    assert stop_row(engine) == "manual_restrict" and display(engine) == ("paused", "none", 1)
    assert command(store, "resume_automation", 1) == 2
    assert stop_row(engine) is None and display(engine) == ("automated", "none", 2)
    # An idempotent replay acknowledges history and changes nothing.
    assert command(store, "pause_automation", 0) == 1
    assert stop_row(engine) is None and display(engine) == ("automated", "none", 2)
    store.close()


def test_a_refused_command_leaves_both_sides_untouched(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    with pytest.raises(Refused):
        command(store, "pause_automation", 5)          # stale revision
    assert stop_row(engine) is None and display(engine) == ("automated", "none", 0)
    command(store, "pause_automation", 0)
    with pytest.raises(Refused):
        command(store, "pause_automation", 1)          # not automated
    assert stop_row(engine) == "manual_restrict" and display(engine)[2] == 1
    store.close()


def test_the_real_account_id_shape_is_written_for_fastt(database):
    url, engine = database
    from test_onlyfans_foundation_pg import seed
    store = seed(url, engine, account="of-176774528", tenant=TENANT, fan=318022380)
    current(engine, mode="automated", account="of-176774528", fan=318022380)
    store.control(TENANT, "of-176774528", 318022380, mode="paused", action="pause_automation",
                  expected_revision=0, command_key="k" * 8, actor=STAFF, reason="Dashboard operator: pause")
    assert stop_row(engine, "of-176774528", 318022380) == "manual_restrict"
    with Session(engine) as s:
        assert s.scalar(select(SkipList.account_id)) == "176774528"
    store.close()


def test_takeover_stops_the_ai_first_and_waits_for_the_unresolved_send(database):
    _, engine = database
    store, authority = configured(database)          # human_controlled, takeover none
    identity = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, identity)   # a send is now in flight
    assert command(store, "request_takeover", 0) == 1
    # The stop is already in force while takeover is only REQUESTED...
    assert stop_row(engine) == "manual_restrict" and display(engine) == ("human_controlled", "requested", 1)
    # ...no manual-send state is exposed yet, and it cannot be handed back either.
    with pytest.raises(Refused, match="takeover_pending"):
        prepare(authority, key="human-early", revision=1)
    with pytest.raises(Refused):
        command(store, "release_takeover", 1)
    authority.record_not_accepted(TENANT, ACCOUNT, identity, grant.token, rejected(grant))
    assert display(engine) == ("human_controlled", "effective", 2)
    assert stop_row(engine) == "manual_restrict"
    assert prepare(authority, key="human-ok", revision=2)    # only now can a human send
    store.close()


def test_dashboard_round_trip_over_the_http_contract(database, monkeypatch):
    _, engine = database
    store, authority = configured(database, mode="automated")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_API_TOKEN", "synthetic-read-token")
    monkeypatch.setenv("ONLYFANS_CONTROL_TOKEN", "synthetic-control-token")
    monkeypatch.setenv("ONLYFANS_CONTROL_TENANT", TENANT)
    monkeypatch.setenv("ONLYFANS_MANUAL_ENABLED", "1")
    app = FastAPI(); app.include_router(router); app.state.control_store = store
    headers = {"Authorization": "Bearer synthetic-control-token", HEADER: SCHEMA}
    url = f"/onlyfans/control/v1/accounts/{ACCOUNT}/conversations/{conversation_id_for(ACCOUNT, FAN)}"

    def post(action, revision, **extra):
        key = hashlib.sha256(f"s5-{action}-{revision}".encode()).hexdigest()
        return TestClient(app).post(url, headers=headers, json=dict(
            action=action, expectedRevision=revision, commandKey=key, actor=STAFF, **extra))

    r = post("pause_automation", 0)
    assert r.status_code == 200 and r.json()["recordedRevision"] == 1
    assert stop_row(engine) == "manual_restrict"
    r = post("resume_automation", 1)
    assert r.status_code == 200 and stop_row(engine) is None
    r = post("request_takeover", 2)
    assert r.status_code == 200 and stop_row(engine) == "manual_restrict"
    assert display(engine) == ("human_controlled", "effective", 3)
    # Human send: the manual path, attributed to the signed-in staff identity.
    r = post("manual_message", 3, body="Hi, it is really me.")
    assert r.status_code == 200 and r.json()["outcome"] == "queued"
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, r.json()["intentId"], wire) == "accepted"
    with Session(engine) as s:
        intent = s.scalar(select(OfTextIntent))
        assert (intent.origin, intent.actor) == ("manual", STAFF)
        receipt = s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001))
        assert receipt.origin == "human" and receipt.outcome == "sent"
    # While a human holds the conversation the AI stop cannot be lifted by a resume.
    assert post("resume_automation", 3).status_code == 409 and stop_row(engine) == "manual_restrict"
    r = post("release_takeover", 3)
    assert r.status_code == 200 and stop_row(engine) is None
    assert display(engine) == ("automated", "none", 4)
    r = post("mark_no_contact", 4)
    assert r.status_code == 200 and stop_row(engine) == "manual_restrict"
    assert post("resume_automation", 5).status_code == 409
    store.close()


def test_no_contact_keeps_its_stop_until_recorded_consent(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    command(store, "mark_no_contact", 0)
    assert stop_row(engine) == "manual_restrict"
    for action in ("resume_automation", "release_takeover", "request_takeover"):
        with pytest.raises(Refused):
            command(store, action, 1)
    assert stop_row(engine) == "manual_restrict"
    store.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=1, command_key="consent-1",
                  actor=STAFF, reason="Renewed consent on file", consent_ref="consent-ref-1")
    assert stop_row(engine) is None and display(engine)[0] == "automated"
    store.close()


def test_resume_releases_only_the_manual_stop_and_never_a_stronger_one(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    set_row(engine, "of_restricted")
    command(store, "pause_automation", 0)
    assert stop_row(engine) == "of_restricted", "a pause never demotes OnlyFans' own restriction"
    with pytest.raises(Refused, match="stronger restriction"):
        command(store, "resume_automation", 1)
    assert stop_row(engine) == "of_restricted" and display(engine) == ("paused", "none", 1)
    with Session(engine) as s, s.begin():
        s.query(SkipList).delete()
    assert command(store, "resume_automation", 1) == 2 and stop_row(engine) is None
    store.close()


def test_resume_restores_an_auto_mute_the_pause_displaced(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    with Session(engine) as s, s.begin():
        s.get(Fan, (ACCOUNT, FAN)).is_muted = True
    set_row(engine, "muted_creator")
    command(store, "pause_automation", 0)
    assert stop_row(engine) == "manual_restrict"
    # Muted fans are refused by Twin's own gates; the runtime row must come back.
    command(store, "resume_automation", 1)
    assert stop_row(engine) == "muted_creator"
    store.close()


def test_reconcile_repairs_drift_fail_closed_and_never_releases(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    # A stop set directly in FASTT (its own restrict route) is SHOWN as paused.
    set_row(engine, "manual_restrict")
    out = store.reconcile_runtime_stop(TENANT, ACCOUNT, FAN)
    assert out["mode"] == "paused" and out["stopped"] and display(engine) == ("paused", "none", 1)
    with Session(engine) as s:
        assert s.scalar(select(OfControlAudit.actor).where(OfControlAudit.action == "runtime_stop")) == "runtime-stop"
    # Twin says paused but the row vanished: the stop is re-written, not the state flipped.
    with Session(engine) as s, s.begin():
        s.query(SkipList).delete()
    out = store.reconcile_runtime_stop(TENANT, ACCOUNT, FAN)
    assert out["stopped"] and stop_row(engine) == "manual_restrict" and display(engine)[0] == "paused"
    # Reconcile is idempotent and does not mint revisions when nothing drifted.
    before = display(engine)
    store.reconcile_runtime_stop(TENANT, ACCOUNT, FAN)
    assert display(engine) == before
    # Only the explicit command resumes.
    command(store, "resume_automation", before[2])
    assert stop_row(engine) is None
    assert store.reconcile_runtime_stop(TENANT, ACCOUNT, FAN)["stopped"] is False
    store.close()


def test_an_unknown_conversation_defaults_to_stopped(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    with Session(engine) as s, s.begin():
        s.delete(s.get(OfConversationState, (ACCOUNT, FAN)))
    out = store.reconcile_runtime_stop(TENANT, ACCOUNT, FAN)
    assert out["mode"] == "paused" and out["stopped"] and stop_row(engine) == "manual_restrict"
    store.close()


# --- human send on a FASTT-owned fan: the runner's boundary, not Twin's connector ---

def live_like(engine):
    """The live account today: never synced, recovery still needed."""
    from db.onlyfans_models import OfRuntime
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        runtime.recovery_needed, runtime.last_sync_at = True, None


def native_policy(authority):
    authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True, limit_units=20,
                        throttle_seconds=1, actor="synthetic-operator", reason="native", transport_kind="native")


def test_owned_fan_human_send_uses_the_runner_boundary_only(database, monkeypatch):
    from onlyfans_runtime.fastt_manual import (FasttManualTransport, SCOPE_ENV, NotSent,
                                               dispatch_next_manual, fastt_owned)
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    _, engine = database
    store, authority = configured(database)
    live_like(engine)
    # Not owned: the history gate still refuses, exactly as before.
    with pytest.raises(Refused, match="history_not_current"):
        prepare(authority, key="plain-1")
    monkeypatch.setenv(SCOPE_ENV, f"{ACCOUNT}:{FAN}")
    assert fastt_owned(ACCOUNT, FAN) and not fastt_owned(ACCOUNT, FAN + 1)
    command(store, "request_takeover", 0)                      # stop first, takeover effective
    native_policy(authority)
    intent = prepare(authority, key="human-1", revision=1, body="It is really me")
    # Twin's own worker connector may not send it (nor any synthetic wire).
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == "queued" and not wire.calls
    seen = []

    def post_once(grant):
        seen.append((grant.fan_id, grant.body, grant.origin))
        return {"id": 424242, "text": grant.body, "createdAt": utcnow_iso()}

    transport = FasttManualTransport(ACCOUNT, post_once, policy_revision=2)
    assert dispatch_next_manual(authority, TENANT, ACCOUNT, transport) == "accepted"
    assert seen == [(FAN, "It is really me", "manual")]
    with Session(engine) as s:
        receipt = s.get(OfMessageDelivery, (ACCOUNT, FAN, 424242))
        assert (receipt.outcome, receipt.origin) == ("sent", "human")
        assert s.scalar(select(OfTextIntent.actor)) == "synthetic-operator"
        assert s.get(Message, (ACCOUNT, FAN, 424242)).direction == "out"
    assert dispatch_next_manual(authority, TENANT, ACCOUNT, transport) is None   # nothing replayed
    assert len(seen) == 1
    # The runner transport sends nothing but a manual message for an owned fan.
    other = prepare(authority, key="human-2", revision=1, body="second")
    foreign = FasttManualTransport(ACCOUNT, post_once, policy_revision=2)
    monkeypatch.setenv(SCOPE_ENV, "")
    assert authority.dispatch(TENANT, ACCOUNT, other, foreign) == "queued" and len(seen) == 1
    store.close()


def utcnow_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_owned_fan_human_send_is_cancelled_by_a_later_control_and_unknown_is_held(database, monkeypatch):
    from onlyfans_runtime.fastt_manual import FasttManualTransport, SCOPE_ENV, NotSent, dispatch_next_manual
    _, engine = database
    store, authority = configured(database)
    live_like(engine)
    monkeypatch.setenv(SCOPE_ENV, f"{ACCOUNT}:{FAN}")
    command(store, "request_takeover", 0)
    native_policy(authority)
    calls = []
    intent = prepare(authority, key="late-1", revision=1)
    # Control moves on after the message was queued: the pre-wire recheck cancels it.
    command(store, "release_takeover", 1)
    t = FasttManualTransport(ACCOUNT, lambda g: calls.append(g) or {"id": 1}, policy_revision=2)
    assert dispatch_next_manual(authority, TENANT, ACCOUNT, t) == "cancelled" and not calls
    # A POST whose outcome is ambiguous is held, never replayed.
    command(store, "request_takeover", 2)
    prepare(authority, key="amb-1", revision=3, body="ambiguous")
    def boom(grant):
        raise TimeoutError("lost response")
    assert dispatch_next_manual(authority, TENANT, ACCOUNT,
                                FasttManualTransport(ACCOUNT, boom, policy_revision=2)) == "unknown"
    with Session(engine) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id is not None
    with pytest.raises(Refused):
        command(store, "release_takeover", 3)             # unresolved holds the hand-back
    assert stop_row(engine) == "manual_restrict"
    store.close()
