"""Offline, stdlib-only regressions for repeat allowance and cleanup boundaries."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twin_bridge.repeat import Refused, arm, discard_old_inputs, stop


class RepeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "synthetic.sqlite"
        self.now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.args = dict(run_id="synthetic-repeat", account_id="123", fan_id=456,
                         expected_used=13, expected_revision=6, extra_replies=15,
                         scope_fingerprint="a" * 32, owner_token="synthetic-owner-token",
                         now=self.now, deadline=self.now + timedelta(minutes=30))
        with closing(sqlite3.connect(self.path)) as con:
            con.executescript("""
                CREATE TABLE twin_bridge_conversations(account_id TEXT,fan_id INTEGER,state TEXT,
                    reason TEXT,reserved INTEGER,revision INTEGER,owner_token TEXT,owner_expires_at TEXT,
                    send_budget_limit INTEGER,send_budget_used INTEGER,scope_fingerprint TEXT,
                    reset_at TEXT,takeover_at TEXT,cancelled_at TEXT,updated_at TEXT,
                    PRIMARY KEY(account_id,fan_id));
                CREATE TABLE twin_bridge_turns(account_id TEXT,fan_id INTEGER,state TEXT);
                CREATE TABLE twin_bridge_send_attempts(account_id TEXT,fan_id INTEGER,state TEXT,provider_message_id TEXT);
                CREATE TABLE twin_bridge_inputs(account_id TEXT,fan_id INTEGER,provider_message_id INTEGER,
                    occurred_at TEXT,received_at TEXT,state TEXT);
                CREATE TABLE native_pilots(account_id TEXT,fan_id INTEGER,state TEXT);
                CREATE TABLE twin_bridge_writes(account_id TEXT,fan_id INTEGER,holder TEXT,state TEXT,expires_at TEXT);
                CREATE TABLE automation_rules(account_id TEXT,is_enabled INTEGER);
                CREATE TABLE scheduled_jobs(account_id TEXT,status TEXT);
            """)
            con.execute("INSERT INTO twin_bridge_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("123",456,"paused","native-pilot-send-limit-reached",1,6,None,None,13,13,"a"*32,
                         "2026-01-01 00:00:00.000000",None,None,"2026-01-01 00:00:00.000000"))
            con.executemany("INSERT INTO twin_bridge_send_attempts VALUES('123',456,'sent',?)", [(str(i),) for i in range(13)])
            con.execute("INSERT INTO twin_bridge_turns VALUES('123',456,'delivered')")
            con.execute("INSERT INTO native_pilots VALUES('123',456,'stopped')")
            con.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def sql(self, query, values=()):
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(row) for row in con.execute(query, values).fetchall()]
            con.commit()
            return rows

    def conversation(self):
        return self.sql("SELECT * FROM twin_bridge_conversations")[0]

    def activate(self, **overrides):
        return arm(self.path, **{**self.args, **overrides})

    def finish(self, **overrides):
        return stop(self.path, **dict(run_id=self.args["run_id"], owner_token=self.args["owner_token"],
                                     now=self.now + timedelta(minutes=1), reason="finished", **overrides))

    def refusal_preserves_state(self, **overrides):
        before = self.conversation()
        attempts = self.sql("SELECT * FROM twin_bridge_send_attempts")
        with self.assertRaises(Refused):
            self.activate(**overrides)
        self.assertEqual(before, self.conversation())
        self.assertEqual(attempts, self.sql("SELECT * FROM twin_bridge_send_attempts"))

    def test_exact_additional_grant_fresh_boundary_and_secret_omission(self):
        grant = self.activate()
        c = self.conversation()
        self.assertEqual((13,28,7,"active",1), (c["send_budget_used"],c["send_budget_limit"],c["revision"],c["state"],c["reserved"]))
        self.assertEqual(grant["cutoff"], c["reset_at"])
        self.assertEqual("2026-01-02 03:06:05.000000", c["owner_expires_at"])
        self.assertNotIn("owner_token", grant)
        self.assertEqual(13, len(self.sql("SELECT * FROM twin_bridge_send_attempts")))
        self.assertEqual("a"*32, c["scope_fingerprint"])

    def test_thirty_reply_grant_preserves_used_and_revokes_unused(self):
        grant = self.activate(extra_replies=30)
        self.assertEqual(43, grant["end_limit"])
        self.assertEqual(13, self.conversation()["send_budget_used"])
        self.finish()
        self.assertEqual(13, self.conversation()["send_budget_limit"])

    def test_repeat_id_never_regrants_even_after_stop(self):
        self.activate()
        self.refusal_preserves_state()
        self.finish()
        self.refusal_preserves_state(expected_revision=8)
        self.assertEqual(13,self.conversation()["send_budget_limit"])

    def test_another_run_id_cannot_extend_active_grant(self):
        self.activate()
        self.refusal_preserves_state(run_id="another-repeat", expected_revision=7)

    def test_audit_insert_failure_rolls_back_entire_grant(self):
        # Create the audit schema, then return to the exhausted paused state.
        self.activate()
        self.finish()
        self.sql("CREATE TRIGGER refuse_audit BEFORE INSERT ON twin_bridge_repeat_grants "
                 "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        before = self.conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.activate(run_id="synthetic-rollback", expected_revision=8, now=self.now+timedelta(minutes=2))
        self.assertEqual(before,self.conversation())
        self.assertEqual([],self.sql("SELECT * FROM twin_bridge_repeat_grants WHERE run_id='synthetic-rollback'"))

    def test_expected_state_validation(self):
        for values in ({"expected_used":12},{"expected_revision":5},{"scope_fingerprint":"b"*32},
                       {"extra_replies":0},{"extra_replies":31},{"extra_replies":True},
                       {"fan_id":True},{"expected_used":True},{"owner_token":"short"},
                       {"deadline":self.now},{"deadline":self.now+timedelta(hours=2)},
                       {"now":self.now-timedelta(days=3),"deadline":self.now-timedelta(days=3)+timedelta(minutes=1)}):
            with self.subTest(values=values):
                self.refusal_preserves_state(**values)

    def test_paused_reserved_exhausted_and_ownerless_required(self):
        for column,value in (("state","active"),("reserved",0),("owner_token","other"),("send_budget_limit",14)):
            original = self.conversation()[column]
            self.sql(f"UPDATE twin_bridge_conversations SET {column}=?", (value,))
            self.refusal_preserves_state()
            self.sql(f"UPDATE twin_bridge_conversations SET {column}=?", (original,))

    def test_all_account_open_turns_refuse(self):
        for state in ("frozen","submitted","cancelling","draft_ready","dispatching","sent","delivery_unknown","attention","unrecognised"):
            self.sql("INSERT INTO twin_bridge_turns VALUES('123',999,?)", (state,))
            self.refusal_preserves_state()
            self.sql("DELETE FROM twin_bridge_turns WHERE fan_id=999")

    def test_unsettled_attempts_refuse_across_account(self):
        for state in ("dispatching","delivery_unknown","unexpected"):
            self.sql("INSERT INTO twin_bridge_send_attempts VALUES('123',999,?,NULL)", (state,))
            self.refusal_preserves_state()
            self.sql("DELETE FROM twin_bridge_send_attempts WHERE fan_id=999")

    def test_other_writers_and_automation_refuse(self):
        cases = [("native_pilots","INSERT INTO native_pilots VALUES('123',999,'active')"),
                 ("twin_bridge_writes","INSERT INTO twin_bridge_writes VALUES('123',0,'old','unresolved','2000-01-01')"),
                 ("automation_rules","INSERT INTO automation_rules VALUES('123',1)"),
                 ("scheduled_jobs","INSERT INTO scheduled_jobs VALUES('123','pending')")]
        for table,query in cases:
            self.sql(query)
            self.refusal_preserves_state()
            self.sql(f"DELETE FROM {table}")

    def test_cleanup_after_normal_release_and_reacquisition(self):
        self.activate()
        self.sql("UPDATE twin_bridge_conversations SET revision=11,reserved=0,owner_token=NULL,send_budget_used=17")
        result = self.finish()
        self.assertTrue(result["revoked"])
        c = self.conversation()
        self.assertEqual((17,17,"paused",1,None,12), (c["send_budget_limit"],c["send_budget_used"],c["state"],c["reserved"],c["owner_token"],c["revision"]))
        self.assertFalse(self.finish()["revoked"])

    def test_cleanup_preserves_newer_operator_state(self):
        for changes in ("owner_token='replacement'", "state='paused',revision=8", "state='attention'",
                        "reset_at='2026-01-02 04:00:00.000000'", "takeover_at='2026-01-02 04:00:00.000000'",
                        "cancelled_at='2026-01-02 04:00:00.000000'", "send_budget_limit=29", "revision=2"):
            with self.subTest(changes=changes):
                grant = self.activate()
                self.sql("UPDATE twin_bridge_conversations SET "+changes)
                before = self.conversation()
                self.assertFalse(self.finish()["revoked"])
                self.assertEqual(before,self.conversation())
                self.tearDown()
                self.setUp()

    def test_unknown_evidence_is_never_cleared(self):
        self.activate()
        self.sql("INSERT INTO twin_bridge_send_attempts VALUES('123',456,'delivery_unknown',NULL)")
        self.sql("UPDATE twin_bridge_conversations SET send_budget_used=14")
        self.assertTrue(self.finish()["revoked"])
        self.assertEqual("attention",self.conversation()["state"])
        self.assertEqual(14,self.conversation()["send_budget_limit"])
        self.assertEqual(1,len(self.sql("SELECT * FROM twin_bridge_send_attempts WHERE state='delivery_unknown'")))

    def test_old_receipts_and_late_backfill_parked_without_modifying_history(self):
        self.activate()
        old,new = "2026-01-01 00:00:00.000000","2026-01-03 00:00:00.000000"
        for ident, occurred, received, state, fan in [(1,old,old,"pending",456),(2,old,new,"pending",456),
                    (3,new,new,"pending",456),(4,old,old,"assigned",456),(5,old,old,"pending",999)]:
            self.sql("INSERT INTO twin_bridge_inputs VALUES('123',?,?,?,?,?)",(fan,ident,occurred,received,state))
        count = discard_old_inputs(self.path, run_id=self.args["run_id"], owner_token=self.args["owner_token"], now=self.now)
        self.assertEqual(2,count)
        self.assertEqual(["parked","parked","pending","assigned","pending"],
                         [r["state"] for r in self.sql("SELECT state FROM twin_bridge_inputs ORDER BY provider_message_id")])
        self.assertEqual(13,self.conversation()["send_budget_used"])

    def test_unknown_owner_and_expired_discard_refuse(self):
        self.activate()
        for owner,now in (("wrong-owner-token",self.now),(self.args["owner_token"],self.now+timedelta(hours=1))):
            with self.assertRaises(Refused):
                discard_old_inputs(self.path,run_id=self.args["run_id"],owner_token=owner,now=now)
        with self.assertRaises(Refused):
            stop(self.path,run_id=self.args["run_id"],owner_token="wrong",now=self.now,reason="finished")

    def test_missing_database_is_not_created(self):
        path = self.path.parent / "absent.sqlite"
        with self.assertRaises(Refused):
            arm(path,**self.args)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RepeatTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("all cases passed")
    sys.exit(0 if result.wasSuccessful() else 1)
