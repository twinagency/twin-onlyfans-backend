"""Account-bound OnlyFansAPI wire adapter; no credentials in browser payloads.

The existing native parser, intake, planner and one-use delivery authority own
behaviour. This module only unwraps the provider's documented data envelope.
"""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import json
import os
import re
from urllib.parse import urlsplit, parse_qsl, urlencode

from .native_protocol import API_PATH, MAX_BYTES, NativeProtocolError, load
from .store import Refused, _id

ORIGIN = "https://app.onlyfansapi.com"
SCHEMA = "ofapi.v1"


class HostedUnavailable(NativeProtocolError):
    """A safe GET may be probed again within the bounded connection attempt."""


def enabled():
    return os.environ.get("ONLYFANS_HOSTED_CONNECT_ENABLED") == "1"


def configured():
    token = os.environ.get("OFAPI_API_KEY", "")
    return enabled() and type(token) is str and bool(re.fullmatch(r"[\x21-\x7e]{16,4096}", token))


@dataclass(frozen=True, slots=True)
class HostedProviderSession:
    account_id: str
    owner_id: int
    policy_revision: int
    expires_at: datetime
    provider_account_id: str
    proxy_url: None = None

    def __post_init__(self):
        _id(self.owner_id)
        now = datetime.now(timezone.utc)
        if (not enabled() or self.account_id != "of-" + str(self.owner_id)
                or type(self.policy_revision) is not int or self.policy_revision < 1
                or type(self.provider_account_id) is not str
                or not re.fullmatch(r"acct_[A-Za-z0-9]{1,80}", self.provider_account_id)
                or type(self.expires_at) is not datetime or self.expires_at.tzinfo is None
                or not now < self.expires_at <= now + timedelta(days=1)
                or self.proxy_url is not None):
            raise Refused("invalid hosted account scope or lease")

    def current(self):
        if not configured() or datetime.now(timezone.utc) >= self.expires_at:
            raise Refused("hosted connection is unavailable")


def session_from_saved(*, payload, account_id, policy_revision, expires_at=None):
    """Pure conversion; the calling API validates exact payload keys separately."""
    expiry = expires_at or datetime.fromisoformat(payload["expiresAt"].replace("Z", "+00:00"))
    if "hosted" in payload:
        value = payload["hosted"]
        if (type(value) is not dict or set(value) != {"schemaVersion", "accountId"}
                or value["schemaVersion"] != SCHEMA or payload.get("proxyUrl") is not None):
            raise Refused("invalid hosted connection reference")
        return HostedProviderSession(account_id, payload["ownerId"], policy_revision, expiry, value["accountId"])
    from .provider_connector import session_from_capture
    return session_from_capture(account_id=account_id, owner_id=payload["ownerId"],
        policy_revision=policy_revision, capture=payload["capture"], x_hash=payload["xHash"],
        proxy_url=payload.get("proxyUrl"), expires_at=expiry)


def _invalid_json():
    raise NativeProtocolError("invalid hosted JSON value")


