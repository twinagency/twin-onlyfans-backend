"""Quarantined-ingress root cause and audited resolution; disposable DB and synthetic data only."""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, prepare
from db.models import Fan, Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfIngress, OfRuntime, OfScan
from onlyfans_runtime import launch_operations as ops
from onlyfans_runtime.launch_operations import (classify_conflict, quarantine_status,
                                                  resolve_quarantined_ingress)
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Event, Refused, body_equivalent, utcnow

ACTOR = "op-session:" + "a" * 64
KEY = "b" * 64
REASON = "Operator reviewed: provider text differs only by rich-text markup"


def out_event(mid, body, **kw):
    return Event(FAN, mid, datetime.now(timezone.utc).isoformat(), "out", body, **kw)


def stored(engine, mid, body, *, direction="out", price=0, media=()):
    """A message as the legacy mirror or our own send leaves it: provider rich text."""
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=mid, direction=direction, body=body,
                      price_cents=price, media_ids=json.dumps(list(media)), media_count=len(media),
                      created_at=utcnow()))


def ingest(store, ev):
    assert store.record(TENANT, ACCOUNT, ev, source="catchup") == "accepted"
    store.drain(TENANT, ACCOUNT)


def rows(engine):
    with Session(engine) as s:
        return {r.message_id: (r.status, r.reason, r.event_key) for r in s.scalars(select(OfIngress))}


def status_of(engine, mid):
    return rows(engine)[mid][0]


def resolve(store, engine, **changes):
    args = dict(command_key=KEY, actor=ACTOR, reason=REASON,
                expected_fingerprint=quarantine_status(store, TENANT, ACCOUNT)["fingerprint"])
    args.update(changes)
    return resolve_quarantined_ingress(store, TENANT, ACCOUNT, **args)


def test_body_equivalence_is_only_the_native_markup_conversion():
    assert body_equivalent("<p>a<br />b &amp; c</p>", "a\nb & c")
    assert body_equivalent("<p>x</p><p>y</p>", "x\ny")
    assert body_equivalent('<p>see <a href="https://e.x/c">https://e.x/c</a>&hellip;</p>', "see https://e.x/c…")
    assert not body_equivalent("<p>a</p>", "b")
    assert not body_equivalent("<p>a </p>", "a")           # no whitespace leniency
    assert not body_equivalent("a", "<p>a</p>")            # only the stored side is rich text
    assert not body_equivalent("<p>a<script>x</script></p>", "ax")  # unsupported markup stays a conflict


def test_markup_difference_does_not_quarantine_but_real_changes_still_do(database):
    url, engine = database
    store = seed(url, engine)
    stored(engine, 10, "<p>Hello<br />there &amp; you</p>")
    stored(engine, 11, "<p>Original text</p>")
    stored(engine, 12, "<p>Priced</p>", price=500)
    stored(engine, 13, "<p>Media</p>", media=[1, 2])
    ingest(store, out_event(10, "Hello\nthere & you"))
    ingest(store, out_event(11, "Edited text"))
    ingest(store, out_event(12, "Priced", price_cents=0))
    ingest(store, out_event(13, "Media", media_ids=(2, 1)))
    assert status_of(engine, 10) == "applied"
    assert status_of(engine, 11) == status_of(engine, 12) == status_of(engine, 13) == "quarantined"
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).recovery_needed is True
        assert s.get(Message, (ACCOUNT, FAN, 10)).body == "<p>Hello<br />there &amp; you</p>", "history is never rewritten"
    store.close()


def seeded_conflicts(database):
    url, engine = database
    store = seed(url, engine)
    stored(engine, 20, "<p>Markup &amp; more</p>")
    stored(engine, 21, "<p>Old words</p>")
    stored(engine, 22, "<p>Paid</p>", price=300)
    stored(engine, 23, "<p>Needs media</p>")
    # Exact-match history made quarantined by an older build: represent with a hand-built row below.
    for mid, ev in ((21, out_event(21, "New words")), (22, out_event(22, "Paid", price_cents=0)),
                    (23, out_event(23, "Needs media", media_ids=(7, 8)))):
        ingest(store, ev)
    # Markup row quarantined by the old comparison: insert directly as the old build left it.
    ev = out_event(20, "Markup & more")
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        s.add(OfIngress(account_id=ACCOUNT, binding_id=runtime.binding_id, event_key="1" * 64, fan_id=FAN,
                        message_id=20, source="catchup", payload=ev.payload(), status="quarantined",
                        reason="conflicting_message_identity", received_at=utcnow(), applied_at=utcnow()))
        # Other-reason rows must stay untouched.
        for mid, key, reason in ((30, "2" * 64, "binding_changed"), (31, "3" * 64, "future_timestamp")):
            s.add(OfIngress(account_id=ACCOUNT, binding_id=runtime.binding_id, event_key=key, fan_id=FAN,
                            message_id=mid, source="rest", payload=out_event(mid, "x").payload(),
                            status="quarantined", reason=reason, received_at=utcnow(), applied_at=utcnow()))
    return store, engine


