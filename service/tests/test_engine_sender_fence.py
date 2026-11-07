"""One sender per creator: engine-owned accounts/fans refuse every legacy send path.

Plain SQLite, no network. Owner=engine is refused at the of_client send layer and
at the bridge boundary; owner=moxy/legacy behaves as before. Also an AST guard so
a new raw OnlyFans send cannot appear outside the approved layer.
"""
import ast
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from moxy_replacement import routing  # noqa: E402
from onlyfans_runtime.store import Refused  # noqa: E402
from twin_bridge import coordination  # noqa: E402

ENGINE_FAN, HUMAN_FAN, OTHER_FAN = 55, 56, 77
ENGINE, MOXY, FREE = "of-3001", "of-3002", "of-3003"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE of_moxy_account (account_id TEXT PRIMARY KEY, mode TEXT)")
    con.execute("INSERT INTO of_moxy_account VALUES (?,?)", (MOXY, "on"))
    con.execute("CREATE TABLE of_engine_owner (account_id TEXT, fan_id INTEGER, mode TEXT,"
                " PRIMARY KEY (account_id, fan_id))")
    con.executemany("INSERT INTO of_engine_owner VALUES (?,?,?)",
                    [(ENGINE, ENGINE_FAN, "active"), (ENGINE, HUMAN_FAN, "human")])
    con.commit()
    con.close()
    monkeypatch.setattr(coordination, "_database_url", lambda: f"sqlite:///{path}")
    return path


def _client(account):
    from of_client import OFClient
    c = OFClient.__new__(OFClient)
    c.account_id = account
    posts = []
    c._post_dropping_auto_cotag = lambda url, body, plan: posts.append(url) or {"id": 1}
    c._apply_cotags = lambda *a, **k: None
    c.post_json = lambda url, **k: posts.append(url) or {"id": 1}
    return c, posts


SCHEDULED = dict(scheduled_date="2030-01-01T00:00:00+00:00")
SENDS = {
    "send_message": lambda c, fan, p: c.send_message(fan, "hi", purpose=p),
    "schedule_message": lambda c, fan, p: c.schedule_message(fan, "hi", purpose=p, **SCHEDULED),
    "send_mass_message": lambda c, fan, p: c.send_mass_message("blast", included_users=[fan]),
    "send_tip": lambda c, fan, p: c.send_tip(fan, 5.0),
    "send_text_once": lambda c, fan, p: c.send_text_once(fan, "hi"),
    "send_text_once_body": lambda c, fan, p: c.send_text_once_body(fan, "<p>hi</p>"),
}


@pytest.mark.parametrize("name", sorted(SENDS))
def test_engine_owner_refused_at_every_ofclient_send(env, name):
    c, posts = _client(ENGINE)
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        SENDS[name](c, ENGINE_FAN, "other")
    assert posts == []


@pytest.mark.parametrize("name", ["send_message", "schedule_message"])
def test_engine_owned_fan_refuses_even_purpose_manual(env, name):
    c, posts = _client(ENGINE)
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        SENDS[name](c, ENGINE_FAN, "manual")
    assert posts == []


@pytest.mark.parametrize("name", ["send_message", "schedule_message"])
def test_human_takeover_fan_allows_manual_only(env, name):
    c, posts = _client(ENGINE)
    SENDS[name](c, HUMAN_FAN, "manual")
    assert len(posts) == 1
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        SENDS[name](c, HUMAN_FAN, "other")


def test_other_fan_of_same_account_is_not_fenced_per_fan(env):
    c, posts = _client(ENGINE)
    c.send_message(OTHER_FAN, "hi", purpose="manual")
    assert len(posts) == 1


def test_account_level_writes_refused_when_any_fan_owned(env):
    c, posts = _client(ENGINE)
    for name in ("send_mass_message", "send_tip"):
        with pytest.raises(Refused, match="engine_canonical_owner_required"):
            SENDS[name](c, OTHER_FAN, "other")
    assert posts == []


@pytest.mark.parametrize("purpose", ["other", "gated", "scheduled", "broadcast", "twin_bridge", "manual"])
def test_bridge_boundary_refuses_engine_owned(env, purpose):
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        with coordination.guarded_send(ENGINE, ENGINE_FAN, purpose=purpose):
            raise AssertionError("send body must not run")


