"""W3 delta-curl-80 red test (W3DC-03) for session_bootstrap.build_connection_capture (PR #80).

Synthetic data only (user 123, sess=FAKE). No network: urllib.request.urlopen is
monkeypatched to record calls. Red at f5205ddd; green once
the curl's auth_id cookie must match its user-id.
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


def curl(*, cookies="-b 'auth_id=123; sess=FAKE'", rev="synthetic-rev", sign_value=None,
         static="synthetic-static", x_hash="synthetic-hash"):
    s = sign_value or sign(URL, OWNER, RULES, T)["sign"]
    extra = (f" -H 'x-relay-static-param: {static}'" if static else "") + (f" -H 'x-hash: {x_hash}'" if x_hash else "")
    return (f"curl '{URL}' -H 'user-id: 123' -H 'x-bc: synthetic-bc' -H 'x-of-rev: {rev}'"
            f" -H 'sign: {s}' -H 'time: {T}' -H 'user-agent: Synthetic browser'{extra} {cookies}")


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
    with pytest.raises(Exception):
        sb.build_connection_capture(curl(cookies="-b 'auth_id=999; sess=FAKE'"))
