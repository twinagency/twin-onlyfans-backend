"""CP4 6.D (isolation): two accounts, the SAME fan id on both, in one tenant.

A pause of fan A or of account A moves nothing on account B; a runner scoped to account A never selects,
reads or owns B's rows; the creator context (persona, facts, fan memory) built for A never contains B's.
Real PostgreSQL schema, disposable databases, synthetic data. The runner-loop side (pause during a hung
model, B answered meanwhile, Hand back -> Resume) is test_native_pilot_service.py::multi_level.
"""
import argparse
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_fastt_controls_pg import template, database          # noqa: F401  (fixtures)
from test_onlyfans_foundation_pg import seed
from test_onlyfans_text_delivery_pg import TENANT, current
from db.models import AccountAiConfig, Fan, Message, SkipList
from onlyfans_commands.settings import ACTIONS
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority

A, B, FAN = "of-111111111", "of-222222222", 101
STAFF = "op-session:" + "a" * 64
PERSONA_A = "Alpha is a lighthouse keeper from Alphaville. She likes calm seas and quiet nights."
PERSONA_B = "Beta is a rodeo clown from Betatown. She likes loud crowds and fast horses."


@pytest.fixture
def two(database):
    url, engine = database
    store = seed(url, engine, account=A, tenant=TENANT, fan=FAN)
    seed(url, engine, account=B, tenant=TENANT, fan=FAN)
    for account in (A, B):
        current(engine, mode="automated", account=account, fan=FAN)
    authority = TextAuthority(store)
    for account in (A, B):
        authority.configure(TENANT, account, expected_revision=0, enabled=True, limit_units=20,
                            throttle_seconds=1, actor="synthetic-operator", reason="iso45", transport_kind="native")
    yield url, engine, store, authority
    store.close()


def command(store, account, action, revision):
    return store.control(TENANT, account, FAN, mode=ACTIONS[action], action=action, expected_revision=revision,
                         command_key=hashlib.sha256(f"{account}{action}{revision}".encode()).hexdigest(),
                         actor=STAFF, reason="Dashboard operator: " + action)


def stops(engine):
    with Session(engine) as s:
        return sorted((r.account_id, r.fan_id, r.reason) for r in s.scalars(select(SkipList)))


def test_pause_fan_a_leaves_the_same_numbered_fan_on_b_running_and_back(two):
    from native_pilot.service import fan_hold_reason
    url, engine, store, _ = two
    assert fan_hold_reason(url, A, FAN) is None and fan_hold_reason(url, B, FAN) is None
    command(store, A, "pause_automation", 0)
    assert stops(engine) == [(A, FAN, "manual_restrict")]
    assert fan_hold_reason(url, A, FAN) == "fan-restricted"
    assert fan_hold_reason(url, B, FAN) is None, "pausing A's fan 101 never holds B's fan 101"
    assert fan_hold_reason(url, B, FAN, True) is None
    # B can be paused and resumed on its own; A stays paused throughout.
    command(store, B, "pause_automation", 0)
    command(store, B, "resume_automation", 1)
    assert fan_hold_reason(url, B, FAN) is None and fan_hold_reason(url, A, FAN) == "fan-restricted"
    command(store, A, "resume_automation", 1)
    assert stops(engine) == [] and fan_hold_reason(url, A, FAN) is None
    # Takeover on A holds A only; hand back releases it; B's state never moved.
    command(store, A, "request_takeover", 2)
    assert fan_hold_reason(url, A, FAN) in ("fan-restricted", "fan-human-controlled")
    assert fan_hold_reason(url, B, FAN) is None
    command(store, A, "release_takeover", 3)
    assert fan_hold_reason(url, A, FAN) is None