def test_classification_of_every_difference(database):
    store, engine = seeded_conflicts(database)
    status = quarantine_status(store, TENANT, ACCOUNT)
    assert status["quarantined"] == 6 and status["wouldResolve"] == 1
    assert status["wouldResolveClasses"] == {"markup_equivalent": 1}
    assert status["heldClasses"] == {"body_changed": 1, "price_changed": 1, "media_added": 1, "not_eligible": 2}
    class Msg:  # stored side only
        direction, body, price_cents = "out", "a", 0
        def __init__(self, media="[]"): self.media_ids = media
    ev = lambda **kw: Event(FAN, 1, "2026-10-28T00:00:00+00:00", **{"direction": "out", "body": "a", **kw})
    assert classify_conflict(Msg(), ev()) == "identical"
    assert classify_conflict(Msg(), ev(direction="in")) == "direction_changed"
    assert classify_conflict(Msg("[1,2]"), ev(media_ids=(2, 1))) == "media_reordered"
    assert classify_conflict(Msg("[1,2]"), ev(media_ids=(3,))) == "media_changed"
    assert classify_conflict(Msg("[1,2]"), ev()) == "media_removed"
    assert classify_conflict(Msg(), ev(body="b", price_cents=5)) == "mixed"
    assert classify_conflict(None, ev()) == "message_missing"
    store.close()


def test_resolve_applies_benign_rows_with_evidence_and_leaves_the_rest(database):
    store, engine = seeded_conflicts(database)
    fingerprint = quarantine_status(store, TENANT, ACCOUNT)["fingerprint"]
    with Session(engine) as s:
        before_messages = {m.message_id: (m.body, m.price_cents, m.media_ids, m.is_paid) for m in s.scalars(select(Message))}
    result = resolve(store, engine)
    assert result["resolved"] == 1 and result["classes"] == {"markup_equivalent": 1} and result["stillQuarantined"] == 5
    state = rows(engine)
    assert state[20][:2] == ("applied", "resolved:markup_equivalent")
    assert state[21][0] == state[22][0] == state[23][0] == "quarantined"
    assert state[30][:2] == ("quarantined", "binding_changed") and state[31][:2] == ("quarantined", "future_timestamp")
    with Session(engine) as s:
        after_messages = {m.message_id: (m.body, m.price_cents, m.media_ids, m.is_paid) for m in s.scalars(select(Message))}
        assert after_messages == before_messages, "stored history, money and media are unchanged"
        command = s.get(OfLaunchCommand, (ACCOUNT, KEY))
        assert command.kind == "quarantine_resolution" and command.actor == ACTOR and command.reason == REASON
        before = json.loads(command.before_json)
        assert before["fingerprint"] == fingerprint and before["quarantined"] == 6
        [evidence] = before["resolving"]
        assert evidence["eventKey"] == "1" * 64 and evidence["class"] == "markup_equivalent"
        assert evidence["storedHash"] != evidence["observedHash"] and len(evidence["storedHash"]) == 64
        assert json.loads(command.result_json) == result
    store.close()


def test_explicit_event_keys_release_a_non_benign_row_and_refuse_other_reasons(database):
    store, engine = seeded_conflicts(database)
    media_key = rows(engine)[23][2]
    with pytest.raises(Refused, match="not a conflicting message identity"):
        resolve(store, engine, event_keys=["2" * 64])
    with pytest.raises(Refused, match="unavailable"):
        resolve(store, engine, event_keys=["9" * 64])
    with pytest.raises(Refused, match="invalid"):
        resolve(store, engine, event_keys=[])
    assert resolve(store, engine, event_keys=[media_key])["classes"] == {"media_added": 1}
    state = rows(engine)
    assert state[23][:2] == ("applied", "resolved:media_added") and state[20][0] == "quarantined"
    store.close()


