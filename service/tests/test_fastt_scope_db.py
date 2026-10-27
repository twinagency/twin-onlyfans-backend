"""Per-account FASTT scope: env allowlist x database selection, cache, validation, PUT, supervisor. No network."""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

from onlyfans_runtime import fastt_manual as m, fastt_scope as fs

ACC, OTHER = "900001", "900002"


class Rig:
    """Injected reader + clock; ``rows`` is {db account id: stored scope value | None}."""
    def __init__(self, monkeypatch):
        self.t, self.rows, self.fail, self.reads = 1000.0, {}, False, 0
        monkeypatch.delenv("DATABASE_URL", raising=False)
        fs.reset_cache()
        monkeypatch.setattr(fs, "_clock", lambda: self.t)
        monkeypatch.setattr(fs, "_reader", self.read)
        self.mp = monkeypatch

    def read(self, ids):
        self.reads += 1
        if self.fail:
            raise OSError("db down")
        return {i: (None if self.rows.get(i) is None else json.dumps({"fastt_scope": self.rows[i], "enabled": True}))
                for i in ids}

    def env(self, value):
        self.mp.setenv(fs.SCOPE_ENV, value)

    def set(self, account, value):
        self.rows["of-" + account] = value

    def wait(self, s):
        self.t += s


@pytest.fixture
def rig(monkeypatch):
    r = Rig(monkeypatch)
    yield r
    fs.reset_cache()


def sel(mode, *fans):
    return {"mode": mode, "fans": list(fans)}


def test_env_only_is_unchanged(rig):
    rig.env("of-900001:*, 555:42 ,bad:x,:*,of-9:")
    assert m.fastt_owned("of-900001", 1) and m.fastt_owned(ACC, 10 ** 9) and m.fastt_account_owned("of-900001")
    assert m.fastt_owned("555", 42) and not m.fastt_owned("555", 43) and not m.fastt_owned("of-9", 1)
    assert m.fastt_serves("of-900001", 5) and m.fastt_serves("555", 42) and not m.fastt_serves("555", 43)
    rig.env("")
    assert not m.fastt_owned(ACC, 1) and rig.reads <= 1
    rig.env(f"of-{ACC}:7")                          # Nicky's live form, no database value
    assert m.fastt_owned(ACC, 7) and m.fastt_serves(ACC, 7) and not m.fastt_owned(ACC, 8) and not m.fastt_serves(ACC, 8)
    assert m._scope() == ({(ACC, 7)}, set())


def test_db_selection_grows_ownership_immediately_serving_after_settle(rig):
    rig.env(f"db:of-{ACC}")
    rig.set(ACC, sel("selected", 5, 6))
    assert m.fastt_owned(ACC, 5) and m.fastt_owned(ACC, 6) and not m.fastt_owned(ACC, 7)
    assert not m.fastt_account_owned(ACC)
    assert m.fastt_serves(ACC, 5) and not m.fastt_serves(ACC, 7)      # first read of a process: no wait
    rig.wait(fs.TTL_S + 1)
    rig.set(ACC, sel("selected", 5, 6, 9))                              # growth during the life: owned now
    assert m.fastt_owned(ACC, 9) and not m.fastt_serves(ACC, 9)         # ...served only after the settle
    rig.wait(fs.SETTLE_S + 1)
    assert m.fastt_serves(ACC, 9)


def test_db_selection_is_cut_by_the_allowlist(rig):
    rig.env(f"of-{ACC}:5")                          # explicit pair only
    rig.set(ACC, sel("selected", 5, 6))
    assert m.fastt_owned(ACC, 5) and not m.fastt_owned(ACC, 6) and not m.fastt_serves(ACC, 6)
    rig.env(f"of-{ACC}:*")                          # any fan allowed, database narrows what is served
    rig.wait(fs.TTL_S + 1)
    rig.set(ACC, sel("single", 6))
    assert m.fastt_serves(ACC, 6) and not m.fastt_serves(ACC, 5) and not m.fastt_serves(ACC, 99)
    assert m.fastt_owned(ACC, 99)                   # `:*` stays whole-account ownership for the legacy worker
    rig.env(f"of-{ACC}:5")
    rig.set(ACC, sel("all"))                        # "all" under a pair allowlist is only that pair
    rig.wait(fs.TTL_S + 1)
    assert m.fastt_serves(ACC, 5) and not m.fastt_serves(ACC, 6)
    rig.env(f"db:of-{ACC}")                         # a database value never reaches an account the env does not name
    assert not m.fastt_owned(OTHER, 1) and not m.fastt_serves(OTHER, 1)


