"""Synthetic SQLite-only admission regressions; no app/session/network imports."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.ledger import Ledger, Refused, SourceSnapshot


NOW = datetime(2026, 10, 16, 12)
OWNER = "synthetic-owner-000001"
ACCT, FAN = "176774528", 318022380
CHECKS = 0


def check(value: object, label: str = "") -> None:
    global CHECKS
    assert value, label
    CHECKS += 1


def refuses(reason: str, fn) -> None:
    try:
        fn()
    except Refused as exc:
        check(exc.reason == reason, (reason, exc.reason))
    else:
        raise AssertionError("expected refusal: " + reason)


SCHEMA = """
CREATE TABLE twin_bridge_conversations(account_id TEXT,fan_id INTEGER,state TEXT,reason TEXT,
 revision INTEGER,owner_token TEXT,owner_expires_at TEXT,reserved INTEGER,
 send_budget_limit INTEGER,send_budget_used INTEGER,updated_at TEXT,PRIMARY KEY(account_id,fan_id));
CREATE TABLE twin_bridge_send_attempts(id INTEGER PRIMARY KEY,account_id TEXT,fan_id INTEGER,turn_id TEXT,
 attempt_no INTEGER,state TEXT,reason TEXT,owner_token TEXT,revision INTEGER,provider_message_id TEXT,
 evidence_json TEXT,created_at TEXT,updated_at TEXT,UNIQUE(account_id,fan_id,turn_id,attempt_no));
CREATE TABLE twin_bridge_turns(account_id TEXT,fan_id INTEGER,state TEXT);
CREATE TABLE twin_bridge_writes(account_id TEXT,fan_id INTEGER,holder TEXT,state TEXT,expires_at TEXT);
CREATE TABLE messages(account_id TEXT,fan_id INTEGER,message_id INTEGER,direction TEXT,body TEXT,
 media_ids TEXT,media_count INTEGER,price_cents INTEGER,is_tip INTEGER,is_unsent INTEGER,
 created_at TEXT,ingested_at TEXT,raw_json TEXT,PRIMARY KEY(account_id,fan_id,message_id));
