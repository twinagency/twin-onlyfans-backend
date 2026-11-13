"""F1b red tests: an Incogniton / Playwright capture is bound to one OnlyFans login.

`_adopt_into_account` (run_incogniton / run_playwright_proxy) filed a capture by
headers.user_id with no auth_id cookie check and no check against the account being
re-captured. It must reuse F1's `capture_owner` check before any move or registry write.
Synthetic data only (user 123, sess=FAKE); no network. Red at f23dc8e4.
"""
import json
import sys
import types
import pytest
import session_bootstrap as sb


@pytest.fixture
def env(tmp_path, monkeypatch):
    legacy = tmp_path / "sessions"
    legacy.mkdir()
    writes = []  # every registry write
    reg = types.SimpleNamespace(
        account_dir=lambda aid: legacy / "accounts" / str(aid),
        get_account=lambda aid: None,
        record_session=lambda aid, p: writes.append(("record_session", aid)),
        upsert_account=lambda aid, **kw: writes.append(("upsert_account", aid)),
        set_active_account_id=lambda aid: writes.append(("set_active", aid)),
    )
    monkeypatch.setattr(sb, "account_registry", reg)
    monkeypatch.setattr(sb, "SESSIONS_DIR", legacy)

    def raw(cookies):
        (legacy / "chunk_2313_T.js").write_text("synthetic chunk")
        p = legacy / "session_T.json"
        p.write_text(json.dumps({"headers": {"user_id": "123"}, "cookies": cookies,
                                 "signing": {"chunk_2313_file": "chunk_2313_T.js"}}))
        return p
    return types.SimpleNamespace(legacy=legacy, writes=writes, raw=raw)


def _unmoved(env):
    assert sorted(p.name for p in env.legacy.iterdir()) == ["chunk_2313_T.js", "session_T.json"]
    assert env.writes == []


def _adopt(path, **kw):
    return sb._adopt_into_account(path, nickname=None, incogniton_profile_id=None, make_active=True, **kw)


def test_auth_id_mismatch_refused_before_any_write(env):
    with pytest.raises(sb.OwnerMismatch) as e:
        _adopt(env.raw([{"name": "auth_id", "value": "999"}, {"name": "sess", "value": "FAKE"}]))
    assert "999" not in str(e.value) and "FAKE" not in str(e.value)
    _unmoved(env)


def test_missing_auth_id_is_a_parse_error_not_a_mismatch(env):
    with pytest.raises(sb.CurlParseError, match="no auth_id") as e:
        _adopt(env.raw([{"name": "sess", "value": "FAKE"}]))
    assert not isinstance(e.value, sb.OwnerMismatch) and "FAKE" not in str(e.value)
    _unmoved(env)


def _fake_capture(monkeypatch, tmp_path, path):
    # run_incogniton pops + re-imports capture_session: serve a synthetic one from tmp.
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "capture_session.py").write_text(f"from pathlib import Path\ndef capture(): return Path({str(path)!r})\n")
    monkeypatch.syspath_prepend(str(mods))
    monkeypatch.setitem(sys.modules, "capture_session", None)  # teardown drops the synthetic module
    monkeypatch.setitem(sys.modules, "extract_rules", types.SimpleNamespace(main=lambda argv: 0))


def test_recapture_of_another_owner_refused(env, monkeypatch, tmp_path):
    # Re-capturing account 999 must not file a login of 123 (and hand it 999's proxy / profile).
    _fake_capture(monkeypatch, tmp_path, env.raw([{"name": "auth_id", "value": "123"}]))
    with pytest.raises(sb.OwnerMismatch):
        sb.run_incogniton(None, account_id="999")
    _unmoved(env)


def test_matching_capture_is_adopted(env, monkeypatch, tmp_path):
    _fake_capture(monkeypatch, tmp_path, env.raw([{"name": "auth_id", "value": "123"}]))
    final = sb.run_incogniton(None, account_id="123")
    assert final == env.legacy / "accounts" / "123" / "session_T.json" and final.exists()
    assert (final.parent / "chunk_2313_T.js").exists()
    assert env.writes == [("record_session", "123"), ("upsert_account", "123"), ("set_active", "123")]
    # a fresh capture (no account hint) still files under its own login
    final.unlink()
    env.writes.clear()
    assert _adopt(env.raw([{"name": "auth_id", "value": "123"}])).parent.name == "123"


def test_playwright_recapture_of_another_owner_refused_before_proxy_bind(env, monkeypatch, tmp_path):
    raw = env.raw([{"name": "auth_id", "value": "123"}])
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "capture_session_proxy.py").write_text(
        f"from pathlib import Path\ndef capture(*a, **kw): return Path({str(raw)!r})\n")
    monkeypatch.syspath_prepend(str(mods))
    monkeypatch.setitem(sys.modules, "capture_session_proxy", None)
    monkeypatch.setitem(sys.modules, "extract_rules", types.SimpleNamespace(main=lambda argv: 0))
    monkeypatch.setitem(sys.modules, "proxies", types.SimpleNamespace(
        get_for_account=lambda aid: {"label": "synthetic"}, proxy_url=lambda p: "http://proxy.example:1",
        assign_account=lambda label, aid: env.writes.append(("assign_proxy", aid))))
    with pytest.raises(sb.OwnerMismatch):
        sb.run_playwright_proxy(None, account_id="999")
    _unmoved(env)


@pytest.mark.parametrize("cookies", [[{"name": "auth_id", "value": "999"}], [{"name": "sess", "value": "FAKE"}]])
def test_refused_capture_left_behind_is_never_migrated_or_activated(env, monkeypatch, cookies):
    # The refused raw file stays in the legacy dir; a later first-boot migrate_legacy must not
    # file it by headers.user_id and activate it (Codex F1b).
    import accounts
    with pytest.raises(sb.CurlParseError if cookies[0]["name"] == "sess" else sb.OwnerMismatch):
        _adopt(env.raw(cookies))
    for name, value in [("SESSIONS_DIR", env.legacy), ("ACCOUNTS_DIR", env.legacy / "accounts"),
                        ("ACTIVE_FILE", env.legacy / "active.json"), ("MIGRATED_MARKER", env.legacy / ".migrated")]:
        monkeypatch.setattr(accounts, name, value)
    out = accounts.migrate_legacy()
    assert out.get("active") is None and not out.get("moved")
    assert not (env.legacy / "active.json").exists()
    assert not list((env.legacy / "accounts").rglob("session_*.json"))


def test_matching_legacy_session_still_migrates(env, monkeypatch):
    import accounts
    env.raw([{"name": "auth_id", "value": "123"}])
    for name, value in [("SESSIONS_DIR", env.legacy), ("ACCOUNTS_DIR", env.legacy / "accounts"),
                        ("ACTIVE_FILE", env.legacy / "active.json"), ("MIGRATED_MARKER", env.legacy / ".migrated")]:
        monkeypatch.setattr(accounts, name, value)
    out = accounts.migrate_legacy()
    assert out["active"] == "123" and (env.legacy / "accounts" / "123" / "session_T.json").exists()
