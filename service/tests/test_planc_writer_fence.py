"""Plan C defect 4: writer fence for a pilot creator's fans. Plain SQLite, no network.

Ownership-store failure HOLDS automation and (for a declared pilot account) manual sends too; a human takeover is a
separate path that must be VERIFIED from the store (owner mode 'human'), never assumed; every competing legacy,
mass, scheduled, campaign and bridge writer is refused for an engine-owned account/fan.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from moxy_replacement import routing  # noqa: E402
from onlyfans_runtime.store import Refused  # noqa: E402
from twin_bridge import coordination  # noqa: E402

ACTIVE_FAN, HUMAN_FAN, OTHER_FAN = 900000000011, 900000000012, 900000000013
PILOT, OTHER = "of-9000001", "of-9000002"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE of_moxy_account (account_id TEXT PRIMARY KEY, mode TEXT)")
    con.execute("CREATE TABLE of_engine_owner (account_id TEXT, fan_id INTEGER, mode TEXT, PRIMARY KEY (account_id, fan_id))")
    con.executemany("INSERT INTO of_engine_owner VALUES (?,?,?)", [(PILOT, ACTIVE_FAN, "active"), (PILOT, HUMAN_FAN, "human")])
    con.commit()
    con.close()
    monkeypatch.setattr(coordination, "_database_url", lambda: f"sqlite:///{path}")
    monkeypatch.setenv(routing.PILOT_ENV, PILOT)
    return path


def client(account):
    from of_client import OFClient
    c = OFClient.__new__(OFClient)
    c.account_id = account
    posts = []
    c._post_dropping_auto_cotag = lambda url, body, plan: posts.append(url) or {"id": 1}
    c._apply_cotags = lambda *a, **k: None
    c.post_json = lambda url, **k: posts.append(url) or {"id": 1}
    return c, posts


SCHEDULED = dict(scheduled_date="2030-01-01T00:00:00+00:00")
WRITERS = {
    "send_message": lambda c, fan, p: c.send_message(fan, "hi", purpose=p),
    "schedule_message": lambda c, fan, p: c.schedule_message(fan, "hi", purpose=p, **SCHEDULED),
    "send_mass_message": lambda c, fan, p: c.send_mass_message("blast", included_users=[fan]),
    "send_tip": lambda c, fan, p: c.send_tip(fan, 5.0),
    "send_text_once": lambda c, fan, p: c.send_text_once(fan, "hi"),
    "send_text_once_body": lambda c, fan, p: c.send_text_once_body(fan, "<p>hi</p>"),
}


@pytest.fixture
def outage(monkeypatch):
    """The ownership store cannot be read."""
    monkeypatch.setattr(coordination, "_database_url", lambda: "")


# ---- lookup outage: automation is held, and so is a manual send into a declared pilot account ------------------------

@pytest.mark.parametrize("name", sorted(WRITERS))
@pytest.mark.parametrize("purpose", ["other", "gated", "scheduled", "broadcast", "twin_bridge"])
def test_store_outage_holds_every_automated_writer_for_the_pilot_account(store, outage, name, purpose):
    c, posts = client(PILOT)
    with pytest.raises(Refused):
        WRITERS[name](c, ACTIVE_FAN, purpose)
    assert posts == []


@pytest.mark.parametrize("name", ["send_message", "schedule_message"])
def test_store_outage_holds_a_manual_send_into_a_declared_pilot_account(store, outage, name):
    c, posts = client(PILOT)
    with pytest.raises(Refused, match="engine_owner_lookup_failed"):
        WRITERS[name](c, HUMAN_FAN, "manual")      # cannot be told apart from a send into an engine-owned chat
    assert posts == []


@pytest.mark.parametrize("name", ["send_message", "schedule_message"])
def test_store_outage_does_not_block_an_unrelated_accounts_operator(store, outage, name):
    c, posts = client(OTHER)
    WRITERS[name](c, OTHER_FAN, "manual")          # documented human policy for accounts outside the pilot scope
    assert len(posts) == 1


def test_store_outage_still_refuses_automation_for_an_unrelated_account(store, outage):
    c, posts = client(OTHER)
    with pytest.raises(Refused, match="engine_owner_lookup_failed"):
        c.send_message(OTHER_FAN, "auto", purpose="other")
    assert posts == []


def test_pilot_scope_is_known_without_the_store(monkeypatch):
    monkeypatch.setenv(routing.PILOT_ENV, "9000001, of-9000003")
    assert routing.in_pilot_scope("of-9000001") and routing.in_pilot_scope("9000003") and routing.in_pilot_scope("of-9000003")
    assert not routing.in_pilot_scope("of-9000002")
    monkeypatch.delenv(routing.PILOT_ENV)
    assert routing.pilot_accounts() == frozenset()


# ---- human takeover: a separate, verified path ---------------------------------------------------------------------

@pytest.mark.parametrize("name", ["send_message", "schedule_message"])
def test_human_takeover_is_verified_from_the_store_not_assumed(store, name):
    c, posts = client(PILOT)
    WRITERS[name](c, HUMAN_FAN, "manual")          # owner row says 'human': the takeover is on record
    assert len(posts) == 1
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        WRITERS[name](c, ACTIVE_FAN, "manual")     # engine still active: a 'manual' label alone is not a takeover
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        WRITERS[name](c, HUMAN_FAN, "other")       # takeover covers a human send only, not automation
    assert len(posts) == 1


# ---- competing legacy / mass / scheduled / bridge writers ------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(WRITERS))
def test_every_competing_writer_is_refused_for_an_engine_owned_fan(store, name):
    c, posts = client(PILOT)
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        WRITERS[name](c, ACTIVE_FAN, "other")
    assert posts == []


@pytest.mark.parametrize("purpose", ["other", "gated", "scheduled", "broadcast", "twin_bridge"])
def test_bridge_boundary_and_native_pilot_writer_refuse_engine_owned_fans(store, purpose):
    with pytest.raises(Refused, match="engine_canonical_owner_required"):
        with coordination.guarded_send(PILOT, ACTIVE_FAN, purpose=purpose):
            raise AssertionError("send body must not run")


def test_mass_and_account_level_writes_are_refused_while_any_fan_is_engine_owned(store):
    c, posts = client(PILOT)
    for name in ("send_mass_message", "send_tip"):
        with pytest.raises(Refused, match="engine_canonical_owner_required"):
            WRITERS[name](c, OTHER_FAN, "other")      # even for a fan the engine does not own: a broadcast cannot exclude them
    assert posts == []


# ---- automation executor: refused BEFORE any model call or state write ----------------------------------------------

@pytest.mark.parametrize("kind", sorted(routing.MOXY_FENCED_KINDS - {"scheduled_send"}))
def test_executor_fence_refuses_every_messaging_kind_for_an_engine_owned_account(store, kind):
    assert routing.job_fence_reason(PILOT, kind, {}) == "engine_canonical_owner_required"
    assert routing.job_fence_reason(OTHER, kind, {}) is None


def test_executor_fence_holds_on_store_outage_and_leaves_non_messaging_kinds_alone(store, outage):
    assert routing.job_fence_reason(PILOT, "ai_chatter", {}) in {"moxy_owner_lookup_failed", "engine_owner_lookup_failed"}
    assert routing.job_fence_reason(PILOT, "scrape_chats", {}) is None
    assert routing.job_fence_reason(PILOT, "scheduled_send", {"send_purpose": "manual"}) is None  # the send-time fence decides


def test_executor_engine_state_override_is_fail_closed(store):
    assert routing.job_fence_reason(OTHER, "ai_chatter", {}, engine_state=routing.UNKNOWN) == "engine_owner_lookup_failed"
    assert routing.job_fence_reason(OTHER, "ai_chatter", {}, engine_state=routing.SELECTED) == "engine_canonical_owner_required"
    assert routing.job_fence_reason(OTHER, "ai_chatter", {}, engine_state=routing.NOT_SELECTED) is None
