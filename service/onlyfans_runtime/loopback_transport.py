"""One-attempt HTTP rehearsal for the existing shared delivery authority.

This module can target only a fixture-owned 127.0.0.1 HTTP listener. It accepts
no URL, provider session, cookie, credential or proxy configuration. The wire
protocol is a local test contract, not a verified OnlyFans API adapter.
"""
from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
import json
import math
import re
import socket
import uuid

import httpx

from onlyfans_read.contract import ID_PATTERN
from .store import Refused, _id
from .text_delivery import Accepted, Dispatch, NotAccepted, TextAuthority, _stamp

SCHEMA = "of-wire-rehearsal.v1"
PATH = "/twin-rehearsal/v1/text"
PEER_HEADER = "X-Twin-Rehearsal-Peer"
MAX_RESPONSE_BYTES = 65536
MAX_TIMEOUT_SECONDS = 5.0


class WireProtocolError(ValueError):
    """A response cannot prove this attempt's outcome; never a refund signal."""


@dataclass(frozen=True, slots=True, init=False)
class LoopbackPeerHandle:
    account_id: str
    policy_revision: int
    port: int
    peer_id: str = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError("Use from_server with a fixture-owned listener")


def _listening(server) -> bool:
    try:
        return server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
    except OSError as error:
        if error.errno != errno.ENOPROTOOPT:
            return False
    # Darwin cannot read SO_ACCEPTCONN (ENOPROTOOPT). listen() is idempotent on a
    # listening socket and consumes no pending connection, so make it hold instead.
    try:
        server.socket.listen(server.request_queue_size)
    except OSError:
        return False
    return True


def from_server(server, *, account_id: str, policy_revision: int) -> LoopbackPeerHandle:
    """Mint a picklable internal fixture handle, never from API/env/URL input.

    The caller owns the local server's lifetime. Its handler echoes peer_id in
    PEER_HEADER and implements SCHEMA. This handle is a trusted test seam, not
    a production session, network permission or untrusted plugin capability.
    """
    if type(server) is not ThreadingHTTPServer:
        raise Refused("a fixture-owned ThreadingHTTPServer is required")
    if (not isinstance(account_id, str) or not ID_PATTERN.fullmatch(account_id)
            or type(policy_revision) is not int or policy_revision < 1):
        raise Refused("invalid fixture scope or generation")
    address = server.server_address
    if (server.address_family != socket.AF_INET or len(address) != 2
            or address[0] != "127.0.0.1" or type(address[1]) is not int
            or not 1 <= address[1] <= 65535 or server.socket.fileno() < 0
            or server.socket.getsockname() != address
            or not _listening(server)):
        raise Refused("listener must be owned and bound to literal IPv4 loopback")
    handle = object.__new__(LoopbackPeerHandle)
    for key, value in dict(account_id=account_id, policy_revision=policy_revision,
                           port=address[1], peer_id=uuid.uuid4().hex).items():
        object.__setattr__(handle, key, value)
    return handle


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise WireProtocolError("duplicate response key")
        value[key] = item
    return value


def _nonfinite(value):
    raise WireProtocolError("nonfinite response number")


def _encode(dispatch):
    payload = dict(account_id=dispatch.account_id, fan_id=dispatch.fan_id,
                   text=dispatch.body, media_ids=list(dispatch.media_ids), price_cents=dispatch.price_cents)
    if dispatch.media_ids:
        payload["preview_ids"] = list(dispatch.preview_ids)
    envelope = dict(schemaVersion=SCHEMA, accountId=dispatch.account_id,
                    fanId=dispatch.fan_id, intentId=dispatch.intent_id,
                    attemptId=dispatch.attempt_id, payloadHash=dispatch.payload_hash,
                    policyRevision=dispatch.policy_revision, payload=payload)
    # Exact UTF-8 text is preserved. In particular, no strip, HTML wrapping,
    # co-tag fallback, content rewrite or retry is performed at this boundary.
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _decode(raw: bytes, dispatch: Dispatch):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_nonfinite)
    except (UnicodeError, ValueError) as exc:
        raise WireProtocolError("invalid JSON receipt") from exc
    if type(value) is not dict:
        raise WireProtocolError("invalid receipt object")
    outcome = value.get("outcome")
    if outcome not in {"accepted", "not_accepted"}:
        raise WireProtocolError("unproved response outcome")
    required = {"schemaVersion", "accountId", "fanId", "intentId", "attemptId",
                "payloadHash", "policyRevision", "outcome", "occurredAt", "evidenceRef"}
    if outcome == "accepted":
        required.add("messageId")
    if set(value) != required:
        raise WireProtocolError("unexpected receipt fields")
    for key, expected in dict(schemaVersion=SCHEMA, accountId=dispatch.account_id,
                              intentId=dispatch.intent_id, attemptId=dispatch.attempt_id,
                              payloadHash=dispatch.payload_hash).items():
        if type(value[key]) is not str or value[key] != expected:
            raise WireProtocolError("receipt scope or payload mismatch")
    for key, expected in dict(fanId=dispatch.fan_id, policyRevision=dispatch.policy_revision).items():
        if type(value[key]) is not int or value[key] != expected:
            raise WireProtocolError("receipt identity or generation mismatch")
    if (type(value["occurredAt"]) is not str or type(value["evidenceRef"]) is not str
            or not value["evidenceRef"].strip() or len(value["evidenceRef"]) > 256):
        raise WireProtocolError("invalid receipt provenance")
    _stamp(value["occurredAt"])
    proof = dict(account_id=dispatch.account_id, fan_id=dispatch.fan_id,
                 intent_id=dispatch.intent_id, attempt_id=dispatch.attempt_id,
                 payload_hash=dispatch.payload_hash, occurred_at=value["occurredAt"],
                 evidence_ref=value["evidenceRef"])
    if outcome == "not_accepted":
        return NotAccepted(**proof)
    _id(value["messageId"])
    return Accepted(**proof, message_id=value["messageId"])