def test_db_mode_all(rig):
    rig.env(f"db:{ACC}")
    rig.set(ACC, sel("all"))
    assert m.fastt_account_owned(ACC) and m.fastt_owned(ACC, 123) and m.fastt_serves(ACC, 123)


def test_db_only_account_without_value_owns_nothing_and_fails_closed_until_read(rig):
    rig.env(f"db:of-{ACC}")
    rig.fail = True
    assert m.fastt_account_owned(ACC) and m.fastt_owned(ACC, 1)        # never read: the legacy worker is kept off
    assert not m.fastt_serves(ACC, 1)
    rig.fail = False
    rig.wait(fs.TTL_S + 1)
    assert not m.fastt_owned(ACC, 1) and not m.fastt_account_owned(ACC) and not m.fastt_serves(ACC, 1)


def test_read_failure_keeps_last_known_value(rig):
    rig.env(f"db:of-{ACC}")
    rig.set(ACC, sel("selected", 5, 6))
    assert m.fastt_owned(ACC, 6)
    rig.fail = True
    rig.wait(fs.TTL_S + 1)
    assert m.fastt_owned(ACC, 5) and m.fastt_owned(ACC, 6) and m.fastt_serves(ACC, 6)   # still the last value
    rig.fail = False
    rig.set(ACC, sel("selected", 5))
    rig.wait(fs.TTL_S + 1)
    assert not m.fastt_serves(ACC, 6)


def test_unreadable_stored_value_is_a_failed_read_for_that_account(rig):
    rig.env(f"db:of-{ACC}")
    rig.set(ACC, sel("selected", 5))
    assert m.fastt_serves(ACC, 5)
    rig.set(ACC, {"mode": "bogus", "fans": [5]})
    rig.wait(fs.TTL_S + 1)
    assert m.fastt_serves(ACC, 5) and m.fastt_owned(ACC, 5)


def test_removal_stops_serving_but_stays_owned(rig):
    rig.env(f"db:of-{ACC}")
    rig.set(ACC, sel("selected", 5, 6))
    assert m.fastt_serves(ACC, 6)
    rig.wait(fs.TTL_S + 1)
    rig.set(ACC, sel("selected", 5))
    assert not m.fastt_serves(ACC, 6) and m.fastt_owned(ACC, 6)         # never handed back to the legacy worker
    rig.wait(fs.TTL_S + 1)
    rig.set(ACC, None)                                                  # cleared entirely
    assert not m.fastt_serves(ACC, 5) and m.fastt_owned(ACC, 5) and m.fastt_owned(ACC, 6)
    rig.set(ACC, sel("all"))
    rig.wait(fs.TTL_S + 1)
    assert m.fastt_account_owned(ACC)
    rig.set(ACC, sel("selected", 5))
    rig.wait(fs.TTL_S + 1)
    assert m.fastt_account_owned(ACC)                                   # "all" is never un-owned in a process


def test_cache_ttl(rig):
    rig.env(f"db:of-{ACC}")
    rig.set(ACC, sel("selected", 5))
    for _ in range(50):
        m.fastt_owned(ACC, 5)
        m.fastt_serves(ACC, 5)
    assert rig.reads == 1
    rig.wait(fs.TTL_S - 1)
    m.fastt_owned(ACC, 5)
    assert rig.reads == 1
    rig.wait(2)
    m.fastt_owned(ACC, 5)
    assert rig.reads == 2
    rig.fail = True
    rig.wait(fs.TTL_S + 1)
    m.fastt_owned(ACC, 5)
    m.fastt_owned(ACC, 5)
    assert rig.reads == 3                                              # a failed read is not retried per call


def test_desired_scope_always_waits_for_the_settle(rig):
    rig.env(f"db:of-{ACC}")
    rig.set(ACC, sel("single", 5))
    assert fs.desired_scope() == (set(), set())
    rig.wait(fs.SETTLE_S + 1)
    assert fs.desired_scope() == ({(ACC, 5)}, set())
    rig.env(f"of-{ACC}:7")                                             # env pair with no value: no wait
    rig.set(ACC, None)
    fs.reset_cache()
    assert fs.desired_scope() == ({(ACC, 7)}, set())


