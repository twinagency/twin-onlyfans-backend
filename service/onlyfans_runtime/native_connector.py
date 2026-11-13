"""Native-client protocol over an explicitly owned local fixture listener.

There is no URL/session/credential loader or real-provider factory. This is an
offline integration seam: existing pure signing and formatting, strict native
response parsing, canonical catch-up and the common one-use delivery authority.
It does not establish the current real platform's API/TLS/session contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import html
import json
import logging
import os
import re
import time
from urllib.parse import urlencode

from of_signer import sign
from .loopback_transport import (
    LoopbackPeerHandle, LoopbackTextTransport, _request_once, from_server,
)
from .native_protocol import API_PATH, MAX_BYTES, MAX_HISTORY_BYTES, NativeProtocolError, acceptance, history_text, load, message, request
from .store import PAGE_LIMIT, Refused, _id
from .text_delivery import Dispatch, TextAuthority, _stamp
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


_TAG = re.compile(r"<[^<>]*>")


def _skip_reason(row, *, owner_id, fan_id):
    """Name a well-formed, in-scope row on the skip whitelist, else None.

    Explicit tip/unsent/deleted flags are skipped: the history Event has no
    tip and these rows carry no conversation text. A row with no media, no
    price and no visible text whose sole defect is unsupported history markup
    (e.g. a bare inline image tag) is skipped too, so one odd older body never
    blocks newer inbound; messages() still refuses when such a row has no newer
    kept row on the page (for a fan row: no newer fan text). A row whose markup wraps visible text still
    refuses: the protocol never silently loses fan words. A skipped creator row
    is not stored (accepted: no text, no media, no price). Every other row the
    strict parser rejects still refuses the whole page.
    """
    try:
        _id(row["id"])
        sender = _id(row["fromUser"]["id"])
        if sender not in {owner_id, fan_id} or owner_id == fan_id:
            raise ValueError()
        if "toUser" in row and _id(row["toUser"]["id"]) != (fan_id if sender == owner_id else owner_id):
            raise ValueError()
        _stamp(row["createdAt"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if row.get("isTip") is True or row.get("responseType") == "tip":
        return "tip"
    if row.get("isUnsent") is True:
        return "unsent"
    if row.get("isDeleted") is True:
        return "deleted"
    text = row.get("text")
    try:
        if type(text) is not str or len(text.encode()) > 20000:
            return None
    except UnicodeError:
        return None  # lone surrogate: message() refuses it as a protocol error
    if html.unescape(_TAG.sub("", text)).strip():
        return None  # visible text behind unsupported markup: refuse, never drop it
    try:
        history_text(text)
        return None
    except NativeProtocolError:
        pass
    try:
        event = message({**row, "text": "x"}, owner_id=owner_id, fan_id=fan_id, history=True)
    except NativeProtocolError:
        return None
    return "formatting" if not event.media_ids and event.price_cents == 0 else None


_SKIP_LOG_SECONDS = 60.0
_SKIP_LOGGED = {}


def _log_skipped(account_id, rows, skipped, indexes):
    """At most one line per account a minute; counts and row positions only."""
    now = time.monotonic()
    last, suppressed = _SKIP_LOGGED.get(account_id, (None, 0))
    if last is not None and now - last < _SKIP_LOG_SECONDS:
        _SKIP_LOGGED[account_id] = (last, suppressed + 1)
        return
    _SKIP_LOGGED[account_id] = (now, 0)
    logging.getLogger(__name__).warning("onlyfans_history_rows_skipped %s", json.dumps(
        {"account": account_id, "rows": rows, "skipped": skipped, "indexes": indexes[:20],
         "suppressed": suppressed}, sort_keys=True))


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
        from .history_telemetry import dispatched
        dispatched()
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
        creator = self.verify_identity()
        path = API_PATH + "/chats?" + urlencode({"limit": limit, "order": "recent", "offset": offset})
        rows, more = self._page(load(self._call("GET", path), max_bytes=MAX_HISTORY_BYTES), limit)
        try:
            fans = [_id(row["withUser"]["id"]) for row in rows if type(row) is dict]
            if len(fans) != len(rows) or len(set(fans)) != len(fans) or self._handle.owner_id in fans:
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise NativeProtocolError("invalid native conversation identity") from exc
        heads = {}
        for fan, row in zip(fans, rows):
            last = row.get("lastMessage")
            mid = last.get("id") if type(last) is dict else None
            if type(mid) is int and 0 < mid < 2**63:
                heads[fan] = mid
        return ChatPage(fans, str(offset + len(rows)) if more else None, heads,
            {fan: row["withUser"] for fan,row in zip(fans,rows)}, creator if type(creator) is dict else None)

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
        rows, more = self._page(load(self._call("GET", path), max_bytes=MAX_HISTORY_BYTES), limit)
        kept, events, ids, skipped, indexes, inbound = [], [], [], {}, [], []
        for index, row in enumerate(rows):
            try:
                event = message(row, owner_id=self._handle.owner_id, fan_id=fan_id, history=True)
            except NativeProtocolError:
                # Only whitelisted, in-scope rows are skipped; any other
                # unsupported or malformed row still refuses the page.
                reason = _skip_reason(row, owner_id=self._handle.owner_id, fan_id=fan_id)
                fan_row = _id(row["fromUser"]["id"]) == fan_id if reason is not None else False
                # A markup-only row is skipped only below a newer kept row: a fan's needs
                # newer fan text (else the engine replies to an older message), a creator's
                # needs any newer kept row (else an older fan message a human already
                # answered becomes the latest event and re-arms a reply). Else refuse.
                # An archive page (before_id) is below the head page that arms replies
                # (recent_inbox reads before_id=None), so its markup rows always skip:
                # refusing would re-queue the same cursor and stall recovery forever.
                if reason == "formatting":
                    newer = before_id is not None or (
                        any(event.direction == "in" for event in events) if fan_row else bool(events))
                if reason is None or reason == "formatting" and not newer:
                    raise
                skipped[reason] = skipped.get(reason, 0) + 1
                indexes.append(index)
                ids.append(_id(row["id"]))
                if fan_row:
                    inbound.append(_id(row["id"]))
                continue
            kept.append(row); events.append(event); ids.append(event.message_id)
        if skipped:
            _log_skipped(self.account_id, len(rows), skipped, indexes)
        if ids != sorted(set(ids), reverse=True) or (before_id is not None and any(value >= before_id for value in ids)):
            raise NativeProtocolError("native message cursor did not advance")
        # Paid-offer terms come from the same validated rows before previews and
        # lockedText are discarded. Event identity and payloads are unchanged.
        from .offer_terms import from_history
        observations = [value for value in (from_history(row, event) for row, event in zip(kept, events)) if value is not None]
        # The cursor covers skipped rows too, so they are never re-read and a
        # page of only skipped rows still advances.
        return MessagePage(events, more, observations, min(ids) if ids else None, tuple(inbound))

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
