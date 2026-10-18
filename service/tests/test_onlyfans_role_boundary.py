"""Dedicated roles cannot construct the legacy live provider client.

Each case uses a fresh interpreter so an earlier import or environment change
cannot mask role selection. The HTTP-session factory is a tripwire; no provider
client/session or network call is created, including in the legacy control case.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SERVICE = Path(__file__).resolve().parents[1]
_CHILD = r"""
import sys
from curl_cffi import requests

def unexpected_http_session(*args, **kwargs):
    raise AssertionError("a live HTTP session was constructed")

requests.Session = unexpected_http_session
sys.path.insert(0, sys.argv[1])
from of_client import OFClient

try:
    OFClient({})
except Exception as exc:
    if sys.argv[2] == "dedicated":
        assert type(exc) is RuntimeError, (type(exc).__name__, str(exc))
        assert str(exc) == "Live OFClient is unavailable in dedicated OnlyFans process roles"
    else:
        assert type(exc) is KeyError, (type(exc).__name__, str(exc))
        assert exc.args == ("headers",), exc.args
else:
    raise AssertionError("empty session was unexpectedly accepted")
"""


@pytest.mark.parametrize("role", ["api", "worker", "invalid-role", "", None],
                         ids=["api", "worker", "invalid", "empty", "legacy-unset"])
def test_live_client_constructor_role_boundary(role):
    env = dict(os.environ)
    env.pop("ONLYFANS_PROCESS_ROLE", None)
    if role is not None:
        env["ONLYFANS_PROCESS_ROLE"] = role
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(_SERVICE),
         "dedicated" if role is not None else "legacy"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