@pytest.mark.parametrize("value,ok", [
    ({"mode": "single", "fans": [7]}, True), ({"mode": "single", "fans": [7, 7]}, False),
    ({"mode": "single", "fans": []}, False), ({"mode": "single"}, False),
    ({"mode": "selected", "fans": [3, 1, 3]}, True), ({"mode": "selected", "fans": []}, False),
    ({"mode": "selected", "fans": [0]}, False), ({"mode": "selected", "fans": [-2]}, False),
    ({"mode": "selected", "fans": [True]}, False), ({"mode": "selected", "fans": ["5"]}, False),
    ({"mode": "selected", "fans": [1.0]}, False), ({"mode": "selected", "fans": "5"}, False),
    ({"mode": "selected", "fans": list(range(1, 5001))}, True),
    ({"mode": "selected", "fans": list(range(1, 5002))}, False),
    ({"mode": "all"}, True), ({"mode": "all", "fans": [1, 2]}, True), ({"mode": "all", "fans": "junk"}, True),
    ({"mode": "some", "fans": [1]}, False), ({"fans": [1]}, False), ({"mode": "all", "extra": 1}, False),
    ("all", False), ([1], False), (None, False),
])
def test_normalize(value, ok):
    if ok:
        out = fs.normalize(value)
        assert out["mode"] == value["mode"]
        if value["mode"] == "all":
            assert out["fans"] == []
        else:
            assert out["fans"] == sorted(set(value["fans"]))
    else:
        with pytest.raises(ValueError):
            fs.normalize(value)


def test_allowlist_parse():
    assert fs.parse_allowlist("db:of-9, db:7,of-5:*,of-5:3,8:2,db:,db:x y") == (
        {("5", 3), ("8", 2)}, {"5"}, {"9", "7", "x y"})


# ── settings route ───────────────────────────────────────────────────────────

@pytest.fixture
def route(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    import scripts_api as api
    from db.models import AccountAiConfig, Base
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")

    async def setup():
        async with engine.begin() as con:
            await con.run_sync(Base.metadata.create_all)
    asyncio.run(setup())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as s, s.begin():
            yield s

    async def noop(*a, **k):
        return "noop"
    monkeypatch.setattr(api, "get_session", get_session)
    monkeypatch.setattr(api, "assert_account_owned", lambda a: None)
    monkeypatch.setattr(api, "ensure_kind_rule", noop)
    yield api, engine, AccountAiConfig
    asyncio.run(engine.dispose())


def test_put_validates_strictly_and_carries_value(route):
    api, engine, Row = route
    from fastapi import HTTPException

    def put(config):
        return asyncio.run(api.put_ai_chatter_config(api._ConfigBody(account_id="of-1", config=config)))

    def stored():
        async def go():
            async with engine.connect() as con:
                from sqlalchemy import select
                return (await con.execute(select(Row.ai_chatter_config_json).where(Row.account_id == "of-1"))).scalar()
        return json.loads(asyncio.run(go()))

    for bad in ({"mode": "single", "fans": [1, 2]}, {"mode": "x"}, {"mode": "selected", "fans": [0]},
                {"mode": "selected", "fans": list(range(1, 5002))}, "all", {"mode": "all", "z": 1}):
        with pytest.raises(HTTPException) as e:
            put({"enabled": True, "fastt_scope": bad})
        assert e.value.status_code == 422
    out = put({"enabled": True, "fastt_scope": {"mode": "all", "fans": [4, 5]}})
    assert out["config"]["fastt_scope"] == {"mode": "all", "fans": []} and stored()["fastt_scope"] == {"mode": "all", "fans": []}
    put({"enabled": False, "payers_only": True})                       # a tab that never heard of the key
    assert stored()["fastt_scope"] == {"mode": "all", "fans": []} and stored()["enabled"] is False
    put({"fastt_scope": {"mode": "selected", "fans": [9, 8]}})
    assert stored()["fastt_scope"] == {"mode": "selected", "fans": [8, 9]}
    put({"enabled": True, "fastt_scope": None})                         # explicit clear
    assert "fastt_scope" not in stored()
