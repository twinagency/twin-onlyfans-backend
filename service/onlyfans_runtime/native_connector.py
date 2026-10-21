"""Native-client protocol over an explicitly owned local fixture listener.

There is no URL/session/credential loader or real-provider factory. This is an
offline integration seam: existing pure signing and formatting, strict native
response parsing, canonical catch-up and the common one-use delivery authority.
It does not establish the current real platform's API/TLS/session contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlencode

from of_signer import sign
from .loopback_transport import (
    LoopbackPeerHandle, LoopbackTextTransport, _request_once, from_server,
)
from .native_protocol import API_PATH, MAX_BYTES, MAX_HISTORY_BYTES, NativeProtocolError, acceptance, load, message, request
from .store import PAGE_LIMIT, Refused, _id
from .text_delivery import Dispatch, TextAuthority
from .worker import ChatPage, MessagePage


@dataclass(frozen=True, slots=True, init=False)
class NativeFixtureHandle:
    peer: LoopbackPeerHandle = field(repr=False)
    owner_id: int

    def __init__(self, *args, **kwargs):
        raise TypeError("Use from_native_server with an owned fixture listener")


def from_native_server(server, *, account_id, owner_id, policy_revision):
    _id(owner_id)
    peer = from_server(server, account_id=account_id, policy_revision=policy_revision)
    handle = object.__new__(NativeFixtureHandle)
    object.__setattr__(handle, "peer", peer)
    object.__setattr__(handle, "owner_id", owner_id)
    return handle


def _synthetic_headers(port, path, owner_id):
    # Fixed, public test values. No captured signing rules or account session.
    rules = {"static_param": "twin-local-fixture-only", "start": "fixture", "end": "offline",
             "checksum_indexes": list(range(32)), "checksum_constant": 17}
    headers = sign(f"http://127.0.0.1:{port}{path}", owner_id, rules)
    return {**headers, "user-id": str(owner_id)}


class NativeLoopbackConnector:
    """Account-bound reader and one-attempt sender for native-shaped fixtures.

    Every operation verifies /users/me before reading history or consuming a
    send grant. No DB transaction spans HTTP. An identity read and a POST have
    separate bounded deadlines; neither retries or follows redirects.
    """
    def __init__(self, handle, *, timeout_seconds=2.0, max_response_bytes=MAX_BYTES):
        if type(handle) is not NativeFixtureHandle:
            raise Refused("an explicit native fixture handle is required")
        _id(handle.owner_id)
        self._http = LoopbackTextTransport(handle.peer, timeout_seconds=timeout_seconds,
                                            max_response_bytes=max_response_bytes)
        self._handle = handle

    @property
    def account_id(self):
        return self._http.account_id

    @property
    def policy_revision(self):
        return self._http.policy_revision

    @staticmethod
    def _ready():
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("native fixture I/O requires the worker role")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise Refused("native fixture I/O requires a synchronous worker or asyncio.to_thread")

    def _call(self, method, path, body=b""):
        self._ready()
        headers = _synthetic_headers(self._handle.peer.port, path, self._handle.owner_id)
        return asyncio.run(_request_once(self._handle.peer, method, path, body,
            timeout=self._http._timeout, max_bytes=self._http._max_bytes, headers=headers))

    def verify_identity(self):
        value = load(self._call("GET", API_PATH + "/users/me"))
        if type(value.get("id")) is not int or value["id"] != self._handle.owner_id:
            raise NativeProtocolError("native fixture owner mismatch")

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= PAGE_LIMIT:
            raise Refused("invalid native page limit")

    @staticmethod
    def _page(value, limit):
        rows, more = value.get("list"), value.get("hasMore")
        if (type(rows) is not list or len(rows) > limit or type(more) is not bool
                or (more and not rows)):
            raise NativeProtocolError("invalid native page completeness evidence")
        return rows, more

    def chats(self, *, cursor, limit):
        self._ready(); self._limit(limit)
        if cursor is not None and (type(cursor) is not str or re.fullmatch(r"[1-9][0-9]{0,6}", cursor) is None):
            raise Refused("invalid native discovery offset")
        offset = 0 if cursor is None else int(cursor)
        if offset > 1_000_000:
            raise Refused("native discovery budget exhausted")
        self.verify_identity()
        path = API_PATH + "/chats?" + urlencode({"limit": limit, "order": "recent", "offset": offset, "skip_users": "all"})
        rows, more = self._page(load(self._call("GET", path), max_bytes=MAX_HISTORY_BYTES), limit)
        try:
            fans = [_id(row["withUser"]["id"]) for row in rows if type(row) is dict]
            if len(fans) != len(rows) or len(set(fans)) != len(fans) or self._handle.owner_id in fans:
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise NativeProtocolError("invalid native conversation identity") from exc
        return ChatPage(fans, str(offset + len(rows)) if more else None)

    def messages(self, *, fan_id, before_id, limit):
        self._ready(); self._limit(limit); _id(fan_id)
        if fan_id == self._handle.owner_id:
            raise Refused("native conversation cannot target its owner")
        if before_id is not None:
            _id(before_id)
        self.verify_identity()
        params = {"limit": limit, "order": "desc", "skip_users": "all"}
        if before_id is not None:
            params["id"] = before_id
        path = API_PATH + f"/chats/{fan_id}/messages?" + urlencode(params)
        rows, more = self._page(load(self._call("GET", path)), limit)
        events = [message(row, owner_id=self._handle.owner_id, fan_id=fan_id, history=True) for row in rows]
        ids = [event.message_id for event in events]
        if ids != sorted(set(ids), reverse=True) or (before_id is not None and any(value >= before_id for value in ids)):
            raise NativeProtocolError("native message cursor did not advance")
        return MessagePage(events, more)

    def send(self, dispatch, *, authority, tenant_id):
        self._ready()
        if type(authority) is not TextAuthority or type(dispatch) is not Dispatch:
            raise Refused("recorded native dispatch authority is required")
        if dispatch.account_id != self.account_id or dispatch.policy_revision != self.policy_revision:
            raise Refused("native fixture scope or generation mismatch")
        # Pure formatting preflight and identity read precede consumption. The
        # only network operation after the committed marker is the single POST.
        request(dispatch)
        authority.check_dispatch_preflight(tenant_id, dispatch, transport_generation=self.policy_revision)
        self.verify_identity()
        current = authority.consume_dispatch(tenant_id, dispatch, transport_generation=self.policy_revision)
        if current is None:
            raise Refused("native dispatch did not pass the one-use egress boundary")
        body = request(current)
        raw = self._call("POST", API_PATH + f"/chats/{current.fan_id}/messages", body)
        return acceptance(raw, current, owner_id=self._handle.owner_id, request_bytes=body)