def call_once(session, method, path, body=b"", *, timeout=5, maximum=MAX_BYTES, idempotency_key=None):
    """One fixed-origin request; an uncertain result is never retried here."""
    session.current()
    if (method not in {"GET", "POST"} or type(path) is not str or len(path) > 2048
            or not path.startswith(API_PATH + "/") or any(c in path for c in "\r\n#\\")
            or type(body) is not bytes or len(body) > MAX_BYTES):
        raise Refused("invalid hosted provider request")
    target = urlsplit(path)
    if target.scheme or target.netloc or ".." in target.path.split("/"):
        raise Refused("invalid hosted provider target")
    relative = target.path[len(API_PATH):]
    if method == "GET":
        allowed = relative in {"/users/me", "/chats", "/vault/media"} or re.fullmatch(r"/chats/[1-9][0-9]*/messages", relative)
        if not allowed or body or idempotency_key is not None:
            raise Refused("unsupported hosted provider read")
    elif (not re.fullmatch(r"/chats/[1-9][0-9]*/messages", relative) or target.query
            or not body or type(idempotency_key) is not str
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key)):
        raise Refused("recorded hosted send identity is required")
    mapped = {"/users/me":"/me","/vault/media":"/media/vault"}.get(relative,relative)
    url = ORIGIN + "/api/" + session.provider_account_id + mapped
    if target.query:
        pairs=parse_qsl(target.query,keep_blank_values=True,strict_parsing=True)
        if len({key for key,_ in pairs})!=len(pairs):raise Refused("duplicate hosted query")
        # The provider names the native descending message cursor first_id.
        if re.fullmatch(r"/chats/[1-9][0-9]*/messages",relative):
            pairs=[("first_id" if key=="id" else key,value) for key,value in pairs]
        url += "?" + urlencode(pairs)
    headers = {"Authorization": "Bearer " + os.environ["OFAPI_API_KEY"],
        "Accept": "application/json", "Content-Type": "application/json",
        "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    if method == "POST":
        headers["Idempotency-Key"] = idempotency_key
    from curl_cffi import requests
    chunks, size = [], 0
    def receive(chunk):
        nonlocal size
        size += len(chunk)
        if size > maximum:
            raise NativeProtocolError("hosted response exceeded its byte budget")
        chunks.append(chunk)
        return len(chunk)
    try:
        with requests.Session(trust_env=False, retry=0) as client:
            result = client.request(method, url, headers=headers, data=body or None,
                timeout=timeout, allow_redirects=False, verify=True, accept_encoding="identity",
                content_callback=receive, discard_cookies=True)
            if method=="GET" and result.status_code in {401,408,425,429,500,502,503,504}:
                raise HostedUnavailable("hosted account verification is not ready")
            if (result.status_code != 200 or result.headers.get("content-type", "").split(";")[0].strip() != "application/json"
                    or result.headers.get("content-encoding", "identity").strip().lower() != "identity"):
                raise NativeProtocolError("hosted request outcome is uncertain")
            envelope = load(b"".join(chunks))
            data=envelope.get("data")
            if method=="GET" and relative!="/users/me":
                pagination=envelope.get("_pagination")
                if type(data)is not list or type(pagination)is not dict or "next_page" not in pagination:
                    raise NativeProtocolError("hosted page completeness is unavailable")
                following=pagination["next_page"]
                if following is not None:
                    if type(following)is not str or len(following)>4096 or not data:
                        raise NativeProtocolError("invalid hosted continuation")
                    next_url=urlsplit(following)
                    if (next_url.scheme!="https" or next_url.netloc!="app.onlyfansapi.com"
                            or next_url.path!="/api/"+session.provider_account_id+mapped or next_url.fragment):
                        raise NativeProtocolError("foreign hosted continuation")
                    next_pairs=parse_qsl(next_url.query,keep_blank_values=True,strict_parsing=True)
                    if len({key for key,_ in next_pairs})!=len(next_pairs):
                        raise NativeProtocolError("duplicate hosted continuation")
                    values=dict(next_pairs)
                    if re.fullmatch(r"/chats/[1-9][0-9]*/messages",relative):
                        if (type(data[-1]) is not dict or type(data[-1].get("id")) is not int
                                or values.get("first_id",values.get("id"))!=str(data[-1]["id"])
                                or values.get("order","desc")!="desc"):
                            raise NativeProtocolError("hosted message cursor is incompatible")
                    elif values.get("offset")!=str(int(dict(parse_qsl(target.query)).get("offset","0"))+len(data)):
                        raise NativeProtocolError("hosted page cursor is incompatible")
                data={"list":data,"hasMore":following is not None}
            elif type(data)is not dict:
                raise NativeProtocolError("invalid hosted response envelope")
            raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=lambda value: str(value) if isinstance(value, Decimal) else _invalid_json()).encode()
            if len(raw) > maximum:
                raise NativeProtocolError("hosted response exceeded its byte budget")
            return raw
    except (Refused, NativeProtocolError):
        raise
    except Exception:
        if size > maximum:raise NativeProtocolError("hosted response exceeded its byte budget") from None
        if method=="GET":raise HostedUnavailable("hosted account verification is unavailable") from None
        raise NativeProtocolError("hosted request outcome is uncertain") from None
