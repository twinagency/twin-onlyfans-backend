"""Actual loopback HTTP delivery rehearsal against a disposable PostgreSQL DB.

The peer is an explicitly created local ThreadingHTTPServer. No provider
endpoint, credentials, model, proxy, or external network is used.
"""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import pickle
import socket
import threading
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import (
    ACCOUNT, FAN, TENANT, assert_no_outbound, configured, prepare, unthrottle,
)
from db.models import Message, OfConversationState, OfMessageDelivery
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.store import Refused, Store
from onlyfans_runtime.text_delivery import Accepted, Dispatch, TextAuthority
from onlyfans_runtime.loopback_transport import LoopbackTextTransport, from_server


PEER_HEADER = "X-Twin-Rehearsal-Peer"
WIRE_PATH = "/twin-rehearsal/v1/text"
SCHEMA = "of-wire-rehearsal.v1"


class Peer:
    """Fixture controller; the HTTP server itself retains its exact stdlib type."""

    def __init__(self, mode="accepted", *, on_request=None, location=None):
        self.mode, self.on_request, self.location = mode, on_request, location
        self.records = []
        self.lock = threading.Lock()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                record = {"path": self.path, "raw": raw,
                          "headers": {key.lower(): value for key, value in self.headers.items()}}
                try:
                    request = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    request = {}
                record["json"] = request
                with peer.lock:
                    peer.records.append(record)
                    count = len(peer.records)
                if peer.on_request is not None:
                    peer.on_request(record)
                if peer.mode == "accepted_drop":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if peer.mode == "slow_headers":
                    time.sleep(.6)
                reply = {
                    "schemaVersion": SCHEMA,
                    "accountId": request.get("accountId"),
                    "fanId": request.get("fanId"),
                    "intentId": request.get("intentId"),
                    "attemptId": request.get("attemptId"),
                    "payloadHash": request.get("payloadHash"),
                    "policyRevision": request.get("policyRevision"),
                    "outcome": "not_accepted" if peer.mode.startswith("not_accepted") else "accepted",
                    "occurredAt": datetime.now(timezone.utc).isoformat(),
                    "evidenceRef": "loopback-proof:" + str(request.get("attemptId")),
                }
                if not peer.mode.startswith("not_accepted") or peer.mode == "not_accepted_extra_message_id":
                    reply["messageId"] = 9000 + count
                mutations = {
                    "wrong_schema": ("schemaVersion", "of-wire-rehearsal.v99"),
                    "wrong_account": ("accountId", "acc-b"),
                    "wrong_fan": ("fanId", FAN + 1),
                    "wrong_intent": ("intentId", "different-intent"),
                    "wrong_attempt": ("attemptId", "different-attempt"),
                    "wrong_hash": ("payloadHash", "0" * 64),
                    "not_accepted_wrong_hash": ("payloadHash", "0" * 64),
                    "wrong_generation": ("policyRevision", 2),
                    "bool_generation": ("policyRevision", True),
                    "float_generation": ("policyRevision", 1.0),
                    "bool_message_id": ("messageId", True),
                    "float_message_id": ("messageId", 9001.0),
                    "nan_message_id": ("messageId", float("nan")),
                    "naive_timestamp": ("occurredAt", "2026-10-18T00:00:00"),
                    "unknown_outcome": ("outcome", "failed"),
                    "extra_key": ("unexpected", "unreviewed field"),
                    "oversize": ("evidenceRef", "x" * 70000),
                }
                if peer.mode in mutations:
                    key, value = mutations[peer.mode]
                    reply[key] = value
                if peer.mode == "missing_message_id":
                    reply.pop("messageId")
                body = json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if peer.mode == "duplicate_key":
                    body = b'{"outcome":"not_accepted",' + body[1:]
                if peer.mode == "invalid_json":
                    body = b"not-json"
                if peer.mode == "compressed":
                    body = gzip.compress(body)
                status = {"redirect": 307, "http_429": 429, "http_500": 500}.get(peer.mode, 200)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain" if peer.mode == "wrong_content_type" else "application/json")
                    if peer.mode != "streamed_over_limit":
                        self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    if peer.mode != "missing_peer":
                        self.send_header(PEER_HEADER, "wrong-peer" if peer.mode == "wrong_peer" else self.headers.get(PEER_HEADER, ""))
                    if peer.mode == "compressed":
                        self.send_header("Content-Encoding", "gzip")
                    if peer.mode == "redirect":
                        self.send_header("Location", peer.location)
                    if peer.mode == "set_cookie":
                        self.send_header("Set-Cookie", "private_fixture_cookie=must-not-be-forwarded; Path=/")
                    self.end_headers()
                    if peer.mode == "drip_body":
                        for byte in body:
                            self.wfile.write(bytes([byte])); self.wfile.flush()
                            time.sleep(.03)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