class LoopbackTextTransport:
    """A synchronous worker transport with bounded async I/O underneath it."""

    def __init__(self, handle, *, timeout_seconds=2.0, max_response_bytes=MAX_RESPONSE_BYTES):
        if (type(handle) is not LoopbackPeerHandle
                or type(getattr(handle, "port", None)) is not int or not 1 <= handle.port <= 65535
                or type(getattr(handle, "policy_revision", None)) is not int or handle.policy_revision < 1
                or type(getattr(handle, "account_id", None)) is not str
                or not ID_PATTERN.fullmatch(handle.account_id)
                or type(getattr(handle, "peer_id", None)) is not str
                or re.fullmatch(r"[a-f0-9]{32}", handle.peer_id) is None):
            raise Refused("a scoped fixture handle is required")
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not .05 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES):
            raise Refused("invalid bounded HTTP resources")
        self._handle = handle
        self._timeout = float(timeout_seconds)
        self._max_bytes = max_response_bytes

    @property
    def account_id(self):
        return self._handle.account_id

    @property
    def policy_revision(self):
        return self._handle.policy_revision

    def send(self, dispatch, *, authority, tenant_id):
        if type(authority) is not TextAuthority or type(dispatch) is not Dispatch:
            raise Refused("recorded dispatch authority is required")
        if dispatch.account_id != self.account_id or dispatch.policy_revision != self.policy_revision:
            raise Refused("fixture scope or generation mismatch")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise Refused("HTTP rehearsal requires a synchronous worker or asyncio.to_thread")
        # The DB marker commits BEFORE client/socket construction. This also
        # reloads immutable bytes and rejects a replayed or copied stale grant.
        current = authority.consume_dispatch(tenant_id, dispatch, transport_generation=self.policy_revision)
        if current is None:
            raise Refused("dispatch did not pass the one-use egress boundary")
        return asyncio.run(self._post_once(current, _encode(current)))

    async def _post_once(self, dispatch, body):
        raw = await _request_once(self._handle, "POST", PATH, body,
                                  timeout=self._timeout, max_bytes=self._max_bytes)
        return _decode(raw, dispatch)


async def _request_once(handle, method, path, body, *, timeout, max_bytes, headers=None):
    """Shared fixture-only bounded I/O; never resolves an environment URL.

    Each call constructs a fresh client. GET identity/discovery and one-use POST
    share the same redirect, cookie, compression, retry and resource boundaries.
    Authorization/consume belongs to the caller before a mutating call.
    """
    if (type(handle) is not LoopbackPeerHandle or method not in {"GET", "POST"}
            or type(path) is not str or not path.startswith("/") or path.startswith("//")
            or len(path) > 2048 or any(char in path for char in "\r\n#\\")):
        raise Refused("invalid fixture HTTP target")
    # Reuse the existing constructor's complete immutable-handle/bound checks.
    LoopbackTextTransport(handle, timeout_seconds=timeout, max_response_bytes=max_bytes)
    url = f"http://127.0.0.1:{handle.port}{path}"
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
    request_headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "Accept-Encoding": "identity", PEER_HEADER: handle.peer_id,
    }
    if headers:
        if set(headers) != {"sign", "time", "user-id"}:
            raise Refused("only explicit synthetic signing headers are supported")
        request_headers.update(headers)
    async with asyncio.timeout(timeout):
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False, proxy=None,
                                             http1=True, http2=False, limits=limits)
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     follow_redirects=False, auth=None, timeout=timeout,
                                     limits=limits, http1=True, http2=False) as client:
            async with client.stream(method, url, content=body, headers=request_headers,
                                     follow_redirects=False) as response:
                if response.status_code != 200:
                    raise WireProtocolError("HTTP status is not receipt evidence")
                if response.headers.get(PEER_HEADER) != handle.peer_id:
                    raise WireProtocolError("fixture peer mismatch")
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise WireProtocolError("encoded response is refused")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise WireProtocolError("receipt must be JSON")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdigit() or int(length) > max_bytes:
                        raise WireProtocolError("invalid response size")
                chunks, size = [], 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > max_bytes:
                        raise WireProtocolError("response exceeded byte budget")
                    chunks.append(chunk)
                return b"".join(chunks)