def test_resolve_is_idempotent_and_conflicting_replay_is_refused(database):
    store, engine = seeded_conflicts(database)
    fingerprint = quarantine_status(store, TENANT, ACCOUNT)["fingerprint"]
    first = resolve(store, engine, expected_fingerprint=fingerprint)
    assert resolve(store, engine, expected_fingerprint=fingerprint) == first
    with pytest.raises(Refused, match="conflict"):
        resolve(store, engine, expected_fingerprint=fingerprint, reason="A different stated reason")
    with pytest.raises(Refused, match="conflict"):
        resolve(store, engine, expected_fingerprint=fingerprint, event_keys=[rows(engine)[21][2]])
    with pytest.raises(Refused, match="changed"):
        resolve(store, engine, command_key="c" * 64, expected_fingerprint="d" * 64)
    with pytest.raises(Refused, match="no quarantined events"):
        resolve(store, engine, command_key="c" * 64)  # nothing benign is left
    with Session(engine) as s:
        assert len(s.scalars(select(OfLaunchCommand)).all()) == 1
    store.close()


def test_invalid_commands_are_refused(database):
    store, engine = seeded_conflicts(database)
    for bad in ({"actor": "someone"}, {"reason": " "}, {"command_key": "short"}, {"event_keys": "x" * 64}):
        with pytest.raises(Refused):
            resolve(store, engine, **bad)
    assert rows(engine)[20][0] == "quarantined"
    store.close()


def test_manual_send_gate_and_recovery_clear_after_resolution(database):
    url, engine = database
    store, authority = configured(database)
    stored(engine, 40, "<p>Hi &amp; bye</p>")
    # The old comparison left this behind; the new one no longer creates it.
    ev = out_event(40, "Hi & bye")
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        s.add(OfIngress(account_id=ACCOUNT, binding_id=runtime.binding_id, event_key="4" * 64, fan_id=FAN,
                        message_id=40, source="catchup", payload=ev.payload(), status="quarantined",
                        reason="conflicting_message_identity", received_at=utcnow(), applied_at=utcnow()))
        s.add(OfScan(account_id=ACCOUNT, status="complete", known_fans_complete=True))

    def gate():
        with Session(engine) as s, s.begin():
            return authority._eligibility(s, s.get(OfRuntime, ACCOUNT), s.get(OfDeliveryPolicy, ACCOUNT),
                                          s.get(OfConversationState, (ACCOUNT, FAN)), s.get(Fan, (ACCOUNT, FAN)),
                                          "manual", utcnow())

    assert gate() == "history_quarantined"
    with pytest.raises(Refused):
        prepare(authority)
    with Session(engine) as s, s.begin():
        s.get(OfRuntime, ACCOUNT).recovery_needed = True  # what _apply sets on a conflict
        s.get(OfRuntime, ACCOUNT).last_sync_at = None
    store.drain(TENANT, ACCOUNT)
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).recovery_needed is True, "a quarantined row keeps recovery open"
    resolve(store, engine)
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).recovery_needed is True, "resolving does not force-clear recovery"
    store.drain(TENANT, ACCOUNT)  # the ordinary cycle end clears it itself
    with Session(engine) as s:
        runtime = s.get(OfRuntime, ACCOUNT)
        assert runtime.recovery_needed is False and runtime.last_sync_at is not None
    assert gate() is None
    assert prepare(authority)
    store.close()


def test_operator_cli_dry_run_writes_nothing_and_run_needs_the_fingerprint(database, monkeypatch, capsys):
    store, engine = seeded_conflicts(database)
    monkeypatch.setenv("DATABASE_URL", database[0])
    base = ["resolve-quarantine", "--account", ACCOUNT, "--actor", ACTOR, "--reason", REASON]
    assert ops.main(base + ["--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dryRun"] is True and dry["quarantined"] == 6 and dry["wouldResolveClasses"] == {"markup_equivalent": 1}
    assert dry["heldClasses"]["media_added"] == 1 and len(dry["heldEventKeys"]["media_added"]) == 1
    assert rows(engine)[20][0] == "quarantined"
    with Session(engine) as s:
        assert s.scalars(select(OfLaunchCommand)).first() is None
    assert ops.main(base) == 2 and "--expected-fingerprint" in capsys.readouterr().out
    assert ops.main(base + ["--expected-fingerprint", "d" * 64]) == 1 and "changed" in capsys.readouterr().out
    assert ops.main(base + ["--expected-fingerprint", dry["fingerprint"]]) == 0
    assert json.loads(capsys.readouterr().out)["resolved"] == 1
    assert ops.main(base + ["--expected-fingerprint", dry["fingerprint"]]) == 0  # derived key: rerun is idempotent
    assert json.loads(capsys.readouterr().out)["resolved"] == 1
    assert rows(engine)[20][:2] == ("applied", "resolved:markup_equivalent")
    store.close()