"""


class Fixture:
    def __init__(self, root: Path, name: str):
        self.path = root / (name + ".db")
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO twin_bridge_conversations VALUES(?,?,'active',NULL,4,NULL,NULL,1,3,3,?)", (ACCT, FAN, NOW.isoformat()))
            con.execute("INSERT INTO twin_bridge_send_attempts(account_id,fan_id,turn_id,attempt_no,state,owner_token,revision) "
                        "VALUES(?,?,'previous-twin-turn',1,'sent','old-owner',3)", (ACCT, FAN))
            con.execute("INSERT INTO twin_bridge_turns VALUES(?,?,'delivered')", (ACCT, FAN))
        self.ledger = Ledger(self.path)
        self.args = dict(pilot_id="synthetic-test", account_id=ACCT, fan_id=FAN,
                         owner_token=OWNER, cutoff=NOW, deadline=NOW + timedelta(minutes=30), limit=10, now=NOW)
        self.call = dict(pilot_id="synthetic-test", owner_token=OWNER, now=NOW + timedelta(seconds=10))
        self.pilot = None

    def sql(self, sql: str, args: tuple = ()):
        with closing(sqlite3.connect(self.path)) as con, con:
            return con.execute(sql, args).fetchall()

    def activate(self):
        self.pilot = self.ledger.activate(**self.args)
        return self.pilot

    def source(self, mid=100, **overrides):
        values = dict(account_id=ACCT, fan_id=FAN, message_id=mid, direction="in", body="neutral synthetic test",
                      media_ids="[]", media_count=0, price_cents=0, is_tip=0, is_unsent=0,
                      created_at=(NOW + timedelta(seconds=1)).isoformat(sep=" "),
                      ingested_at=(NOW + timedelta(seconds=2)).isoformat(sep=" "), raw_json='{"synthetic":true}')
        values.update(overrides)
        self.sql("INSERT INTO messages(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))

    def claim(self):
        snapshot = self.ledger.snapshot(**self.call)
        check(snapshot is not None)
        return self.ledger.claim_input(**self.call, snapshot=snapshot)

    def attempt(self, key, bubble=1):
        return self.ledger.begin_attempt(**self.call, input_key=key, bubble_no=bubble, revision=self.pilot.revision)

    def sent(self, attempt, mid):
        self.ledger.record_sent(**self.call, attempt_id=attempt.id, provider_message_id=str(mid),
                                validated_response={"id": mid, "text": "synthetic reply"}, evidence={"verified": True})


def run(root: Path):
    f = Fixture(root, "cap")
    p = f.activate()
    check(p.baseline_used == 3 and p.end_limit == 13 and p.revision == 5)
    check(f.sql("SELECT send_budget_used,send_budget_limit,owner_token,reserved FROM twin_bridge_conversations")[0] == (3, 13, OWNER, 1))
    check(f.ledger.activate(**f.args) == p)
    check(f.sql("SELECT count(*) FROM twin_bridge_send_attempts")[0][0] == 1)
    check(Ledger(f.path).activate(**f.args) == p, "restart never tops up")
    for field, value in (("limit", 9), ("fan_id", FAN + 1), ("owner_token", "another-owner-000001"),
                         ("cutoff", NOW - timedelta(seconds=1)), ("deadline", NOW + timedelta(minutes=31))):
        refuses("activation-conflict", lambda field=field, value=value: f.ledger.activate(**dict(f.args, **{field: value})))
    refuses("pilot-already-active", lambda: f.ledger.activate(**dict(f.args, pilot_id="another-pilot")))
    f.source()
    key = f.claim()
    for bubble in range(1, 11):
        a = f.attempt(key, bubble)
        check(a.attempt_no == 1 and a.owner_token == OWNER and a.turn_id.startswith("native-pilot-"))
        check(f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 3 + bubble)
        check(f.ledger.validate_dispatch(**f.call, attempt_id=a.id) is None,
              "admitted final-slot dispatch remains legal")
        f.sent(a, 1000 + bubble)
        # A locally attributed own bubble is allowed; unrelated outbound is not.
        f.source(1000 + bubble, direction="out", created_at=(NOW + timedelta(seconds=3)).isoformat(sep=" "))
    refuses("budget-exhausted", lambda: f.attempt(key, 1))
    check(len(f.ledger.receipts(pilot_id=p.pilot_id, input_key=key)) == 10)
    check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 10})
    check("owner_token" not in f.ledger.status(p.pilot_id))
    check("response_json" not in f.ledger.receipts(pilot_id=p.pilot_id)[0])
    f.ledger.stop(**f.call, reason="completed")
    check(f.sql("SELECT state,reserved,send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0] == ("paused", 1, 13, 13))
    refuses("pilot-stopped", lambda: f.attempt(key, 1))

    # Strict maximum and genuine positive integer parsing.
    for n, bad in enumerate((0, -1, 31, True, 1.5, "10")):
        g = Fixture(root, "invalid" + str(n))
        refuses("invalid-limit", lambda: g.ledger.activate(**dict(g.args, limit=bad)))

    # Admission and stop are serialized in the SAME database as old evidence.
    cases = [
        ("owner-live", "UPDATE twin_bridge_conversations SET owner_token='other',owner_expires_at='2026-10-16 12:10:00'"),
        ("owner-live", "UPDATE twin_bridge_conversations SET owner_token='other',owner_expires_at=NULL"),
        ("conversation-not-active", "UPDATE twin_bridge_conversations SET state='paused'"),
        ("prior-budget-not-exhausted", "UPDATE twin_bridge_conversations SET send_budget_limit=4"),
        ("delivery-unresolved", "UPDATE twin_bridge_send_attempts SET state='dispatching'"),
        ("delivery-unresolved", "UPDATE twin_bridge_send_attempts SET state='delivery_unknown'"),
        ("twin-work-open", "UPDATE twin_bridge_turns SET state='draft_ready'"),
        ("twin-work-open", "UPDATE twin_bridge_turns SET state='attention'"),
        ("writer-unresolved", "INSERT INTO twin_bridge_writes VALUES('176774528',318022380,'stale','held','2000-01-01')"),
        ("writer-unresolved", "INSERT INTO twin_bridge_writes VALUES('176774528',0,'broadcast','held','2000-01-01')"),
    ]
    for n, (reason, sql) in enumerate(cases):
        g = Fixture(root, "gate" + str(n))
        g.sql(sql)
        refuses(reason, g.activate)
        check(g.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 3)

    # Canonical input invalidation and newest-message race are rejected before any spend.
    invalids = ({"direction": "out"}, {"is_tip": 1}, {"is_unsent": 1}, {"price_cents": 1},
                {"media_count": 1}, {"media_ids": '[9]'}, {"media_ids": 'bad'}, {"body": '  '},
                {"created_at": NOW.isoformat(sep=" ")}, {"ingested_at": NOW.isoformat(sep=" ")},
                {"created_at": "bad"}, {"created_at": "2099-01-01"}, {"fan_id": FAN + 1}, {"account_id": "999"})
    for n, invalid in enumerate(invalids):
        g = Fixture(root, "source" + str(n))
        g.activate(); g.source(**invalid)
        check(g.ledger.snapshot(**g.call) is None)
        check(g.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 3)
    for n, mutation in enumerate(("UPDATE messages SET is_unsent=1", "UPDATE messages SET body='edited'", "UPDATE messages SET raw_json='{}'")):
        g = Fixture(root, "mutation" + str(n)); g.activate(); g.source(); k = g.claim()
        g.sql(mutation)
        refuses("source-changed", lambda: g.attempt(k))
        check(g.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 3)
    for direction in ("in", "out", "system"):
        g = Fixture(root, "newer" + direction); g.activate(); g.source(); k = g.claim()
        g.source(101, direction=direction, created_at="2026-10-16 12:00:04")
        refuses("source-changed", lambda: g.attempt(k))

    g = Fixture(root, "burst"); g.activate(); g.source(100); g.source(101, created_at="2026-10-16 12:00:04")
    snapshot = g.ledger.snapshot(**g.call)
    check(snapshot.member_ids == (100, 101))
    check(snapshot.earliest_created_at == "2026-10-16 12:00:01.000000")
    k = g.ledger.claim_input(**g.call, snapshot=snapshot)
    g.sql("UPDATE messages SET is_unsent=1 WHERE message_id=100")
    refuses("source-changed", lambda: g.attempt(k))
    check(g.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 3)
    g = Fixture(root, "burst-cap"); g.activate()
    for mid in range(1, 102):
        g.source(mid)
    check(g.ledger.snapshot(**g.call) is None)
    g = Fixture(root, "burst-boundary"); g.activate(); g.source(1)
    g.source(2, direction="out", created_at="2026-10-16 12:00:03")
    g.source(3, created_at="2026-10-16 12:00:04")
    check(g.ledger.snapshot(**g.call).member_ids == (3,))

    g = Fixture(root, "claim"); g.activate(); g.source(); snapshot = g.ledger.snapshot(**g.call); k = g.claim()
    refuses("input-already-claimed", lambda: g.ledger.claim_input(**g.call, snapshot=snapshot))
    g.ledger.finish_input(**g.call, input_key=k)
    refuses("input-already-claimed", lambda: g.ledger.claim_input(**g.call, snapshot=snapshot))
    refuses("input-not-generating", lambda: g.attempt(k))
    refuses("pilot-owner-mismatch", lambda: g.ledger.snapshot(**dict(g.call, owner_token="wrong-owner-000001")))

    for n, mutation in enumerate(("UPDATE twin_bridge_conversations SET owner_token='replacement'",
                                  "UPDATE twin_bridge_conversations SET revision=revision+1",
                                  "UPDATE twin_bridge_conversations SET owner_expires_at='2000-01-01'",
                                  "UPDATE twin_bridge_conversations SET reserved=0")):
        g = Fixture(root, "fence" + str(n)); g.activate(); g.source(); k = g.claim(); g.sql(mutation)
        refuses("authority-lost", lambda: g.attempt(k))
    g = Fixture(root, "deadline"); g.activate(); g.source(); k = g.claim()
    refuses("pilot-deadline", lambda: g.ledger.begin_attempt(**dict(g.call, now=NOW + timedelta(minutes=30)), input_key=k, bubble_no=1, revision=g.pilot.revision))
    g.ledger.stop(**g.call, reason="operator-stop")
    check(g.sql("SELECT send_budget_limit,send_budget_used,reserved FROM twin_bridge_conversations")[0] == (3, 3, 1))
    refuses("pilot-stopped", lambda: g.attempt(k))

    # Unknown delivery always keeps a spent unit and blocks all further activity.
    g = Fixture(root, "unknown"); g.activate(); g.source(); k = g.claim(); a = g.attempt(k)
    refuses("delivery-unresolved", lambda: g.attempt(k, 2))
    g.ledger.record_unknown(**g.call, attempt_id=a.id)
    check(g.sql("SELECT send_budget_limit,send_budget_used,state,reserved FROM twin_bridge_conversations")[0] == (4, 4, "attention", 1))
    check(g.ledger.status(g.pilot.pilot_id)["attempts"] == {"delivery_unknown": 1})
    refuses("pilot-stopped", lambda: g.attempt(k, 2))
    refuses("attempt-not-dispatching", lambda: g.sent(a, 999))

    # Proven response must survive native history/bookkeeping failure or deadline.
    g = Fixture(root, "receipt"); g.activate(); g.source(); k = g.claim(); a = g.attempt(k)
    g.sent(a, 200); g.sent(a, 200)
    check(g.sql("SELECT response_json FROM native_pilot_receipts")[0][0] == '{"id":200,"text":"synthetic reply"}')
    refuses("receipt-conflict", lambda: g.sent(a, 201))
    refuses("already-proven-sent", lambda: g.ledger.record_unknown(**g.call, attempt_id=a.id))
    refuses("attempt-already-exists", lambda: g.attempt(k))
    a2 = g.attempt(k, 2)
    refuses("provider-id-reused", lambda: g.sent(a2, 200))
    g.ledger.stop(**g.call, reason="operator-stop")
    g.ledger.record_sent(**dict(g.call, now=NOW + timedelta(hours=1)), attempt_id=a2.id, provider_message_id="201", validated_response={"id": 201}, evidence={})
    check(g.ledger.status(g.pilot.pilot_id)["attempts"] == {"sent": 2})

    # Model attempts (including failed requests) have a durable independent ceiling.
    g = Fixture(root, "models"); g.activate(); g.source(); g.claim()
    for i in range(80):
        check(g.ledger.reserve_model_call(**g.call) == i + 1)
    refuses("model-budget-exhausted", lambda: Ledger(g.path).reserve_model_call(**g.call))

    # Two independent connections race the single remaining slot: exactly one
    # durable intent appears, and an unresolved attempt prevents a second POST.
    g = Fixture(root, "race"); g.args["limit"] = 1; g.activate(); g.source(); k = g.claim()
    def race(bubble):
        try:
            return Ledger(g.path).begin_attempt(**g.call, input_key=k, bubble_no=bubble, revision=g.pilot.revision)
        except Refused as exc:
            return exc.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, (1, 2)))
    check(sum(not isinstance(r, str) for r in results) == 1)
    check(g.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 4)
    check(g.sql("SELECT count(*) FROM native_pilot_receipts")[0][0] == 1)

    # Separate handles race activation; grant persists exactly once.
    g = Fixture(root, "activation-race")
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: Ledger(g.path).activate(**g.args), (1, 2)))
    check(rows[0] == rows[1])
    check(g.sql("SELECT send_budget_limit FROM twin_bridge_conversations")[0][0] == 13)

    # Admission and physical transport are separate boundaries. Revalidation
    # after the thread handoff must still see stop/source/authority changes,
    # without charging another unit or changing any durable intent.
    wire_cases = (
        ("source-changed", "UPDATE messages SET is_unsent=1"),
        ("source-changed", "UPDATE messages SET body='edited after admission'"),
        ("authority-lost", "UPDATE twin_bridge_conversations SET owner_token='new-owner'"),
        ("authority-lost", "UPDATE twin_bridge_conversations SET revision=revision+1"),
        ("authority-lost", "UPDATE twin_bridge_conversations SET owner_expires_at='2000-01-01'"),
        ("authority-lost", "UPDATE twin_bridge_conversations SET state='paused'"),
        ("authority-lost", "UPDATE twin_bridge_conversations SET reserved=0"),
        ("writer-unresolved", "INSERT INTO twin_bridge_writes VALUES('176774528',318022380,'other','held','2099-01-01')"),
        ("delivery-unresolved", "UPDATE twin_bridge_send_attempts SET state='delivery_unknown' WHERE id=1"),
        ("twin-work-open", "UPDATE twin_bridge_turns SET state='draft_ready'"),
        ("input-not-generating", "UPDATE native_pilot_inputs SET state='complete'"),
        ("attempt-not-dispatching", "UPDATE native_pilot_receipts SET state='sent'"),
    )
    for n, (reason, mutation) in enumerate(wire_cases):
        g = Fixture(root, "wire" + str(n)); g.activate(); g.source(); k = g.claim(); a = g.attempt(k)
        g.sql(mutation)
        refuses(reason, lambda: g.ledger.validate_dispatch(**g.call, attempt_id=a.id))
        check(g.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 4)
        check(g.sql("SELECT count(*) FROM native_pilot_receipts")[0][0] == 1)
    g = Fixture(root, "wire-stop"); g.activate(); g.source(); k = g.claim(); a = g.attempt(k)
    g.ledger.stop(**g.call, reason="operator-stop")
    refuses("pilot-stopped", lambda: g.ledger.validate_dispatch(**g.call, attempt_id=a.id))
    g = Fixture(root, "wire-deadline"); g.activate(); g.source(); k = g.claim(); a = g.attempt(k)
    refuses("pilot-deadline", lambda: g.ledger.validate_dispatch(**dict(g.call, now=NOW + timedelta(minutes=30)), attempt_id=a.id))
    g = Fixture(root, "wire-burst"); g.activate(); g.source(100); g.source(101, created_at="2026-10-16 12:00:04")
    k = g.claim(); a = g.attempt(k); g.sql("UPDATE messages SET is_unsent=1 WHERE message_id=100")
    refuses("source-changed", lambda: g.ledger.validate_dispatch(**g.call, attempt_id=a.id))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="native-pilot-ledger-synthetic-") as directory:
        run(Path(directory))
    print(f"native pilot ledger: {CHECKS} checks; all cases passed")
