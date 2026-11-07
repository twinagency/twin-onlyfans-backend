"""Opt-in, single-attempt HTTPS transport. Construction performs no I/O.

This primitive is not registered with the delivery worker. A production sender
must still go through the durable delivery authority and receive separate
activation approval. A custom httpx transport is accepted solely by callers
explicitly constructing this object (e.g. MockTransport in offline tests).
"""
from urllib.parse import urlsplit
import httpx
from .protocol import BASE, Response

MAX_BYTES = 1_000_000


class FansHttpsTransport:
    def __init__(self, *, enabled=False, transport=None):
        if enabled is not True:
            raise ValueError("explicit HTTPS transport enablement required")
        self._transport = transport

    def __call__(self, *, method, url, headers, body, timeout, follow_redirects):
        parsed = urlsplit(url)
        if (method not in ("GET", "POST") or parsed.scheme != "https"
                or parsed.netloc != urlsplit(BASE).netloc or parsed.username or parsed.password
                or parsed.fragment or not parsed.path.startswith("/api/")
                or type(headers) is not dict or follow_redirects is not False
                or type(timeout) not in (int, float) or not 0 < timeout <= 30
                or body is not None and (type(body) is not bytes or len(body) > MAX_BYTES)):
            raise ValueError("invalid bounded Fans HTTPS request")
        request_headers = dict(headers)
        request_headers["Accept-Encoding"] = "identity"
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
        transport = self._transport or httpx.HTTPTransport(retries=0, trust_env=False, limits=limits)
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                          timeout=timeout, limits=limits) as client:
            with client.stream(method, url, headers=request_headers, content=body,
                               follow_redirects=False) as response:
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise ValueError("encoded provider response refused")
                chunks, size = [], 0
                for chunk in response.iter_raw():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("provider response too large")
                    chunks.append(chunk)
                return Response(response.status_code, b"".join(chunks), dict(response.headers))