@contextmanager
def peer_server(mode="accepted", **kwargs):
    peer = Peer(mode, **kwargs).start()
    try:
        yield peer
    finally:
        peer.close()


def transport_for(peer, **kwargs):
    return LoopbackTextTransport(from_server(peer.server, account_id=ACCOUNT, policy_revision=1), **kwargs)


def assert_unknown_retained(engine, intent):
    assert_no_outbound(engine)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert row.status == "unknown" and row.reservation_held
        assert row.provider_message_id is None and row.projected_at is None
        assert row.wire_started_at is not None
        assert policy.used_units == 1 and policy.active_intent_id == intent
        assert state.unresolved_attempt_id == row.attempt_id and state.requires_attention


def test_actual_http_peer_preserves_exact_text_scope_and_commits_wire_marker_before_post(database):
    _, engine = database
    store, authority = configured(database)
    body = "  Hello 👩🏽‍🚀 cafe\u0301\n<script>literal text</script>\t & goodbye  "
    intent = prepare(authority, body=body)
    checked = []

    def check_committed(record):
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            assert c.scalar(text("SELECT account_id FROM of_runtime WHERE account_id=:id FOR UPDATE NOWAIT"), {"id": ACCOUNT}) == ACCOUNT
            row = c.execute(text("SELECT status,wire_started_at,dispatch_token FROM of_text_intent WHERE account_id=:account AND intent_id=:intent"), {"account": ACCOUNT, "intent": intent}).mappings().one()
            assert row["status"] == "dispatching" and row["wire_started_at"] is not None
            assert row["dispatch_token"].encode() not in record["raw"]
        checked.append(True)

    with peer_server(on_request=check_committed) as peer:
        transport = transport_for(peer)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "accepted"
        assert checked == [True] and len(peer.records) == 1
        record = peer.records[0]
        request = record["json"]
        assert record["path"] == WIRE_PATH
        assert set(request) == {"schemaVersion", "accountId", "fanId", "intentId", "attemptId", "payloadHash", "policyRevision", "payload"}
        assert request["schemaVersion"] == SCHEMA
        assert (request["accountId"], request["fanId"], request["intentId"], request["policyRevision"]) == (ACCOUNT, FAN, intent, 1)
        assert request["payload"] == {"account_id": ACCOUNT, "fan_id": FAN, "text": body, "media_ids": [], "price_cents": 0}
        assert "authorization" not in record["headers"] and "cookie" not in record["headers"]
        assert record["headers"]["accept-encoding"] == "identity"
        assert record["headers"]["content-type"].split(";")[0] == "application/json"
        assert TENANT.encode() not in record["raw"] and b"synthetic-operator" not in record["raw"]
        with Session(engine) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9001)).body == body
            assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001)).outcome == "sent"
            assert s.get(OfTextIntent, (ACCOUNT, intent)).wire_version == 1
    store.close()


