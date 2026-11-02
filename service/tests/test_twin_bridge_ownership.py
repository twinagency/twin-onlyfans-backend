#!/usr/bin/env python3
"""Stage 1C part 2 repairs - permanent regressions at the real boundaries.

Windows:  .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_ownership.py

Reuses the delivery harness (real coordination/delivery/worker + the real
OFClient transport boundary) and fakes only the LOWEST external layers: the
OnlyFans HTTP transport, the Twin HTTP client and the model. Each case was an
Astra review reproduction; every assertion now states the CORRECT behaviour.
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

_DELIVERY = TESTS_DIR / "test_twin_bridge_delivery.py"
_h = types.ModuleType("ownership_fixture")
_h.__file__ = str(_DELIVERY)
_source = _DELIVERY.read_text(encoding="utf-8").split("\ntry:\n    run(main())")[0]
exec(compile(_source, str(_DELIVERY), "exec"), _h.__dict__)

# The delivery fixture raises `threading.stack_size` to 64 MiB for its
# recursion-heavy round trip. This suite does not need that, and reserving a
# 64 MiB stack for every short-lived worker/transport thread can exhaust the
# host's thread limit (a bare `RuntimeError: can't start new thread`, seen at
# cleanup on a busy box). Use the platform default here.
threading.stack_size(0)

from twin_bridge import cli, coordination, delivery, platform, turns, worker  # noqa: E402
from twin_bridge.delivery import HistoryUnreadable, external_outbound_ids  # noqa: E402

from db.models import Account, Fan, Message  # noqa: E402
from db.onlyfans_pilot_models import TwinBridgeSendAttempt  # noqa: E402
from sqlalchemy import update  # noqa: E402

ACCOUNT = _h.ACCOUNT
FAN = _h.FAN
BASE_TIME = _h.BASE_TIME
ENV = _h.ENV
# The fixture owns the failure list; reuse it so a failed check is really counted.
failures = _h.failures
check = _h.check
run = _h.run
reset_all = _h.reset_all
freeze = _h.freeze
seed_turn = _h.seed_turn
turns_for = _h.turns_for
conv_for = _h.conv_for
attempts_for = _h.attempts_for
scope = _h.scope
get_session = _h.get_session
FakeTwin = _h.FakeTwin
FakeAdapter = _h.FakeAdapter
FakeSession = _h.FakeSession
raw_client = _h.raw_client
TwinBridgeTurn = _h.TwinBridgeTurn
CATEGORY_UNCERTAIN = _h.CATEGORY_UNCERTAIN
TwinClientError = _h.TwinClientError


async def advance(twin, adapter, seconds, **kw):
    return await worker.run_once(
        now=BASE_TIME + timedelta(seconds=seconds), scope=kw.pop("scope", scope()),
        client=twin, adapter=adapter, enable_send=kw.pop("enable_send", True), **kw,
    )


def _clear_process_registry() -> None:
    """Simulate a fresh process: in-memory mirrors empty, the DB intact."""
    coordination._OWNERS.clear()
    coordination._RESERVED.clear()
    coordination._ACQUIRING.clear()
    coordination._OWNER_EXPIRES.clear()
    coordination._IN_FLIGHT.clear()
    coordination._DISPATCH_AUTH.clear()


async def r1_durable_ownership() -> None:
    print("\nR1: the durable reservation excludes real competing writers")
    await reset_all()
    await freeze()
    await advance(FakeTwin(), FakeAdapter(
        send_error=platform.PlatformUnknown("dispatch-uncertain")), 9)
    await advance(FakeTwin(), FakeAdapter(
        send_error=platform.PlatformUnknown("dispatch-uncertain")), 12)
    conv = await conv_for()
    check("a delivery-unknown conversation is paused", conv.state, "paused")
    check("and stays durably reserved", bool(conv.reserved), True)

    _clear_process_registry()  # restart
    session = FakeSession()
    client = raw_client(session)
    denied = None
    try:
        client.send_message(FAN, "a competing automation reply", auto_tag=False)
    except Exception as exc:  # noqa: BLE001
        denied = type(exc).__name__
    check("after restart the durable reservation still blocks send_message",
          denied, "BridgeOwnedError")
    check("and no HTTP call is made", len(session.calls), 0)

    session = FakeSession()
    client = raw_client(session)
    raised: dict = {}
    for name, call in (
        ("send_mass_message", lambda: client.send_mass_message("blast", included_users=[FAN])),
        ("send_mass_message-none", lambda: client.send_mass_message("blast")),
        ("schedule_mass_message", lambda: client.schedule_mass_message(
            text="blast", scheduled_date="2026-10-15T06:00:00+00:00")),
        ("schedule_message", lambda: client.schedule_message(
            FAN, "hi", scheduled_date="2026-10-15T06:00:00+00:00")),
    ):
        try:
            call()
            raised[name] = None
        except Exception as exc:  # noqa: BLE001
            raised[name] = type(exc).__name__
    check("send_mass_message is refused for a reserved account",
          raised["send_mass_message"], "BroadcastRefusedError")
    check("an audience-less broadcast is refused too",
          raised["send_mass_message-none"], "BroadcastRefusedError")
    check("schedule_mass_message is refused",
          raised["schedule_mass_message"], "BroadcastRefusedError")
    check("schedule_message to the reserved fan is refused",
          raised["schedule_message"], "BridgeOwnedError")
    check("no scheduled/mass POST escaped", len(session.calls), 0)

    bypass = None
    try:
        with coordination.guarded_send(ACCOUNT, FAN, purpose="twin_bridge"):
            pass
    except Exception as exc:  # noqa: BLE001
        bypass = type(exc).__name__
    check("purpose='twin_bridge' without a live token is refused",
          bypass, "DispatchNotAuthorized")


async def r1_replacement_and_lease() -> None:
    print("\nR1: a replacement claim and a foreign lease both stop the POST")
    await reset_all()
    await freeze()

    class StealOnHistory(FakeAdapter):
        async def preflight_history(self, fan_id, **kwargs):
            assert await coordination.reserve_ownership(
                account_id=ACCOUNT, fan_id=FAN, owner_token="replacement",
                now=BASE_TIME + timedelta(seconds=200))
            cur = (await turns_for())[0]
            assert await coordination.claim_turn(
                account_id=ACCOUNT, fan_id=FAN, turn_id=cur.turn_id,
                owner_token="replacement", now=BASE_TIME + timedelta(seconds=200),
                expected_state="draft_ready")
            return []

    adapter = StealOnHistory()
    await advance(FakeTwin(), adapter, 9, owner_token="old-worker")
    await advance(FakeTwin(), adapter, 12, owner_token="old-worker")
    check("a stolen claim means the old worker performs no POST", len(adapter.posts), 0)
    check("and records no send attempt", len(await attempts_for()), 0)
    check("the replacement owns the conversation",
          (await conv_for()).owner_token, "replacement")

    await reset_all()
    await freeze()
    from automation_executor import acquire_fan_lease, release_fan_lease
    check("another automation takes the shared fan lease",
          await acquire_fan_lease(ACCOUNT, FAN, "other_automation"), True)
    adapter = FakeAdapter()
    await advance(FakeTwin(), adapter, 9)
    report = await advance(FakeTwin(), adapter, 12)
    check("the bridge does not POST while a foreign lease is live",
          len(adapter.posts), 0)
    check("and reports the lease as the reason", "fan-lease-held" in report.reasons, True)
    await release_fan_lease(ACCOUNT, FAN, holder="other_automation")


async def r1_manual_takeover() -> None:
    print("\nR1: the authenticated manual path takes over before dispatch")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)
    decision = await coordination.human_takeover(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=10))
    check("a normal draft is taken over for the human", decision, "takeover")
    check("and the reservation is released", (await conv_for()).reserved, False)
    session = FakeSession()
    client = raw_client(session)
    client.send_message(FAN, "the human reply", auto_tag=False)
    check("the human's own send is permitted", len(session.calls), 1)

    await reset_all()
    await freeze()
    await advance(FakeTwin(), FakeAdapter(
        send_error=platform.PlatformUnknown("dispatch-uncertain")), 9)
    await advance(FakeTwin(), FakeAdapter(
        send_error=platform.PlatformUnknown("dispatch-uncertain")), 12)
    blocked = await coordination.human_takeover(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=20))
    check("an unknown bridge delivery blocks the manual takeover",
          blocked, "blocked")


async def r2_obsolete_work() -> None:
    print("\nR2: obsolete work is fenced, cancelled and tombstoned")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)
    await coordination.mark_cancelled(account_id=ACCOUNT, fan_id=FAN,
                                      now=BASE_TIME + timedelta(seconds=10))
    await advance(twin, adapter, 12)
    check("a cancelled conversation posts nothing", len(adapter.posts), 0)
    check("and the frozen turn is tombstoned", (await turns_for())[0].state, "parked")

    await reset_all()
    await freeze()

    class CancelledTwin(FakeTwin):
        async def read_status(self, event):
            self.status_reads += 1
            from twin_bridge.client import TwinReplyResult
            return TwinReplyResult(
                request_id="ofm_stub", conversation_key="of_stub", status="cancelled",
                outcome="ok", deliverable=False, text="", epoch=0,
                created_at="2026-10-15T04:00:01.000Z",
                completed_at="2026-10-15T04:00:02.000Z",
                delivered_at=None, committed_at=None, usage={},
            )

    twin, adapter = CancelledTwin(), FakeAdapter()
    await advance(twin, adapter, 9)
    await advance(twin, adapter, 12)
    check("a Twin-cancelled draft is never POSTed", len(adapter.posts), 0)
    check("and the turn is parked, not acknowledged",
          (await turns_for())[0].state, "parked")

    await reset_all()
    await freeze()
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=9), scope=scope())
    await coordination.take_takeover(account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME)
    await coordination.resume_conversation(account_id=ACCOUNT, fan_id=FAN,
                                           now=BASE_TIME + timedelta(seconds=10))
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 12)
    check("the old frozen backlog is not sent after resume", len(adapter.posts), 0)
    check("the old turn is tombstoned", (await turns_for())[0].state, "parked")

    await seed_turn(message_id=6001, text="a fresh message",
                    received_at=BASE_TIME + timedelta(seconds=20))
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=40), scope=scope())
    await advance(twin, adapter, 42)
    await advance(twin, adapter, 45)
    check("a fresh input after resume IS delivered", len(adapter.posts), 1)


async def r3_recovery() -> None:
    print("\nR3: interrupted work recovers without a second POST")
    await reset_all()
    await freeze()

    class TimeoutTwin(FakeTwin):
        async def request_draft(self, event):
            self.drafts += 1
            raise TwinClientError(CATEGORY_UNCERTAIN, uncertain=True)

    twin, adapter = TimeoutTwin(), FakeAdapter()
    await advance(twin, adapter, 9)
    await advance(twin, adapter, 12)
    turn = (await turns_for())[0]
    check("an uncertain submit recovers to a status poll", turn.state, "draft_ready")
    check("with the dispatch fence recorded", turn.dispatch_revision is not None, True)
    await advance(twin, adapter, 15)
    check("the recovered draft is sent exactly once", len(adapter.posts), 1)

    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9, owner_token="process-a")
    await advance(twin, adapter, 200, owner_token="process-b")
    check("a legitimately replaced worker does not park an unchanged draft",
          len(adapter.posts), 1)

    await reset_all()
    await freeze()

    class CrashOnSubmit(FakeTwin):
        async def request_draft(self, event):
            self.drafts += 1
            raise SystemExit("synthetic interruption during request")

    twin, adapter = CrashOnSubmit(), FakeAdapter()
    for seconds in (9, 200, 400, 600):
        try:
            await advance(twin, adapter, seconds)
        except SystemExit:
            pass
    turn = (await turns_for())[0]
    check("interrupted submits still consume the durable retry bound",
          int(turn.generation_attempts) >= 1, True)
    check("and never silently reset to frozen", turn.state != "frozen", True)

    await reset_all()
    await freeze()
    turn = (await turns_for())[0]
    async with get_session() as s:
        from sqlalchemy import update
        await s.execute(update(TwinBridgeTurn).where(
            TwinBridgeTurn.turn_id == turn.turn_id).values(state="dispatching"))
        from db.onlyfans_pilot_models import TwinBridgeSendAttempt
        s.add(TwinBridgeSendAttempt(
            account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id, attempt_no=1,
            state="sent", owner_token="crashed", revision=1,
            provider_message_id="777000111", created_at=BASE_TIME, updated_at=BASE_TIME,
        ))
    adapter = FakeAdapter()
    twin = FakeTwin()
    await advance(twin, adapter, 40)
    check("sent evidence is reconstructed for acknowledgement", len(adapter.posts), 0)
    check("the turn reaches delivered without a second POST",
          (await turns_for())[0].state, "delivered")
    check("still exactly zero provider POSTs", len(adapter.posts), 0)


async def r4_evidence_and_text() -> None:
    print("\nR4: provider evidence and plain text are validated")

    class HistClient:
        platform = "onlyfans"

        def __init__(self, account_id, page):
            self.account_id = account_id
            self._page = page

        def get_messages(self, chat_id, limit=10, order="desc"):
            return self._page

    for label, page in (
        ("an empty object", {}),
        ("an error envelope", {"error": "unavailable"}),
        ("a null list entry", {"list": [None]}),
    ):
        adapter = platform.OnlyFansTextAdapter(ACCOUNT, client=HistClient(ACCOUNT, page))
        raised = None
        try:
            await adapter.preflight_history(FAN)
        except platform.PlatformError as exc:
            raised = exc.reason
        check(f"{label} is refused, not read as empty history",
              raised, "history-unreadable")

    from twin_bridge.delivery import external_outbound_ids
    ids = external_outbound_ids(
        [{"id": 555, "fromUser": {"id": ACCOUNT}}], known=set(), creator_id=ACCOUNT)
    check("a creator row carrying fromUser.id is outbound", ids, [555])
    inbound = external_outbound_ids(
        [{"id": 556, "fromUser": {"id": FAN}}], known=set(), creator_id=ACCOUNT,
        fan_id=FAN)
    check("a fan row is inbound", inbound, [])
    raised = None
    try:
        # An author that is neither the bound creator nor the bound fan is
        # unrelated history, not proof of an inbound fan message.
        external_outbound_ids(
            [{"id": 557, "fromUser": {"id": 424242}}], known=set(),
            creator_id=ACCOUNT, fan_id=FAN)
    except HistoryUnreadable:
        raised = "unreadable"
    check("a third-party author is refused, not read as inbound", raised, "unreadable")

    for label, payload in (
        ("isFromMe=false", {"id": 9, "isFromMe": False}),
        ("authored by the recipient", {"id": 9, "fromUser": {"id": FAN}}),
        ("a malformed author", {"id": 9, "fromUser": {"id": True}}),
    ):
        raised = None
        try:
            platform.validate_created_message(payload, chat_id=FAN)
        except platform.PlatformUnknown:
            raised = "refused"
        check(f"a created message with {label} is not delivery", raised, "refused")

    check("literal HTML-looking text is escaped, not treated as markup",
          platform.escape_plain_text("<p>literal</p>"),
          "<p>&lt;p&gt;literal&lt;/p&gt;</p>")
    check("ampersands and emoji survive",
          platform.escape_plain_text("a & b \U0001F600"), "<p>a &amp; b \U0001F600</p>")
    check("leading/trailing whitespace is preserved",
          platform.escape_plain_text("  pad  "), "<p>  pad  </p>")


async def r5_burst_boundary() -> None:
    print("\nR5: a query page boundary never closes an open burst early")
    await reset_all()
    for mid, secs in ((100, 0), (102, 2), (104, 4)):
        await seed_turn(message_id=mid, received_at=BASE_TIME + timedelta(seconds=secs),
                        text=f"m{mid}")
    report = await turns.form_turns(now=BASE_TIME + timedelta(seconds=9),
                                    batch_size=2, scope=scope())
    check("no turn is frozen while the burst may continue", report.turns_frozen, 0)
    check("and the burst is reported held open", report.held_open, 1)

    report = await turns.form_turns(now=BASE_TIME + timedelta(seconds=11),
                                    batch_size=2, scope=scope())
    check("the closed burst freezes exactly one turn", report.turns_frozen, 1)
    rows = await turns_for()
    check("with one turn only", len(rows), 1)
    check("the full membership is preserved",
          json.loads(rows[0].member_ids_json), [100, 102, 104])


async def r6_cli_entry_point() -> None:
    print("\nR6: the documented CLI runs the real delivery path")
    parser = cli.build_parser()
    parsed = parser.parse_args(["pause", "--fan", str(FAN)])
    check("the documented `pause --fan N` syntax parses", parsed.fan, FAN)
    parsed = parser.parse_args(["--fan", str(FAN), "status"])
    check("the parent `--fan N status` form parses", parsed.fan, FAN)
    parsed = parser.parse_args(["run-once", "--enable-send"])
    check("--enable-send alone carries NO default limit", parsed.send_limit, None)

    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    worker._default_client = lambda: twin
    cli.ADAPTER_FACTORY = lambda _acct: adapter
    try:
        args = types.SimpleNamespace(enable_send=True, send_limit=5, batch=100,
                                     account=ACCOUNT)
        first = await cli._run_once(args)
        check("the CLI reports sending enabled", first.get("send_enabled"), True)
        second = await cli._run_once(args)
        check("the CLI constructs an adapter and dispatches", second["dispatched"], 1)
        check("with exactly one provider POST", len(adapter.posts), 1)

        os.environ.pop("TWIN_BRIDGE_SEND_LIMIT", None)
        no_limit = await cli._run_once(
            types.SimpleNamespace(enable_send=True, send_limit=None, batch=100,
                                  account=ACCOUNT))
        check("--enable-send without a positive limit is refused",
              no_limit.get("error"), "send-limit-required")
    finally:
        cli.ADAPTER_FACTORY = None
        os.environ.update(ENV)


async def rr1_takeover_race() -> None:
    print("\nRR1: a takeover loses to an in-flight dispatch (barrier-controlled)")
    await reset_all()
    await freeze()
    await advance(FakeTwin(), FakeAdapter(), 9)      # draft_ready

    class BlockingOFClient:
        """The guarded LOWEST HTTP layer, held open mid-POST."""

        platform = "onlyfans"

        def __init__(self):
            self.account_id = ACCOUNT
            self.entered = threading.Event()
            self.release = threading.Event()
            self.posts = []

        def get_messages(self, chat_id, limit=10, order="desc"):
            # Include the frozen source so the real history preflight reaches
            # the dispatch race this test is intended to exercise.
            return {"list": [{"id": 5001, "isFromMe": False,
                              "fromUser": {"id": FAN},
                              "createdAt": "2026-10-15T04:00:00Z"}],
                    "hasMore": False}

        def send_text_once_body(self, chat_id, body_text):
            self.entered.set()
            self.release.wait(timeout=15.0)
            self.posts.append((int(chat_id), body_text))
            return {"id": 555000999, "responseType": "message", "isFromMe": True,
                    "text": body_text}

    blocking = BlockingOFClient()
    adapter = platform.OnlyFansTextAdapter(ACCOUNT, client=blocking)
    twin = FakeTwin()
    real_wait = coordination.wait_for_quiescence_all
    state: dict = {"task": None, "armed": False}

    async def wait_wrapper(account_id, fan_id, *, timeout_s, poll_s=0.02, io_offload=False):
        if state["armed"] and state["task"] is None:
            # The takeover has ALREADY passed its checks; now the worker commits
            # `dispatching` + the attempt row and enters the guarded POST.
            state["task"] = asyncio.ensure_future(worker.run_once(
                now=BASE_TIME + timedelta(seconds=12), scope=scope(), client=twin,
                adapter=adapter, enable_send=True))
            for _ in range(2000):
                if blocking.entered.is_set() or state["task"].done():
                    break
                await asyncio.sleep(0.01)
        return await real_wait(account_id, fan_id, timeout_s=timeout_s, poll_s=poll_s,
                               io_offload=io_offload)

    coordination.wait_for_quiescence_all = wait_wrapper
    state["armed"] = True
    try:
        decision = await coordination.human_takeover(
            account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=12))
    finally:
        state["armed"] = False
        coordination.wait_for_quiescence_all = real_wait
    check("the takeover loses arbitration to the live dispatch", decision, "blocked")
    check("and does not clear the reservation underneath it",
          (await conv_for()).reserved, True)
    check("the guarded POST was mid-flight during the takeover",
          blocking.entered.is_set(), True)
    blocking.release.set()
    if state["task"] is not None:
        await state["task"]
    check("exactly one provider POST (the bridge's own)", len(blocking.posts), 1)
    check("the loser wrote no second attempt", len(await attempts_for()), 1)


async def rr1_admission_race() -> None:
    print("\nRR1: quiescence-to-reservation admission is atomic")
    await reset_all()
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=_h.CREATOR,
            conversation_id=str(FAN), scope_fingerprint="x", now=BASE_TIME,
            send_budget_limit=5)
    # The conversation starts OWNED BY NOBODY so the older writer may enter
    # transport; the competition is the reservation racing that writer.
    async with get_session() as s:
        from db.onlyfans_pilot_models import TwinBridgeConversation
        await s.execute(
            update(TwinBridgeConversation)
            .where(TwinBridgeConversation.account_id == ACCOUNT,
                   TwinBridgeConversation.fan_id == FAN)
            .values(reserved=False, owner_token=None, owner_expires_at=None)
        )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def older_writer():
        with coordination.guarded_send(ACCOUNT, FAN, purpose="other"):
            entered.set()
            await release.wait()

    task = asyncio.ensure_future(older_writer())
    entered_ok = True
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        entered_ok = False
    if not entered_ok:
        release.set()
        await task
        check("the older writer entered transport", False, True)
        return
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge", now=BASE_TIME)
    check("a reservation is refused while an older writer is inside transport",
          got, False)
    release.set()
    await task
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge", now=BASE_TIME)
    check("and succeeds once that writer has settled", got, True)

    # The other direction: while the reservation is mid-acquisition, a competing
    # write is refused rather than slipping between observe and reserve.
    await coordination.release_ownership(account_id=ACCOUNT, fan_id=FAN,
                                         owner_token="bridge", now=BASE_TIME)
    check("the admission token is taken",
          coordination._begin_admission(ACCOUNT, FAN, "bridge", BASE_TIME), True)
    refused = None
    try:
        with coordination.guarded_send(ACCOUNT, FAN, purpose="other"):
            pass
    except Exception as exc:  # noqa: BLE001
        refused = type(exc).__name__
    coordination._end_admission(ACCOUNT, FAN)
    check("a write during acquisition is refused", refused, "BridgeOwnedError")


async def rr1_cooldown() -> None:
    print("\nRR1: the existing fan cooldown is respected")
    await reset_all()
    await freeze()
    async with get_session() as s:
        if await s.get(Account, ACCOUNT) is None:
            s.add(Account(id=ACCOUNT, nickname="synthetic"))
    async with get_session() as s:
        if await s.get(Fan, (ACCOUNT, FAN)) is None:
            s.add(Fan(account_id=ACCOUNT, fan_id=FAN))
    async with get_session() as s:
        await s.execute(
            update(Fan)
            .where(Fan.account_id == ACCOUNT, Fan.fan_id == FAN)
            .values(automation_paused_until=datetime.utcnow() + timedelta(days=1))
        )
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)
    report = await advance(twin, adapter, 12)
    check("a fan on the existing cooldown receives no bridge POST",
          len(adapter.posts), 0)
    check("and the eligibility reason is recorded",
          "fan-not-eligible" in report.reasons, True)
    # Clear the cooldown so it cannot leak into the later sections.
    async with get_session() as s:
        await s.execute(
            update(Fan)
            .where(Fan.account_id == ACCOUNT, Fan.fan_id == FAN)
            .values(automation_paused_until=None)
        )


async def rr1_route_seam() -> None:
    print("\nRR1: the manual route's takeover -> transport seam")
    # `server.py` cannot be imported under this 3.11 harness (llm_models), so the
    # route's DECISION and its guarded TRANSPORT are exercised directly: the
    # route maps `takeover` -> send and `blocked` -> 409.
    await reset_all()
    await freeze()
    await advance(FakeTwin(), FakeAdapter(), 9)
    decision = await coordination.human_takeover(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=10))
    check("the route's takeover decision is 'takeover'", decision, "takeover")
    session = FakeSession()
    client = raw_client(session)
    client.send_message(FAN, "the human reply", auto_tag=False)
    check("the post-takeover human send reaches transport", len(session.calls), 1)

    await reset_all()
    await freeze()
    await advance(FakeTwin(), FakeAdapter(
        send_error=platform.PlatformUnknown("u")), 9)
    await advance(FakeTwin(), FakeAdapter(
        send_error=platform.PlatformUnknown("u")), 12)
    blocked = await coordination.human_takeover(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=20))
    check("an unknown bridge delivery makes the route refuse", blocked, "blocked")


async def rr2_cancel_sent_ack() -> None:
    print("\nRR2: cancellation never overwrites a confirmed send")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(ack_failures=1), FakeAdapter()
    await advance(twin, adapter, 9)
    await advance(twin, adapter, 12)
    check("the provider POST happened once", len(adapter.posts), 1)
    check("the turn awaits its acknowledgement", (await turns_for())[0].state, "sent")
    await coordination.mark_cancelled(account_id=ACCOUNT, fan_id=FAN,
                                      now=BASE_TIME + timedelta(seconds=13))
    await advance(twin, adapter, 14)
    check("the confirmed send is not tombstoned",
          (await turns_for())[0].state, "delivered")
    check("its ledger keeps the sent evidence", (await attempts_for())[0].state, "sent")
    check("ack-only recovery, still one POST", len(adapter.posts), 1)


async def rr2_resume_input_boundary() -> None:
    print("\nRR2: resume excludes pre-resume PENDING inputs")
    await reset_all()
    await freeze()
    await seed_turn(message_id=5002, received_at=BASE_TIME + timedelta(seconds=10))
    await coordination.pause_conversation(account_id=ACCOUNT, fan_id=FAN,
                                          now=BASE_TIME + timedelta(seconds=19),
                                          reason="operator-pause")
    await coordination.resume_conversation(account_id=ACCOUNT, fan_id=FAN,
                                           now=BASE_TIME + timedelta(seconds=20))
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 29)
    await advance(twin, adapter, 32)
    check("a pending pre-resume input is never sent", len(adapter.posts), 0)
    await seed_turn(message_id=5003, received_at=BASE_TIME + timedelta(seconds=30))
    await advance(twin, adapter, 40)
    await advance(twin, adapter, 42)
    check("a fresh post-resume input IS delivered", len(adapter.posts), 1)


async def rr2_source_unsent_in_flight() -> None:
    print("\nRR2: a source invalidated during history yields zero POSTs")
    await reset_all()
    await freeze()

    class DeleteDuringHistory(FakeAdapter):
        async def preflight_history(self, chat_id, **kwargs):
            async with get_session() as s:
                row = await s.get(Message, (ACCOUNT, FAN, 5001))
                row.is_unsent = True
            return []

    twin, adapter = FakeTwin(), DeleteDuringHistory()
    await advance(twin, adapter, 9)
    await advance(twin, adapter, 12)
    check("no POST is made for an invalidated source", len(adapter.posts), 0)
    check("and the turn is parked", (await turns_for())[0].reason, "source-invalidated")


async def rr2_uncertain_cancel_reconcile() -> None:
    print("\nRR2: a lost submit response is still cancelled and reconciled")
    await reset_all()
    await freeze()

    class TimeoutTwin(FakeTwin):
        async def request_draft(self, event):
            self.drafts += 1
            raise TwinClientError(CATEGORY_UNCERTAIN, uncertain=True)

    twin, adapter = TimeoutTwin(), FakeAdapter()
    await advance(twin, adapter, 9)
    check("the submitted turn has no returned request id",
          (await turns_for())[0].request_id, None)
    await coordination.mark_cancelled(account_id=ACCOUNT, fan_id=FAN,
                                      now=BASE_TIME + timedelta(seconds=10))
    for seconds in (12, 15, 20):
        await advance(twin, adapter, seconds)
    check("the uncertain admission is cancelled via the frozen event",
          twin.cancels >= 1, True)
    check("and the turn is parked, never dispatched",
          (await turns_for())[0].state, "parked")
    check("with zero provider POSTs", len(adapter.posts), 0)


async def rr4_strict_evidence() -> None:
    print("\nRR4: strict created-message and history evidence")
    for label, payload in (
        ("a third-party author", {"id": 9, "fromUser": {"id": 424242}}),
        ("a non-boolean direction", {"id": 9, "isFromMe": 0}),
        ("a null recipient id", {"id": 9, "withUser": {"id": None}}),
        ("a non-object recipient container", {"id": 9, "withUser": 5}),
        ("a malformed author", {"id": 9, "fromUser": {"id": True}}),
    ):
        raised = None
        try:
            platform.validate_created_message(payload, chat_id=FAN, creator_id=ACCOUNT)
        except platform.PlatformUnknown:
            raised = "refused"
        check(f"{label} is not delivery", raised, "refused")

    ok = platform.validate_created_message({"id": 9}, chat_id=FAN, creator_id=ACCOUNT)
    check("absent optional fields stay legal", ok.provider_message_id, "9")

    for label, row in (
        ("a boolean author", {"id": 600, "fromUser": {"id": True}}),
        ("an empty author object", {"id": 601, "fromUser": {}}),
        ("a null author id", {"id": 602, "fromUser": {"id": None}}),
        ("a non-boolean direction", {"id": 603, "isFromMe": 0}),
    ):
        raised = None
        try:
            external_outbound_ids([row], known=set(), creator_id=ACCOUNT)
        except HistoryUnreadable:
            raised = "unreadable"
        check(f"history with {label} is refused", raised, "unreadable")

    # A locally attributed human outbound row is NOT exempt; a proven bridge id
    # is. (Also asserted end-to-end in the delivery suite.)
    ids = external_outbound_ids([{"id": 777, "isFromMe": True}], known=set(),
                                creator_id=ACCOUNT)
    check("an unproven outbound row is human activity", ids, [777])
    ids = external_outbound_ids([{"id": 777, "isFromMe": True}], known={777},
                                creator_id=ACCOUNT)
    check("a proven bridge id is exempt", ids, [])


async def rr5_hard_cap() -> None:
    print("\nRR5: a 501-message burst is not closed at the scan cap")
    await reset_all()
    for i in range(501):
        await seed_turn(message_id=2000 + i,
                        received_at=BASE_TIME + timedelta(seconds=2 * i),
                        text=f"m{i}")
    report = await turns.form_turns(now=BASE_TIME + timedelta(seconds=1006),
                                    batch_size=2, scope=scope())
    check("nothing freezes while the burst's true end is still quiet",
          report.turns_frozen, 0)
    check("and the burst is held open", report.held_open, 1)
    report = await turns.form_turns(now=BASE_TIME + timedelta(seconds=1007),
                                    batch_size=2, scope=scope())
    check("the long burst closes once its end is quiet", report.turns_frozen, 1)
    rows = await turns_for()
    check("with all 501 receipts in one turn",
          len(json.loads(rows[0].member_ids_json)), 501)


async def rr6_explicit_limit() -> None:
    print("\nRR6: an explicit invalid CLI cap refuses before any work")
    await reset_all()
    await freeze()
    frozen = (await turns_for())[0].turn_id
    os.environ["TWIN_BRIDGE_SEND_LIMIT"] = "5"
    worker._default_client = lambda: FakeTwin()
    cli.ADAPTER_FACTORY = lambda _acct: FakeAdapter()
    try:
        zero = await cli._run_once(types.SimpleNamespace(
            enable_send=True, send_limit=0, batch=100, account=ACCOUNT))
        check("an explicit zero cap refuses", zero.get("error"), "invalid-send-limit")
        negative = await cli._run_once(types.SimpleNamespace(
            enable_send=True, send_limit=-3, batch=100, account=ACCOUNT))
        check("an explicit negative cap refuses",
              negative.get("error"), "invalid-send-limit")
        check("and no draft/dispatch work ran",
              [t.turn_id for t in await turns_for()], [frozen])
        check("and the frozen turn is untouched", (await turns_for())[0].state, "frozen")
        # An ABSENT CLI cap still falls back to the environment limit.
        fallback = await cli._run_once(types.SimpleNamespace(
            enable_send=True, send_limit=None, batch=100, account=ACCOUNT))
        check("an absent CLI cap falls back to the environment limit",
              fallback.get("send_limit"), 5)
    finally:
        cli.ADAPTER_FACTORY = None
        os.environ.update(ENV)


# ── RR1: the actual CLI/Fastt process arrangement ─────────────────────

_SIBLING_WRITER = r'''
import sys, types
sys.path.insert(0, sys.argv[1])
from of_client import OFClient

class Response:
    status_code = 200; url = "https://api.test/messages"; headers = {}
    text = '{"id": 99}'; ok = True
    def json(self):
        return {"id": 99}

class HTTP:
    def request(self, *a, **kw):
        print("LEGACY_POST_ENTERED", flush=True)
        sys.stdin.readline()
        print("LEGACY_POST_FINISHED", flush=True)
        return Response()

c = OFClient.__new__(OFClient)
c.account_id = sys.argv[2]
c.http = HTTP(); c.timeout_s = 5; c.proxy_url = ""; c.proxy_label = ""
c.x_of_rev = "stub"
c._build_url = lambda path, params=None: "https://api.test/" + str(path).lstrip("/")
c._signed_headers = lambda url: {}
c.send_message(int(sys.argv[3]), "synthetic older writer", auto_tag=False)
'''


async def rr1_cross_process_admission() -> None:
    print("\nRR1: a legacy writer in a SIBLING process blocks admission")
    await reset_all()
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=_h.CREATOR,
            conversation_id=str(FAN), scope_fingerprint="x", now=BASE_TIME,
            send_budget_limit=5)
    async with get_session() as s:
        from db.onlyfans_pilot_models import TwinBridgeConversation
        await s.execute(
            update(TwinBridgeConversation)
            .where(TwinBridgeConversation.account_id == ACCOUNT,
                   TwinBridgeConversation.fan_id == FAN)
            .values(reserved=False, owner_token=None, owner_expires_at=None))

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _SIBLING_WRITER, str(SERVICE_DIR),
         ACCOUNT, str(FAN)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=dict(os.environ))
    out = ""
    try:
        entered = await asyncio.to_thread(proc.stdout.readline)
        check("the sibling process entered the legacy POST",
              entered.strip(), "LEGACY_POST_ENTERED")
        got = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-sibling",
            now=BASE_TIME, quiesce_timeout_s=1.5)
        check("admission is REFUSED while a sibling POST is in flight", got, False)
    finally:
        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass
        out, err = await asyncio.to_thread(proc.communicate, timeout=20)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    check("the sibling POST finished and released its marker",
          "LEGACY_POST_FINISHED" in (out or ""), True)
    check("the sibling exited cleanly", proc.returncode, 0)
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-sibling",
        now=BASE_TIME, quiesce_timeout_s=1.5)
    check("admission succeeds once the sibling writer has settled", got, True)
    await coordination.release_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-sibling", now=BASE_TIME)


async def rr1_expired_authority() -> None:
    print("\nRR1: an expired claim is refused on a FRESH clock")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)  # draft_ready; the claim expires ~t+120
    report = await worker.run_once(
        now=BASE_TIME + timedelta(seconds=12), scope=scope(), client=twin,
        adapter=adapter, enable_send=True,
        clock=lambda: BASE_TIME + timedelta(seconds=200))
    check("an expired claim dispatches nothing", report.dispatched, 0)
    check("and is parked authority-expired", "authority-expired" in report.reasons, True)
    check("with zero provider POSTs", len(adapter.posts), 0)
    check("and the draft is not sent",
          (await turns_for())[0].state != "sent", True)


_RR1_SIBLING = r'''
import sys, dotenv
dotenv.load_dotenv = lambda *a, **k: False
sys.path.insert(0, sys.argv[1])
import requests
from of_client import OFClient, OFAPIError
from twin_bridge.coordination import BridgeOwnedError, BroadcastRefusedError

mode, account, fan = sys.argv[2], sys.argv[3], int(sys.argv[4])

class Response:
    status_code = 200; url = "https://api.test/messages"; headers = {}
    text = '{"id": 99}'; ok = True
    def json(self):
        return {"id": 99}

class RefusedResponse:
    status_code = 400; url = "https://api.test/messages"; headers = {}
    text = '{"error": "bad request"}'; ok = False
    def json(self):
        return {"error": "bad request"}

class HTTP:
    def request(self, *a, **kw):
        if mode == "prefail":
            # A CONNECT timeout is the precise never-connected signal.
            raise requests.exceptions.ConnectTimeout("synthetic never-connected")
        if mode == "connfail":
            # A general ConnectionError can be raised AFTER the POST was
            # transmitted, so it must NOT be treated as proof of no send.
            raise requests.exceptions.ConnectionError("synthetic mid-post loss")
        print("POST_ENTERED", flush=True)
        sys.stdin.readline()
        if mode == "timeout":
            raise requests.exceptions.ReadTimeout("synthetic post-response unknown")
        if mode == "refused":
            raise OFAPIError("synthetic 400", response=RefusedResponse())
        print("POST_FINISHED", flush=True)
        return Response()

c = OFClient.__new__(OFClient)
c.account_id = account; c.http = HTTP(); c.timeout_s = 60
c.proxy_url = ""; c.proxy_label = ""; c.x_of_rev = "stub"
c._build_url = lambda path, params=None: "https://api.test/" + str(path).lstrip("/")
c._signed_headers = lambda url: {}
print("READY", flush=True)
sys.stdin.readline()
try:
    if mode == "broadcast":
        c.send_mass_message("synthetic broadcast", included_users=[fan], auto_tag=False)
    else:
        c.send_message(fan, "synthetic older writer", auto_tag=False)
except (BridgeOwnedError, BroadcastRefusedError) as exc:
    print("REFUSED:" + type(exc).__name__, flush=True)
    sys.exit(3)
except Exception as exc:
    print("WRITER_EXCEPTION:" + type(exc).__name__, flush=True)
'''


async def _sibling(mode: str = "dm", fan: int = None) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _RR1_SIBLING, str(SERVICE_DIR), mode,
         ACCOUNT, str(FAN if fan is None else fan)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=dict(os.environ))
    ready = await asyncio.to_thread(proc.stdout.readline)
    check("the sibling process is READY", ready.strip(), "READY")
    return proc


async def _sibling_finish(proc: subprocess.Popen) -> tuple[str, str, int]:
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
    except Exception:  # noqa: BLE001
        pass
    out, err = await asyncio.to_thread(proc.communicate, timeout=20)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    return out or "", err or "", proc.returncode


async def _open_conversation(*, reserved: bool) -> None:
    await reset_all()
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=_h.CREATOR,
            conversation_id=str(FAN), scope_fingerprint="x", now=BASE_TIME,
            send_budget_limit=5)
    if not reserved:
        from db.onlyfans_pilot_models import TwinBridgeConversation
        async with get_session() as s:
            await s.execute(
                update(TwinBridgeConversation)
                .where(TwinBridgeConversation.account_id == ACCOUNT,
                       TwinBridgeConversation.fan_id == FAN)
                .values(reserved=False, owner_token=None, owner_expires_at=None))


async def rr1_sibling_writer_wins() -> None:
    print("\nRR1: an OLDER sibling writer wins - admission is refused")
    await _open_conversation(reserved=False)
    proc = await _sibling("dm")
    try:
        proc.stdin.write("\n")  # let it enter the real guarded POST and hold
        proc.stdin.flush()
        entered = await asyncio.to_thread(proc.stdout.readline)
        check("the sibling is inside the real guarded POST",
              entered.strip(), "POST_ENTERED")
        got = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev",
            now=BASE_TIME, quiesce_timeout_s=1.5)
        check("bridge admission is REFUSED while the sibling POST is in flight",
              got, False)
    finally:
        out, err, rc = await _sibling_finish(proc)
    check("the sibling POST finished and released its marker",
          "POST_FINISHED" in out, True)
    check("the sibling exited cleanly", rc, 0)
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev",
        now=BASE_TIME, quiesce_timeout_s=1.5)
    check("admission succeeds once the sibling writer has settled", got, True)
    await coordination.release_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev", now=BASE_TIME)


async def rr1_bridge_wins() -> None:
    print("\nRR1: the bridge wins - a waiting sibling writer is REFUSED")
    await _open_conversation(reserved=False)
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev",
        now=BASE_TIME, quiesce_timeout_s=1.5)
    check("the bridge reserves first", got, True)
    proc = await _sibling("dm")
    out, err, rc = await _sibling_finish(proc)
    check("the sibling writer is refused at the transport boundary",
          "REFUSED:BridgeOwnedError" in out, True)
    check("the sibling made no POST", "POST_ENTERED" in out, False)
    check("the sibling exited non-zero", rc != 0, True)
    await coordination.release_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev", now=BASE_TIME)


async def rr1_broadcast_conflicts() -> None:
    print("\nRR1: an account-wide broadcast conflicts with a reserved fan")
    # Ordering 1: the broadcast is in flight, admission must refuse.
    await _open_conversation(reserved=False)
    proc = await _sibling("broadcast")
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
        entered = await asyncio.to_thread(proc.stdout.readline)
        check("the sibling broadcast is inside its guarded POST",
              entered.strip(), "POST_ENTERED")
        got = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev",
            now=BASE_TIME, quiesce_timeout_s=1.5)
        check("admission is REFUSED while an account-wide broadcast is in flight",
              got, False)
    finally:
        out, err, rc = await _sibling_finish(proc)
    check("the broadcast finished", "POST_FINISHED" in out, True)

    # Ordering 2: the bridge reserves first, the broadcast must be refused.
    await _open_conversation(reserved=False)
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev",
        now=BASE_TIME, quiesce_timeout_s=1.5)
    check("the bridge reserves first", got, True)
    proc = await _sibling("broadcast")
    out, err, rc = await _sibling_finish(proc)
    check("the broadcast is refused for a reserved account",
          "REFUSED:BroadcastRefusedError" in out, True)
    await coordination.release_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev", now=BASE_TIME)


async def rr1_marker_renewal() -> None:
    print("\nRR1: a live writer's marker is RENEWED, not cleared by TTL")
    os.environ["TWIN_BRIDGE_WRITE_TTL_S"] = "1"
    os.environ["TWIN_BRIDGE_WRITE_RENEW_S"] = "0.3"
    holder = "renewal-probe"
    try:
        coordination._durable_write_enter(ACCOUNT, 7777, holder, ttl_s=1)
        check("the marker starts fresh", coordination._durable_write_fresh(ACCOUNT, 7777), True)
        await asyncio.sleep(1.6)  # > TTL: only renewal can keep it live
        check("the marker is still live past its original TTL (renewed)",
              coordination._durable_write_fresh(ACCOUNT, 7777), True)
    finally:
        coordination._durable_write_exit(ACCOUNT, 7777, holder)
        os.environ.pop("TWIN_BRIDGE_WRITE_TTL_S", None)
        os.environ.pop("TWIN_BRIDGE_WRITE_RENEW_S", None)
    check("the marker is gone after a clean exit",
          coordination._durable_write_fresh(ACCOUNT, 7777), False)


async def rr1_coordination_failure() -> None:
    print("\nRR1: coordination-storage failure refuses instead of guessing")
    await _open_conversation(reserved=False)
    real_conn = coordination._durable_write_conn
    real_path = coordination._sqlite_path
    coordination._durable_write_conn = lambda: None
    coordination._sqlite_path = lambda: None
    try:
        # An ENABLED bridge must not silently degrade: the reservation and a
        # competing writer both refuse when coordination storage is unusable.
        got = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-rev",
            now=BASE_TIME, quiesce_timeout_s=1.0)
        check("admission refuses when coordination storage is unavailable", got, False)
        refused = None
        try:
            with coordination.guarded_send(ACCOUNT, FAN, purpose="manual"):
                pass
        except Exception as exc:  # noqa: BLE001
            refused = type(exc).__name__
        check("a writer refuses too (not silently sent)", refused, "BridgeOwnedError")
    finally:
        coordination._durable_write_conn = real_conn
        coordination._sqlite_path = real_path


async def rr2_source_invalidated_before_admission() -> None:
    print("\nRR2: a source invalidated before admission yields zero POSTs")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)  # draft_ready
    real_count = worker.count_attempts

    async def invalidate_then_count(*args, **kw):
        async with get_session() as s:
            row = await s.get(Message, (ACCOUNT, FAN, 5001))
            row.is_unsent = True
        return await real_count(*args, **kw)

    worker.count_attempts = invalidate_then_count
    try:
        report = await advance(twin, adapter, 12)
    finally:
        worker.count_attempts = real_count
    check("no POST is made for a source invalidated before admission",
          len(adapter.posts), 0)
    check("and the refusal names the source",
          any("source-invalidated" in r for r in report.reasons), True)
    check("with no attempt row written", [], await attempts_for())


async def rr4_known_id_contradictory_author() -> None:
    print("\nRR4: a known bridge id does not excuse a contradictory author")
    await reset_all()
    await freeze()
    first_twin, first_adapter = FakeTwin(), FakeAdapter()
    for seconds in (9, 12, 15):
        await advance(first_twin, first_adapter, seconds)
    first_turn = (await turns_for())[0]
    check("the first turn is delivered", first_turn.state, "delivered")
    check("with exactly one POST", len(first_adapter.posts), 1)
    bridge_id = first_turn.provider_message_id
    known_bridge_id = int(bridge_id)

    # A valid row authored by the CREATOR carrying a known bridge id is accepted.
    accepted = external_outbound_ids(
        [{"id": known_bridge_id, "isFromMe": True}], known={known_bridge_id},
        creator_id=ACCOUNT, fan_id=FAN)
    check("valid known bridge history is accepted", accepted, [])

    await seed_turn(message_id=5002, received_at=BASE_TIME + timedelta(seconds=30))
    second_twin = FakeTwin()
    second_adapter = FakeAdapter(
        history=[{"id": known_bridge_id, "isFromMe": True,
                  "fromUser": {"id": 424242}}],
        provider_id="777000112")
    second_report = None
    for seconds in (40, 42):
        second_report = await advance(second_twin, second_adapter, seconds)
    check("a contradictory author on a known id produces no POST",
          len(second_adapter.posts), 0)
    check("and the draft is parked history-unreadable",
          "history-unreadable" in second_report.reasons, True)


async def rr1_uncertain_writer_blocks_until_reconciled() -> None:
    print("\nRR1: an uncertain writer blocks admission until reconciled")
    await _open_conversation(reserved=False)
    proc = await _sibling("timeout")
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
        entered = await asyncio.to_thread(proc.stdout.readline)
        check("the sibling is inside the real guarded POST",
              entered.strip(), "POST_ENTERED")
        blocked = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-unknown",
            now=BASE_TIME, quiesce_timeout_s=1.0)
        check("admission is refused while the POST is in flight", blocked, False)
    finally:
        out, err, rc = await _sibling_finish(proc)
    check("the sibling's POST raised an uncertain timeout",
          "WRITER_EXCEPTION:ReadTimeout" in out, True)
    check("the sibling exited cleanly", rc, 0)
    check("the uncertain marker is RETAINED, not cleared",
          coordination._durable_write_fresh(ACCOUNT, FAN), True)
    blocked = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-unknown",
        now=BASE_TIME, quiesce_timeout_s=1.0)
    check("admission STILL refuses after the uncertain outcome", blocked, False)
    # Only an EXPLICIT reconciliation clears it.
    cleared = coordination.reconcile_writes(account_id=ACCOUNT, fan_id=FAN)
    check("reconciliation clears the unresolved marker", cleared >= 1, True)
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-unknown",
        now=BASE_TIME, quiesce_timeout_s=1.0)
    check("admission succeeds only after reconciliation", got, True)
    await coordination.release_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-unknown", now=BASE_TIME)


async def rr1_crashed_writer_blocks_after_ttl() -> None:
    print("\nRR1: a writer that died inside the POST still blocks after its TTL")
    await _open_conversation(reserved=False)
    os.environ["TWIN_BRIDGE_WRITE_TTL_S"] = "0.4"
    os.environ["TWIN_BRIDGE_WRITE_RENEW_S"] = "0.1"
    proc = await _sibling("dm")
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
        entered = await asyncio.to_thread(proc.stdout.readline)
        check("the sibling entered the POST before dying", entered.strip(), "POST_ENTERED")
        proc.kill()
        await asyncio.to_thread(proc.wait, 10)
        # Bounded wait only to elapse the SHORT configured TTL. The barrier above
        # is the sync proof; this proves the marker does not lapse.
        await asyncio.sleep(0.8)
        check("a crashed writer's marker is still blocking evidence",
              coordination._durable_write_fresh(ACCOUNT, FAN), True)
        blocked = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-crash",
            now=BASE_TIME, quiesce_timeout_s=1.0)
        check("admission refuses a crashed writer past its TTL", blocked, False)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.environ.pop("TWIN_BRIDGE_WRITE_TTL_S", None)
        os.environ.pop("TWIN_BRIDGE_WRITE_RENEW_S", None)
    cleared = coordination.reconcile_writes(account_id=ACCOUNT, fan_id=FAN)
    check("reconciliation clears the crashed writer's marker", cleared >= 1, True)
    got = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-crash",
        now=BASE_TIME, quiesce_timeout_s=1.0)
    check("and admission then succeeds", got, True)
    await coordination.release_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-crash", now=BASE_TIME)


async def rr1_settled_writer_releases() -> None:
    print("\nRR1: a proven-settled writer releases its marker (no reconciliation)")
    for mode, label in (("dm", "clean success"),
                        ("refused", "a provider 400 refusal"),
                        ("prefail", "a never-connected failure")):
        await _open_conversation(reserved=False)
        proc = await _sibling(mode)
        out, err, rc = await _sibling_finish(proc)
        if mode == "dm":
            check(f"{label}: the sibling POST completed", "POST_FINISHED" in out, True)
        elif mode == "refused":
            check(f"{label}: the sibling saw the refusal",
                  "WRITER_EXCEPTION:OFAPIError" in out, True)
        else:
            check(f"{label}: the sibling failed before connecting",
                  "WRITER_EXCEPTION:ConnectTimeout" in out, True)
        check(f"{label}: no marker is left",
              coordination._durable_write_fresh(ACCOUNT, FAN), False)
        got = await coordination.reserve_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-settled",
            now=BASE_TIME, quiesce_timeout_s=1.0)
        check(f"{label}: admission succeeds without reconciliation", got, True)
        await coordination.release_ownership(
            account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-settled", now=BASE_TIME)

    # A general ConnectionError can be raised AFTER the POST was transmitted, so
    # it must retain the marker (uncertain), not clear it.
    await _open_conversation(reserved=False)
    proc = await _sibling("connfail")
    out, err, rc = await _sibling_finish(proc)
    check("mid-post ConnectionError: the sibling saw the error",
          "WRITER_EXCEPTION:ConnectionError" in out, True)
    check("mid-post ConnectionError: the marker is RETAINED",
          coordination._durable_write_fresh(ACCOUNT, FAN), True)
    blocked = await coordination.reserve_ownership(
        account_id=ACCOUNT, fan_id=FAN, owner_token="bridge-settled",
        now=BASE_TIME, quiesce_timeout_s=1.0)
    check("mid-post ConnectionError: admission refuses", blocked, False)
    coordination.reconcile_writes(account_id=ACCOUNT, fan_id=FAN)


async def rr2_invalidation_first_at_db_boundary() -> None:
    print("\nRR2: an invalidation committed BEFORE admission yields no POST")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)  # draft_ready
    async with get_session() as s:
        row = await s.get(Message, (ACCOUNT, FAN, 5001))
        row.is_unsent = True
    report = await advance(twin, adapter, 12)
    check("no provider POST for an already-invalidated source",
          len(adapter.posts), 0)
    check("no attempt row was written", [], await attempts_for())
    conv = await conv_for()
    check("and no budget unit was spent", int(conv.send_budget_used), 0)
    check("the refusal names the source",
          any("source-invalidated" in r for r in report.reasons), True)


async def rr2_admission_ordering_holds() -> None:
    print("\nRR2: admission holds real write ordering across its source read")
    await reset_all()
    await freeze()
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 9)  # draft_ready
    path = coordination.sqlite_path()
    check("a SQLite coordination store is in use", bool(path), True)

    go = threading.Event()
    done = threading.Event()
    result: dict = {}

    def competitor() -> None:
        go.wait(timeout=10)
        con = None
        try:
            con = sqlite3.connect(path, timeout=0.3, isolation_level=None)
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE messages SET is_unsent = 1 "
                "WHERE account_id = ? AND fan_id = ? AND message_id = ?",
                (ACCOUNT, FAN, 5001),
            )
            con.execute("COMMIT")
            result["error"] = None
        except sqlite3.OperationalError as exc:
            result["error"] = str(exc)
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            done.set()

    def seam() -> None:
        go.set()
        done.wait(timeout=10)

    thread = threading.Thread(target=competitor)
    thread.start()
    delivery.ADMISSION_ORDERING_SEAM = seam
    try:
        report = await advance(twin, adapter, 12)
    finally:
        delivery.ADMISSION_ORDERING_SEAM = None
        thread.join(timeout=10)
    check("a competing invalidation is BLOCKED inside the admission window",
          "locked" in (result.get("error") or ""), True)
    check("admission won and POSTed exactly once", len(adapter.posts), 1)
    check("and the source check did not refuse",
          any("source-invalidated" in r for r in report.reasons), False)


async def rr2_source_unsent_during_status() -> None:
    print("\nRR2: a source invalidated during the Twin status await yields zero POSTs")
    await reset_all()
    await freeze()

    class UnsendDuringStatus(FakeTwin):
        async def read_status(self, event):
            if self.acks == 0:
                async with get_session() as s:
                    row = await s.get(Message, (ACCOUNT, FAN, 5001))
                    row.is_unsent = True
            return await super().read_status(event)

    twin, adapter = UnsendDuringStatus(), FakeAdapter()
    await advance(twin, adapter, 9)
    await advance(twin, adapter, 12)
    check("no POST is made for a source unsent during the status await",
          len(adapter.posts), 0)
    check("and the turn is parked source-invalidated",
          (await turns_for())[0].reason, "source-invalidated")


async def rr2_cancel_late_admission() -> None:
    print("\nRR2: a lost submit is reconciled across a not-found (late admission)")
    await reset_all()
    await freeze()

    class LateAdmission(FakeTwin):
        async def request_draft(self, event):
            self.drafts += 1
            raise TwinClientError(CATEGORY_UNCERTAIN, uncertain=True)

        async def cancel_request(self, event):
            self.cancels += 1
            if self.cancels == 1:
                raise TwinClientError("not-found", uncertain=False)
            return await super().cancel_request(event)

    twin, adapter = LateAdmission(), FakeAdapter()
    await advance(twin, adapter, 9)
    check("the uncertain submit returned no request id",
          (await turns_for())[0].request_id, None)
    await coordination.mark_cancelled(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=10))
    for seconds in (12, 20, 30, 200):
        await advance(twin, adapter, seconds)
    check("the first not-found did NOT end reconciliation", twin.cancels >= 2, True)
    check("a status read reconciled the frozen identity", twin.status_reads >= 1, True)
    check("the turn is parked, never dispatched",
          (await turns_for())[0].state, "parked")
    check("with zero provider POSTs", len(adapter.posts), 0)


async def rr2_latest_control_boundary() -> None:
    print("\nRR2: a newer cancel boundary invalidates a newer draft")
    await reset_all()
    await freeze()
    await coordination.pause_conversation(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=10),
        reason="operator-pause")
    await coordination.resume_conversation(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=20))
    twin, adapter = FakeTwin(), FakeAdapter()
    await advance(twin, adapter, 21)
    await seed_turn(message_id=5002, received_at=BASE_TIME + timedelta(seconds=30))
    await advance(twin, adapter, 40)
    newest = next(t for t in await turns_for() if "5002" in t.member_ids_json)
    check("the post-resume input is drafted", newest.state, "draft_ready")
    await coordination.mark_cancelled(
        account_id=ACCOUNT, fan_id=FAN, now=BASE_TIME + timedelta(seconds=41))
    cancels_before = twin.cancels
    await advance(twin, adapter, 42)
    await advance(twin, adapter, 45)
    newest = next(t for t in await turns_for() if "5002" in t.member_ids_json)
    check("the newer cancel invalidates the newer draft", newest.state != "draft_ready", True)
    check("and the newer draft was reconciled with Twin",
          twin.cancels > cancels_before, True)
    check("with zero provider POSTs", len(adapter.posts), 0)


async def rr4_worker_history_identity() -> None:
    print("\nRR4: the worker refuses history authored by neither fan nor creator")
    await reset_all()
    await freeze()
    adapter = FakeAdapter(history=[{"id": 123, "isFromMe": False,
                                    "fromUser": {"id": 424242}}])
    twin = FakeTwin()
    await advance(twin, adapter, 9)
    report = await advance(twin, adapter, 12)
    check("ambiguous history produces no POST", len(adapter.posts), 0)
    check("and the draft is parked history-unreadable",
          "history-unreadable" in report.reasons, True)


async def rr5_above_cap() -> None:
    print("\nRR5: a burst larger than the walk cap is not frozen while open")
    await reset_all()
    stamp = _h.CREATOR
    cutoff = scope().activation_cutoff
    values = []
    for i in range(5001):
        ts = BASE_TIME + timedelta(seconds=2 * i)
        values.append(dict(
            account_id=ACCOUNT, fan_id=FAN, provider_message_id=20000 + i,
            creator_slug=stamp, activation_cutoff=cutoff, conversation_id=str(FAN),
            normalized_text="x", event_json="{}", occurred_at=ts,
            received_at=ts, admitted_at=ts, state="pending"))
    async with get_session() as s:
        await s.execute(_h.TwinBridgeInput.__table__.insert(), values)

    report = await turns.form_turns(
        now=BASE_TIME + timedelta(seconds=10006), batch_size=2, scope=scope())
    check("an above-cap burst is not frozen while its true tail is fresh",
          report.turns_frozen, 0)
    check("and is held open", report.held_open, 1)

    total = 0
    for _ in range(12):
        # A fresh process between passes must not lose the continuation: all of
        # the burst state is durable, so draining survives a restart.
        _clear_process_registry()
        report = await turns.form_turns(
            now=BASE_TIME + timedelta(seconds=10007), batch_size=2, scope=scope())
        total += report.turns_frozen
        if await _pending_count() == 0:
            break
    check("an above-cap burst makes progress once its tail is quiet", total >= 1, True)
    check("and every receipt is eventually frozen", await _pending_count(), 0)
    members = 0
    for t in await turns_for():
        members += len(json.loads(t.member_ids_json))
    check("with all 5001 receipts assigned to a turn", members, 5001)


async def _pending_count() -> int:
    from sqlalchemy import func, select as _select
    async with get_session() as s:
        return int((await s.execute(
            _select(func.count()).select_from(_h.TwinBridgeInput).where(
                _h.TwinBridgeInput.state == "pending"))).scalar() or 0)


async def main() -> None:
    await _h.init_db()
    os.environ.update(ENV)
    await rr1_cross_process_admission()
    await rr1_expired_authority()
    await rr1_sibling_writer_wins()
    await rr1_bridge_wins()
    await rr1_broadcast_conflicts()
    await rr1_marker_renewal()
    await rr1_coordination_failure()
    await rr1_uncertain_writer_blocks_until_reconciled()
    await rr1_crashed_writer_blocks_after_ttl()
    await rr1_settled_writer_releases()
    await rr2_invalidation_first_at_db_boundary()
    await rr2_admission_ordering_holds()
    await rr2_source_invalidated_before_admission()
    await rr2_source_unsent_during_status()
    await rr2_cancel_late_admission()
    await rr2_latest_control_boundary()
    await rr4_known_id_contradictory_author()
    await rr4_worker_history_identity()
    await rr5_above_cap()
    await r1_durable_ownership()
    await r1_replacement_and_lease()
    await r1_manual_takeover()
    await r2_obsolete_work()
    await r3_recovery()
    await r4_evidence_and_text()
    await r5_burst_boundary()
    await r6_cli_entry_point()
    await rr1_takeover_race()
    await rr1_admission_race()
    await rr1_cooldown()
    await rr1_route_seam()
    await rr2_cancel_sent_ack()
    await rr2_resume_input_boundary()
    await rr2_source_unsent_in_flight()
    await rr2_uncertain_cancel_reconcile()
    await rr4_strict_evidence()
    await rr5_hard_cap()
    await rr6_explicit_limit()


try:
    run(main())
except BaseException as exc:  # noqa: BLE001
    failures.append(f"suite crashed: {type(exc).__name__} ({exc})")
    print(f"  FAIL suite crashed: {type(exc).__name__}: {exc}")

run(_h.engine.dispose())
_h.shutil.rmtree(_h._TMP_ROOT, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge ownership/repairs)")