def test_pause_account_a_leaves_account_b_able_to_send(two):
    from native_pilot.ledger import Ledger
    from twin_bridge import store as bridge
    url, engine, store, authority = two
    bridge.ensure_runtime_schema(url)
    ledger = Ledger(url, allowance=True)
    assert ledger.allowance_status(A)["enabled"] and ledger.allowance_status(B)["enabled"]
    authority.configure(TENANT, A, expected_revision=1, enabled=False, limit_units=20, throttle_seconds=1,
                        actor="synthetic-operator", reason="pause account A")
    assert ledger.allowance_status(A)["enabled"] is False
    assert ledger.allowance_status(B)["enabled"] is True and ledger.allowance_status(B)["used"] == 0
    # The admission step itself reads only its own account's row.
    with bridge.transaction(url) as con:
        with pytest.raises(Exception, match="allowance-disabled"):
            ledger._consume_allowance(con, A, "2030-01-01T00:00:00")
    with bridge.transaction(url) as con:
        ledger._consume_allowance(con, B, "2030-01-01T00:00:00")
    assert ledger.allowance_status(B)["used"] == 1 and ledger.allowance_status(A)["used"] == 0
    authority.configure(TENANT, A, expected_revision=2, enabled=True, limit_units=20, throttle_seconds=1,
                        actor="synthetic-operator", reason="resume account A")
    assert ledger.allowance_status(A)["enabled"] is True


def test_pilot_owners_of_one_account_can_not_act_on_the_other(two):
    from native_pilot.ledger import Ledger, Refused as LedgerRefused
    from twin_bridge import store as bridge
    from datetime import timezone
    url, engine, store, authority = two
    bridge.ensure_runtime_schema(url)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with bridge.transaction(url) as con:
        for account in (A, B):
            con.execute("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,"
                        "scope_fingerprint,state,revision,reserved,send_budget_limit,send_budget_used,updated_at) "
                        "VALUES(?,?,'iso45','c','f','active',4,?,3,3,?)", (account, FAN, True, now.isoformat()))
    ledger = Ledger(url, allowance=True)
    tokens = {A: "synthetic-owner-iso45-aaaaaa", B: "synthetic-owner-iso45-bbbbbb"}
    pilots = {}
    for account, tag in ((A, "a"), (B, "b")):
        pilots[account] = ledger.activate_service(pilot_id=f"svc-{account[3:]}-{FAN}", account_id=account, fan_id=FAN,
                                                  owner_token=tokens[account], now=now, ttl_s=120)
        assert pilots[account].account_id == account
    # A's token can not renew, stop or release B's pilot; B's pilot is untouched.
    pb = pilots[B].pilot_id
    for call in (lambda: ledger.renew(pilot_id=pb, owner_token=tokens[A], now=now, ttl_s=120),
                 lambda: ledger.stop(pilot_id=pb, owner_token=tokens[A], reason="operator-stop", now=now),
                 lambda: ledger.release_service(pilot_id=pb, owner_token=tokens[A], now=now)):
        with pytest.raises(LedgerRefused):
            call()
    assert ledger.status(pb)["state"] == "active"
    # Stopping A (what a delivery-unknown does) leaves B's pilot and conversation active.
    ledger.stop(pilot_id=pilots[A].pilot_id, owner_token=tokens[A], reason="delivery-unknown", now=now)
    assert ledger.status(pilots[A].pilot_id)["state"] == "stopped" and ledger.status(pb)["state"] == "active"
    with bridge.readonly(url) as con:
        rows = {r[0]: r[1] for r in con.execute("SELECT account_id,state FROM twin_bridge_conversations").fetchall()}
    assert rows[A] != "active" and rows[B] == "active"


def messages(engine, account, fan, ids, text_, base):
    with Session(engine) as s, s.begin():
        for n, mid in enumerate(ids):
            s.add(Message(account_id=account, fan_id=fan, message_id=mid, direction="in", body=text_,
                          created_at=base + timedelta(seconds=n)))


