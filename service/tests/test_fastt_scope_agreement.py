"""One writer per fan when the sender AND the API both run ``ONLYFANS_FASTT_SCOPE=db:*``.

Synthetic only: SQLite scratch tables for the sender's selector, disposable PostgreSQL for manual delivery.
No provider, network or credentials.
"""
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onlyfans_runtime import fastt_manual as manual, fastt_scope as fs
from native_pilot.ledger import Refused as PilotRefused
from native_pilot.scope import AccountScope
from native_pilot.service import Service, fan_hold_reason

ACC = "of-900001"
NOW = datetime(2026, 10, 28, 12, 0, 0)


@pytest.fixture
def platform(monkeypatch):
    """Sender and API view alike: ``db:*`` with ACC discovered and no per-account selection."""
    fs.reset_cache()
    accounts = {ACC}
    monkeypatch.setenv(fs.SCOPE_ENV, "db:*")
    monkeypatch.setattr(fs, "_clock", lambda: 1000.0)
    monkeypatch.setattr(fs, "_discoverer", lambda: set(accounts))
    monkeypatch.setattr(fs, "_reader", lambda ids: {i: None for i in ids})
    yield accounts
    fs.reset_cache()


def scratch(tmp_path, states):
    """messages + (optionally) of_conversation_state / skip_list; fan -> (mode, takeover) | None (no row)."""
    path = str(tmp_path / "app.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE messages (account_id TEXT, fan_id INTEGER, message_id INTEGER, direction TEXT,
            created_at TEXT, is_unsent INTEGER DEFAULT 0);
        CREATE TABLE of_conversation_state (account_id TEXT, fan_id INTEGER, mode TEXT, takeover_state TEXT);
        CREATE TABLE skip_list (account_id TEXT, fan_id INTEGER, reason TEXT);
    """)
    created = (NOW + timedelta(minutes=5)).isoformat(sep=" ", timespec="microseconds")
    for fan, state in states.items():
        con.execute("INSERT INTO messages VALUES (?,?,?,?,?,0)", (ACC, fan, fan * 10, "in", created))
        if state is not None:
            con.execute("INSERT INTO of_conversation_state VALUES (?,?,?,?)", (ACC, fan, *state))
    con.commit()
    con.close()
    return path


def sender(path):
    svc = Service.__new__(Service)
    svc.db, svc.path, svc.started_at = ACC, path, NOW
    svc.scope, svc.fan_holds, svc.all_fans = AccountScope.parse(ACC), {}, True
    return svc


STATES = {1: ("automated", "none"), 2: ("paused", "none"), 3: None, 4: ("automated", "requested"),
          5: ("automated", "none"), 6: ("human_controlled", "effective"), 7: ("no_contact", "none")}


def test_db_star_sender_serves_only_explicitly_automated_fans(platform, tmp_path):
    path = scratch(tmp_path, STATES)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO skip_list VALUES (?,?,?)", (ACC, 5, "manual_restrict"))
    con.commit()
    con.close()
    svc = sender(path)
    assert svc._owned(1) and svc._owned(3) and svc._owned(999)          # db:* owns every fan of the account...
    serve, controlled = svc.pending_fans()
    assert [fan for fan, _ in serve] == [1]                              # ...but only fan 1 is automated and unstopped
    assert set(controlled) >= {1, 2, 3, 4, 5, 6, 7}                      # Twin's controls still see them (admit is locked)
    assert fan_hold_reason(path, ACC, 1, True) is None
    assert fan_hold_reason(path, ACC, 2, True) == "fan-paused"
    assert fan_hold_reason(path, ACC, 3, True) == "fan-not-enabled"      # missing row: never enabled
    assert fan_hold_reason(path, ACC, 3) is None                         # single-fan mode keeps its old meaning
    assert fan_hold_reason(path, ACC, 4, True) == "fan-takeover"
    assert fan_hold_reason(path, ACC, 5, True) == "fan-restricted"
    assert fan_hold_reason(path, ACC, 6, True) == "fan-human-controlled"
    assert fan_hold_reason(path, ACC, 7, True) == "fan-no-contact"
    assert svc._postsale_in_scope(1) and not svc._postsale_in_scope(3) and not svc._postsale_in_scope(2)


def test_no_control_table_means_nobody_is_enabled(platform, tmp_path):
    path = str(tmp_path / "bare.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages (account_id TEXT, fan_id INTEGER, message_id INTEGER, direction TEXT,"
                " created_at TEXT, is_unsent INTEGER DEFAULT 0)")
    con.execute("INSERT INTO messages VALUES (?,?,?,?,?,0)", (ACC, 1, 10, "in",
                (NOW + timedelta(minutes=5)).isoformat(sep=" ", timespec="microseconds")))
    con.commit()
    con.close()
    assert fan_hold_reason(path, ACC, 1, True) == "fan-not-enabled"
    assert sender(path).pending_fans()[0] == []


def test_strict_per_fan_child_refuses_a_fan_without_a_row_before_any_pilot(platform, tmp_path):
    path = scratch(tmp_path, STATES)
    child = sender(path)
    child.all_fans, child.strict, child.args, child.pilot = False, True, SimpleNamespace(fan=3), None
    with pytest.raises(PilotRefused, match="fan-not-enabled"):
        asyncio.run(child.serve_fan())


def test_other_accounts_fans_are_not_served_by_this_sender(platform, tmp_path):
    svc = sender(scratch(tmp_path, {1: ("automated", "none")}))
    svc.db = "of-900099"                                                 # not a discovered account
    assert not svc._owned(1) and svc.pending_fans()[0] == []


def test_sender_and_api_agree_on_ownership_under_db_star(platform):
    # Both processes run the identical predicates over the identical env; serving implies ownership.
    for account in (ACC, "of-900099", "acc-x"):
        for fan in (1, 12345, 10 ** 9):
            if manual.fastt_serves(account, fan):
                assert manual.fastt_owned(account, fan)
    assert manual.fastt_serves(ACC, 77) and manual.fastt_owned(ACC, 77)
    assert manual.fastt_account_owned(ACC)
    # An undiscovered account is owned (fail closed: the legacy worker never sends) but not served.
    assert manual.fastt_owned("of-900099", 1) and not manual.fastt_serves("of-900099", 1)
