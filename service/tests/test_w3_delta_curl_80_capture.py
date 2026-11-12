"""W3 delta-curl-80 red tests (W3DC-03): a pasted curl is bound to one OnlyFans login.

Synthetic data only (user 123, sess=FAKE). No network: urllib.request.urlopen is
monkeypatched to record calls. Red at f5205ddd / a253c84c (the auth_id cookie and
the target account were never compared with the curl's user-id).
"""
import io
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


def curl(cookies="-b 'auth_id=123; sess=FAKE'"):
    s = sign(URL, OWNER, RULES, T)["sign"]
    return (f"curl '{URL}' -H 'user-id: 123' -H 'x-bc: synthetic-bc' -H 'x-of-rev: synthetic-rev'"
            f" -H 'sign: {s}' -H 'time: {T}' -H 'user-agent: Synthetic browser'"
            f" -H 'x-relay-static-param: synthetic-static' -H 'x-hash: synthetic-hash' {cookies}")


@pytest.fixture
def wire(monkeypatch):
    calls = []
    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        return io.BytesIO((CHUNK if url.endswith("2313.js") else "synthetic-hash").encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def test_auth_id_cookie_must_match_user_id(wire):
    # browser_capture.validate_browser_payload requires auth_id == owner; the curl path must too.
    with pytest.raises(sb.OwnerMismatch):
        sb.build_connection_capture(curl("-b 'auth_id=999; sess=FAKE'"), account="of-123")
    assert wire == []


def test_curl_of_another_owner_refused_for_account(wire):
    with pytest.raises(sb.OwnerMismatch):
        sb.build_connection_capture(curl(), account="of-999")
    assert wire == []
    assert sb.build_connection_capture(curl(), account="of-123")["ownerId"] == OWNER


def test_missing_auth_id_is_a_parse_error_not_a_mismatch(wire):
    with pytest.raises(sb.CurlParseError, match="no auth_id") as e:
        sb.build_connection_capture(curl("-b 'sess=FAKE'"), account="of-123")
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
