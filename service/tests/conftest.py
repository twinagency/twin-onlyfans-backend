"""Test-wide settings.

The provider read pace (default five seconds per account) is switched off for the existing suites, which drive
several readers back to back on a real clock; the lease stays active and the pace has its own tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _provider_read_pace_off(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROVIDER_READ_INTERVAL_SECONDS", "0")
