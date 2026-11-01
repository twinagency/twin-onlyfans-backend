"""Moxy adapter: off / shadow / live gating, decision log, purchase replay. Sqlite in-memory, no network."""
import json
import socket
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from moxy_workflow import adapter
from moxy_workflow.adapter import LiveNotEnabled, run_workflow
from moxy_workflow.state import JsonStateStore

EX = Path(__file__).resolve().parent.parent / "moxy_workflow" / "examples"
ACC, FAN = "of-1", 42


@pytest.fixture
def persona(tmp_path):
    d = json.loads((EX / "example_persona.json").read_text())
    d["vault"] = [{"name": "L1", "steps": [{"ppv_id": "101", "label": "set one", "price": 10},
                                           {"ppv_id": "102", "label": "set two", "price": 15}]}]
    d["first_offer_after_turns"] = 0
    p = tmp_path / "persona.json"
    p.write_text(json.dumps(d))
    return str(p)


@pytest.fixture
def conn(tmp_path):
    eng = create_engine("sqlite://")
    c = eng.connect()
    c.execute(text("CREATE TABLE account_ai_config(account_id TEXT, ai_chatter_config_json TEXT)"))
    c.execute(text("CREATE TABLE vault_sends(account_id TEXT,fan_id INTEGER,media_id INTEGER,message_id INTEGER,"
                   "price_cents INTEGER,was_purchased BOOLEAN,sent_at TEXT)"))
    c.execute(text("CREATE TABLE of_sales_decision(id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT,fan_id INTEGER,"
                   "source_message_id INTEGER,stage TEXT,mode TEXT,action TEXT,approach TEXT,outcome TEXT,reason TEXT,"
                   "settings_json TEXT,evidence_json TEXT,grouped_turns INTEGER,raw_bubbles INTEGER,invitation_id TEXT,"
                   "created_at TEXT)"))
    yield c
    c.close()


def cfg(conn, block):
    conn.execute(text("DELETE FROM account_ai_config"))
    s = {} if block is None else {"moxy_workflow": block}
    conn.execute(text("INSERT INTO account_ai_config VALUES(:a,:j)"), {"a": ACC, "j": json.dumps(s)})


def shadow(persona):
    return {"mode": "shadow", "persona_path": persona, "playbook_paths": [str(EX / "example_playbook.json")]}


def rows(conn):
    return conn.execute(text("SELECT * FROM of_sales_decision")).mappings().all()


def test_off_by_default_does_nothing(conn, tmp_path):
    for block in (None, {"mode": "off"}, {"mode": "bogus"}):
        cfg(conn, block)
        r = run_workflow(conn, ACC, FAN, "hi", source_message_id=1, state_store=JsonStateStore(tmp_path / "s"))
        assert r.status == "off" and r.decision is None
    assert rows(conn) == [] and not (tmp_path / "s").exists()


def test_shadow_records_and_sends_nothing(conn, persona, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    cfg(conn, shadow(persona))
    r = run_workflow(conn, ACC, FAN, ["hey you", "how are you"], source_message_id=7,
                     state_store=JsonStateStore(tmp_path / "s"))
    assert r.status == "shadow" and r.decision is not None and r.logged
    [row] = rows(conn)
    assert row["mode"] == "shadow" and row["stage"] == "moxy" and row["outcome"] == "evaluated"
    ev = json.loads(row["evidence_json"])
    assert ev["sent"] is False and ev["decision"]["action"] == r.decision.action
    # idempotent per source message
    again = run_workflow(conn, ACC, FAN, "hey", source_message_id=7, state_store=JsonStateStore(tmp_path / "s"))
    assert not again.logged and len(rows(conn)) == 1
    src = Path(adapter.__file__).read_text()
    assert "of_client" not in src and "curl_cffi" not in src and "send_message" not in src


def test_live_refuses(conn, persona):
    cfg(conn, {**shadow(persona), "mode": "live"})
    with pytest.raises(LiveNotEnabled, match="not enabled in this build"):
        run_workflow(conn, ACC, FAN, "hi", source_message_id=1)
    assert rows(conn) == []


def test_db_purchases_advance_vault(conn, persona, tmp_path):
    cfg(conn, shadow(persona))
    conn.execute(text("INSERT INTO vault_sends VALUES(:a,:f,101,1,1000,1,'2026-11-01')"), {"a": ACC, "f": FAN})
    conn.execute(text("INSERT INTO vault_sends VALUES(:a,:f,102,2,1500,0,'2026-11-02')"), {"a": ACC, "f": FAN})
    store = JsonStateStore(tmp_path / "s")
    r = run_workflow(conn, ACC, FAN, "hey how is your day", source_message_id=9, state_store=store)
    st = store.load(ACC, str(FAN))
    assert r.purchases_replayed == 1 and st.purchased == ["101"] and st.spend_cents == 1000
    assert st.vault_step == 1
    assert st.invite == {"kind": "vault", "id": "102"}   # next scripted step, not the bought one
    r_yes = run_workflow(conn, ACC, FAN, "yes", source_message_id=11, state_store=store)
    assert r_yes.offer == {"vault_id": "102", "kind": "vault", "price_cents": 1500, "label": "set two"}
    # a second call does not replay
    r2 = run_workflow(conn, ACC, FAN, "hello", source_message_id=10, state_store=store)
    assert r2.purchases_replayed == 0


@pytest.mark.parametrize("block", [
    {"mode": "shadow"},
    {"mode": "shadow", "persona_path": "/nonexistent/p.json", "playbook_paths": []},
])
def test_missing_persona_safe_refusal(conn, block, tmp_path):
    cfg(conn, block)
    r = run_workflow(conn, ACC, FAN, "hi", source_message_id=3, state_store=JsonStateStore(tmp_path / "s"))
    assert r.status == "refused" and r.decision is None
    [row] = rows(conn)
    assert row["outcome"] == "blocked"
