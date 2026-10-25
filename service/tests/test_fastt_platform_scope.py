"""Platform discovery, new creator onboarding and sender ownership; no provider I/O."""
import json
import pytest
from onlyfans_runtime import fastt_scope as fs, fastt_manual as manual
from onlyfans_runtime.activation import deployment_scope, validate_selection

@pytest.fixture
def platform(monkeypatch):
    fs.reset_cache()
    clock = [1000.0]
    accounts = {"of-7"}
    rows = {}
    monkeypatch.setenv(fs.SCOPE_ENV, "db:*")
    monkeypatch.setattr(fs, "_clock", lambda: clock[0])
    monkeypatch.setattr(fs, "_discoverer", lambda: set(accounts))
    monkeypatch.setattr(fs, "_reader", lambda ids: {i: json.dumps({"fastt_scope": rows[i]}) if i in rows else None for i in ids})
    yield clock, accounts, rows
    fs.reset_cache()

def test_registered_creator_defaults_to_all_eligible_fans(platform):
    assert fs.desired_scope() == (set(), {"7"})
    assert manual.fastt_serves("of-7", 123)
    assert not manual.fastt_serves("of-8", 123)
    assert deployment_scope("of-7") == {"deploymentAllowed": True, "allFansAllowed": True, "allowedFans": []}
    assert validate_selection("of-7", {"mode": "all", "fans": []})["mode"] == "all"

def test_new_creator_appears_without_environment_edit(platform):
    clock, accounts, _ = platform
    fs.desired_scope()
    accounts.add("of-8")
    clock[0] += fs.TTL_S + 1
    assert fs.desired_scope() == (set(), {"7", "8"})
    assert manual.fastt_serves("of-8", 456)

def test_revoked_binding_is_removed_from_serving(platform):
    clock, accounts, _ = platform
    fs.desired_scope()
    accounts.clear()
    clock[0] += fs.TTL_S + 1
    assert fs.desired_scope() == (set(), set())
    assert not manual.fastt_serves("of-7", 123)

def test_explicit_per_creator_fan_selection_is_retained(platform):
    _, _, rows = platform
    rows["of-7"] = {"mode": "single", "fans": [12]}
    assert manual.fastt_serves("of-7", 12)
    assert not manual.fastt_serves("of-7", 13)
    assert manual.fastt_owned("of-7", 13)

def test_discovery_failure_never_activates_unknown_or_legacy_sender(platform, monkeypatch):
    def fail():
        raise OSError("offline")
    monkeypatch.setattr(fs, "_discoverer", fail)
    assert fs.desired_scope() == (set(), set())
    assert manual.fastt_owned("of-8", 456)
    assert manual.fastt_account_owned("of-8")
    assert not manual.fastt_serves("of-8", 456)

def test_discovery_transient_failure_preserves_registered_accounts(platform, monkeypatch):
    clock, _, _ = platform
    fs.desired_scope()
    def fail():
        raise OSError("offline")
    monkeypatch.setattr(fs, "_discoverer", fail)
    clock[0] += fs.TTL_S + 1
    assert fs.desired_scope() == (set(), {"7"})

def test_platform_token_is_explicit(monkeypatch):
    for value in ("", "db:7", "db:*oops", "of-7:*"):
        assert not fs.platform_scope_enabled(value)
    assert fs.platform_scope_enabled(" db:* ")
