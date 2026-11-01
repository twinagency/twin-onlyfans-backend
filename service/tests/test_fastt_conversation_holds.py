"""R1: the FASTT runner plane honours the SAME conversation-hold policy as the worker plane.

``fan_hold_reason`` / ``fan_commercial_hold_reason`` read a synthetic SQLite store, the gateway picks the commercial
check for a priced card or a follow-up, and ContextualAuthority refuses on it. No provider, no network.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot import runner
from native_pilot.contextual import ContextualAuthority
from native_pilot.ledger import Refused
from native_pilot.service import fan_commercial_hold_reason, fan_hold_reason

ACC = "of-synthetic-1"


def scratch(tmp_path, *, name="hold.db", state=("automated", "none", None), skip=None, flag=False, cases=()):
    path = str(tmp_path / name)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE of_conversation_state (account_id TEXT, fan_id INTEGER, mode TEXT, takeover_state TEXT, unresolved_attempt_id TEXT);
        CREATE TABLE skip_list (account_id TEXT, fan_id INTEGER, reason TEXT);
        CREATE TABLE fan_safety_flags (account_id TEXT, fan_id INTEGER, severity TEXT, resolved_at TEXT);
        CREATE TABLE of_operator_case (account_id TEXT, fan_id INTEGER, category TEXT, scope TEXT, status TEXT);
    """)
    if state:
        con.execute("INSERT INTO of_conversation_state VALUES (?,?,?,?,?)", (ACC, 1, *state))
    if skip:
        con.execute("INSERT INTO skip_list VALUES (?,?,?)", (ACC, 1, skip))
    if flag:
        con.execute("INSERT INTO fan_safety_flags VALUES (?,?,?,NULL)", (ACC, 1, "critical"))
    for category, scope, status in cases:
        con.execute("INSERT INTO of_operator_case VALUES (?,?,?,?,?)", (ACC, 1, category, scope, status))
    con.commit()
    con.close()
    return path


def test_clean_conversation_is_allowed_for_chat_and_cards(tmp_path):
    path = scratch(tmp_path)
    assert fan_hold_reason(path, ACC, 1, True) is None
    assert fan_commercial_hold_reason(path, ACC, 1, True) is None


@pytest.mark.parametrize("category,scope", [("stop_contact", "all"), ("distress", "all"), ("consent", "all"),
                                           ("identity", "all"), ("external_sender", "all")])
def test_all_scope_cases_hold_chat_and_cards(tmp_path, category, scope):
    path = scratch(tmp_path, cases=[(category, scope, "open")])
    assert fan_hold_reason(path, ACC, 1, True) == "fan-case-" + category.replace("_", "-")
    assert fan_commercial_hold_reason(path, ACC, 1, True) == "fan-case-" + category.replace("_", "-")


@pytest.mark.parametrize("category", ["hardship", "complaint"])
def test_commercial_cases_hold_cards_and_follow_ups_but_not_chat(tmp_path, category):
    path = scratch(tmp_path, cases=[(category, "commercial", "acknowledged")])
    assert fan_hold_reason(path, ACC, 1, True) is None
    assert fan_commercial_hold_reason(path, ACC, 1, True) == "fan-case-" + category


@pytest.mark.parametrize("status", ["shadow", "resolved"])
def test_shadow_and_resolved_cases_hold_nothing(tmp_path, status):
    path = scratch(tmp_path, cases=[("distress", "all", status), ("hardship", "commercial", status)])
    assert fan_hold_reason(path, ACC, 1, True) is None
    assert fan_commercial_hold_reason(path, ACC, 1, True) is None


def test_unresolved_delivery_and_critical_flag_hold_the_runner_too(tmp_path):
    assert fan_hold_reason(scratch(tmp_path, state=("automated", "none", "attempt-1")), ACC, 1, True) == "fan-unresolved-delivery"
    assert fan_hold_reason(scratch(tmp_path, name="flag.db", flag=True), ACC, 1, True) == "fan-safety-hold"


def test_gateway_uses_commercial_check_for_cards_followups_and_specific_invitations():
    gateway = runner.Gateway.__new__(runner.Gateway)
    chat, commercial = (lambda: "chat"), (lambda: "commercial")
    gateway.hold_check, gateway.commercial_hold_check, gateway.followup = chat, commercial, False
    assert gateway._hold_for(None) is chat
    assert gateway._hold_for({"media_ids": [1]}) is commercial
    from types import SimpleNamespace
    gateway.content = SimpleNamespace(commercial_text=True)
    assert gateway._hold_for(None) is commercial
    gateway.content.commercial_text = False
    assert gateway._hold_for(None) is chat
    gateway.followup = True
    assert gateway._hold_for(None) is commercial
    gateway.commercial_hold_check = None                     # a gateway built without one keeps the old behaviour
    assert gateway._hold_for({"media_ids": [1]}) is chat


def test_contextual_authority_refuses_on_a_commercial_hold(tmp_path):
    path = scratch(tmp_path, cases=[("complaint", "commercial", "open")])
    authority = ContextualAuthority(path, ACC, 1, 5001, hold_check=lambda: fan_commercial_hold_reason(path, ACC, 1, True))
    with pytest.raises(Refused) as caught:
        authority.check()
    assert caught.value.reason == "contextual-fan-ineligible"
