"""Per-account scope lookup: legacy row 1, preparation rows and promoted commercial rows side by side."""
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope, preparation as prep, scope_contract as sc
from onlyfans_runtime.store import Refused
from test_commercial_preparation_pg import ACTOR, TENANT, setup
from test_commercial_scope_contract import body, legacy_config
from test_commercial_scope_guard_pg import promote, scope
from test_onlyfans_foundation_pg import database, seed  # noqa: F401

OTHER = 'op-session:' + 'b' * 64


def legacy_account(database, account='legacy-a'):
    url, engine = database
    store = seed(url, engine, account=account, tenant=TENANT)
    config = pilot_scope.validate(legacy_config())
    with engine.begin() as c:
        c.execute(text("INSERT INTO of_engine_pilot_scope(scope_id,account_id,binding_id,revision,status,config_json,fingerprint,"
                       "provider_requests_used,provider_credits_reserved,created_at,updated_at) "
                       "SELECT 1,:a,binding_id,1,'configured',:cfg,:fp,0,0,now(),now() FROM of_runtime WHERE account_id=:a"),
                  {"a": account, "cfg": pilot_scope._encoded(config), "fp": pilot_scope._fingerprint(config)})
    return store, config


def runtime(engine, store, account):
    with Session(engine) as s:
        return SimpleNamespace(account_id=account, binding_id=store._runtime(s, TENANT, account).binding_id)


def tamper(engine, sql, **params):
    """Out-of-band damage the guard would normally refuse (the test database is throwaway)."""
    with engine.begin() as c:
        c.execute(text("ALTER TABLE of_engine_pilot_scope DISABLE TRIGGER of_engine_pilot_scope_guard"))
        c.execute(text(sql), params)
        c.execute(text("ALTER TABLE of_engine_pilot_scope ENABLE TRIGGER of_engine_pilot_scope_guard"))


def fleet(database):
    """legacy-a (row 1), prep-1 and prep-2 promoted with overlapping fan ids, prep-3 prepared only."""
    legacy_store, legacy = legacy_account(database)
    stores = {"legacy-a": legacy_store}
    for i in (1, 2, 3):
        stores[f"prep-{i}"] = setup(database, i)[0]
    promote(database[1], "prep-1", key="a1" * 32, fans=[11, 12, 13])
    promote(database[1], "prep-2", key="a2" * 32, fans=[12, 99])
    return stores, legacy


def close(stores):
    for store in stores.values():
        store.close()


def test_each_account_resolves_its_own_scope_and_preparation_stays_invisible(database):
    stores, legacy = fleet(database)
    engine = database[1]
    try:
        with Session(engine) as s:
            row, config = pilot_scope.current(s, runtime(engine, stores["legacy-a"], "legacy-a"))
            assert row.scope_id == 1 and config == legacy and "contract_version" not in config
            rows = {}
            for account in ("prep-1", "prep-2"):
                row, config = pilot_scope.current(s, runtime(engine, stores[account], account))
                assert row.account_id == account and row.scope_id > 1 and config["contract_version"] == "commercial.v1"
                assert row.status == "configured" and row.fingerprint == sc.fingerprint(config)
                rows[account] = (row.scope_id, config["fans"])
            assert rows["prep-1"][0] != rows["prep-2"][0] and rows["prep-1"][1] == [11, 12, 13] and rows["prep-2"][1] == [12, 99]
            # A prepared-only account is "no pilot", exactly as before.
            assert pilot_scope.current(s, runtime(engine, stores["prep-3"], "prep-3")) == (None, None)
            with pytest.raises(Refused, match="unavailable"):
                pilot_scope.require(s, runtime(engine, stores["prep-3"], "prep-3"))
            assert pilot_scope.require(s, runtime(engine, stores["prep-1"], "prep-1"))[1]["contract_version"] == "commercial.v1"
            with pytest.raises(Refused, match="not active"):
                pilot_scope.require(s, runtime(engine, stores["prep-1"], "prep-1"), active=True)
        shown = pilot_scope.describe(stores["prep-1"], TENANT, "prep-1")
        assert shown["status"] == "configured" and shown["config"]["fans"] == [11, 12, 13] and shown["contractVersion"] == "commercial.v1"
        assert pilot_scope.describe(stores["prep-3"], TENANT, "prep-3") == {"accountId": "prep-3", "status": "unconfigured", "revision": 0, "config": None}
        assert "contractVersion" not in pilot_scope.describe(stores["legacy-a"], TENANT, "legacy-a")
    finally:
        close(stores)