@pytest.mark.parametrize("mode", [
    "missing_message_id", "bool_message_id", "float_message_id", "nan_message_id",
    "duplicate_key", "invalid_json", "wrong_schema", "wrong_account", "wrong_fan",
    "wrong_intent", "wrong_attempt", "wrong_hash", "wrong_generation", "bool_generation",
    "float_generation", "naive_timestamp", "unknown_outcome", "extra_key", "oversize",
    "compressed", "wrong_content_type", "missing_peer", "wrong_peer", "http_429", "http_500",
    "not_accepted_wrong_hash", "not_accepted_extra_message_id",
])
def test_malformed_or_unproven_http_response_holds_budget_without_history_or_retry(database, mode):
    _, engine = database
    store, authority = configured(database, limit=1)
    intent = prepare(authority)
    with peer_server(mode) as peer:
        transport = transport_for(peer)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
        assert len(peer.records) == 1
        assert_unknown_retained(engine, intent)
    store.close()


def test_only_explicit_matching_not_accepted_http_proof_refunds_once(database):
    _, engine = database
    store, authority = configured(database, limit=1)
    intent = prepare(authority)
    with peer_server("not_accepted") as peer:
        transport = transport_for(peer)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "not_accepted"
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "not_accepted"
        assert len(peer.records) == 1
        assert_no_outbound(engine)
        with Session(engine) as s:
            row = s.get(OfTextIntent, (ACCOUNT, intent))
            policy = s.get(OfDeliveryPolicy, ACCOUNT)
            assert row.receipt_json and not row.reservation_held and row.wire_started_at
            assert policy.used_units == 0 and policy.active_intent_id is None
            assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id is None
    store.close()


def test_raw_stream_byte_budget_is_enforced_without_content_length(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with peer_server("streamed_over_limit") as peer:
        # The response is valid acceptance JSON, but exceeds this explicit
        # 128-byte cap. There is no length header to provide an earlier check.
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport_for(peer, max_response_bytes=128)) == "unknown"
        assert len(peer.records) == 1
        assert_unknown_retained(engine, intent)
    store.close()


def test_peer_accepting_then_losing_http_response_stays_unknown_across_restart(database):
    url, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with peer_server("accepted_drop") as peer:
        transport = transport_for(peer)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
        store.close(); store = Store(url); authority = TextAuthority(store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
        assert authority.expire_dispatches(TENANT, ACCOUNT) == 0
        assert len(peer.records) == 1
        assert_unknown_retained(engine, intent)
    store.close()


def test_redirect_never_reaches_second_endpoint(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with peer_server() as other, peer_server("redirect", location=other.url + WIRE_PATH) as peer:
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport_for(peer)) == "unknown"
        assert len(peer.records) == 1 and not other.records
        assert_unknown_retained(engine, intent)
    store.close()


def test_hostile_proxy_and_certificate_environment_are_ignored(database, monkeypatch):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with peer_server() as hostile_proxy, peer_server() as peer:
        for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
            monkeypatch.setenv(name, hostile_proxy.url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("SSL_CERT_FILE", "/synthetic-nonexistent-ca.pem")
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport_for(peer)) == "accepted"
        assert len(peer.records) == 1 and not hostile_proxy.records
    store.close()


def test_peer_set_cookie_is_never_forwarded_to_another_send(database):
    _, engine = database
    store, authority = configured(database)
    with peer_server("set_cookie") as peer:
        transport = transport_for(peer)
        first = prepare(authority)
        assert authority.dispatch(TENANT, ACCOUNT, first, transport) == "accepted"
        unthrottle(engine)
        second = prepare(authority, "second-without-cookies")
        assert authority.dispatch(TENANT, ACCOUNT, second, transport) == "accepted"
        assert len(peer.records) == 2
        assert all("cookie" not in record["headers"] for record in peer.records)
        with Session(engine) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9001)) is not None
            assert s.get(Message, (ACCOUNT, FAN, 9002)) is not None
    store.close()


