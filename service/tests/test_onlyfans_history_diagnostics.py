import logging
import pytest
from onlyfans_runtime import history_diagnostics as diagnostics

def test_failure_diagnostics_redact_exception_values_and_limit_rate(monkeypatch, caplog):
    monkeypatch.setattr(diagnostics, "_LAST", float("-inf"))
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: 100)
    with caplog.at_level(logging.WARNING):
        diagnostics.record_history_failure(ValueError("SECRET body token user@example.com"))
        diagnostics.record_history_failure(ValueError("SECOND PRIVATE VALUE"))
    assert len(caplog.records) == 1
    assert "onlyfans_history_read_failed" in caplog.text
    assert "SECRET" not in caplog.text and "PRIVATE" not in caplog.text and "@" not in caplog.text

def test_history_failure_is_rethrown_without_retries(monkeypatch):
    from onlyfans_runtime.worker import read_step
    from types import SimpleNamespace
    error = ValueError("private provider failure")
    seen = []
    monkeypatch.setattr(diagnostics, "record_history_failure", lambda e: seen.append(e))
    class Reader:
        account_id = "fixture"
        calls = 0
        def messages(self, **kwargs):
            self.calls += 1
            raise error
    class Store:
        def drain(self, *args, **kwargs): return 0
        def claim_scan(self, *args, **kwargs): return None
        def claim_recovery(self, *args, **kwargs): return SimpleNamespace(fan_id=123, before_id=None)
    reader = Reader()
    with pytest.raises(ValueError) as caught:
        read_step(Store(), "tenant", "fixture", reader)
    assert caught.value is error and reader.calls == 1 and seen == [error]
