"""FASTT runtime controls over real PostgreSQL; synthetic data, no provider, no model.

Covers the compatibility seams for running FASTT's ai_chatter against Twin's
canonical rows: one control authority (of_conversation_state) projected to
FASTT's skip_list stop in the same transaction, human/AI attribution of
outbound rows, the last-look re-check before every bubble, and the dialect-aware
upserts FASTT's write path uses. The async FASTT functions run in a child
process started like the production worker role (see fastt_controls_async_probe).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, prepare
from db.models import Fan, Message, OfConversationState, SkipList
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_models import OfControlAudit
from onlyfans_read.contract import conversation_id_for
from onlyfans_runtime import fastt_controls as controls
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport

SERVICE = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("fastt_controls_async_probe.py")
ACTOR = "op-session:" + "a" * 64


def key(label):
    return hashlib.sha256(label.encode()).hexdigest()


def ctl(store, mode, revision, label, *, action=None, fan=FAN, consent_ref=None):
    return store.control(TENANT, ACCOUNT, fan, mode=mode, expected_revision=revision, command_key=key(label),
                         actor=ACTOR, reason="Dashboard operator: " + label, action=action, consent_ref=consent_ref)


def skip_reasons(engine, fan=FAN):
    with Session(engine) as s:
        return s.scalars(select(SkipList.reason).where(SkipList.account_id == ACCOUNT, SkipList.fan_id == fan)).all()


def may_act(engine, fan=FAN):
    with Session(engine) as s:
        return controls.stop_reason(s, ACCOUNT, fan)


def add_message(engine, message_id, direction, *, ago=timedelta(minutes=5), kind=None, body="synthetic", fan=FAN):
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=message_id, direction=direction, body=body,
                      created_at=utcnow() - ago, automation_kind=kind))


def probe(url, scenario, **args):
    env = {k: v for k, v in os.environ.items() if k.startswith(("PATH", "HOME", "LANG"))}
    env.update(ONLYFANS_PROCESS_ROLE="worker", DATABASE_URL=url, PYTHONPATH=str(SERVICE))
    done = subprocess.run([sys.executable, str(PROBE), scenario, json.dumps(dict(account=ACCOUNT, fan=FAN, **args))],
                          env=env, capture_output=True, text=True, timeout=90)
    lines = [l for l in done.stdout.splitlines() if l.startswith("PROBE_RESULT ")]
    assert done.returncode == 0 and lines, done.stderr[-3000:]
    return json.loads(lines[-1][len("PROBE_RESULT "):])


# ── control authority and projection ─────────────────────────────────────────

def test_reasons_match_the_fastt_constants():
    source = (SERVICE / "automations/_common.py").read_text()
    for name, value in (("MANUAL_RESTRICT_REASON", controls.MANUAL_RESTRICT), ("OF_RESTRICTED_REASON", controls.OF_RESTRICTED),
                        ("MUTED_CREATOR_REASON", controls.MUTED_CREATOR), ("LADDER_STOP_REASON", controls.LADDER_STOP)):
        assert re.search(rf'^{name} = "{value}"$', source, re.M), name


def test_pause_projects_restrict_atomically_and_failure_rolls_both_back(database, monkeypatch):
    url, engine = database
    store = seed(url, engine)
    assert may_act(engine) is None and controls.fastt_may_act(Session(engine), ACCOUNT, FAN)
    real = controls.project_stop
    monkeypatch.setattr(controls, "project_stop", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("projection failed")))
    with pytest.raises(RuntimeError):
        ctl(store, "paused", 0, "pause", action="pause_automation")
    with Session(engine) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert (state.mode, state.revision) == ("automated", 0), "state must not change without its projection"
        assert not s.scalars(select(OfControlAudit)).all()
    assert skip_reasons(engine) == []
    monkeypatch.setattr(controls, "project_stop", real)
    assert ctl(store, "paused", 0, "pause", action="pause_automation") == 1
    assert skip_reasons(engine) == ["manual_restrict"]
    assert may_act(engine) == "paused"
    store.close()


@pytest.mark.parametrize("action,mode", [("request_takeover", "human_controlled"), ("mark_no_contact", "no_contact")])
def test_takeover_and_no_contact_also_restrict(database, action, mode):
    url, engine = database
    store = seed(url, engine)
    ctl(store, mode, 0, action, action=action)
    assert skip_reasons(engine) == ["manual_restrict"]
    assert may_act(engine) in {"human_controlled", "no_contact"}
    store.close()


def test_resume_releases_only_the_manual_restrict_and_keeps_stronger_rows(database):
    url, engine = database
    store = seed(url, engine, fan=FAN)
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=102))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=102, mode="automated", revision=0, takeover_state="none"))
        s.add(SkipList(account_id=ACCOUNT, fan_id=102, reason="of_restricted", added_at=utcnow()))
    # plain: pause then resume removes the manual row
    ctl(store, "paused", 0, "p1", action="pause_automation")
    ctl(store, "automated", 1, "r1", action="resume_automation")
    assert skip_reasons(engine) == [] and may_act(engine) is None
    # stronger OnlyFans restriction: pause never demotes it, resume never deletes it
    ctl(store, "paused", 0, "p2", action="pause_automation", fan=102)
    assert skip_reasons(engine, 102) == ["of_restricted"]
    ctl(store, "automated", 1, "r2", action="resume_automation", fan=102)
    assert skip_reasons(engine, 102) == ["of_restricted"]
    assert may_act(engine, 102) == "of_restricted", "an OnlyFans restriction still stops FASTT after resume"
    # no-contact: resume without recorded consent is refused and the stop stays
    ctl(store, "no_contact", 2, "nc", action="mark_no_contact")
    with pytest.raises(Refused):
        ctl(store, "automated", 3, "nc-resume", action="resume_automation")
    assert skip_reasons(engine) == ["manual_restrict"] and may_act(engine) == "no_contact"
    store.close()


def test_explicit_stop_survives_the_one_hour_human_reply_cooldown(database):
    url, engine = database
    store = seed(url, engine)
    ctl(store, "paused", 0, "stop", action="pause_automation")
    add_message(engine, 1, "in", ago=timedelta(minutes=30))
    add_message(engine, 2, "out", ago=timedelta(minutes=10), body="a human answered")
    assert may_act(engine) == "paused"
    with engine.begin() as c:
        c.execute(text("UPDATE messages SET created_at = created_at - INTERVAL '3 hours'"))
    assert may_act(engine) == "paused", "cooldown elapsed but the operator stop must remain"
    assert skip_reasons(engine) == ["manual_restrict"]
    ctl(store, "automated", 1, "go", action="resume_automation")
    assert may_act(engine) is None
    store.close()


def test_drift_fails_closed_and_projection_repairs(database):
    url, engine = database
    store = seed(url, engine)
    with Session(engine) as s, s.begin():
        s.add(SkipList(account_id=ACCOUNT, fan_id=FAN, reason="manual_restrict", added_at=utcnow()))
    assert may_act(engine) == "projection_drift"
    with Session(engine) as s, s.begin():
        assert controls.project_account(s, ACCOUNT) == 1
    assert skip_reasons(engine) == [] and may_act(engine) is None
    ctl(store, "paused", 0, "p", action="pause_automation")
    with engine.begin() as c:
        c.execute(text("DELETE FROM skip_list"))      # a state paused before this code shipped
    with Session(engine) as s, s.begin():
        assert controls.project_account(s, ACCOUNT) == 1
    assert skip_reasons(engine) == ["manual_restrict"]
    store.close()


def settled_takeover(database, outcome):
    url, engine = database
    store, authority = configured(database, mode="human_controlled")
    identity = prepare(authority, "inflight-1")
    grant = authority.admit(TENANT, ACCOUNT, identity)
    assert ctl(store, "human_controlled", 0, "tk", action="request_takeover") == 1
    with Session(engine) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert (state.takeover_state, state.unresolved_attempt_id) == ("requested", grant.attempt_id)
    # complete_takeover under the same lock cannot finish it while the attempt is unresolved
    with Session(engine) as s, s.begin():
        Store.complete_takeover(s, ACCOUNT, FAN)
    with Session(engine) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).takeover_state == "requested"
    assert may_act(engine) == "human_controlled" and skip_reasons(engine) == ["manual_restrict"]
    if outcome == "unknown":
        assert authority.mark_unknown(TENANT, ACCOUNT, identity, grant.token) == "unknown"
        with Session(engine) as s:
            assert s.get(OfConversationState, (ACCOUNT, FAN)).takeover_state == "requested"
    return store, authority, identity, grant


def test_unresolved_unknown_send_keeps_takeover_requested_and_never_completes(database):
    url, engine = database
    store, *_ = settled_takeover(database, "unknown")
    with Session(engine) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert state.takeover_state == "requested" and state.unresolved_attempt_id
    with pytest.raises(Refused):
        ctl(store, "automated", 1, "release", action="release_takeover")
    assert skip_reasons(engine) == ["manual_restrict"]
    store.close()


def test_takeover_completes_once_the_previous_send_is_settled(database):
    from test_onlyfans_text_delivery_pg import accepted
    url, engine = database
    store, authority, identity, grant = settled_takeover(database, "settle")
    authority.record_accepted(TENANT, ACCOUNT, identity, grant.token, accepted(grant))
    authority.materialize(TENANT, ACCOUNT, identity)
    with Session(engine) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert (state.takeover_state, state.unresolved_attempt_id) == ("effective", None)
    assert skip_reasons(engine) == ["manual_restrict"], "completing a takeover is not a resume"
    ctl(store, "automated", 2, "release", action="release_takeover")
    assert skip_reasons(engine) == [] and may_act(engine) is None
    store.close()


# ── human / AI attribution ───────────────────────────────────────────────────

def test_attribution_staff_external_and_ai(database):
    from test_onlyfans_text_delivery_pg import accepted
    url, engine = database
    store, authority = configured(database, mode="human_controlled")
    identity = prepare(authority, "staff-1", body="staff words")
    assert authority.dispatch(TENANT, ACCOUNT, identity, SyntheticTextTransport(ACCOUNT, message_id=9101)) == "accepted"
    add_message(engine, 9102, "out", body="typed in the OnlyFans app")
    add_message(engine, 9103, "out", kind="ai_chatter", body="stock FASTT bubble")
    add_message(engine, 9104, "in", body="fan")
    with Session(engine) as s:
        staff = controls.human_attribution(s, ACCOUNT, FAN, 9101)
        assert (staff.kind, staff.actor) == ("staff", "synthetic-operator")
        external = controls.human_attribution(s, ACCOUNT, FAN, 9102)
        assert (external.kind, external.actor) == ("external_unattributed", None), "never assumed to be a named chatter"
        assert controls.human_attribution(s, ACCOUNT, FAN, 9103) is None
        assert controls.human_attribution(s, ACCOUNT, FAN, 9104) is None
        assert sorted(s.scalars(select(Message.message_id).where(controls.human_out_clause())).all()) == [9101, 9102]
    store.close()


def untagged_ai_message(database):
    """An AI-sent row whose automation_kind is NULL (poll ingestion beat the receipt projection)."""
    url, engine = database
    store, authority = configured(database, mode="automated")
    store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT); due(engine)
    claim = store.claim(TENANT)
    intent = prepare(authority, "ai-1", origin="automated", work=claim)
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT, message_id=9201)) == "accepted"
    with engine.begin() as c:
        c.execute(text("UPDATE messages SET automation_kind = NULL WHERE message_id = 9201"))
    return store


def test_ai_send_without_automation_kind_is_not_a_human_reply(database):
    url, engine = database
    store = untagged_ai_message(database)
    with Session(engine) as s:
        assert controls.human_attribution(s, ACCOUNT, FAN, 9201) is None
        assert s.scalars(select(Message.message_id).where(controls.human_out_clause())).all() == []
        assert [r[0] for r in s.execute(controls.ai_message_ids(ACCOUNT, [FAN]))] == [9201]
    store.close()


# ── dashboard commands through the single authority (HTTP) ───────────────────

def test_dashboard_commands_route_through_one_authority(database, monkeypatch):
    from onlyfans_commands.api import router
    from onlyfans_commands.settings import HEADER, SCHEMA
    url, engine = database
    store, authority = configured(database, mode="automated")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    for name, value in (("ONLYFANS_API_TOKEN", "synthetic-read-token"), ("ONLYFANS_CONTROL_TOKEN", "synthetic-control-token"),
                        ("ONLYFANS_CONTROL_TENANT", TENANT), ("ONLYFANS_MANUAL_ENABLED", "1")):
        monkeypatch.setenv(name, value)
    app = FastAPI(); app.include_router(router); app.state.control_store = store
    headers = {"Authorization": "Bearer synthetic-control-token", HEADER: SCHEMA}
    base = f"/onlyfans/control/v1/accounts/{ACCOUNT}/conversations/{conversation_id_for(ACCOUNT, FAN)}"

    def post(action, revision, label, **extra):
        body = dict(action=action, expectedRevision=revision, commandKey=key(label), actor=ACTOR, **extra)
        reply = TestClient(app).post(base, headers=headers, json=body)
        assert reply.status_code == 200, reply.text
        return reply.json()

    post("pause_automation", 0, "d-pause")
    assert skip_reasons(engine) == ["manual_restrict"] and may_act(engine) == "paused"
    post("resume_automation", 1, "d-resume")
    assert skip_reasons(engine) == [] and may_act(engine) is None
    post("request_takeover", 2, "d-takeover")
    assert skip_reasons(engine) == ["manual_restrict"]
    ack = post("manual_message", 3, "d-send", body="A human reply from staff.")
    assert authority.dispatch(TENANT, ACCOUNT, ack["intentId"], SyntheticTextTransport(ACCOUNT, message_id=9301)) == "accepted"
    with Session(engine) as s:
        sent = controls.human_attribution(s, ACCOUNT, FAN, 9301)
        assert (sent.kind, sent.actor) == ("staff", ACTOR), "authenticated staff identity from the command is the recorded actor"
        assert s.get(OfTextIntent, (ACCOUNT, ack["intentId"])).origin == "manual"
    post("release_takeover", 3, "d-release")
    assert skip_reasons(engine) == [] and may_act(engine) is None
    with Session(engine) as s:
        actors = set(s.scalars(select(OfControlAudit.actor)).all())
    assert actors == {ACTOR}
    store.close()


# ── upserts on PostgreSQL ────────────────────────────────────────────────────

def test_fastt_upserts_run_on_postgres(database):
    url, engine = database
    store = seed(url, engine)
    trace = probe(url, "upserts", message_id=9401)
    assert trace["restrict_status"]["reason"] == "manual_restrict" and trace["restrict_status"]["restricted"]
    assert trace["released_status"]["restricted"] is False
    assert trace["after_restrict_over_of_restricted"]["reason"] == "of_restricted", "manual restrict never demotes OnlyFans restriction"
    assert (trace["lease_first"], trace["lease_second"], trace["lease_after_release"]) == (True, False, True)
    with Session(engine) as s:
        assert s.scalar(text("SELECT count(*) FROM messages WHERE message_id = 9401")) == 1, "second attribution write is a no-op"
        assert s.scalar(text("SELECT deferrals FROM rhythm_state")) == 3
        assert s.scalar(text("SELECT rung_index FROM ladder_state")) == 2
        assert s.scalar(text("SELECT count(*) FROM pending_offer")) == 1
        assert s.scalar(text("SELECT automation_paused_until IS NOT NULL FROM fans WHERE fan_id = :f"), {"f": FAN})
        assert s.scalar(text("SELECT last_message_id FROM chats WHERE fan_id = :f"), {"f": FAN}) == 9401
    store.close()


# ── last look before every bubble ────────────────────────────────────────────

def look(url, **kw):
    return probe(url, "last_look", **kw)


def setup_thread(engine):
    add_message(engine, 1, "in", ago=timedelta(minutes=2), body="hi")
    with Session(engine) as s:
        return s.get(Message, (ACCOUNT, FAN, 1)).created_at.isoformat()


def test_last_look_is_clear_then_blocks_on_new_inbound_pause_and_human(database):
    url, engine = database
    store = seed(url, engine)
    inbound = setup_thread(engine)
    assert look(url, inbound_at=inbound) == ""
    # a bubble this turn already sent is not "somebody answered"
    add_message(engine, 2, "out", ago=timedelta(seconds=30), kind="ai_chatter", body="bubble one")
    assert look(url, inbound_at=inbound) == "already_answered"
    assert look(url, inbound_at=inbound, sent_ids=[2]) == ""
    # he writes again while we wait
    add_message(engine, 3, "in", ago=timedelta(seconds=10), body="hello??")
    assert look(url, inbound_at=inbound, sent_ids=[2]) == "fan_spoke_again"
    store.close()


def test_last_look_blocks_after_pause_even_with_nothing_else_changed(database):
    url, engine = database
    store = seed(url, engine)
    inbound = setup_thread(engine)
    assert look(url, inbound_at=inbound) == ""
    ctl(store, "paused", 0, "mid-pause", action="pause_automation")
    assert look(url, inbound_at=inbound) == "stopped_manual_restrict"
    with engine.begin() as c:
        c.execute(text("DELETE FROM skip_list"))
    assert look(url, inbound_at=inbound) == "stopped_paused", "the control authority alone stops it too"
    store.close()


def test_last_look_blocks_on_unattributed_human_reply_but_not_on_own_ai_send(database):
    url, engine = database
    store = untagged_ai_message(database)       # inbound id 1, then Twin's own AI reply 9201 (kind stripped)
    with Session(engine) as s:
        inbound = s.scalar(select(Message.created_at).where(Message.message_id == 1)).isoformat()
    assert look(url, inbound_at=inbound) == "", "Twin's own AI send is not a human reply"
    add_message(engine, 9500, "out", ago=timedelta(seconds=1), body="typed in the OnlyFans app")
    assert look(url, inbound_at=inbound) == "already_answered"
    store.close()


def test_human_reply_blocks_overlapping_ai_reply_in_candidate_selection_clock(database):
    url, engine = database
    store = seed(url, engine)
    add_message(engine, 1, "in", ago=timedelta(minutes=20), body="hi")
    add_message(engine, 2, "out", ago=timedelta(minutes=10), body="human typed this")
    got = probe(url, "gather")
    assert got["last_human_out_at"] is not None, "resume_after_manual_hours sees an unattributed human reply"
    assert got["last_dir"] == "out"
    assert [None, True] in got["answered_after_in"]
    store.close()


def test_twin_ai_send_without_kind_does_not_start_the_human_cooldown(database):
    url, engine = database
    store = untagged_ai_message(database)
    got = probe(url, "gather")
    assert got["last_human_out_at"] is None
    store.close()


def test_every_bubble_is_looked_at_after_its_wait_and_before_its_send():
    source = (SERVICE / "automations/ai_chatter.py").read_text()
    loop = source[source.index("            for idx, part in enumerate(parts):"):]
    loop = loop[:loop.index("            if look_stopped:")]
    assert loop.index("await hold_with_typing(") < loop.index("await _last_look(") < loop.index("client.send_message(fan_id, p, **kw)")
    assert "sent_ids.add(int(msg_id))" in loop and "look_stopped = True" in loop
