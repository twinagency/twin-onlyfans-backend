#!/usr/bin/env python3
"""Durable merged turns + conversation coordination - synthetic, no network.

Windows:  set TEMP to a writable folder first, then
          .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_turns.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="twin-bridge-turns-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_ROOT / 'turns.db').as_posix()}"
for _var in ("TWIN_BRIDGE_ENABLED", "TWIN_BRIDGE_ACCOUNT_ID", "TWIN_BRIDGE_CREATOR_SLUG",
             "TWIN_BRIDGE_FAN_ALLOWLIST", "TWIN_BRIDGE_ACTIVATION_CUTOFF",
             "TWIN_BRIDGE_SEND_ENABLED", "TWIN_BRIDGE_SEND_LIMIT", "TWIN_BRIDGE_BASE_URL",
             "TWIN_BRIDGE_TOKEN"):
    os.environ.pop(_var, None)

_relay_cache = types.ModuleType("relay_cache")
_relay_cache.invalidate = lambda *a, **k: None
_relay_cache.invalidate_money_feeds = lambda *a, **k: None
sys.modules["relay_cache"] = _relay_cache

from sqlalchemy import select  # noqa: E402

from db.engine import engine, get_session, init_db  # noqa: E402
from db.models import (  # noqa: E402
    TwinBridgeConversation, TwinBridgeInput, TwinBridgeSendAttempt,
    TwinBridgeTurn, TwinBridgeTurnMember,
)
from twin_bridge import coordination, delivery, turns  # noqa: E402
from twin_bridge.scope import load_scope  # noqa: E402

ACCOUNT = "acct-synthetic-1"
OTHER_ACCOUNT = "acct-synthetic-2"
CREATOR = "creator-synthetic-1"
FAN = 9001
FAN2 = 9002
BASE_TIME = datetime(2026, 10, 15, 4, 0, 0)

ENV = {
    "TWIN_BRIDGE_ENABLED": "1",
    "TWIN_BRIDGE_ACCOUNT_ID": ACCOUNT,
    "TWIN_BRIDGE_CREATOR_SLUG": CREATOR,
    "TWIN_BRIDGE_FAN_ALLOWLIST": f"{FAN},{FAN2}",
    "TWIN_BRIDGE_ACTIVATION_CUTOFF": "2026-10-15T00:00:00Z",
}

failures: list[str] = []


def check(label, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got) -> None:
    check(label, bool(got), True)


sys.setrecursionlimit(50_000)
threading.stack_size(64 * 1024 * 1024)


def run(coro):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def scope(**over):
    env = dict(ENV)
    env.update(over)
    return load_scope(env)


async def add_input(account_id, fan_id, message_id, text, *, received_at, occurred_at=None,
                    creator=CREATOR, state="pending") -> None:
    async with get_session() as s:
        s.add(TwinBridgeInput(
            account_id=account_id, fan_id=int(fan_id), provider_message_id=int(message_id),
            creator_slug=creator, activation_cutoff=datetime(2026, 10, 15, 0, 0, 0),
            conversation_id=str(fan_id), normalized_text=text,
            event_json=json.dumps({"stub": True}),
            occurred_at=occurred_at or received_at, received_at=received_at,
            state=state,
        ))


async def turn_rows(account_id=ACCOUNT):
    async with get_session() as s:
        return list((await s.execute(
            select(TwinBridgeTurn).where(TwinBridgeTurn.account_id == account_id)
            .order_by(TwinBridgeTurn.created_at, TwinBridgeTurn.turn_id)
        )).scalars())


async def member_rows(account_id=ACCOUNT, fan_id=FAN):
    async with get_session() as s:
        return list((await s.execute(
            select(TwinBridgeTurnMember).where(
                TwinBridgeTurnMember.account_id == account_id,
                TwinBridgeTurnMember.fan_id == int(fan_id),
            ).order_by(TwinBridgeTurnMember.position)
        )).scalars())


async def _input_state(account_id, fan_id, message_id):
    async with get_session() as s:
        row = await s.get(TwinBridgeInput, (account_id, int(fan_id), int(message_id)))
        return row.state if row else None


async def _input_states(account_id, fan_id, ids):
    return [await _input_state(account_id, fan_id, i) for i in ids]


async def _set_input_state(account_id, fan_id, ids, state):
    async with get_session() as s:
        from sqlalchemy import update
        await s.execute(
            update(TwinBridgeInput)
            .where(
                TwinBridgeInput.account_id == account_id,
                TwinBridgeInput.fan_id == int(fan_id),
                TwinBridgeInput.provider_message_id.in_([int(i) for i in ids]),
            )
            .values(state=state)
        )


async def _conv_state(account_id, fan_id):
    async with get_session() as s:
        row = await s.get(TwinBridgeConversation, (account_id, int(fan_id)))
        return row.state if row else None


async def reset_all() -> None:
    async with get_session() as s:
        for model in (TwinBridgeSendAttempt, TwinBridgeTurnMember, TwinBridgeTurn,
                      TwinBridgeConversation, TwinBridgeInput):
            await s.execute(model.__table__.delete())
    coordination._OWNERS.clear()
    coordination._RESERVED.clear()
    coordination._ACQUIRING.clear()
    coordination._OWNER_EXPIRES.clear()
    coordination._IN_FLIGHT.clear()
    coordination._DISPATCH_AUTH.clear()
    # Clear any durable cross-process in-flight markers too.
    con = coordination._durable_write_conn()
    if con is not None:
        try:
            con.execute("DELETE FROM twin_bridge_writes")
            con.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            con.close()


async def main() -> None:
    await init_db()

    # ── quiet window ──────────────────────────────────────────────────
    print("\nturns: seven-second quiet window")
    await reset_all()
    for offset in (0, 2, 4):
        await add_input(ACCOUNT, FAN, 100 + offset, f"line {offset}",
                        received_at=BASE_TIME + timedelta(seconds=offset))
    early = await turns.form_turns(now=BASE_TIME + timedelta(seconds=10), scope=scope())
    check("three receipts t=0,2,4 freeze nothing before t=11", early.turns_frozen, 0)
    check("and the conversation is held open", early.held_open, 1)
    check("no turn row exists yet", len(await turn_rows()), 0)

    frozen = await turns.form_turns(now=BASE_TIME + timedelta(seconds=11), scope=scope())
    check("at t=11 exactly one turn is frozen", frozen.turns_frozen, 1)
    rows = await turn_rows()
    check("and it is the only one", len(rows), 1)
    turn = rows[0]
    check("all three receipts joined the burst", json.loads(turn.member_ids_json), [100, 102, 104])
    check("text is joined with a single newline", turn.text, "line 0\nline 2\nline 4")
    event = json.loads(turn.event_json)
    check("occurredAt is the FIRST member's provider instant", event["occurredAt"],
          "2026-10-15T04:00:00.000Z")
    check("receivedAt is the LAST member's receipt", event["receivedAt"],
          "2026-10-15T04:00:04.000Z")
    check("messageId uses the synthetic turn namespace",
          event["messageId"].startswith("twin-turn-v1-"), True)
    check("conversationId stays the mapped fan string", event["conversationId"], "9001")
    check("direction is inbound", event["direction"], "inbound")

    again = await turns.form_turns(now=BASE_TIME + timedelta(seconds=30), scope=scope())
    check("re-running freezes nothing new", again.turns_frozen, 0)
    check("membership is unchanged after a restart", len(await turn_rows()), 1)
    check("members still number three", len(await member_rows()), 3)
    check("the event bytes are identical", (await turn_rows())[0].event_json, turn.event_json)

    # A duplicate intake/replay re-presents the same receipts: the fully-derived
    # turn id means the freeze is a no-op, not a second reply.
    await _set_input_state(ACCOUNT, FAN, [100, 102, 104], "pending")
    replay = await turns.form_turns(now=BASE_TIME + timedelta(seconds=31), scope=scope())
    check("a replay of the same receipts freezes nothing", replay.turns_frozen, 0)
    check("and is recognised as the existing turn", replay.already_frozen, 1)
    check("there is still exactly one turn", len(await turn_rows()), 1)
    check("and its memberships are unchanged", len(await member_rows()), 3)
    check("the replayed inputs end up assigned again",
          await _input_states(ACCOUNT, FAN, [100, 102, 104]), ["assigned"] * 3)

    # ── quiet gap ─────────────────────────────────────────────────────
    print("\nturns: a gap of seven seconds starts a new burst")
    await reset_all()
    for offset in (0, 2, 4, 11):
        await add_input(ACCOUNT, FAN, 200 + offset, f"m{offset}",
                        received_at=BASE_TIME + timedelta(seconds=offset))
    first = await turns.form_turns(now=BASE_TIME + timedelta(seconds=11), scope=scope())
    check("the leading burst freezes first", first.turns_frozen, 1)
    check("with the three pre-gap receipts", json.loads((await turn_rows())[0].member_ids_json),
          [200, 202, 204])
    second = await turns.form_turns(now=BASE_TIME + timedelta(seconds=18), scope=scope())
    check("the post-gap receipt forms its own turn", second.turns_frozen, 1)
    rows = await turn_rows()
    check("there are now two turns", len(rows), 2)
    check("the second holds only the later receipt",
          json.loads(rows[1].member_ids_json), [211])
    check("the two turn ids differ", rows[0].turn_id != rows[1].turn_id, True)

    # ── deterministic tie-break ───────────────────────────────────────
    print("\nturns: equal receipts order by provider id")
    await reset_all()
    for mid in (303, 301, 302):
        await add_input(ACCOUNT, FAN, mid, f"m{mid}", received_at=BASE_TIME)
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=8), scope=scope())
    check("equal-time members are ordered by provider id",
          json.loads((await turn_rows())[0].member_ids_json), [301, 302, 303])

    # ── merge limits ──────────────────────────────────────────────────
    print("\nturns: merge limits split at source boundaries")
    await reset_all()
    for i in range(3):
        await add_input(ACCOUNT, FAN, 400 + i, "a" * 3000,
                        received_at=BASE_TIME + timedelta(seconds=i))
    report = await turns.form_turns(now=BASE_TIME + timedelta(seconds=10), scope=scope())
    check("one bounded turn is frozen from the overflow burst", report.turns_frozen, 1)
    members = json.loads((await turn_rows())[0].member_ids_json)
    check("the prefix stops before the limit", len(members), 1)
    check("the overflow input is still pending",
          (await _input_state(ACCOUNT, FAN, 401)), "pending")
    check("the first input is assigned",
          (await _input_state(ACCOUNT, FAN, 400)), "assigned")
    check("no source text was truncated", (await turn_rows())[0].text, "a" * 3000)

    await reset_all()
    for i in range(2):
        await add_input(ACCOUNT, FAN, 500 + i, "\U0001F600" * 1500,
                        received_at=BASE_TIME + timedelta(seconds=i))
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=10), scope=scope())
    members = json.loads((await turn_rows())[0].member_ids_json)
    check("3000 non-BMP units fit in exactly one member", members, [500])
    check("the second emoji message is left for the next turn",
          (await _input_state(ACCOUNT, FAN, 501)), "pending")

    await reset_all()
    await add_input(ACCOUNT, FAN, 600, "b" * 4001, received_at=BASE_TIME)
    lonely = await turns.form_turns(now=BASE_TIME + timedelta(seconds=8), scope=scope())
    check("an individually oversized input freezes no turn", lonely.turns_frozen, 0)
    check("and is parked rather than truncated",
          (await _input_state(ACCOUNT, FAN, 600)), "parked")

    await reset_all()
    await add_input(ACCOUNT, FAN, 700, "   ", received_at=BASE_TIME)
    await add_input(ACCOUNT, FAN, 701, "ok", received_at=BASE_TIME + timedelta(seconds=1))
    mixed = await turns.form_turns(now=BASE_TIME + timedelta(seconds=9), scope=scope())
    check("an unusable input is parked with a fixed reason", mixed.inputs_parked, 1)
    check("its neighbour still forms a turn", mixed.turns_frozen, 1)
    check("the parked row keeps its state",
          (await _input_state(ACCOUNT, FAN, 700)), "parked")

    # ── racing workers ────────────────────────────────────────────────
    print("\nturns: two workers cannot double-assign")
    await reset_all()
    for offset in (0, 2, 4):
        await add_input(ACCOUNT, FAN, 800 + offset, f"r{offset}",
                        received_at=BASE_TIME + timedelta(seconds=offset))
    results = await asyncio.gather(
        turns.form_turns(now=BASE_TIME + timedelta(seconds=11), scope=scope()),
        turns.form_turns(now=BASE_TIME + timedelta(seconds=11), scope=scope()),
    )
    total_frozen = sum(r.turns_frozen for r in results)
    check("the database yields exactly one turn", total_frozen, 1)
    check("and exactly one turn row", len(await turn_rows()), 1)
    check("with one membership per input", len(await member_rows()), 3)
    check("and every input assigned",
          await _input_states(ACCOUNT, FAN, [800, 802, 804]), ["assigned"] * 3)

    # ── account isolation ─────────────────────────────────────────────
    print("\nturns: account and mapping isolation")
    await reset_all()
    await add_input(ACCOUNT, FAN, 900, "mine", received_at=BASE_TIME)
    await add_input(OTHER_ACCOUNT, FAN, 900, "theirs", received_at=BASE_TIME)
    out = await turns.form_turns(now=BASE_TIME + timedelta(seconds=8), scope=scope())
    check("only the scoped account forms a turn", out.turns_frozen, 1)
    check("the other account's input stays pending",
          (await _input_state(OTHER_ACCOUNT, FAN, 900)), "pending")
    check("the turn carries the scoped account", (await turn_rows())[0].account_id, ACCOUNT)

    await reset_all()
    await add_input(ACCOUNT, FAN, 901, "old mapping", received_at=BASE_TIME,
                    creator="creator-someone-else")
    changed = await turns.form_turns(now=BASE_TIME + timedelta(seconds=8), scope=scope())
    check("a changed creator mapping freezes no turn", changed.turns_frozen, 0)
    check("and parks the input for an operator",
          (await _input_state(ACCOUNT, FAN, 901)), "parked")

    # ── turn claims ───────────────────────────────────────────────────
    print("\ncoordination: claims fence stale workers")
    await reset_all()
    await add_input(ACCOUNT, FAN, 1000, "claim me", received_at=BASE_TIME)
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=8), scope=scope())
    turn = (await turn_rows())[0]
    token_a, token_b = "owner-a", "owner-b"
    check("worker A claims the frozen turn",
          await coordination.claim_turn(account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id,
                                        owner_token=token_a, now=BASE_TIME,
                                        expected_state="frozen"), True)
    check("worker B cannot claim it while A's claim is live",
          await coordination.claim_turn(account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id,
                                        owner_token=token_b, now=BASE_TIME,
                                        expected_state="frozen"), False)
    later = BASE_TIME + timedelta(seconds=1000)
    check("after expiry worker B takes over",
          await coordination.claim_turn(account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id,
                                        owner_token=token_b, now=later,
                                        expected_state="frozen"), True)
    check("A's stale release cannot tear down B's claim",
          await coordination.release_turn(account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id,
                                          owner_token=token_a, now=later), False)
    check("B still owns the turn", (await turn_rows())[0].owner_token, token_b)
    check("B's own release works",
          await coordination.release_turn(account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id,
                                          owner_token=token_b, now=later), True)

    # ── ownership + shared writer boundary ────────────────────────────
    print("\ncoordination: ownership and the shared send boundary")
    await reset_all()
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=CREATOR,
            conversation_id=str(FAN), scope_fingerprint=turns.scope_fingerprint(scope()),
            now=BASE_TIME, send_budget_limit=2,
        )
    check("the bridge takes ownership",
          await coordination.reserve_ownership(account_id=ACCOUNT, fan_id=FAN,
                                               owner_token="t1", now=BASE_TIME), True)
    check("and the boundary reports it owned", coordination.is_owned(ACCOUNT, FAN), True)
    denied = False
    try:
        with coordination.guarded_send(ACCOUNT, FAN, purpose="manual"):
            pass
    except coordination.BridgeOwnedError:
        denied = True
    check("a competing local writer is denied at the transport boundary", denied, True)
    other_ok = True
    try:
        with coordination.guarded_send(OTHER_ACCOUNT, FAN, purpose="other"):
            pass
    except Exception:  # noqa: BLE001
        other_ok = False
    check("an unowned chat on another account is unaffected", other_ok, True)
    broadcast_denied = False
    try:
        with coordination.guarded_send(ACCOUNT, FAN2, purpose="broadcast"):
            pass
    except coordination.BroadcastRefusedError:
        broadcast_denied = True
    check("an ambiguous broadcast is refused for a reserved account", broadcast_denied, True)
    broadcast_ok = True
    try:
        with coordination.guarded_send(OTHER_ACCOUNT, FAN, purpose="broadcast"):
            pass
    except Exception:  # noqa: BLE001
        broadcast_ok = False
    check("the same broadcast on an unowned account still works", broadcast_ok, True)
    check("a stale release cannot drop the current reservation",
          await coordination.release_ownership(account_id=ACCOUNT, fan_id=FAN,
                                               owner_token="other", now=BASE_TIME), False)
    check("the reservation is still held", coordination.is_owned(ACCOUNT, FAN), True)
    check("the owner's own release clears it",
          await coordination.release_ownership(account_id=ACCOUNT, fan_id=FAN,
                                               owner_token="t1", now=BASE_TIME), True)
    check("a pause advances the revision and clears the owner",
          await coordination.pause_conversation(account_id=ACCOUNT, fan_id=FAN,
                                                reason="operator", now=BASE_TIME), True)
    check("a paused conversation refuses new ownership",
          await coordination.reserve_ownership(account_id=ACCOUNT, fan_id=FAN,
                                               owner_token="t2", now=BASE_TIME), False)
    check("resume is explicit and succeeds",
          await coordination.resume_conversation(account_id=ACCOUNT, fan_id=FAN,
                                                 now=BASE_TIME), True)
    check("and ownership can be taken again",
          await coordination.reserve_ownership(account_id=ACCOUNT, fan_id=FAN,
                                               owner_token="t3", now=BASE_TIME), True)

    # ── send budget ───────────────────────────────────────────────────
    print("\ndelivery: the pilot send budget is durable")
    await reset_all()
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=CREATOR,
            conversation_id=str(FAN), scope_fingerprint=turns.scope_fingerprint(scope()),
            now=BASE_TIME, send_budget_limit=1,
        )
    # The durable authority the attempt reserves under: a live, reserved
    # conversation owned by this token at this revision.
    check("the delivery claim is reserved for the attempt owner",
          await coordination.reserve_ownership(account_id=ACCOUNT, fan_id=FAN,
                                               owner_token="t1", now=BASE_TIME), True)
    first_attempt = await delivery.begin_attempt(
        account_id=ACCOUNT, fan_id=FAN, turn_id="turn-x", attempt_no=1,
        owner_token="t1", revision=1, now=BASE_TIME)
    check("the first attempt reserves the single unit", first_attempt.attempt_no, 1)
    exhausted = False
    try:
        await delivery.begin_attempt(account_id=ACCOUNT, fan_id=FAN, turn_id="turn-y",
                                     attempt_no=1, owner_token="t1", revision=1, now=BASE_TIME)
    except delivery.BudgetExhausted:
        exhausted = True
    check("a second attempt is refused by the durable cap", exhausted, True)
    duplicate = False
    try:
        await delivery.begin_attempt(account_id=ACCOUNT, fan_id=FAN, turn_id="turn-x",
                                     attempt_no=1, owner_token="t1", revision=1, now=BASE_TIME)
    except delivery.AttemptExists:
        duplicate = True
    check("the same attempt number is never recorded twice", duplicate, True)

    # ── crash recovery ────────────────────────────────────────────────
    print("\ndelivery: an interrupted dispatch is uncertainty, not absence")
    # Recovery is bounded by an attempt age: a row younger than the bound may
    # belong to a LIVE dispatch and is left alone. Advance past the bound so this
    # is a genuinely abandoned dispatch.
    check("a live (fresh) dispatch is NOT reclaimed as a crash",
          await delivery.recover_dispatching(now=BASE_TIME + timedelta(seconds=5)), [])
    recovered = await delivery.recover_dispatching(now=BASE_TIME + timedelta(seconds=40))
    check("the dispatching attempt is recovered", len(recovered), 1)
    async with get_session() as s:
        attempt_rows = list((await s.execute(select(TwinBridgeSendAttempt))).scalars())
    check("as delivery_unknown", attempt_rows[0].state, "delivery_unknown")
    check("with a fixed reason", attempt_rows[0].reason, "interrupted-before-evidence")
    check("and the conversation is paused, not silently resent",
          (await _conv_state(ACCOUNT, FAN)), "paused")

    # ── import and configuration safety ───────────────────────────────
    print("\nsafety: importing the bridge and its CLI starts nothing")
    _before = (dict(coordination._OWNERS), set(coordination._RESERVED))
    import twin_bridge.cli as bridge_cli  # noqa: PLC0415
    import twin_bridge.worker as bridge_worker  # noqa: PLC0415
    check("the manual CLI imports without touching state",
          (dict(coordination._OWNERS), set(coordination._RESERVED)), _before)
    check("and exposes only its own commands",
          sorted(bridge_cli.HANDLERS),
          ["cancel", "pause", "reconcile-writes", "recover", "resume", "run-once",
           "status", "takeover"])
    check("send enablement is off when nothing is configured",
          bridge_worker.send_enabled({}), False)
    check("the send flag alone is not enough",
          bridge_worker.send_enabled({"TWIN_BRIDGE_SEND_ENABLED": "1"}), False)
    check("a zero limit is not enough",
          bridge_worker.send_enabled({"TWIN_BRIDGE_SEND_ENABLED": "1",
                                      "TWIN_BRIDGE_SEND_LIMIT": "0"}), False)
    check("flag plus a positive limit enables sending",
          bridge_worker.send_enabled({"TWIN_BRIDGE_SEND_ENABLED": "1",
                                      "TWIN_BRIDGE_SEND_LIMIT": "3"}), True)


try:
    run(main())
except BaseException as exc:  # noqa: BLE001
    failures.append(f"suite crashed: {type(exc).__name__} ({exc})")
    print(f"  FAIL suite crashed: {type(exc).__name__}: {exc}")

run(engine.dispose())
shutil.rmtree(_TMP_ROOT, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge turns)")
