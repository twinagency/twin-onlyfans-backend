"""Neutral multi-message evidence must survive the delivery authority unchanged."""
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from automations.contextual_evidence import evidence
from native_pilot import contextual
from native_pilot.ledger import Refused


ACCOUNT, FAN = "of-900001", 900002


@pytest.fixture
def thread(tmp_path, monkeypatch):
    path = tmp_path / "evidence.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE messages(account_id TEXT,fan_id INTEGER,message_id INTEGER,"
                    "direction TEXT,created_at TEXT,is_unsent BOOLEAN NOT NULL DEFAULT FALSE)")
        con.executemany("INSERT INTO messages(account_id,fan_id,message_id,direction,created_at) VALUES(?,?,?,?,?)", [
            (ACCOUNT, FAN, 7, "in", "2026-10-30T10:00:00"),
            (ACCOUNT, FAN, 8, "out", "2026-10-30T10:01:00"),
            (ACCOUNT, FAN, 9, "in", "2026-10-30T10:02:00"),
            (ACCOUNT, FAN, 10, "in", "2026-10-30T10:03:00"),
            (ACCOUNT, FAN + 1, 11, "in", "2026-10-30T10:04:00"),
            ("of-other", FAN, 12, "in", "2026-10-30T10:04:00"),
        ])

    @contextmanager
    def readonly(target):
        con = sqlite3.connect(target)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    monkeypatch.setattr(contextual.store, "readonly", readonly)
    monkeypatch.setattr(contextual, "vault_revision", lambda *args: "vault-revision")
    return path


def authority(path):
    return contextual.ContextualAuthority(path, ACCOUNT, FAN, 10)


def delivery(account=ACCOUNT, fan=FAN):
    return SimpleNamespace(plan=SimpleNamespace(account_id=account, fan_id=fan,
        attached=[101], previews=[], media=[101], price_cents=500))


PERMISSION = {"binding": "binding", "revision": 5}


def test_two_valid_evidence_ids_reach_armed_snapshot(thread):
    context = {"messages": [
        {"id": 8, "role": "creator", "text": "Which colour?"},
        {"id": 9, "role": "fan", "text": "I prefer blue."},
        {"id": 10, "role": "fan", "text": "The portrait looks interesting."},
    ]}
    ids = evidence({"action": "send_ppv", "reason": "Current interest in portrait",
        "supporting_message_ids": [9, 10], "evidence": [
            {"id": 9, "quote": "prefer blue"},
            {"id": 10, "quote": "portrait looks interesting"}]}, context)
    assert ids == (9, 10)
    a = authority(thread)
    with a.arm(delivery(), PERMISSION, "Current portrait interest", ids):
        assert a.armed["supporting_message_ids"] == [9, 10]
        assert a.armed["source_message_id"] == 10
    assert a.armed is None


def test_earlier_message_in_latest_unanswered_run_is_valid(thread):
    a = authority(thread)
    with a.arm(delivery(), PERMISSION, "Current interest", (9,)):
        assert a.armed["source_message_id"] == 10


@pytest.mark.parametrize("ids", [(7,), (8, 10), (11, 10), (12, 10), (999, 10),
                                (), (True,), (9, 9), (1, 2, 3, 4, 10)])
def test_stale_creator_foreign_unknown_or_malformed_evidence_rejected(thread, ids):
    a = authority(thread)
    with pytest.raises(Refused, match="contextual-decision-scope"):
        with a.arm(delivery(), PERMISSION, "Current interest", ids):
            pytest.fail("Rejected evidence reached the armed scope")
    assert a.armed is None


@pytest.mark.parametrize("account,fan", [("of-other", FAN), (ACCOUNT, FAN + 1)])
def test_delivery_provenance_stays_scoped(thread, account, fan):
    with pytest.raises(Refused, match="contextual-decision-scope"):
        with authority(thread).arm(delivery(account, fan), PERMISSION, "Current interest", (9, 10)):
            pytest.fail("Foreign delivery reached the armed scope")


def test_newer_inbound_invalidates_multi_message_evidence(thread):
    with sqlite3.connect(thread) as con:
        con.execute("INSERT INTO messages(account_id,fan_id,message_id,direction,created_at) VALUES(?,?,?,?,?)",
                    (ACCOUNT, FAN, 13, "in", "2026-10-30T10:05:00"))
    with pytest.raises(Refused, match="contextual-decision-scope"):
        with authority(thread).arm(delivery(), PERMISSION, "Current interest", (9, 10)):
            pytest.fail("Stale turn reached the armed scope")


def test_original_single_source_remains_compatible(thread):
    with authority(thread).arm(delivery(), PERMISSION, "Current interest", (10,)):
        pass


def test_new_input_between_arm_and_post_is_rechecked(thread, monkeypatch):
    a = authority(thread)
    monkeypatch.setattr(a, "_permission", lambda *_: ("live", {
        "configuration_revision": 5, "binding_id": "binding"}))
    with a.arm(delivery(), PERMISSION, "Current interest", (9, 10)):
        metadata = dict(a.armed)
        with sqlite3.connect(thread) as con:
            con.execute("INSERT INTO messages(account_id,fan_id,message_id,direction,created_at) VALUES(?,?,?,?,?)",
                        (ACCOUNT, FAN, 13, "in", "2026-10-30T10:05:00"))
        with pytest.raises(Refused, match="contextual-decision-scope"):
            a.before_post(metadata)


def test_retracted_supporting_message_is_rechecked_before_post(thread, monkeypatch):
    a = authority(thread)
    monkeypatch.setattr(a, "_permission", lambda *_: ("live", {
        "configuration_revision": 5, "binding_id": "binding"}))
    with a.arm(delivery(), PERMISSION, "Current interest", (9, 10)):
        metadata = dict(a.armed)
        with sqlite3.connect(thread) as con:
            con.execute("UPDATE messages SET is_unsent=TRUE WHERE account_id=? AND fan_id=? AND message_id=9",
                        (ACCOUNT, FAN))
        with pytest.raises(Refused, match="contextual-decision-scope"):
            a.before_post(metadata)