def test_the_same_fan_id_in_two_accounts_never_shares_audience_or_authority(database):
    stores, _ = fleet(database)
    engine = database[1]
    try:
        with Session(engine) as s:
            one, two = (runtime(engine, stores[a], a) for a in ("prep-1", "prep-2"))
            pilot_scope.authorize_fan(s, one, 12, ACTOR, "stop")
            pilot_scope.authorize_fan(s, two, 12, ACTOR, "stop")                      # id 12 is in both audiences
            with pytest.raises(Refused, match="outside"):
                pilot_scope.authorize_fan(s, two, 11, ACTOR, "stop")                  # 11 only belongs to prep-1
            with pytest.raises(Refused, match="outside"):
                pilot_scope.authorize_fan(s, one, 99, ACTOR, "stop")
            with pytest.raises(Refused, match="operator permission"):
                pilot_scope.authorize_fan(s, one, 12, OTHER, "stop")
            # The legacy account and the prepared-only account are not scoped by anybody else's audience.
            pilot_scope.authorize_fan(s, runtime(engine, stores["prep-3"], "prep-3"), 11, OTHER, "stop")
            with pytest.raises(Refused, match="outside"):
                pilot_scope.authorize_fan(s, runtime(engine, stores["legacy-a"], "legacy-a"), 12, ACTOR, "stop")
    finally:
        close(stores)


def test_damaged_or_rebound_commercial_scope_fails_closed_and_preparation_visibility_is_unchanged(database):
    stores, _ = fleet(database)
    engine = database[1]
    try:
        with Session(engine) as s:
            ok = runtime(engine, stores["prep-1"], "prep-1")
            with pytest.raises(Refused, match="binding"):
                pilot_scope.current(s, SimpleNamespace(account_id="prep-1", binding_id="another-binding"))
        tamper(engine, "UPDATE of_engine_pilot_scope SET fingerprint=:f WHERE account_id='prep-1'", f="0" * 64)
        tamper(engine, "UPDATE of_engine_pilot_scope SET config_json=replace(config_json,'\"fans\":[12,99]','\"fans\":[12,98]') WHERE account_id='prep-2'")
        tamper(engine, "UPDATE of_engine_pilot_scope SET config_json=replace(config_json,'\"provider_account\":\"acct_prep_3\"','\"provider_account\":\"nope\"') WHERE account_id='prep-3'")
        with Session(engine) as s:
            with pytest.raises(Refused, match="fingerprint"):
                pilot_scope.current(s, ok)
            with pytest.raises(Refused, match="fingerprint"):
                pilot_scope.require(s, ok)
            # Edited audience bytes no longer match the stored fingerprint: damage, never silently accepted.
            two = runtime(engine, stores["prep-2"], "prep-2")
            with pytest.raises(Refused):
                pilot_scope.current(s, two)
            # A damaged PREPARATION row keeps its old behaviour: not a pilot, and the preparation owner still fails closed.
            three = runtime(engine, stores["prep-3"], "prep-3")
            assert pilot_scope.current(s, three) == (None, None)
            with pytest.raises(Refused, match="invalid preparation authority"):
                prep.current(s, three)
    finally:
        close(stores)