def test_a_runner_for_a_selects_and_reads_only_account_a(two, monkeypatch):
    from native_pilot import runner, service
    from native_pilot.ledger import Refused as LedgerRefused
    url, engine, store, _ = two
    now = datetime.utcnow()
    messages(engine, A, FAN, [1001], "from a", now + timedelta(seconds=5))
    messages(engine, B, FAN, [2001], "from b", now + timedelta(seconds=6))
    monkeypatch.setattr(service.Service, "_owned", lambda self, fan: True)

    def runner_for(account):
        args = argparse.Namespace(account=account, fan=None, all_fans=True, handle="h", enable_send=True,
                                  twin_context=False, content_manifest=None, service_id=None)
        svc = service.Service(args)
        svc.path, svc.started_at = url, now.isoformat(sep=" ")
        return svc
    sa, sb = runner_for(A), runner_for(B)
    assert [m for _, m in sa.pending_fans()[0]] == [1001] and [m for _, m in sb.pending_fans()[0]] == [2001]
    # Pausing A's fan removes A's candidate only; B's same-numbered fan is still served.
    command(store, A, "pause_automation", 0)
    assert sa.pending_fans()[0] == [] and [m for _, m in sb.pending_fans()[0]] == [2001]
    # Reads are account-scoped: B's message id is not a readable source for A's runner.
    with pytest.raises(LedgerRefused, match="source-missing"):
        runner.read_sources(url, A, FAN, [2001])
    assert set(runner.read_sources(url, A, FAN, [1001])) == {1001}
    # The engine boundary refuses the other account outright.
    from native_pilot.engine import NativeEngine, PilotBoundaryError
    import asyncio
    eng = NativeEngine(A, FAN, chat=None, send=None)
    for call in (lambda: eng.prompt_context(B, FAN, last_inbound="x"), lambda: eng.load_config(B, {}),
                 lambda: eng.resolve_model(B, "ai_chatter"), lambda: eng.hold(B, FAN, 1)):
        with pytest.raises(PilotBoundaryError, match="wrong-account"):
            asyncio.run(call())
    with pytest.raises(PilotBoundaryError, match="wrong-fan"):
        asyncio.run(eng.hold(A, FAN + 1, 1))


def test_twin_context_never_crosses_accounts(two):
    from native_pilot.twin_context import _build
    from onlyfans_runtime.launch_history_memory.models import MemoryCorrection
    url, engine, store, _ = two
    now = datetime.utcnow()
    with Session(engine) as s, s.begin():
        for account, persona, hobby, tag in ((A, PERSONA_A, "rock climbing", "a"), (B, PERSONA_B, "chess club", "b")):
            s.merge(AccountAiConfig(account_id=account, persona=persona,
                                    persona_facts_json=json.dumps({"job": "keeper" if tag == "a" else "clown"})))
            s.flush()
            s.add(MemoryCorrection(account_id=account, command_key="k-" + tag, fan_id=FAN, field="hobbies",
                                   operation="set", value=hobby, before_json="{}", request_hash=tag * 64,
                                   revision=1, actor="test", reason="synthetic", created_at=now))
    seen = {}
    with Session(engine) as s:
        for account in (A, B):
            ctx = _build(s, account, FAN, "what do you like?")
            assert ctx.finish is not None, "the operator memory is added once as evidence"
            memory = json.dumps(ctx.finish([{"role": "system", "content": "p"}, {"role": "user", "content": "hi"}]))
            seen[account] = (ctx.persona, memory)
    persona_a, memory_a = seen[A]
    persona_b, memory_b = seen[B]
    assert "Alphaville" in persona_a and "Betatown" not in persona_a and "rodeo" not in persona_a
    assert "Betatown" in persona_b and "Alphaville" not in persona_b and "lighthouse" not in persona_b
    assert "keeper" in persona_a and "clown" not in persona_a and "clown" in persona_b and "keeper" not in persona_b
    assert "rock climbing" in memory_a and "chess club" not in memory_a
    assert "chess club" in memory_b and "rock climbing" not in memory_b
    # A third party asking with A's id after B's persona exists still gets only A's.
    with Session(engine) as s:
        again = _build(s, A, FAN, "again")
    assert again.persona == persona_a
    # The hook is bound to the engine's own account: the other account's context can not be requested.
    import asyncio
    from native_pilot.engine import NativeEngine, PilotBoundaryError
    calls = []

    class Spy:
        async def prompt_context(self, account_id, fan_id, *, last_inbound):
            calls.append(account_id)
            return SimpleNamespace(persona="x", finish=None)
    eng = NativeEngine(A, FAN, chat=None, send=None, context=Spy())
    with pytest.raises(PilotBoundaryError, match="wrong-account"):
        asyncio.run(eng.prompt_context(B, FAN, last_inbound="x"))
    assert calls == []
    assert asyncio.run(eng.prompt_context(A, FAN, last_inbound="x")).persona == "x" and calls == [A]
