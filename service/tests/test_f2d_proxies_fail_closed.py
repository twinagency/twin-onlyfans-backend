"""F2d: an existing-but-unreadable proxies.json must fail closed, never read as
"no proxy assigned" (which routes the account's OF traffic out direct)."""
import json
import os

import pytest

import proxies

BAD = {
    "corrupt_json": '{"proxies": [',
    "not_a_dict": '[{"label": "x"}]',
    "no_proxies_key": '{"other": []}',
    "proxies_not_list": '{"proxies": {"label": "x"}}',
}
ENTRY = {"label": "t-1", "scheme": "http", "host": "127.0.0.1", "port": 9,
         "assigned_account_ids": ["acct-test"]}


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "proxies.json"
    monkeypatch.setattr(proxies, "REGISTRY_PATH", path)
    return path


def test_missing_file_is_empty_registry(reg):
    assert proxies.load() == {"proxies": []}
    assert proxies.get_for_account("acct-test") is None


@pytest.mark.parametrize("raw", BAD.values(), ids=BAD.keys())
def test_unreadable_registry_raises_not_empty(reg, raw):
    reg.write_text(raw)
    with pytest.raises(proxies.ProxyRegistryUnreadable):
        proxies.load()
    with pytest.raises(proxies.ProxyRegistryUnreadable):
        proxies.get_for_account("acct-test")
    with pytest.raises(proxies.ProxyRegistryUnreadable):
        proxies.get_for_session("session_x.json")


@pytest.mark.parametrize("raw", BAD.values(), ids=BAD.keys())
def test_mutation_on_corrupt_file_does_not_overwrite(reg, raw):
    reg.write_text(raw)
    for op in (lambda: proxies.upsert(dict(ENTRY)),
               lambda: proxies.assign_account("t-1", "acct-test"),
               lambda: proxies.remove("t-1")):
        with pytest.raises(proxies.ProxyRegistryUnreadable):
            op()
    assert reg.read_text() == raw


def test_of_client_refuses_instead_of_direct(reg, tmp_path, monkeypatch):
    import of_client
    monkeypatch.setattr(of_client, "require_legacy_provider_role", lambda: None)
    built = []
    monkeypatch.setattr(of_client.OFClient, "__init__",
                        lambda self, data, **kw: built.append(kw))
    sess = tmp_path / "accounts" / "acct-test" / "session_x.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}")
    reg.write_text(BAD["corrupt_json"])
    with pytest.raises(proxies.ProxyRegistryUnreadable):
        of_client.OFClient.from_session_file(sess)
    assert built == []  # no client (hence no request) was ever constructed
    reg.write_text(json.dumps({"proxies": [ENTRY]}))
    of_client.OFClient.from_session_file(sess)
    assert built and built[0]["proxy_label"] == "t-1"


def test_save_is_atomic_temp_then_replace(reg, monkeypatch):
    proxies.upsert(dict(ENTRY))
    calls = []
    real = os.replace
    monkeypatch.setattr(proxies.os, "replace",
                        lambda s, d: (calls.append((s, d)), real(s, d)))
    proxies.upsert({**ENTRY, "notes": "n2"})
    assert len(calls) == 1 and str(calls[0][1]) == str(reg)
    assert os.path.dirname(calls[0][0]) == str(reg.parent)
    assert proxies.get_by_label("t-1")["notes"] == "n2"

    def boom(s, d):
        raise OSError("crash mid-save")
    monkeypatch.setattr(proxies.os, "replace", boom)
    with pytest.raises(OSError):
        proxies.upsert({**ENTRY, "notes": "n3"})
    assert json.loads(reg.read_text())["proxies"][0]["notes"] == "n2"
    assert [p.name for p in reg.parent.iterdir()] == ["proxies.json"]  # no temp litter


def test_relay_surfaces_503_not_no_proxy(reg, tmp_path, monkeypatch):
    import asyncio
    import server
    reg.write_text(BAD["corrupt_json"])
    with pytest.raises(proxies.ProxyRegistryUnreadable) as ei:
        server.admin_proxies_list()  # never an empty "no proxies" list
    h = server.app.exception_handlers[proxies.ProxyRegistryUnreadable]
    r = asyncio.run(h(None, ei.value))
    assert r.status_code == 503 and json.loads(r.body)["error"] == "proxy_registry_unreadable"
    fake = tmp_path / "sessions" / "accounts" / "acct-test" / "session_x.json"
    fake.parent.mkdir(parents=True)
    fake.write_text("{}")
    monkeypatch.setattr(server, "HERE", tmp_path)
    server._migrate_proxy_bindings_to_accounts()  # boot path logs, doesn't crash
    assert reg.read_text() == BAD["corrupt_json"]