def test_send_gates_and_preparation_view_know_the_three_kinds(database):
    stores, _ = fleet(database)
    engine = database[1]
    try:
        with Session(engine) as s:
            legacy, one, three = (runtime(engine, stores[a], a) for a in ("legacy-a", "prep-1", "prep-3"))
            prep.assert_send_allowed(s, legacy)                                       # legacy row 1 never blocked here
            prep.assert_send_allowed(s, SimpleNamespace(account_id="nobody", binding_id="x"))
            with pytest.raises(Refused, match="preparation"):
                prep.assert_send_allowed(s, three)
            with pytest.raises(Refused, match="not active"):
                prep.assert_send_allowed(s, one)                                      # promoted but never activated
            prep.assert_enable_allowed(s, legacy)
            prep.assert_enable_allowed(s, one)                                        # an operator may enable a promoted policy
            with pytest.raises(Refused, match="preparation"):
                prep.assert_enable_allowed(s, three)
            row, projected = prep.current(s, one)                                     # budget reads continue before launch
            assert row.scope_id > 1 and projected == sc.preparation_projection(pilot_scope.current(s, one)[1])
            assert prep.current(s, three)[0].status == "configured"
        view = prep.view(stores["prep-1"], TENANT, "prep-1")
        assert view["configured"] and view["status"] == "configured" and view["commercial"] == {"state": "promoted", "status": "configured"}
        assert view["config"]["contract_version"] == "preparation.v1" and view["automaticRepliesEnabled"] is False
        assert "commercial" not in prep.view(stores["prep-3"], TENANT, "prep-3")
        # Active: delivery admission is open for the promoted row only; stopped or preparation rows stay closed.
        with engine.begin() as c:
            c.execute(text("UPDATE of_engine_pilot_scope SET status='active',active_since=now(),revision=revision+1 WHERE account_id='prep-1'"))
        with Session(engine) as s:
            prep.assert_send_allowed(s, one)
            assert prep.current(s, one) == (None, None)                               # active scopes draw on the pilot guard
            assert pilot_scope.require(s, one, active=True)[0].status == "active"
        assert prep.view(stores["prep-1"], TENANT, "prep-1")["commercial"]["status"] == "active"
        with engine.begin() as c:
            c.execute(text("UPDATE of_engine_pilot_scope SET status='stopped',active_since=NULL,revision=revision+1 WHERE account_id='prep-1'"))
        with Session(engine) as s:
            with pytest.raises(Refused, match="not active"):
                prep.assert_send_allowed(s, one)
    finally:
        close(stores)


def test_pilot_configure_is_per_account_and_cannot_create_convert_or_touch_other_accounts(database):
    from test_plan_c_scope_pg import key
    stores, legacy = fleet(database)
    engine = database[1]
    fresh = seed(database[0], engine, account="fresh-b", tenant=TENANT)
    try:
        config = pilot_scope.validate(legacy_config())
        # A preparation account cannot be turned into the legacy pilot (it used to hit a raw unique violation).
        with pytest.raises(Refused, match="promote"):
            pilot_scope.configure(stores["prep-3"], TENANT, "prep-3", config, expected_revision=0, command_key=key(1), actor=ACTOR, reason="no")
        # The one legacy pilot stays bound to its own creator: a fresh account cannot become a second pilot.
        with pytest.raises(Refused, match="another creator"):
            pilot_scope.configure(fresh, TENANT, "fresh-b", config, expected_revision=0, command_key=key(2), actor=ACTOR, reason="no")
        # No caller can create, convert or downgrade a commercial scope through the legacy command.
        commercial = sc.promotion_config({"contract_version": "preparation.v1", "provider_account": "acct_prep_3", "creator_id": "103",
            "qualification": {"mode": "fixture", "reference": "x"},
            "limits": {"provider_requests": 1, "provider_credit_budget": 1, "provider_credits_per_request": 1}}, 1, "f" * 64, "e" * 64, body())
        for store, account in ((stores["legacy-a"], "legacy-a"), (stores["prep-3"], "prep-3"), (fresh, "fresh-b")):
            with pytest.raises(Refused):
                pilot_scope.configure(store, TENANT, account, commercial, expected_revision=1, command_key=key(3), actor=ACTOR, reason="no")
        with engine.connect() as c:
            assert scope(c, "legacy-a")["revision"] == 1 and scope(c, "prep-3")["scope_id"] > 1
            assert c.scalar(text("SELECT count(*) FROM of_engine_pilot_scope")) == 4
    finally:
        fresh.close()
        close(stores)
