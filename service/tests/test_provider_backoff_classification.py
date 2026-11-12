"""Timeouts, upstream throttling and local response limits stay distinct."""
from datetime import datetime, timedelta, timezone
import sys
from types import SimpleNamespace

import pytest

from onlyfans_runtime import provider_connector as connector
from onlyfans_runtime import provider_read_budget as budget
from onlyfans_runtime.worker import _release_provider
from onlyfans_runtime.store import Refused


class CurlFault(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__('https://synthetic-secret.invalid/?cookie=do-not-log')


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.delenv('ONLYFANS_PROVIDER_READ_TIMEOUT_SECONDS', raising=False)
    monkeypatch.setattr(connector, 'sign', lambda *_: {'sign': 'synthetic', 'time': '1'})
    state = SimpleNamespace(calls=[], status=200, error=None, payload=b'{}', wrapped=False)

    class HttpSession:
        def __init__(self, **kw):
            assert kw['retry'] == 0
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def request(self, method, url, **kw):
            state.calls.append((method, kw['timeout']))
            if state.error: raise state.error
            try:
                kw['content_callback'](state.payload)
            except Exception:
                if state.wrapped: raise CurlFault(23) from None
                raise
            return SimpleNamespace(status_code=state.status, headers={
                'content-type': 'application/json', 'retry-after': '17'})

    monkeypatch.setitem(sys.modules, 'curl_cffi', SimpleNamespace(requests=SimpleNamespace(Session=HttpSession)))
    session = connector.NativeProviderSession('of-read-test', 7, 1,
        datetime.now(timezone.utc)+timedelta(hours=1), 'synthetic-agent', 'bc', 'rev', 'hash',
        (('sess', 'synthetic'),), ('static', 'start', 'end', tuple([1]*32), 1), None)
    return state, session


def test_read_timeout_configuration_keeps_write_timeout_and_one_attempt(transport, monkeypatch):
    state, session = transport
    monkeypatch.setenv('ONLYFANS_PROVIDER_READ_TIMEOUT_SECONDS', '10')
    reader = connector.NativeProviderConnector(session, timeout_seconds=2)
    reader._signed_request('GET', '/api2/v2/users/me')
    reader._signed_request('POST', '/api2/v2/chats/9/messages', b'{}')
    assert state.calls == [('GET', 10), ('POST', 2)]


def test_default_read_timeout_outlasts_slow_history_pages(transport):
    # Measured: a 77-row (536 KB) message page takes ~6 s server-side; 5 s timed out every time.
    state, session = transport
    reader = connector.NativeProviderConnector(session)
    reader._signed_request('GET', '/api2/v2/chats/9/messages?limit=100')
    reader._signed_request('POST', '/api2/v2/chats/9/messages', b'{}')
    assert state.calls == [('GET', 10), ('POST', 5)]


@pytest.mark.parametrize('value', ['0', '11', 'nan', 'inf', '', 'invalid'])
def test_invalid_read_timeout_refused(transport, monkeypatch, value):
    _, session = transport
    monkeypatch.setenv('ONLYFANS_PROVIDER_READ_TIMEOUT_SECONDS', value)
    with pytest.raises(Refused, match='read timeout'):
        connector.NativeProviderConnector(session)


def test_timeout_records_safe_curl_code_and_is_not_rate_limit(transport, caplog):
    state, session = transport
    state.error = CurlFault(28)
    with pytest.raises(connector.NativeReadTransientError) as caught:
        connector.NativeProviderConnector(session)._signed_request('GET', '/api2/v2/users/me')
    assert caught.value.read_failure['failure'] == 'provider_timeout'
    assert caught.value.read_failure['curlCode'] == 28
    assert len(state.calls) == 1
    assert 'do-not-log' not in caplog.text and 'synthetic-secret' not in caplog.text


@pytest.mark.parametrize('status,kind,failure', [
    (429, connector.NativeRateLimitError, 'provider_rate_limited'),
    (503, connector.NativeReadTransientError, 'provider_http_transient'),
])
def test_http_failure_preserves_status_and_type(transport, status, kind, failure):
    state, session = transport
    state.status = status
    with pytest.raises(kind) as caught:
        connector.NativeProviderConnector(session)._signed_request('GET', '/api2/v2/users/me')
    assert caught.value.read_failure['httpStatus'] == status
    assert caught.value.read_failure['failure'] == failure
    assert len(state.calls) == 1


@pytest.mark.parametrize('wrapped', [False, True])
def test_response_overflow_remains_a_local_size_failure(transport, wrapped):
    state, session = transport
    state.payload, state.wrapped = b'x'*9, wrapped
    with pytest.raises(connector.NativeProtocolError, match='byte budget') as caught:
        connector.NativeProviderConnector(session)._signed_request('GET', '/api2/v2/users/me', max_response_bytes=8)
    assert type(caught.value) is connector.NativeProtocolError
    assert caught.value.read_failure['category'] == 'response_limit'
    assert caught.value.read_failure['responseBytes'] == 9
    assert len(state.calls) == 1


@pytest.mark.parametrize('error,limited', [
    (connector.NativeRateLimitError(17), True),
    (connector.NativeReadTransientError(5), False),
    (None, False),
])
def test_release_marks_only_a_real_rate_limit(monkeypatch, error, limited):
    calls = []
    monkeypatch.setattr(budget, 'release', lambda *a, **kw: calls.append(kw))
    _release_provider(None, 'tenant', object(), error)
    assert calls == [{'retry_after': getattr(error, 'retry_after', None), 'rate_limited': limited}]


@pytest.mark.parametrize('error,limited', [
    (connector.NativeRateLimitError(17), True),
    (connector.NativeReadTransientError(5), False),
])
def test_independent_recent_inbox_preserves_rate_limit_classification(monkeypatch, error, limited):
    from onlyfans_runtime import recent_inbox
    claim = object()
    calls = []
    monkeypatch.setattr(recent_inbox, 'acquire', lambda *a, **kw: claim)
    monkeypatch.setattr(recent_inbox, 'release', lambda *a, **kw: calls.append(kw))
    def read(): raise error
    with pytest.raises(type(error)):
        recent_inbox._read(None, 'tenant', 'of-read-test', 1, read, None)
    assert calls == [{'retry_after': error.retry_after, 'rate_limited': limited}]
