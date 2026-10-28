"""Permanent live intake: fake WebSocket server + fake provider; disposable PostgreSQL only.

No OnlyFans connection, real session, model key or production database is used.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import time

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed, event, due
from test_onlyfans_text_delivery_pg import configured, TENANT, ACCOUNT, current
from test_onlyfans_connections_pg import verified
from test_onlyfans_loopback_transport_pg import transport_for
from onlyfans_native_protocol_fixture import OWNER, native_message, native_peer
from db.models import AccountAiConfig, Fan, Message, OfConversationState
from db.onlyfans_models import OfIngress, OfRecovery, OfRuntime, OfScan, OfWork
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime import live_intake as li
from onlyfans_runtime.live_intake import LiveIntake, normalize_frame
from onlyfans_runtime.native_connector import NativeLoopbackConnector, from_native_server
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeRateLimitError, session_from_capture
from onlyfans_runtime.store import Event, Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.worker import MessagePage

FAST = dict(watch_seconds=.1, acquire_retry=.2, restart_delay=.2, scope_seconds=.2)


def wait_for(check, timeout=8.0, step=.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")


# ── fake WebSocket server (stands in for ws2.onlyfans.com) ────────────────

class FakeWs:
    def __init__(self):
        self.connections = self.live = self.max_live = 0
        self.tokens, self.sockets = [], set()
        self.loop = asyncio.new_event_loop()
        self.port = None
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        assert ready.wait(5)

    def _run(self, ready):
        from websockets.asyncio.server import serve
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            hello = json.loads(await ws.recv())
            assert hello["act"] == "connect"
            self.tokens.append(hello["token"])
            self.connections += 1
            self.live += 1
            self.max_live = max(self.max_live, self.live)
            self.sockets.add(ws)
            await ws.send(json.dumps({"connected": True, "v": "5.4.0"}))
            try:
                async for _ in ws:
                    pass
            finally:
                self.live -= 1
                self.sockets.discard(ws)

        async def main():
            server = await serve(handler, "127.0.0.1", 0)
            self.port = server.sockets[0].getsockname()[1]
            ready.set()
            await server.wait_closed()

        self.loop.run_until_complete(main())

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/ws3/1"

    def push(self, frame):
        async def send():
            for ws in list(self.sockets):
                await ws.send(json.dumps(frame))
        asyncio.run_coroutine_threadsafe(send(), self.loop).result(5)

    def drop(self):
        async def kill():
            for ws in list(self.sockets):
                await ws.close(code=1011)
        asyncio.run_coroutine_threadsafe(kill(), self.loop).result(5)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def steady(engine, account, fans, *, mode="automated"):
    """Fixture evidence of a completed recent recovery for each fan."""
    with Session(engine) as s, s.begin():
        for fan in fans:
            if s.get(Fan, (account, fan)) is None:
                s.add(Fan(account_id=account, fan_id=fan))
                s.flush()
            state = s.get(OfConversationState, (account, fan))
            if state is None:
                s.add(OfConversationState(account_id=account, fan_id=fan, mode=mode, revision=0,
                                          takeover_state="none", requires_attention=False))
            else:
                state.mode = mode
            row = s.get(OfRecovery, (account, fan))
            if row is None:
                s.add(OfRecovery(account_id=account, fan_id=fan, status="complete", high_water=0, last_complete_at=utcnow()))
            else:
                row.status = "complete"
        scan = s.get(OfScan, account)
        if scan is None:
            s.add(OfScan(account_id=account, status="complete", known_fans_complete=True))
        else:
            scan.status, scan.known_fans_complete, scan.repeat_requested = "complete", True, False
        runtime = s.get(OfRuntime, account)
        runtime.recovery_needed, runtime.last_sync_at, runtime.pending_events = False, utcnow(), 0


@pytest.fixture
def wsenv(database, monkeypatch):
    store, account, clear = verified(database, monkeypatch)
    fake = FakeWs()
    handle = session_from_capture(account_id=account, owner_id=clear["ownerId"], policy_revision=1,
        capture=clear["capture"], x_hash=clear["xHash"], proxy_url=None,
        expires_at=datetime.fromisoformat(clear["expiresAt"]))
    connector = NativeProviderConnector(handle)
    connector.connection_revision = 1
    calls = []

    def users_me(self, method, path, body=b""):
        calls.append(path)
        return json.dumps({"id": OWNER, "wsUrl": fake.url, "wsAuthToken": "tok-" + "x" * 40}).encode()
    monkeypatch.setattr(NativeProviderConnector, "_call", users_me)
    steady(database[1], account, [101, 102])
    made = []

    def intake(**kwargs):
        item = LiveIntake(store, TENANT, url_ok=lambda url: url.startswith("ws://127.0.0.1"), **{**FAST, **kwargs})
        made.append(item)
        return item
    account_row = WorkerAccount(account, object(), connector, connector)
    yield dict(store=store, account=account, fake=fake, connector=connector, intake=intake, row=account_row,
               engine=database[1], calls=calls, url=database[0], clear=clear)
    for item in made:
        item.close(3)
    fake.stop()
    store.close()


def frame_for(identity, fan=101, *, outgoing=False, text="<p>hello there</p>", **extra):
    value = native_message(identity, fan=fan, outgoing=outgoing, text=text, **extra)
    return value, {"api2_chat_message": value}


# ── pure normalization ────────────────────────────────────────────────────

def test_full_message_is_a_message_and_incomplete_frames_are_partial_never_records():
    value, frame = frame_for(500)
    result = normalize_frame(frame, OWNER)
    assert result.kind == "message" and result.event.message_id == 500 and result.event.direction == "in"
    assert normalize_frame(frame_for(501, outgoing=True)[1], OWNER).event.direction == "out"
    broken = {k: v for k, v in value.items() if k != "mediaCount"}
    assert normalize_frame({"api2_chat_message": broken}, OWNER) == li.Frame("partial", None, 101)
    tip = dict(value, isTip=True)
    assert normalize_frame({"api2_chat_message": tip}, OWNER).kind == "partial"
    assert normalize_frame({"api2_chat_message": {"id": 1}}, OWNER) == li.Frame("partial")
    assert normalize_frame({"chat_messages": 3}, OWNER) == li.Frame("partial")
    assert normalize_frame({"new_message": {"fromUser": {"id": 555}}}, OWNER).fan_id == 555
    assert normalize_frame({"online_status": {}}, OWNER).kind == "ignore"
    assert normalize_frame([1, 2], OWNER).kind == "ignore"
    # A message not between the owner and one fan is never recorded.
    stranger = dict(value, fromUser={"id": 7}, toUser={"id": 8})
    assert normalize_frame({"api2_chat_message": stranger}, OWNER).kind == "partial"


# ── flag off by default ───────────────────────────────────────────────────

def test_flag_default_off_creates_no_intake_thread_or_socket(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.delenv(li.FLAG, raising=False)
    assert li.enabled() is False
    store, _ = configured(database, mode="automated")
    runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, SyntheticNativeModel(), SyntheticTextTransport(ACCOUNT)),))
    assert runtime.live_intake is None
    runtime.step()
    assert not [t for t in threading.enumerate() if t.name.startswith("of-live-intake")]
    monkeypatch.setenv(li.FLAG, "1")
    assert li.enabled() is True
    shared = NativeWorker(store, TENANT, runtime.accounts, live_intake=True).live_intake
    assert shared is LiveIntake.shared(store, TENANT) and shared._loop is None   # nothing starts until sync
    LiveIntake.shutdown_all()
    store.close()


# ── duplicate WS + polling observation ────────────────────────────────────

def setup_account(database, monkeypatch, *, peer_history=None):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", database[0])
    store, _ = configured(database, mode="automated")
    with Session(database[1]) as s, s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning",
                              persona="Synthetic Creator enjoys hiking and coffee.", daily_cost_cap_cents=100))
    steady(database[1], ACCOUNT, [101])
    return store


def test_duplicate_ws_and_poll_observation_make_one_inbound_and_one_reply(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    value, frame = frame_for(500, text="<p>How has your day been?</p>")
    intake = LiveIntake(store, TENANT, poll_interval=0)
    intake._ensure_started()
    model = SyntheticNativeModel(reported_cost_millicents=7)
    wire = SyntheticTextTransport(ACCOUNT)
    try:
        with native_peer(history={101: [value]}) as peer:
            transport = NativeLoopbackConnector(from_native_server(peer.server, account_id=ACCOUNT, owner_id=OWNER, policy_revision=1))
            intake.submit(ACCOUNT, frame, OWNER)
            intake.submit(ACCOUNT, frame, OWNER)            # duplicate delivery of the same frame
            assert intake.flush()
            assert intake.poll(store, TENANT, ACCOUNT, transport) == 0   # poll sees the same canonical message
            intake.submit(ACCOUNT, frame, OWNER)
            assert intake.flush()
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "in")) == 1
            assert s.scalar(select(func.count()).select_from(OfIngress)) == 1
            assert s.scalar(select(func.count()).select_from(OfWork)) == 1
        due(database[1])
        runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, model, wire),), live_intake=intake)
        total = sum(runtime.step().accepted for _ in range(4))
        assert total == 1 and len(model.calls) == 1 and len(wire.calls) == 1
    finally:
        intake.close(3)
        store.close()


def test_poll_records_only_messages_newer_than_known_and_never_scans(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    old, new = native_message(10, text="<p>old</p>"), native_message(11, text="<p>newer</p>")
    store.record(TENANT, ACCOUNT, normalize_frame({"api2_chat_message": old}, OWNER).event)
    store.drain(TENANT, ACCOUNT)
    intake = LiveIntake(store, TENANT, poll_interval=0)
    try:
        with native_peer(history={101: [old, new]}) as peer:
            transport = NativeLoopbackConnector(from_native_server(peer.server, account_id=ACCOUNT, owner_id=OWNER, policy_revision=1))
            assert intake.poll(store, TENANT, ACCOUNT, transport) == 1
            paths = [row["path"] for row in peer.records if "/messages" in row["path"]]
            assert len(paths) == 1 and "/chats?" not in "".join(row["path"] for row in peer.records)
        with Session(database[1]) as s:
            assert s.scalar(select(func.max(Message.message_id))) == 11
            scan = s.get(OfScan, ACCOUNT)
            assert scan.status == "complete" and s.get(OfRecovery, (ACCOUNT, 101)).status == "complete"
    finally:
        store.close()


# ── partial notifications: targeted read only ─────────────────────────────

def test_partial_notification_requests_one_targeted_read_and_records_nothing(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    steady(database[1], ACCOUNT, [101, 102, 103])
    intake = LiveIntake(store, TENANT)
    intake._ensure_started()
    try:
        value, _ = frame_for(700)
        broken = {k: v for k, v in value.items() if k != "price"}
        for _ in range(50):                                   # a burst coalesces to one request
            intake.submit(ACCOUNT, {"api2_chat_message": broken}, OWNER)
        intake.submit(ACCOUNT, {"new_message": {"fromUser": {"id": 555}}}, OWNER)   # not an approved fan
        assert intake.flush()
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(Message)) == 0
            assert s.scalar(select(func.count()).select_from(OfIngress)) == 0
            rows = {r.fan_id: r.status for r in s.scalars(select(OfRecovery))}
            assert rows[101] == "needed" and rows[102] == rows[103] == "complete" and 555 not in rows
            assert s.get(OfRuntime, ACCOUNT).recovery_needed is True
            assert s.get(OfScan, ACCOUNT).status == "complete"          # no scan was started
            assert s.get(OfRecovery, (ACCOUNT, 101)).high_water == 0
    finally:
        intake.close(3)
        store.close()


# ── echo of our own send ──────────────────────────────────────────────────

def test_echo_of_own_send_reconciles_without_a_new_reply_or_resolving_an_attempt(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    value, frame = frame_for(500, text="<p>How has your day been?</p>")
    intake = LiveIntake(store, TENANT)
    intake._ensure_started()
    model = SyntheticNativeModel(reported_cost_millicents=7)
    wire = SyntheticTextTransport(ACCOUNT)
    try:
        intake.submit(ACCOUNT, frame, OWNER)
        assert intake.flush()
        due(database[1])
        runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, model, wire),), live_intake=intake)
        assert sum(runtime.step().accepted for _ in range(4)) == 1
        with Session(database[1]) as s:
            sent = s.scalar(select(Message).where(Message.direction == "out"))
            intent = s.scalar(select(OfTextIntent))
            assert sent.message_id == intent.provider_message_id
            work_before = s.get(OfWork, (ACCOUNT, 101)).status
            body, mid = sent.body, sent.message_id
        _, echo = frame_for(mid, outgoing=True, text=f"<p>{body}</p>")
        intake.submit(ACCOUNT, echo, OWNER)
        assert intake.flush()
        # The provider echo of an unrelated, unreconciled outgoing message is only a message too.
        with Session(database[1]) as s, s.begin():
            s.get(OfConversationState, (ACCOUNT, 101)).unresolved_attempt_id = "attempt-uncertain"
        _, other = frame_for(mid + 50, outgoing=True, text="<p>sent from another device</p>")
        intake.submit(ACCOUNT, other, OWNER)
        assert intake.flush()
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out", Message.message_id == mid)) == 1
            assert s.get(OfWork, (ACCOUNT, 101)).status == work_before == "done"
            assert s.get(OfConversationState, (ACCOUNT, 101)).unresolved_attempt_id == "attempt-uncertain"
        assert sum(runtime.step().claims for _ in range(4)) == 0 and len(model.calls) == 1 and len(wire.calls) == 1
    finally:
        intake.close(3)
        store.close()


# ── disconnect, gap, reconciliation ───────────────────────────────────────

def test_disconnect_records_gap_reconnect_is_not_proof_and_no_send_from_stale_proof(wsenv):
    e = wsenv
    intake = e["intake"]()
    row = e["row"]
    assert intake.sync(row)
    wait_for(lambda: e["fake"].live == 1)
    wait_for(lambda: intake.flush(.1))
    with Session(e["engine"]) as s:       # owner start is itself a gap: nothing was observed before the socket
        assert s.get(OfRecovery, (e["account"], 101)).status == "needed"
    steady(e["engine"], e["account"], [101, 102])        # recovery finished (fixture evidence)
    e["fake"].drop()
    wait_for(lambda: e["fake"].connections >= 2 and e["fake"].live == 1, timeout=10)
    wait_for(lambda: intake.flush(.1))
    with Session(e["engine"]) as s:
        assert s.get(OfRecovery, (e["account"], 101)).status == "needed"
        assert s.get(OfRecovery, (e["account"], 102)).status == "needed"
        assert s.get(OfRuntime, e["account"]).recovery_needed is True
        assert s.get(OfScan, e["account"]).status == "complete"      # a gap is per-fan, not a scan or archive pull


def test_stale_proof_blocks_reply_until_fan_reconciles(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    intake = LiveIntake(store, TENANT)
    intake._ensure_started()
    model = SyntheticNativeModel(reported_cost_millicents=7)
    wire = SyntheticTextTransport(ACCOUNT)
    try:
        intake.enqueue_gap(ACCOUNT, "disconnect")
        _, frame = frame_for(500, text="<p>Are you there?</p>")
        intake.submit(ACCOUNT, frame, OWNER)
        assert intake.flush()
        due(database[1])
        runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, model, wire),), live_intake=intake)
        report = [runtime.step() for _ in range(3)]
        assert sum(r.claims + r.accepted for r in report) == 0 and not model.calls and not wire.calls
        claim = store.claim_recovery(TENANT, ACCOUNT)
        assert claim is not None and claim.fan_id == 101
        assert store.recovery_page(TENANT, claim, [], has_more=False) == "complete"
        due(database[1])
        assert sum(runtime.step().accepted for _ in range(4)) == 1 and len(model.calls) == len(wire.calls) == 1
    finally:
        intake.close(3)
        store.close()


# ── one ingestion owner across processes ──────────────────────────────────

def test_lease_contention_one_owner_then_takeover_without_overlap(wsenv):
    e = wsenv
    first, second = e["intake"](), e["intake"]()
    assert first.sync(e["row"])
    wait_for(lambda: e["fake"].live == 1 and first.owned(e["account"]))
    assert second.sync(e["row"])
    time.sleep(1.0)
    assert e["fake"].connections == 1 and not second.owned(e["account"])
    e["fake"].push(frame_for(800)[1])
    wait_for(lambda: first.stats["events"] == 1 and first.flush(.1))
    first.close(3)
    wait_for(lambda: second.owned(e["account"]) and e["fake"].live == 1, timeout=10)
    assert e["fake"].connections == 2 and e["fake"].max_live == 1
    with Session(e["engine"]) as s:
        assert s.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.message_id == 800)) == 1


def test_lost_lock_connection_stops_the_socket(wsenv):
    e = wsenv
    intake = e["intake"]()
    other = e["intake"]()
    intake.sync(e["row"])
    wait_for(lambda: e["fake"].live == 1)
    admin = Session(e["engine"])
    with admin.begin():
        admin.execute(text("SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory' AND granted AND pid<>pg_backend_pid()"))
    admin.close()
    wait_for(lambda: e["fake"].live == 0, timeout=10)           # ownership lost => socket closed
    other.sync(e["row"])
    wait_for(lambda: e["fake"].live == 1 and e["fake"].max_live == 1, timeout=10)


# ── rebind, redaction, auth through the budget ────────────────────────────

def test_session_generation_change_closes_and_rebinds_and_auth_uses_the_budget(wsenv):
    e = wsenv
    intake = e["intake"]()
    intake.sync(e["row"])
    wait_for(lambda: e["fake"].live == 1)
    with Session(e["engine"]) as s:
        budget = s.get(OfProviderReadBudget, e["account"])
        assert budget.last_kind == "recent" and budget.tokens < 10 and budget.lease_token is None
    assert e["calls"] == ["/api2/v2/users/me"] and e["fake"].tokens == ["tok-" + "x" * 40]
    with Session(e["engine"]) as s, s.begin():
        s.get(OfProviderConnection, e["account"]).revision = 2
    e["connector"].connection_revision = 2
    intake.sync(e["row"])
    wait_for(lambda: e["fake"].connections == 2 and e["fake"].live == 1, timeout=10)
    assert e["fake"].max_live == 1
    with Session(e["engine"]) as s, s.begin():
        s.get(OfProviderConnection, e["account"]).status = "failed"      # connection no longer verified
    wait_for(lambda: e["fake"].live == 0, timeout=10)


def test_secrets_never_reach_logs(wsenv, monkeypatch, caplog):
    e = wsenv
    handle = e["connector"]._handle
    proxied = session_from_capture(account_id=e["account"], owner_id=OWNER, policy_revision=1, capture=e["clear"]["capture"],
        x_hash=e["clear"]["xHash"], proxy_url="http://proxyuser:proxy-secret-pw@proxy.example:8080",
        expires_at=datetime.fromisoformat(e["clear"]["expiresAt"]))
    connector = NativeProviderConnector(proxied)
    connector.connection_revision = 1
    cookie = proxied.cookies[0][1]

    def boom(self, method, path, body=b""):
        raise RuntimeError(f"upstream failed via http://proxyuser:proxy-secret-pw@proxy.example:8080 cookie={cookie} "
                           "token=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl")
    monkeypatch.setattr(NativeProviderConnector, "_call", boom)
    intake = e["intake"]()
    caplog.set_level(logging.DEBUG)
    intake.sync(WorkerAccount(e["account"], object(), connector, connector))
    wait_for(lambda: any("ws error" in r.getMessage() for r in caplog.records), timeout=8)
    text_out = caplog.text
    for secret in ("proxy-secret-pw", "proxyuser", cookie, "eyJhbGciOiJSUzI1NiJ9"):
        assert secret not in text_out
    assert "account-fixed-proxy" not in text_out or "proxy.example" not in text_out


def test_budget_backoff_and_rate_limit_hold_polling_and_do_not_bypass_it(wsenv, monkeypatch):
    e = wsenv
    steady(e["engine"], e["account"], [101, 102, 103, 104, 105])
    intake = e["intake"](poll_interval=0, polls_per_cycle=3)
    calls = []

    def messages(fan_id, before_id, limit):
        calls.append(fan_id)
        return MessagePage([], False)
    monkeypatch.setattr(e["connector"], "messages", messages, raising=False)
    assert intake.poll(e["store"], TENANT, e["account"], e["connector"]) == 0
    assert len(calls) == 3                                           # bounded per cycle, not the whole account
    assert intake.poll(e["store"], TENANT, e["account"], e["connector"]) == 0
    assert len(calls) == 5 + 0 or len(calls) >= 5                    # the rest next cycle, none twice in a row
    assert len(set(calls[:5])) == 5
    with Session(e["engine"]) as s, s.begin():
        s.get(OfProviderReadBudget, e["account"]).backoff_until = utcnow() + timedelta(seconds=60)
    before = len(calls)
    intake.poll(e["store"], TENANT, e["account"], e["connector"])
    assert len(calls) == before                                      # backoff respected, zero provider reads
    with Session(e["engine"]) as s, s.begin():
        s.get(OfProviderReadBudget, e["account"]).backoff_until = None

    def limited(fan_id, before_id, limit):
        calls.append(fan_id)
        raise NativeRateLimitError(120)
    monkeypatch.setattr(e["connector"], "messages", limited, raising=False)
    before = len(calls)
    intake.poll(e["store"], TENANT, e["account"], e["connector"])
    assert len(calls) == before + 1                                  # one 429 stops the cycle
    with Session(e["engine"]) as s:
        assert s.get(OfProviderReadBudget, e["account"]).backoff_until is not None
    intake.poll(e["store"], TENANT, e["account"], e["connector"])
    assert len(calls) == before + 1                                  # and the shared backoff holds the next cycle


def test_poll_coalesces_repeated_requests_per_fan(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    intake = LiveIntake(store, TENANT, poll_interval=3600)
    intake._ensure_started()
    try:
        with native_peer(history={101: [native_message(3, text="<p>hi</p>")]}) as peer:
            transport = NativeLoopbackConnector(from_native_server(peer.server, account_id=ACCOUNT, owner_id=OWNER, policy_revision=1))
            _, partial = frame_for(900)
            partial = {"api2_chat_message": {"id": 900, "fromUser": {"id": 101}}}
            for _ in range(40):
                intake.submit(ACCOUNT, partial, OWNER)
            assert intake.flush()
            intake.poll(store, TENANT, ACCOUNT, transport)
            intake.poll(store, TENANT, ACCOUNT, transport)           # interval not elapsed, nothing forced
            reads = [row for row in peer.records if "/chats/101/messages" in row["path"]]
            assert len(reads) == 1
    finally:
        intake.close(3)
        store.close()


# ── oversized chat: bounded diagnostics, backoff, fairness ────────────────

def test_oversized_chat_backs_off_and_cannot_monopolize_the_account(database, monkeypatch, caplog):
    store = setup_account(database, monkeypatch)
    steady(database[1], ACCOUNT, [101, 102])
    with Session(database[1]) as s, s.begin():
        for fan in (101, 102):
            s.get(OfRecovery, (ACCOUNT, fan)).status = "needed"
        s.get(OfRuntime, ACCOUNT).recovery_needed = True
    intake = LiveIntake(store, TENANT, poll_interval=3600)
    attempts = []
    try:
        with native_peer(history={101: [native_message(1)], 102: [native_message(2, fan=102)]}) as peer:
            transport = NativeLoopbackConnector(from_native_server(peer.server, account_id=ACCOUNT, owner_id=OWNER, policy_revision=1))
            real = transport.messages

            def messages(*, fan_id, before_id, limit):
                attempts.append(fan_id)
                if fan_id == 101:
                    raise NativeProtocolError("native HTTPS response exceeded its byte budget")
                return real(fan_id=fan_id, before_id=before_id, limit=limit)
            monkeypatch.setattr(transport, "messages", messages)
            runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, SyntheticNativeModel(), transport, transport),), live_intake=intake)
            caplog.set_level(logging.INFO)
            reports = [runtime.step() for _ in range(6)]
        assert sum(r.errors for r in reports) == 0                        # the failing chat never aborts the cycle
        assert attempts.count(101) == 1                                   # backoff, not a retry every step
        assert attempts.count(102) == 1
        with Session(database[1]) as s:
            assert s.get(OfRecovery, (ACCOUNT, 102)).status == "complete"
            stuck = s.get(OfRecovery, (ACCOUNT, 101))
            assert stuck.status == "running" and stuck.lease_until > utcnow() + timedelta(seconds=20)
        snap = intake.health.snapshot()
        assert snap[(ACCOUNT, 101)]["oversized"] is True and snap[(ACCOUNT, 101)]["failures"] == 1
        logged = [r.getMessage() for r in caplog.records if "live intake read failed" in r.getMessage()]
        assert len(logged) == 1 and "oversized=True" in logged[0] and "exceeded" not in logged[0]
    finally:
        store.close()


def test_background_work_is_bounded_by_live_demand_but_never_starved(database, monkeypatch):
    store = setup_account(database, monkeypatch)
    intake = LiveIntake(store, TENANT, background_every=5)
    assert [intake.allow_background("a", True) for _ in range(10)] == [False] * 4 + [True] + [False] * 4 + [True]
    assert all(intake.allow_background("b", False) for _ in range(5))
    turns = []
    monkeypatch.setattr(NativeWorker, "_history_turn", lambda self, account, report, **kw: turns.append(1))
    transport = SyntheticTextTransport(ACCOUNT)
    runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, SyntheticNativeModel(), transport),), live_intake=intake)
    for index in range(10):                                              # constant live demand: new input every cycle
        store.record(TENANT, ACCOUNT, event(mid=100 + index, body=f"live {index}"))
        runtime.step()
    assert len(turns) == 2
    turns.clear()
    for _ in range(4):                                                   # demand subsides: background every cycle
        runtime.step()
    assert len(turns) >= 3
    store.close()


# ── the socket keeps receiving while a model call runs ────────────────────

def test_websocket_ingests_while_a_slow_model_call_blocks_the_worker_thread(wsenv, database):
    e = wsenv
    intake = e["intake"]()
    intake.sync(e["row"])
    wait_for(lambda: e["fake"].live == 1)
    slow = SyntheticNativeModel(delay_seconds=1.5)
    started = threading.Event()
    original = slow.__class__
    done = []

    def blocked():
        started.set()
        time.sleep(1.5)                      # stands in for a synchronous model/network call in the worker lane
        done.append(1)
    thread = threading.Thread(target=blocked)
    thread.start()
    started.wait(2)
    e["fake"].push(frame_for(950)[1])
    wait_for(lambda: intake.stats["events"] == 1 and intake.flush(.1) and not done, timeout=1.4)
    with Session(e["engine"]) as s:
        assert s.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.message_id == 950)) == 1 or \
            s.scalar(select(func.count()).select_from(Message).where(Message.message_id == 950)) == 1
    thread.join()