@pytest.mark.parametrize("mode", ["slow_headers", "drip_body"])
def test_absolute_transport_deadline_bounds_headers_and_continuous_body_drip(database, mode):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with peer_server(mode) as peer:
        started = time.monotonic()
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport_for(peer, timeout_seconds=.2)) == "unknown"
        elapsed = time.monotonic() - started
        assert elapsed < 1.5, "continuous bytes must not extend the total deadline"
        assert len(peer.records) == 1
        assert_unknown_retained(engine, intent)
    store.close()


def test_peer_policy_generation_mismatch_refuses_before_cap_or_network(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with peer_server() as peer:
        transport = LoopbackTextTransport(from_server(peer.server, account_id=ACCOUNT, policy_revision=2))
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "cancelled"
        assert not peer.records
        with Session(engine) as s:
            assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
            assert not s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
        assert_no_outbound(engine)
    store.close()


def _send_grant_process(url, handle, grant, ready, go, output):
    store = Store(url)
    try:
        authority = TextAuthority(store)
        transport = LoopbackTextTransport(handle)
        ready.put(True)
        if not go.wait(15): raise AssertionError("synthetic process barrier timed out")
        try:
            proof = transport.send(grant, authority=authority, tenant_id=TENANT)
            output.put(("proof", proof))
        except Refused as exc:
            output.put(("refused", str(exc)))
    finally:
        store.close()


def test_two_processes_with_same_grant_can_start_only_one_http_post(database):
    url, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert isinstance(grant, Dispatch)
    with peer_server() as peer:
        handle = from_server(peer.server, account_id=ACCOUNT, policy_revision=1)
        assert pickle.loads(pickle.dumps(handle)) == handle
        ctx = multiprocessing.get_context("spawn")
        ready, output, go = ctx.Queue(), ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=_send_grant_process, args=(url, handle, grant, ready, go, output)) for _ in range(2)]
        try:
            for worker in workers: worker.start()
            for _ in workers: assert ready.get(timeout=20)
            go.set()
            results = [output.get(timeout=20) for _ in workers]
            for worker in workers: worker.join(15); assert worker.exitcode == 0
            proofs = [value for status, value in results if status == "proof"]
            assert len(proofs) == 1 and type(proofs[0]) is Accepted
            assert [status for status, _ in results].count("refused") == 1
            assert len(peer.records) == 1
            assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, proofs[0]) == "accepted"
            assert authority.materialize(TENANT, ACCOUNT, intent) is True
            with Session(engine) as s:
                assert s.get(OfTextIntent, (ACCOUNT, intent)).wire_started_at is not None
                assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
                assert s.get(Message, (ACCOUNT, FAN, 9001)) is not None
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join(5)
    store.close()


def test_peer_handle_is_frozen_and_wildcard_or_custom_servers_are_refused():
    with peer_server() as peer:
        handle = from_server(peer.server, account_id=ACCOUNT, policy_revision=1)
        with pytest.raises((FrozenInstanceError, AttributeError)):
            handle.port = 1
        with pytest.raises((Refused, TypeError, ValueError)):
            LoopbackTextTransport("http://127.0.0.1:1234/arbitrary")
    wildcard = ThreadingHTTPServer(("0.0.0.0", 0), BaseHTTPRequestHandler)
    try:
        with pytest.raises((Refused, TypeError, ValueError)):
            from_server(wildcard, account_id=ACCOUNT, policy_revision=1)
    finally:
        wildcard.server_close()
    class CustomServer(ThreadingHTTPServer): pass
    custom = CustomServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        with pytest.raises((Refused, TypeError, ValueError)):
            from_server(custom, account_id=ACCOUNT, policy_revision=1)
    finally:
        custom.server_close()


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": 6}, {"timeout_seconds": True},
    {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
    {"max_response_bytes": 0}, {"max_response_bytes": 65537}, {"max_response_bytes": True},
])
def test_transport_resource_limits_cannot_be_disabled_or_expanded(kwargs):
    with peer_server() as peer:
        with pytest.raises((Refused, TypeError, ValueError)):
            transport_for(peer, **kwargs)
