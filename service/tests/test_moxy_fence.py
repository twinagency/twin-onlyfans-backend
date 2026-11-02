"""Moxy ownership fence: job layer, transport boundary and fail-closed lookup.

Plain SQLite, no network, no model API. A Moxy-selected account (an
`of_moxy_account` row, in mode off OR on) must run no legacy fan-messaging
automation (no model call, no state write, no send, no caption rewrite) and its
automated legacy sends are refused at the transport boundary; a manual operator
send still works; an unmigrated account is unaffected; an undeterminable owner
fails closed for automated work only.
"""
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import automation_executor as ax  # noqa: E402
from automation_registry import get_automation, load_automation_plugins  # noqa: E402
from db.models import AutomationRule, AutomationRun, ScheduledJob  # noqa: E402
from moxy_replacement import routing  # noqa: E402
from onlyfans_runtime.store import Refused  # noqa: E402
from twin_bridge import coordination  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from automations._common import apply_word_restriction  # noqa: E402

MOXY_ON, MOXY_OFF, FREE = "of-1001", "of-1002", "of-2001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    sync = create_engine(f"sqlite:///{path}")
    for model in (AutomationRun, ScheduledJob, AutomationRule):
        model.__table__.create(sync)
    sync.dispose()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE of_moxy_account (account_id TEXT PRIMARY KEY, mode TEXT)")
    con.executemany("INSERT INTO of_moxy_account VALUES (?,?)",
                    [(MOXY_ON, "on"), (MOXY_OFF, "off")])
    con.commit()
    con.close()
    eng = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(eng, expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as s:
            yield s
            await s.commit()

    monkeypatch.setattr(ax, "get_session", get_session)
    monkeypatch.setattr(coordination, "_database_url", lambda: f"sqlite:///{path}")
    ax._MOXY_FENCE_RECORDED.clear()
    probe = Probe()
    monkeypatch.setattr(ax, "get_automation", lambda kind: probe)

    async def no_dead(_a):
        return False
    monkeypatch.setattr(ax.account_health, "is_session_dead", no_dead)
    yield types.SimpleNamespace(path=path, probe=probe, sync_url=f"sqlite:///{path}")
    asyncio.run(eng.dispose())


import types  # noqa: E402


class Probe:
    """Stands in for any legacy automation: calling it is the 'LLM call / send'."""
    def __init__(self):
        self.calls = []

    async def __call__(self, account_id, payload, *, run_id):
        self.calls.append((account_id, payload))
        return {"sent": 1}


def rows(env, model):
    sync = create_engine(env.sync_url)
    try:
        with Session(sync) as s:
            return list(s.scalars(select(model)))
    finally:
        sync.dispose()


def enqueue(account, kind, payload=None):
    return asyncio.run(ax.enqueue_job(account, kind, payload=payload or {}))


# ── owner lookup ──────────────────────────────────────────────────────

def test_owner_state_selected_unselected_and_id_spellings(env):
    assert routing.owner_state(MOXY_ON) == routing.SELECTED
    assert routing.owner_state(MOXY_OFF) == routing.SELECTED      # mode off is still owned
    assert routing.owner_state("1001") == routing.SELECTED        # digits -> of-<digits>
    assert routing.owner_state(1001) == routing.SELECTED
    assert routing.owner_state(" of-1001 ") == routing.SELECTED
    assert routing.owner_state(FREE) == routing.NOT_SELECTED
    assert routing.owner_state("2001") == routing.NOT_SELECTED
    assert routing.owner_state("") == routing.NOT_SELECTED


def test_owner_state_reverse_spelling(env):
    con = sqlite3.connect(env.path)
    con.execute("INSERT INTO of_moxy_account VALUES ('3003','on')")
    con.commit()
    con.close()
    assert routing.owner_state("of-3003") == routing.SELECTED     # of-<digits> -> digits
    assert routing.owner_state("of-3004") == routing.NOT_SELECTED
    assert routing.owner_state("of-abc") == routing.NOT_SELECTED


def test_missing_table_is_definitively_not_selected(env):
    con = sqlite3.connect(env.path)
    con.execute("DROP TABLE of_moxy_account")
    con.commit()
    con.close()
    assert routing.owner_state(MOXY_ON) == routing.NOT_SELECTED   # unmigrated schema


def test_lookup_failure_is_unknown_not_unselected(env, monkeypatch, tmp_path):
    monkeypatch.setattr(coordination, "_database_url",
                        lambda: f"sqlite:///{tmp_path / 'absent.db'}")
    assert routing.owner_state(FREE) == routing.UNKNOWN           # store unreadable
    monkeypatch.setattr(coordination, "_database_url", lambda: "")
    assert routing.owner_state(FREE) == routing.UNKNOWN           # no store target
    assert routing.selected_sync(FREE) is False
    assert routing.selected_sync(FREE, unknown=True) is True


# ── job layer ─────────────────────────────────────────────────────────

def test_every_fenced_kind_is_a_registered_automation():
    load_automation_plugins()
    # `get_automation` may be patched by other fixtures; import the real one.
    from automation_registry import get_automation as real
    missing = [k for k in routing.MOXY_FENCED_KINDS if real(k) is None]
    assert missing == []


@pytest.mark.parametrize("account", [MOXY_ON, MOXY_OFF])
@pytest.mark.parametrize("kind", sorted(routing.MOXY_FENCED_KINDS))
def test_moxy_account_runs_no_legacy_messaging_job(env, account, kind):
    job_id = enqueue(account, kind, {"fan_id": 7, "text": "hi"})
    out = asyncio.run(ax.run_once(account, kind))
    assert out["status"] == "skipped" and out["reason"] == "moxy_canonical_owner_required"
    assert env.probe.calls == []                                   # no LLM call, no send
    job = [j for j in rows(env, ScheduledJob) if j.id == job_id][0]
    assert job.status == "canceled" and job.last_error == "moxy_canonical_owner_required"
    run = rows(env, AutomationRun)[-1]
    assert run.status == "skipped" and run.error_text == "moxy_canonical_owner_required"
    assert json.loads(run.stats_json)["moxy_fenced"] is True
    # also fenced when the caller passes an explicit payload (no queue claim)
    out = asyncio.run(ax.run_once(account, kind, payload={"fan_id": 7}))
    assert out["status"] == "skipped" and env.probe.calls == []


def test_unmigrated_account_is_unaffected(env):
    for kind in sorted(routing.MOXY_FENCED_KINDS):
        out = asyncio.run(ax.run_once(FREE, kind, payload={}))
        assert out["status"] == "ok"
    assert len(env.probe.calls) == len(routing.MOXY_FENCED_KINDS)


def test_non_messaging_kinds_still_run_for_moxy_account(env):
    for kind in ("scrape_chats", "describe_media", "audience_sync", "push_to_sheets"):
        assert asyncio.run(ax.run_once(MOXY_ON, kind, payload={}))["status"] == "ok"
    assert len(env.probe.calls) == 4


def test_manual_scheduled_send_still_fires_for_moxy_account(env):
    out = asyncio.run(ax.run_once(MOXY_ON, "scheduled_send",
                                  payload={"fan_id": 7, "send_purpose": "manual"}))
    assert out["status"] == "ok" and len(env.probe.calls) == 1
    out = asyncio.run(ax.run_once(MOXY_ON, "scheduled_send", payload={"fan_id": 7}))
    assert out["status"] == "skipped" and len(env.probe.calls) == 1


def test_lookup_failure_fails_closed_for_messaging_jobs_only(env, monkeypatch, tmp_path):
    monkeypatch.setattr(coordination, "_database_url", lambda: "")
    out = asyncio.run(ax.run_once(FREE, "ai_chatter", payload={}))
    assert out["status"] == "skipped" and out["reason"] == "moxy_owner_lookup_failed"
    assert env.probe.calls == []
    assert asyncio.run(ax.run_once(FREE, "scrape_chats", payload={}))["status"] == "ok"
    # a human-stamped scheduled send is never blocked by a failed lookup
    assert asyncio.run(ax.run_once(FREE, "scheduled_send",
                                   payload={"send_purpose": "manual"}))["status"] == "ok"


def test_rules_for_moxy_account_do_not_materialize_jobs(env):
    steps = json.dumps({})
    trig = json.dumps({"every_seconds": 60})
    sync = create_engine(env.sync_url)
    with Session(sync) as s, s.begin():
        for acct in (MOXY_ON, FREE):
            s.add(AutomationRule(account_id=acct, name="r", kind="ai_chatter",
                                 trigger_json=trig, steps_json=steps, is_enabled=True))
    sync.dispose()
    assert asyncio.run(ax._materialize_due_rules()) == 1
    assert {j.account_id for j in rows(env, ScheduledJob)} == {FREE}
    skipped = [r for r in rows(env, AutomationRun) if r.status == "skipped"]
    assert [(r.account_id, r.kind) for r in skipped] == [(MOXY_ON, "ai_chatter")]
    # recorded once per process, not once per tick
    asyncio.run(ax._materialize_due_rules())
    assert len([r for r in rows(env, AutomationRun) if r.status == "skipped"]) == 1


# ── transport boundary ────────────────────────────────────────────────

AUTOMATED = ["other", "gated", "scheduled", "broadcast", "twin_bridge"]


@pytest.mark.parametrize("account", [MOXY_ON, MOXY_OFF])
@pytest.mark.parametrize("purpose", AUTOMATED)
def test_guarded_send_refuses_automated_legacy_send_for_moxy(env, account, purpose):
    with pytest.raises(Refused, match="moxy_canonical_owner_required"):
        with coordination.guarded_send(account, 55, purpose=purpose):
            raise AssertionError("send body must not run")


@pytest.mark.parametrize("account", [MOXY_ON, MOXY_OFF])
def test_guarded_send_allows_manual_operator_send(env, account):
    ran = []
    with coordination.guarded_send(account, 55, purpose="manual"):
        ran.append(1)
    assert ran == [1]


def test_guarded_send_unmigrated_account_unaffected(env):
    for purpose in ("other", "gated", "manual", "scheduled"):
        with coordination.guarded_send(FREE, 55, purpose=purpose):
            pass


def test_guarded_send_fails_closed_on_lookup_failure_but_not_manual(env, monkeypatch):
    monkeypatch.setattr(coordination, "_database_url", lambda: "")
    with pytest.raises(Refused, match="moxy_owner_lookup_failed"):
        with coordination.guarded_send(FREE, 55, purpose="other"):
            raise AssertionError("send body must not run")
    with coordination.guarded_send(FREE, 55, purpose="manual"):
        pass


class _Posted(Exception):
    pass


def _client(account):
    from of_client import OFClient
    c = OFClient.__new__(OFClient)
    c.account_id = account
    posts = []
    c._post_dropping_auto_cotag = lambda url, body, plan: posts.append(url) or {"id": 1}
    c._apply_cotags = lambda *a, **k: None
    return c, posts


@pytest.mark.parametrize("account", [MOXY_ON, MOXY_OFF])
def test_ofclient_send_message_real_boundary(env, account):
    c, posts = _client(account)
    with pytest.raises(Refused, match="moxy_canonical_owner_required"):
        c.send_message(55, "legacy writer text")                   # default purpose
    with pytest.raises(Refused):
        c.send_message(55, "legacy writer text", purpose="gated")
    assert posts == []                                             # nothing reached the wire
    assert c.send_message(55, "operator text", purpose="manual") == {"id": 1}
    assert len(posts) == 1


def test_ofclient_mass_and_scheduled_sends_refused_for_moxy(env):
    c, posts = _client(MOXY_ON)
    with pytest.raises(Refused):
        c.send_mass_message("blast", included_users=[1])
    with pytest.raises(Refused):
        c.schedule_message(55, "later", scheduled_date="2030-01-01T00:00:00+00:00")
    assert posts == []
    # the human composer's deferral passes purpose="manual"
    c.schedule_message(55, "later", scheduled_date="2030-01-01T00:00:00+00:00", purpose="manual")
    assert len(posts) == 1


def test_ofclient_unmigrated_account_sends_normally(env):
    c, posts = _client(FREE)
    c.send_message(55, "hello")
    c.send_mass_message("blast", included_users=[1])
    assert len(posts) == 2


# ── caption rewrite ───────────────────────────────────────────────────

def _caption_probe(monkeypatch):
    from automations import _ppv_caption as pc
    calls = []

    async def fake_caption_for(*a, **k):
        calls.append(a)
        return "rewritten caption"
    monkeypatch.setattr(pc, "enabled", lambda cfg: True)
    monkeypatch.setattr(pc, "caption_for", fake_caption_for)
    return pc, calls


def test_caption_rewrite_never_runs_for_moxy_account(env, monkeypatch):
    pc, calls = _caption_probe(monkeypatch)
    out = asyncio.run(pc.line_for("canned line", {}, MOXY_ON, 55, [9]))
    assert out == apply_word_restriction("canned line") and calls == []
    out = asyncio.run(pc.line_for("canned line", {}, MOXY_OFF, 55, [9]))
    assert out == apply_word_restriction("canned line") and calls == []


def test_caption_rewrite_unmigrated_runs_and_unknown_owner_skips(env, monkeypatch):
    pc, calls = _caption_probe(monkeypatch)
    assert asyncio.run(pc.line_for("canned line", {}, FREE, 55, [9])) == apply_word_restriction("rewritten caption")
    monkeypatch.setattr(coordination, "_database_url", lambda: "")
    assert asyncio.run(pc.line_for("canned line", {}, FREE, 55, [9])) == apply_word_restriction("canned line")
    assert len(calls) == 1


# ── media descriptions ────────────────────────────────────────────────

def test_media_description_states_and_moxy_only(env, monkeypatch):
    from db.models import VaultItem
    from moxy_replacement import media_descriptions as md
    monkeypatch.setattr(md, "get_session", ax.get_session)
    sync = create_engine(env.sync_url)
    VaultItem.__table__.create(sync)
    now = datetime(2026, 11, 1)
    with Session(sync) as s, s.begin():
        def add(mid, kind, **kw):
            s.add(VaultItem(account_id=MOXY_ON, media_id=mid, kind=kind, created_at=now, **kw))
        add(1, "photo", description="a", describe_status="described", describe_generated_at=now, updated_at=now)
        add(2, "photo")
        add(3, "video", describe_status="failed")
        add(4, "video", describe_status="blocked_drm")
        add(5, "audio")
        add(6, "photo", description="old", describe_status="described",
            describe_generated_at=now, updated_at=now + timedelta(days=1))
    sync.dispose()
    got = {r["media_id"]: r["state"] for r in asyncio.run(md.descriptions(MOXY_ON, [1, 2, 3, 4, 5, 6, 99]))}
    assert got == {1: "described", 2: "pending", 3: "failed", 4: "unsupported",
                   5: "unsupported", 6: "pending", 99: "missing"}
    calls = []

    async def fake(account, mid):
        calls.append(mid)
    asyncio.run(md.ensure_described(MOXY_ON, [1, 2, 3, 6], describe=fake))
    assert sorted(calls) == [2, 6]
    calls.clear()
    asyncio.run(md.ensure_described(MOXY_ON, [3], describe=fake, retry_failed=True))
    assert calls == [3]
    with pytest.raises(Refused):
        asyncio.run(md.ensure_described(FREE, [1], describe=fake))


def test_vault_ingest_describes_for_moxy_accounts_only_and_bounded(env, monkeypatch):
    import vault_ingest
    from moxy_replacement import media_descriptions as md
    seen = []

    async def fake(account, mid):
        seen.append((account, mid))

    monkeypatch.setattr(md, "get_session", ax.get_session)
    from db.models import VaultItem
    sync = create_engine(env.sync_url)
    VaultItem.__table__.create(sync)
    now = datetime(2026, 11, 1)
    with Session(sync) as s, s.begin():
        for acct in (MOXY_ON, FREE):
            for mid in range(1, 31):
                s.add(VaultItem(account_id=acct, media_id=mid, kind="photo", created_at=now))
    sync.dispose()
    asyncio.run(vault_ingest._describe_for_moxy(FREE, [1, 2], describe=fake))
    assert seen == []                                   # non-Moxy account untouched
    asyncio.run(vault_ingest._describe_for_moxy(MOXY_ON, list(range(1, 31)), describe=fake))
    assert len(seen) == md.MAX_ENSURE and {a for a, _ in seen} == {MOXY_ON}   # bounded
    seen.clear()

    async def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(md, "ensure_described", boom)
    asyncio.run(vault_ingest._describe_for_moxy(MOXY_ON, [1]))   # never raises
