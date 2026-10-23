"""429 classification exposes bounded pacing metadata, never response data."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import pytest

from test_onlyfans_provider_adapters import fake_curl, credential
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeRateLimitError, _rate_limit_seconds


@pytest.mark.parametrize('value,expected',[('15',15),('0',1),('999999999',3600),
    ('synthetic-secret-invalid-header',60),(None,60),('x'*129,60)])
def test_retry_after_is_bounded_and_invalid_values_use_fixed_default(value,expected):
    assert _rate_limit_seconds(value)==expected


def test_http_date_and_past_date_are_bounded():
    now=datetime.now(timezone.utc)
    assert 119<=_rate_limit_seconds(format_datetime(now+timedelta(seconds=120)))<=120
    assert _rate_limit_seconds(format_datetime(now-timedelta(seconds=120)))==1


def test_native_429_carries_no_body_url_or_session_in_error(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    fake_curl(monkeypatch,status=429,body=b'synthetic-private-provider-body',headers={'retry-after':'23'})
    reader=NativeProviderConnector(credential())
    with pytest.raises(NativeRateLimitError) as caught:reader.verify_identity()
    assert caught.value.retry_after==23
    assert str(caught.value)=='native provider rate limit'
    assert 'synthetic' not in repr(caught.value)
