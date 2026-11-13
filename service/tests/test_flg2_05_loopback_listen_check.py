"""FLG2-05: the loopback mint's listening check must not depend on the OS.

macOS raises ENOPROTOOPT for SO_ACCEPTCONN; that must still mint a listening
handle. Any other answer (0, or another OSError) must keep refusing.
"""
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket

import pytest

from onlyfans_runtime.loopback_transport import from_server
from onlyfans_runtime.store import Refused

ACCOUNT = "123456"
REAL = socket.socket.getsockopt


def _patched(monkeypatch, answer):
    def fake(self, level, name, *rest):
        if level == socket.SOL_SOCKET and name == socket.SO_ACCEPTCONN:
            if isinstance(answer, OSError):
                raise answer
            return answer
        return REAL(self, level, name, *rest)
    monkeypatch.setattr(socket.socket, "getsockopt", fake)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    yield srv
    srv.server_close()


def test_enoprotoopt_still_mints_a_listening_handle(monkeypatch, server):
    _patched(monkeypatch, OSError(errno.ENOPROTOOPT, "Protocol not available"))
    handle = from_server(server, account_id=ACCOUNT, policy_revision=1)
    assert handle.port == server.server_address[1]
    with socket.create_connection(("127.0.0.1", handle.port), timeout=2):
        pass


@pytest.mark.parametrize("answer", [0, OSError(errno.EBADF, "bad fd"), OSError(errno.EINVAL, "inval")])
def test_not_listening_or_other_error_refuses(monkeypatch, server, answer):
    _patched(monkeypatch, answer)
    with pytest.raises(Refused):
        from_server(server, account_id=ACCOUNT, policy_revision=1)


def test_readable_listening_socket_mints(monkeypatch, server):
    _patched(monkeypatch, 1)
    assert from_server(server, account_id=ACCOUNT, policy_revision=1).port == server.server_address[1]


def test_enoprotoopt_makes_a_bound_not_listening_fixture_listen(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler, bind_and_activate=False)
    try:
        srv.server_bind()
        _patched(monkeypatch, OSError(errno.ENOPROTOOPT, "Protocol not available"))
        handle = from_server(srv, account_id=ACCOUNT, policy_revision=1)
        with socket.create_connection(("127.0.0.1", handle.port), timeout=2):
            pass
    finally:
        srv.server_close()


def test_listen_failure_refuses(monkeypatch, server):
    _patched(monkeypatch, OSError(errno.ENOPROTOOPT, "Protocol not available"))
    def boom(self, *args):
        raise OSError(errno.EOPNOTSUPP, "not supported")
    monkeypatch.setattr(socket.socket, "listen", boom)
    with pytest.raises(Refused):
        from_server(server, account_id=ACCOUNT, policy_revision=1)
