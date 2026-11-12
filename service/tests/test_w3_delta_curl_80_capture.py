"""W3 delta-curl-80 red tests (W3DC-03): a pasted curl is bound to one OnlyFans login.

Synthetic data only (user 123, sess=FAKE). No network: urllib.request.urlopen and
curl_cffi are faked to record calls. Red at f5205ddd / a253c84c (the auth_id cookie and
the target account were never compared with the curl's user-id). W3DC-01/07 red at b9a8c516.
"""
import io
import sys
import types
import urllib.request
import pytest
from of_signer import sign
import session_bootstrap as sb

OWNER = 123
RULES = dict(static_param="synthetic-static", start="synthetic", end="test",
             checksum_indexes=list(range(32)), checksum_constant=17)
CHUNK = " + ".join(f"W[{10000+i},W[x]](0)" for i in range(32))
URL = "https://onlyfans.com/api2/v2/users/me"
T = 1762900000000
PROXY = "http://synthetic:proxy-secret@proxy.example:8080"


def curl(cookies="-b 'auth_id=123; sess=FAKE'", *, rev="synthetic-rev", x_hash=" -H 'x-hash: synthetic-hash'", sign_value=None):
    s = sign_value or sign(URL, OWNER, RULES, T)["sign"]
    return (f"curl '{URL}' -H 'user-id: 123' -H 'x-bc: synthetic-bc' -H 'x-of-rev: {rev}'"
            f" -H 'sign: {s}' -H 'time: {T}' -H 'user-agent: Synthetic browser'"
            f" -H 'x-relay-static-param: synthetic-static'{x_hash} {cookies}")


def _body(url):
    return (CHUNK if url.endswith("2313.js") else "synthetic-hash").encode()


@pytest.fixture
def wire(monkeypatch):
    calls = []  # (transport, url, kwargs)
    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(("urlopen", url, None))
        return io.BytesIO(_body(url))
    class Client:
        def __init__(self, **kw): self.kw = kw
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw):
            calls.append(("curl_cffi", url, {**self.kw, **kw}))
            kw["content_callback"](_body(url))
            return types.SimpleNamespace(status_code=200, headers={})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setitem(sys.modules, "curl_cffi", types.SimpleNamespace(requests=types.SimpleNamespace(Session=Client)))
    return calls


def test_auth_id_cookie_must_match_user_id(wire):
    # browser_capture.validate_browser_payload requires auth_id == owner; the curl path must too.
    with pytest.raises(sb.OwnerMismatch):
        sb.build_connection_capture(curl("-b 'auth_id=999; sess=FAKE'"), account="of-123", proxy=None)
    assert wire == []


def test_curl_of_another_owner_refused_for_account(wire):
    with pytest.raises(sb.OwnerMismatch):
        sb.build_connection_capture(curl(), account="of-999", proxy=None)
    assert wire == []
    assert sb.build_connection_capture(curl(), account="of-123", proxy=None)["ownerId"] == OWNER


def test_missing_auth_id_is_a_parse_error_not_a_mismatch(wire):
    with pytest.raises(sb.CurlParseError, match="no auth_id") as e:
        sb.build_connection_capture(curl("-b 'sess=FAKE'"), account="of-123", proxy=None)
    assert not isinstance(e.value, sb.OwnerMismatch)


def test_legacy_from_curl_refuses_auth_id_mismatch_before_any_write(wire, monkeypatch):
    monkeypatch.setattr(sb, "SESSIONS_DIR", None)  # any write attempt would crash differently
    with pytest.raises(sb.OwnerMismatch):
        sb.from_curl(curl("-b 'auth_id=999; sess=FAKE'"))
    assert wire == []


def test_bootstrap_route_maps_owner_mismatch_to_409_not_500(wire):
    from fastapi import HTTPException
    import server
    body = server._BootstrapBody(mode="paste-curl", curl=curl("-b 'auth_id=999; sess=FAKE'"))
    with pytest.raises(HTTPException) as e:
        server.bootstrap_session(body)
    assert (e.value.status_code, e.value.detail) == (409, "owner_mismatch")
    assert wire == []