def test_fails_closed_when_engine_lookup_errors(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(routing, "engine_owner_modes", lambda *a, **k: (routing.UNKNOWN, frozenset()))
    c, posts = _client(FREE)
    for name in sorted(SENDS):
        if name == "send_message":
            with pytest.raises(Refused, match="engine_owner_lookup_failed"):
                SENDS[name](c, 9, "other")
        elif name != "send_message":
            with pytest.raises(Refused):
                SENDS[name](c, 9, "other")
    assert posts == []


def test_fails_closed_when_store_unreadable(env, monkeypatch):
    monkeypatch.setattr(coordination, "_database_url", lambda: "")
    c, posts = _client(FREE)
    with pytest.raises(Refused):
        c.send_text_once_body(9, "<p>x</p>")
    assert posts == []


def test_fails_closed_when_fence_module_unavailable(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "moxy_replacement", None)
    c, posts = _client(FREE)
    with pytest.raises(Refused, match="owner_fence_unavailable"):
        c.send_message(9, "x")
    assert posts == []


def test_moxy_owner_unchanged(env):
    c, posts = _client(MOXY)
    with pytest.raises(Refused, match="moxy_canonical_owner_required"):
        c.send_message(55, "legacy")
    with pytest.raises(Refused, match="moxy_canonical_owner_required"):
        c.send_mass_message("blast", included_users=[1])
    assert c.send_message(55, "operator", purpose="manual") == {"id": 1}
    c.send_text_once_body(55, "<p>x</p>")      # bridge/operator text writer: not Moxy-fenced here
    c.send_tip(55, 1.0)
    assert len(posts) == 3


def test_legacy_owner_unchanged(env):
    c, posts = _client(FREE)
    for name in sorted(SENDS):
        SENDS[name](c, 55, "other")
    assert len(posts) == len(SENDS)


# ── AST guard: no new raw OnlyFans send outside the approved layer ─────

APPROVED_RAW_SEND_FILES = {
    "of_client.py",                          # the fenced send layer
    "onlyfans_runtime/fastt_manual.py",      # Moxy canonical operator (TextAuthority-admitted)
    "onlyfans_runtime/provider_connector.py",  # engine's own provider connector
    "onlyfans_runtime/native_connector.py",    # native canonical connector
    "native_pilot/runner.py",                # bridge media POST, inside guarded_send(twin_bridge)
}
SEND_URL = re.compile(r"chats/[^'\"]*messages(?![/\w?])|messages/queue|/tips\b")
POSTERS = {"post_json", "_post_dropping_auto_cotag", "_call", "post"}


def _py_files():
    for p in SERVICE.rglob("*.py"):
        rel = p.relative_to(SERVICE).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or p.name.startswith("test_"):
            continue
        if rel.startswith(("db/migrations/", "scratchpad/", "node_modules/")):
            continue
        yield rel, p


def test_no_raw_onlyfans_send_outside_approved_layer():
    offenders = []
    for rel, p in _py_files():
        if rel in APPROVED_RAW_SEND_FILES:
            continue
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in POSTERS and node.args
                    and not (isinstance(node.func.value, ast.Name) and node.func.value.id in {"app", "router"})):
                text = " ".join(ast.unparse(a) for a in node.args[:2])
                if SEND_URL.search(text):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"raw OnlyFans send outside approved layer: {offenders}"


def test_every_ofclient_sender_calls_the_fence():
    tree = ast.parse((SERVICE / "of_client.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OFClient")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    missing = []
    for name in ("send_message", "schedule_message", "send_mass_message", "send_tip", "send_text_once_body"):
        fn = methods[name]
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "_owner_fence" for n in ast.walk(fn)):
            missing.append(name)
    assert missing == []
    # send_text_once must delegate to the fenced body writer, not post itself.
    src = ast.unparse(methods["send_text_once"])
    assert "send_text_once_body" in src and "post_json" not in src


def test_bridge_runner_raw_post_sits_under_guarded_send():
    src = (SERVICE / "native_pilot/runner.py").read_text()
    assert src.index("guarded_send(self.pilot.account_id") < src.index('self.client.post_json(f"{API_BASE}/chats/')
