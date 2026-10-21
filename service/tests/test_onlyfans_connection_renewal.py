"""Provider rejection classification uses synthetic HTTPS responses only."""
import pytest
from test_onlyfans_provider_adapters import fake_curl, credential
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeAuthenticationError
from onlyfans_runtime.native_protocol import NativeProtocolError


@pytest.mark.parametrize('status',[401,403])
def test_explicit_provider_rejection_requires_authentication(monkeypatch,status):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    fake_curl(monkeypatch,status=status)
    with pytest.raises(NativeAuthenticationError):NativeProviderConnector(credential()).verify_identity()


def test_different_provider_owner_is_never_a_successful_renewal(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    fake_curl(monkeypatch,body=b'{"id":9999}')
    with pytest.raises(NativeAuthenticationError):NativeProviderConnector(credential()).verify_identity()


@pytest.mark.parametrize('status',[429,500,502,503])
def test_transient_provider_errors_are_not_reported_as_logout(monkeypatch,status):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    fake_curl(monkeypatch,status=status)
    with pytest.raises(NativeProtocolError) as error:NativeProviderConnector(credential()).verify_identity()
    assert not isinstance(error.value,NativeAuthenticationError)