def _route(monkeypatch, proxy):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from onlyfans_commands import connection_api as api
    for k, v in {"ONLYFANS_PROCESS_ROLE": "api", "ONLYFANS_CONNECTION_TOKEN": "synthetic-connect",
                 "ONLYFANS_CONNECTION_TENANT": "tenant-synthetic", "ONLYFANS_CONNECTION_KEY": "42" * 32}.items():
        monkeypatch.setenv(k, v)
    seen = []
    def get_capture_proxy(store, tenant, account):
        seen.append((tenant, account))
        if isinstance(proxy, Exception): raise proxy
        return proxy
    monkeypatch.setattr(api, "get_capture_proxy", get_capture_proxy, raising=False)
    app = FastAPI(); app.include_router(api.router); app.state.control_store = object()
    r = TestClient(app).post("/onlyfans/connection/v1/accounts/of-123/capture-from-curl",
                             headers={"Authorization": "Bearer synthetic-connect", api.HEADER: api.SCHEMA},
                             json={"curl": curl(x_hash="")})
    return r, seen


def test_no_direct_urllib_egress_to_onlyfans(wire, monkeypatch):
    # W3DC-01: the curl-paste route fetches X-Hash + 2313.js via curl_cffi through the
    # account's assigned proxy (no env proxies, no redirects) -- never direct urllib.
    r, seen = _route(monkeypatch, PROXY)
    assert [c for c in wire if c[0] == "urlopen"] == [], f"direct urllib egress: {wire}"
    assert r.status_code == 200 and r.json()["xHash"] == "synthetic-hash", r.text
    assert seen == [("tenant-synthetic", "of-123")]
    assert [u for _, u, _ in wire] == ["https://cdn2.onlyfans.com/hash/?u=123",
                                       "https://static2.onlyfans.com/static/prod/f/synthetic-rev/2313.js"]
    for _, _, kw in wire:
        assert (kw["proxy"], kw["trust_env"], kw["allow_redirects"], kw["impersonate"], kw["timeout"]) == (PROXY, False, False, "chrome", 8)


def test_unreadable_account_proxy_refuses_without_fetch(wire, monkeypatch):
    # No fallback to direct egress when the account's sealed proxy cannot be read.
    from onlyfans_runtime.store import Refused
    r, seen = _route(monkeypatch, Refused("stored connection credentials are unavailable"))
    assert seen and r.status_code == 409 and "capture" not in r.json() and wire == []


def test_x_of_rev_validated_before_url(wire):
    # W3DC-07: x-of-rev must match the browser-capture rule before it reaches a URL.
    with pytest.raises(sb.CurlParseError):
        sb.build_connection_capture(curl(rev="../../evil"), account="of-123", proxy=None)
    assert not any(".." in u for _, u, _ in wire), f"unvalidated x-of-rev reached URL: {wire}"


def test_asset_fetch_is_byte_capped(wire, monkeypatch):
    # F2-08: an oversized X-Hash body is refused, not buffered (cap 4096 bytes).
    from onlyfans_runtime.browser_capture import fetch_asset
    from onlyfans_runtime.store import Refused
    monkeypatch.setitem(globals(), "_body", lambda url: b"h" * 4097)
    with pytest.raises(Refused):
        fetch_asset("hash", owner=123, rev="synthetic-rev", user_agent="Synthetic browser", proxy=PROXY)


def test_capture_build_requires_explicit_proxy():
    # F2-02: no default proxy, so a new caller cannot silently go direct.
    import inspect
    for f in (sb.build_connection_capture, sb.build_connection_capture_from_request):
        assert inspect.signature(f).parameters["proxy"].default is inspect.Parameter.empty, f


@pytest.mark.parametrize("bad", ["FAKESIGNSECRET", "synthetic:" + "a" * 40 + ":NOTHEXSECRET:test"])
def test_sign_value_not_echoed_in_error(wire, bad):
    # W3DC-06: malformed Sign / non-hex checksum errors state the problem, never the value.
    for build in (lambda c: sb.build_connection_capture(c, account="of-123", proxy=None), sb.from_curl):
        with pytest.raises(sb.CurlParseError) as e:
            build(curl(sign_value=bad))
        assert "SECRET" not in str(e.value), str(e.value)


def test_cookie_header_form_not_silently_dropped(wire):
    # W3DC-04: Firefox "Copy as cURL" sends -H 'Cookie: ...' instead of -b.
    out = sb.build_connection_capture(curl("-H 'Cookie: auth_id=123; sess=FAKE'"), account="of-123", proxy=None)
    assert {c["name"]: c["value"] for c in out["capture"]["cookies"]} == {"auth_id": "123", "sess": "FAKE"}
