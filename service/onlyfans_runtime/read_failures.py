"""Allowlisted read failures. Never retain exception text, URLs or responses."""
from collections import OrderedDict
from enum import IntEnum
import json
import logging
import re
import time

_LOG = logging.getLogger(__name__)
_LAST = OrderedDict()
_CURL = {5: "proxy_dns", 6: "host_dns", 7: "connect", 18: "partial_response",
         23: "response_callback", 28: "timeout", 35: "tls", 52: "empty_response",
         55: "send", 56: "receive", 60: "tls_certificate", 77: "tls_certificate",
         92: "http2", 97: "proxy"}
ERROR_CODES = frozenset({"provider_timeout", "provider_dns_failed", "provider_proxy_failed",
    "provider_tls_failed", "provider_transport_failed", "provider_http_transient",
    "provider_rate_limited", "provider_auth_required", "provider_protocol_failed", "provider_thread_unavailable"})


def operation(path):
    # Only return fixed labels. In particular, never log a query or fan ID.
    path = path.split("?", 1)[0]
    if path == "/api2/v2/users/me": return "identity"
    if path == "/api2/v2/users/list": return "profiles"
    if path == "/api2/v2/chats": return "chats"
    if re.fullmatch(r"/api2/v2/chats/[0-9]+/messages", path): return "messages"
    return "other"


def observe(error, *, account, path, started, status=None, response_bytes=0,
            callback_failed=False):
    """Classify one failed GET and log bounded scalar metadata, without retries."""
    from .provider_connector import (NativeAuthenticationError, NativeRateLimitError,
                                     NativeReadTransientError)
    from .native_protocol import NativeProtocolError
    raw = getattr(error, "code", None)
    code = int(raw) if type(raw) is int or isinstance(raw, IntEnum) else None
    code = code if code in _CURL else None
    category = _CURL.get(code, "transport")
    if callback_failed:
        category, failure = "response_limit", "provider_protocol_failed"
    elif type(status) is int and status in {404, 410} and operation(path) == 'messages':
        category, failure = 'thread_unavailable', 'provider_thread_unavailable'
    elif isinstance(error, NativeAuthenticationError):
        category, failure = "authentication", "provider_auth_required"
    elif isinstance(error, NativeRateLimitError):
        category, failure = "rate_limit", "provider_rate_limited"
    elif type(status) is int and status in {408, 425, 500, 502, 503, 504}:
        category, failure = "http_transient", "provider_http_transient"
    elif isinstance(error, NativeProtocolError) and not isinstance(error, NativeReadTransientError):
        category, failure = "protocol", "provider_protocol_failed"
    else:
        failure = {"timeout": "provider_timeout", "host_dns": "provider_dns_failed",
            "proxy_dns": "provider_proxy_failed", "proxy": "provider_proxy_failed",
            "tls": "provider_tls_failed", "tls_certificate": "provider_tls_failed"}.get(
                category, "provider_transport_failed")
    result = {"failure": failure, "category": category, "curlCode": code,
        "httpStatus": status if type(status) is int and 100 <= status <= 599 else None,
        "elapsedMs": max(0, min(3_600_000, int((time.monotonic() - started) * 1000))),
        "responseBytes": max(0, min(10_000_000, response_bytes))}
    # Only account IDs, fixed operation/category labels and bounded numbers.
    account = account if type(account) is str and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", account) else "unavailable"
    key = (account, operation(path), category)
    now = time.monotonic()
    previous = _LAST.get(key, float("-inf"))
    if now - previous >= 60:
        _LAST[key] = now
        _LAST.move_to_end(key)
        while len(_LAST) > 256: _LAST.popitem(last=False)
        _LOG.warning("onlyfans_provider_read %s", json.dumps({"account": account,
            "operation": key[1], **result}, sort_keys=True))
    return result


def failure_code(error):
    """Durable cursor status uses the same fixed vocabulary as diagnostics."""
    value = getattr(error, "read_failure", None)
    if type(value) is dict and type(value.get("failure")) is str and value["failure"] in ERROR_CODES:
        return value["failure"]
    from .provider_connector import NativeAuthenticationError, NativeRateLimitError, NativeReadTransientError
    from .native_protocol import NativeProtocolError
    if isinstance(error, NativeAuthenticationError): return "provider_auth_required"
    if isinstance(error, NativeRateLimitError): return "provider_rate_limited"
    if isinstance(error, NativeReadTransientError): return "provider_transport_failed"
    if isinstance(error, NativeProtocolError): return "provider_protocol_failed"
    return "provider_read_failed"
