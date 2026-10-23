"""Known post detail reads are bounded, account-pinned and strictly read-only."""
import pytest

from test_onlyfans_provider_adapters import credential
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused


def test_post_detail_pins_owner_and_preserves_explicit_missing_status(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    calls = []
    def call(self, method, path, body=b""):
        calls.append((method, path, body))
        return b'{"id":7001}'
    def signed(self, method, path, body=b"", **options):
        calls.append((method, path, body, options))
        return 404, b'{}'
    monkeypatch.setattr(NativeProviderConnector, "_call", call)
    monkeypatch.setattr(NativeProviderConnector, "_signed_request", signed)
    assert NativeProviderConnector(credential()).post_by_id(901) == (404, b'{}')
    assert calls[0] == ("GET", "/api2/v2/users/me", b"")
    assert calls[1][:3] == ("GET", "/api2/v2/posts/901", b"{}")
    assert calls[1][3]["accepted_statuses"] == (200, 404) and calls[1][3]["with_status"] is True


@pytest.mark.parametrize("value", [True, 0, -1, "901", 2**63])
def test_invalid_post_detail_never_reads_provider(value, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setattr(NativeProviderConnector, "verify_identity", lambda self: pytest.fail("invalid source opened a read"))
    with pytest.raises(Refused):
        NativeProviderConnector(credential()).post_by_id(value)
