"""Explicit native HTTPS transport; never loads captured sessions or activates itself.

The selected account's session, signing rules, hash and fixed proxy are supplied
by the deployment's credential owner. This adapter deliberately does not use
OFClient: that client retries POSTs and may perform hidden warmup requests.
The existing strict native protocol remains the acceptance boundary. Its
supported response subset still requires validation against a selected account.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import math
import os
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlsplit

from of_signer import sign
from transaction_contract import transaction_marker
from onlyfans_read.contract import ID_PATTERN
from .native_connector import NativeLoopbackConnector
from .native_protocol import API_PATH, MAX_BYTES, MAX_VAULT_BYTES, MAX_HISTORY_BYTES, NativeProtocolError, acceptance, request, load
from .store import Refused, _id
from .text_delivery import Dispatch, TextAuthority

ORIGIN = "https://onlyfans.com"
APP_TOKEN = "33d57ade8c02dbc5a333db99ff9ae26a"


class NativeAuthenticationError(NativeProtocolError):
    """The provider rejected authentication or returned another account."""


class NativeRateLimitError(NativeProtocolError):
    """Bounded non-secret pacing hint, shared by every provider read owner."""
    def __init__(self, retry_after=60):
        self.retry_after = min(3600, max(1, int(retry_after)))
        super().__init__('native provider rate limit')


class NativeReadTransientError(NativeProtocolError):
    """A read-only request may retry its same checkpoint after bounded backoff."""
    def __init__(self, retry_after=5):
        self.retry_after = min(3600, max(1, int(retry_after)))
        super().__init__('native provider read temporarily unavailable')


def _rate_limit_seconds(value):
    try:
        if type(value) is not str or len(value) > 128:
            return 60
        if value.isascii() and value.isdecimal():
            return min(3600, max(1, int(value)))
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return 60
        return min(3600, max(1, math.ceil((stamp - datetime.now(timezone.utc)).total_seconds())))
    except (TypeError, ValueError, OverflowError):
        return 60


def validate_cdn_url(url):
    if (type(url) is not str or len(url) > 8192 or not url.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url):
        raise Refused("unsupported native media target")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not re.fullmatch(r"cdn[0-9]{0,2}\.onlyfans\.com", parsed.hostname or "")
                or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise Refused("unsupported native media target") from None


def _header(value, label, maximum=4096):
    if (type(value) is not str or not 1 <= len(value) <= maximum
            or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise Refused("invalid native " + label)
    return value


def _deadline(value):
    now = datetime.now(timezone.utc)
    if (type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None
            or not now < value <= now + timedelta(days=1)):
        raise Refused("an explicit native credential expiry within 24 hours is required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class NativeProviderSession:
    """Account-bound in-memory credentials, with no secret-bearing repr."""
    account_id: str
    owner_id: int
    policy_revision: int
    expires_at: datetime
    user_agent: str = field(repr=False)
    x_bc: str = field(repr=False)
    x_of_rev: str = field(repr=False)
    x_hash: str = field(repr=False)
    cookies: tuple = field(repr=False)
    signing_rules: tuple = field(repr=False)
    proxy_url: str | None = field(repr=False)

    def __post_init__(self):
        if (type(self.account_id) is not str or not ID_PATTERN.fullmatch(self.account_id)
                or type(self.policy_revision) is not int or self.policy_revision < 1):
            raise Refused("invalid native account scope or policy generation")
        _id(self.owner_id)
        object.__setattr__(self, "expires_at", _deadline(self.expires_at))
        for name in ("user_agent", "x_bc", "x_of_rev", "x_hash"):
            _header(getattr(self, name), name)
        if type(self.cookies) is not tuple or not 1 <= len(self.cookies) <= 64:
            raise Refused("explicit native session cookies are required")
        names = set()
        for row in self.cookies:
            if type(row) is not tuple or len(row) != 2:
                raise Refused("invalid native cookie")
            name, value = row
            if (type(name) is not str or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name) is None
                    or name in names):
                raise Refused("invalid or duplicate native cookie name")
            _header(value, "cookie", 8192)
            if any(char in value for char in ';,"\\') or any(char.isspace() for char in value):
                raise Refused("ambiguous native cookie value")
            names.add(name)
        if len(self.cookie_header.encode()) > 16384:
            raise Refused("native cookie header exceeds its byte budget")
        if (type(self.signing_rules) is not tuple or len(self.signing_rules) != 5
                or type(self.signing_rules[3]) is not tuple or len(self.signing_rules[3]) != 32
                or any(type(i) is not int or not 0 <= i < 40 for i in self.signing_rules[3])
                or type(self.signing_rules[4]) is not int or abs(self.signing_rules[4]) > 10**9):
            raise Refused("invalid native signing rules")
        for value in self.signing_rules[:3]:
            _header(value, "signing field", 4096)
        if self.proxy_url is not None:
            _header(self.proxy_url, "fixed proxy", 4096)
            parsed = urlsplit(self.proxy_url)
            try:
                port = parsed.port
            except ValueError as exc:
                raise Refused("invalid native fixed proxy") from exc
            if (parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname
                    or port is None or not 1 <= port <= 65535
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise Refused("invalid native fixed proxy")

    @property
    def cookie_header(self):
        return "; ".join(name + "=" + value for name, value in self.cookies)

    def current(self):
        if datetime.now(timezone.utc) >= self.expires_at:
            raise Refused("native session credentials expired")

    def rules(self):
        static, start, end, indexes, constant = self.signing_rules
        return dict(static_param=static, start=start, end=end,
                    checksum_indexes=list(indexes), checksum_constant=constant)


def session_from_capture(*, account_id, owner_id, policy_revision, capture, x_hash,
                         proxy_url, expires_at):
    """Pure explicit conversion of the existing capture shape; no path/env reads.

    Cookie domains/paths must be valid for OnlyFans. The returned session never
    follows redirects, updates the captured cookies or falls back to another
    account or proxy. A fresh capture replaces the credential object explicitly.
    """
    if type(capture) is not dict:
        raise Refused("an explicit native session capture is required")
    try:
        headers = capture["headers"]
        rules = capture["signing"]["rules"]
        if type(headers) is not dict or type(rules) is not dict or str(owner_id) != str(headers["user_id"]):
            raise ValueError()
        rows = capture["cookies"]
        if type(rows) is not list:
            raise ValueError()
        cookies = []
        for row in rows:
            if (type(row) is not dict or row.get("domain", "onlyfans.com").lstrip(".") != "onlyfans.com"
                    or row.get("path", "/") != "/"):
                raise ValueError()
            cookies.append((row["name"], row["value"]))
        signing = (rules["static_param"], rules["start"], rules["end"],
                   tuple(rules["checksum_indexes"]), rules["checksum_constant"])
        return NativeProviderSession(account_id, owner_id, policy_revision, expires_at,
            headers["user_agent"], headers["x_bc"], headers["x_of_rev"], x_hash,
            tuple(cookies), signing, proxy_url)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Refused("native session capture does not match the selected owner") from exc


class NativeProviderConnector(NativeLoopbackConnector):
    """Same bounded native reads and receipt parser, with one HTTPS send attempt.

    Constructing this class performs no I/O. The common authority must commit
    the one-use marker before the only POST. Identity checks precede it. Live
    account activation and credential custody belong to the deployment owner.
    """
    def __init__(self, session, *, timeout_seconds=5, max_response_bytes=MAX_BYTES,
                 max_vault_response_bytes=MAX_VAULT_BYTES):
        if type(session) is not NativeProviderSession:
            raise Refused("an explicit account-bound native session is required")
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not .05 <= timeout_seconds <= 10
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_BYTES
                or type(max_vault_response_bytes) is not int or not 1 <= max_vault_response_bytes <= MAX_VAULT_BYTES):
            raise Refused("invalid native HTTPS resource bound")
        self._handle = session
        self._timeout, self._max_bytes = float(timeout_seconds), max_response_bytes
        try:
            read_timeout = float(os.environ.get('ONLYFANS_PROVIDER_READ_TIMEOUT_SECONDS', timeout_seconds))
        except (TypeError, ValueError):
            raise Refused('invalid native HTTPS read timeout') from None
        if not math.isfinite(read_timeout) or not .05 <= read_timeout <= 10:
            raise Refused('invalid native HTTPS read timeout')
        self._read_timeout = read_timeout
        self._vault_max_bytes = max_vault_response_bytes

    @property
    def account_id(self):
        return self._handle.account_id

    @property
    def policy_revision(self):
        return self._handle.policy_revision

    @staticmethod
    def _ready():
        NativeLoopbackConnector._ready()

    def _call(self, method, path, body=b""):
        self._ready()
        self._handle.current()
        if (method not in {"GET", "POST"} or type(path) is not str or len(path) > 2048
                or not path.startswith(API_PATH + "/") or any(c in path for c in "\r\n#\\")
                or type(body) is not bytes or len(body) > MAX_BYTES):
            raise Refused("invalid native HTTPS target")
        target = urlsplit(path)
        if target.scheme or target.netloc or ".." in target.path.split("/"):
            raise Refused("invalid native HTTPS target")
        # Only the narrow read and send paths used by this connector are legal.
        if method == "GET":
            allowed = target.path in {API_PATH + "/users/me", API_PATH + "/chats", API_PATH + "/vault/media", API_PATH + "/payouts/transactions", API_PATH + "/earnings/chart"} or re.fullmatch(
                re.escape(API_PATH) + r"/chats/[1-9][0-9]*/messages", target.path)
            if not allowed or body:
                raise Refused("unsupported native HTTPS read")
        elif (not re.fullmatch(re.escape(API_PATH) + r"/chats/[1-9][0-9]*/messages", target.path)
              or target.query or not body):
            raise Refused("unsupported native HTTPS write")
        if method == "GET" and target.path == API_PATH + "/vault/media":
            return self._signed_request(method, path, body, max_response_bytes=self._vault_max_bytes)
        if method == "GET" and (target.path == API_PATH + "/chats" or re.fullmatch(
                re.escape(API_PATH) + r"/chats/[1-9][0-9]*/messages", target.path)):
            return self._signed_request(method, path, body, max_response_bytes=MAX_HISTORY_BYTES)
        return self._signed_request(method, path, body)

    def messages(self, *, fan_id, before_id, limit):
            # The upstream message GET can clear a human chatter's unread badge.
            # Only this explicitly owned connector gains the narrow undo operation;
            # the general _call write allowlist remains unchanged.
            from unread_preservation import preserve_unread
            self._ready(); self._limit(limit); _id(fan_id)
            if fan_id == self._handle.owner_id:
                raise Refused("native conversation cannot target its owner")
            if before_id is not None:
                _id(before_id)
            self.verify_identity()
            parent_messages = super().messages
            def unread_state():
                return load(self._signed_request("GET", API_PATH + f"/chats/{fan_id}"), max_bytes=MAX_HISTORY_BYTES)
            return preserve_unread(
                account_id=self.account_id, fan_id=fan_id,
                snapshot=unread_state,
                read=lambda: parent_messages(fan_id=fan_id, before_id=before_id, limit=limit),
                restore=self.restore_unread,
                confirm=unread_state,
            )

    def restore_unread(self, fan_id):
        """Scoped undo only; the preservation owner verifies the actual chat flag."""
        _id(fan_id)
        if fan_id==self._handle.owner_id:raise Refused('native conversation cannot target its owner')
        status,raw=self._signed_request('DELETE',API_PATH+f'/chats/{fan_id}/mark-as-read',
            accepted_statuses=(200,204),with_status=True)
        return load(raw)  # 204 is {}, never a manufactured success receipt.

    def verify_identity(self):
        value = load(self._call("GET", API_PATH + "/users/me"))
        if type(value.get("id")) is not int or value["id"] != self._handle.owner_id:
            raise NativeAuthenticationError("native provider owner mismatch")
        return value

    def _signed_request(self, method, path, body=b"", *, max_response_bytes=None,
                        accepted_statuses=(200,), with_status=False):
        """Shared one-attempt signed wire call; each caller owns its narrow route authority."""
        self._ready()
        self._handle.current()
        if (method not in {"GET", "POST", "DELETE"} or type(path) is not str
                or not path.startswith(API_PATH + "/") or len(path) > 8192
                or any(c in path for c in "\r\n#\\") or ".." in urlsplit(path).path.split("/")
                or type(body) is not bytes or len(body) > MAX_BYTES):
            raise Refused("invalid signed transport request")
        limit = self._max_bytes if max_response_bytes is None else max_response_bytes
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_BYTES:
            raise Refused("invalid signed transport response budget")
        url = ORIGIN + path
        sig = sign(url, self._handle.owner_id, self._handle.rules())
        headers = {"Accept": "application/json", "Content-Type": "application/json",
            "Accept-Encoding": "identity", "App-Token": APP_TOKEN, "Sign": sig["sign"], "Time": sig["time"],
            "User-ID": str(self._handle.owner_id), "X-BC": self._handle.x_bc,
            "X-Hash": self._handle.x_hash, "X-OF-Rev": self._handle.x_of_rev,
            "User-Agent": self._handle.user_agent, "Referer": ORIGIN + "/",
            "Cookie": self._handle.cookie_header,
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
        # Import only inside the permitted worker call; constructors/imports do
        # not open clients, read session files or reach the network.
        from curl_cffi import requests
        chunks, size = [], 0
        started, status, oversized = time.monotonic(), None, False
        def bounded(chunk):
            nonlocal size, oversized
            size += len(chunk)
            if size > limit:
                oversized = True
                raise NativeProtocolError("native HTTPS response exceeded its byte budget")
            chunks.append(chunk)
            return len(chunk)
        try:
            with requests.Session(impersonate="chrome", trust_env=False, retry=0) as client:
                from .history_telemetry import dispatched
                dispatched()
                response = client.request(method, url, headers=headers, data=body,
                    proxy=self._handle.proxy_url, timeout=self._read_timeout if method == 'GET' else self._timeout,
                    allow_redirects=False, verify=True, accept_encoding="identity",
                    content_callback=bounded, discard_cookies=True)
                status = response.status_code
                if response.status_code in {401, 403}:
                    raise NativeAuthenticationError("native provider rejected authentication")
                if response.status_code == 429:
                    raise NativeRateLimitError(_rate_limit_seconds(response.headers.get('retry-after')))
                if method == "GET" and response.status_code in {408, 425, 500, 502, 503, 504}:
                    raise NativeReadTransientError()
                if response.status_code not in accepted_statuses:
                    error = NativeProtocolError("native HTTPS status is not delivery evidence")
                    if method == "POST":
                        error.send_http_status = status
                    raise error
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise NativeProtocolError("encoded native HTTPS response is refused")
                if response.status_code != 204 and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise NativeProtocolError("native HTTPS response must be JSON")
                raw = b"".join(chunks) if response.status_code != 204 else b"{}"
                if len(raw) > limit:
                    raise NativeProtocolError("native HTTPS response exceeded its byte budget")
                return (response.status_code, raw) if with_status else raw
        except (NativeProtocolError, Refused) as error:
            if method == 'GET' and isinstance(error, NativeProtocolError):
                from .read_failures import observe
                error.read_failure = observe(error, account=self.account_id, path=path,
                    started=started, status=status, response_bytes=size, callback_failed=oversized)
            raise
        except Exception as error:
            # curl errors may contain authenticated proxy URLs or response
            # bodies. Never propagate those to logs/API/worker observations.
            if method == "GET":
                # curl may wrap a callback exception. Preserve the local size
                # failure so the archive retries the same cursor at a smaller limit.
                failure = NativeProtocolError('native HTTPS response exceeded its byte budget') if oversized else NativeReadTransientError()
                from .read_failures import observe
                failure.read_failure = observe(error, account=self.account_id, path=path,
                    started=started, status=status, response_bytes=size, callback_failed=oversized)
                raise failure from None
            raise NativeProtocolError("native HTTPS request outcome is uncertain") from None

    def vault_page(self, *, offset, limit=24):
        """Read one bounded selected-account vault page, without importing it.

        The vault job owner validates the full page and persists catalogue
        metadata. Vault reads have a separate explicit 1 MiB maximum because signed
        media URLs make these pages larger than message responses. Message and
        receipt reads keep their existing 64 KiB budget.
        """
        self._ready()
        if (type(offset) is not int or not 0 <= offset <= 1000000
                or type(limit) is not int or not 1 <= limit <= 24):
            raise Refused("invalid bounded native vault page")
        self.verify_identity()
        path = API_PATH + "/vault/media?" + urlencode(
            {"limit":limit,"offset":offset,"sort":"desc","field":"recent","list":"all"})
        return self._call("GET", path)

    def vault_media_by_id(self, media_id):
        """One bounded read of a known imported asset; caller owns provenance."""
        self._ready()
        _id(media_id)
        self.verify_identity()
        status, raw = self._signed_request("GET", API_PATH + f"/vault/media/{media_id}",
            max_response_bytes=self._vault_max_bytes, accepted_statuses=(200, 404), with_status=True)
        if status == 404:
            return None
        return load(raw, max_bytes=self._vault_max_bytes)

    def posts_page(self, *, before_marker=None, limit=10):
        """One bounded page of this creator's own feed posts, newest first.

        Verified read-only on 23 Oct 2026: the endpoint parses the request body
        as JSON, so the GET carries "{}" (an empty body is refused with 400).
        The page returns headMarker/tailMarker; older pages pass the tail marker
        as beforePublishTime (the web client's convention, not yet re-verified).
        """
        self._ready()
        if (type(limit) is not int or not 1 <= limit <= 10
                or before_marker is not None and (type(before_marker) is not str
                    or re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,6})?", before_marker) is None)):
            raise Refused("invalid bounded native posts page")
        self.verify_identity()
        params = {"limit": limit, "order": "publish_date_desc", "skip_users": "all", "format": "infinite",
                  "pinned": 0, "counters": 0}
        if before_marker is not None:
            params["beforePublishTime"] = before_marker
        return self._signed_request("GET", API_PATH + f"/users/{self._handle.owner_id}/posts?" + urlencode(params),
                                    b"{}", max_response_bytes=self._vault_max_bytes)

    def post_by_id(self, post_id):
        """Bounded read of one known creator-post source, including explicit 404.

        Callers validate the returned identity/owner and hold their account read
        lease through the resulting domain commit. Missing list rows alone are
        never treated as proof of deletion.
        """
        self._ready()
        _id(post_id)
        self.verify_identity()
        return self._signed_request("GET", API_PATH + f"/posts/{post_id}", b"{}",
            max_response_bytes=self._vault_max_bytes, accepted_statuses=(200, 404), with_status=True)

    def still_bytes(self, url, *, timeout_seconds=2):
        """One credential-free CDN image GET, through this account's fixed proxy.

        URLs come only from a validated vault page, never from an HTTP caller.
        No cookies/signing headers, redirects, decompression or retries. The
        permanent still store owns format detection and its existing byte cap.
        """
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not .05 <= timeout_seconds <= 10:
            raise NativeProtocolError("invalid native image timeout")
        self._ready()
        self._handle.current()
        from vault_still_store import MAX_STILL_BYTES, image_type
        validate_cdn_url(url)
        from curl_cffi import requests
        chunks, size = [], 0
        def bounded(chunk):
            nonlocal size
            size += len(chunk)
            if size > MAX_STILL_BYTES:
                raise NativeProtocolError("native image exceeded its byte budget")
            chunks.append(chunk)
            return len(chunk)
        try:
            with requests.Session(impersonate="chrome", trust_env=False, retry=0) as client:
                response = client.request("GET", url,
                    headers={"Accept": "image/jpeg,image/png,image/webp,image/gif", "Accept-Encoding": "identity",
                             "User-Agent": self._handle.user_agent, "Referer": ORIGIN + "/"},
                    proxy=self._handle.proxy_url, timeout=min(self._timeout, timeout_seconds),
                    allow_redirects=False, verify=True, accept_encoding="identity",
                    content_callback=bounded, discard_cookies=True)
                data = b"".join(chunks)
                mime = image_type(data)
                if (response.status_code != 200 or not mime or size > MAX_STILL_BYTES
                        or response.headers.get("content-encoding", "identity").strip().lower() != "identity"
                        or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != mime):
                    raise NativeProtocolError("native image response is unavailable")
                return data
        except (NativeProtocolError, Refused):
            raise
        except Exception:
            raise NativeProtocolError("native image read is unavailable") from None

    def transaction_page(self, *, offset, limit=20, start, end, history=False, marker=None):
        """Reuse the legacy ledger endpoint with frozen UTC bounds and no retries."""
        self._ready()
        if (type(offset) is not int or not 0 <= offset <= (1000000000 if history is True or marker is not None else 10000)
                or type(limit) is not int or not 1 <= limit <= 20
                or type(start) is not datetime or type(end) is not datetime
                or start.tzinfo is not None or end.tzinfo is not None
                or type(history) is not bool
                or start < datetime(1970, 1, 1)
                or not timedelta(0) <= end - start <= timedelta(days=60000 if history else 90)):
            raise Refused("invalid bounded native ledger page")
        # The provider ignores start/end on this endpoint. Its date filters use
        # startDate/endDate at second precision; request a covering superset
        # and let ledger._provider_page retain only the exact frozen window.
        rounded_end = end.replace(microsecond=0) + timedelta(seconds=bool(end.microsecond))
        params = {"limit": limit, "startDate": start.strftime("%Y-%m-%d %H:%M:%S"),
                  "endDate": rounded_end.strftime("%Y-%m-%d %H:%M:%S")}
        if marker is None:
            params["offset"] = offset
        else:
            try:
                params["marker"] = transaction_marker(marker)
            except (ValueError, UnicodeError):
                raise Refused("invalid bounded native ledger marker") from None
        self.verify_identity()
        path = API_PATH + "/payouts/transactions?" + urlencode(params)
        return self._call("GET", path)

    def earnings_chart(self, *, start, end):
        """The creator's own Statistics > Earnings totals for one frozen UTC window (one read, no retries)."""
        self._ready()
        if (type(start) is not datetime or type(end) is not datetime or start.tzinfo is not None or end.tzinfo is not None
                or start < datetime(2010, 1, 1) or not timedelta(0) < end - start <= timedelta(days=60000)):
            raise Refused("invalid bounded native earnings window")
        params = {"startDate": start.strftime("%Y-%m-%d %H:%M:%S"), "endDate": end.strftime("%Y-%m-%d %H:%M:%S"),
                  "withTotal": "true", "filter[total_amount]": "total_amount"}
        self.verify_identity()
        return self._call("GET", API_PATH + "/earnings/chart?" + urlencode(params))

    def send(self, dispatch, *, authority, tenant_id):
        self._ready()
        self._handle.current()
        if type(authority) is not TextAuthority or type(dispatch) is not Dispatch:
            raise Refused("recorded native dispatch authority is required")
        if dispatch.account_id != self.account_id or dispatch.policy_revision != self.policy_revision:
            raise Refused("native HTTPS scope or generation mismatch")
        request(dispatch)
        authority.check_dispatch_preflight(tenant_id, dispatch, transport_generation=self.policy_revision)
        self.verify_identity()
        current = authority.consume_dispatch(tenant_id, dispatch, transport_generation=self.policy_revision)
        if current is None:
            raise Refused("native dispatch did not pass the one-use egress boundary")
        body = request(current)
        raw = self._call("POST", API_PATH + f"/chats/{current.fan_id}/messages", body)
        proof = acceptance(raw, current, owner_id=self._handle.owner_id, request_bytes=body)
        return replace(proof, evidence_ref=proof.evidence_ref.replace("native-local:", "native-https:", 1))

