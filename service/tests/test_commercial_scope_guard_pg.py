"""Database state machine for the commercial scope contract (0134): raw SQL against real PostgreSQL only."""
import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conversation_engine import preparation as prep, scope_contract as sc
from db.schema import HEAD, SchemaMismatch, check_schema
from test_commercial_preparation_pg import ACTOR, TENANT, setup
from test_commercial_scope_contract import body
from test_onlyfans_foundation_pg import database  # noqa: F401

def migration(name):
    import importlib.util
    from pathlib import Path
    from db import schema
    path = Path(schema.HERE) / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location("step2_" + path.stem.split("_", 2)[2], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KEY = 'd' * 64
OTHER_KEY = 'e' * 64


def scope(c, account):
    return dict(c.execute(text("SELECT * FROM of_engine_pilot_scope WHERE account_id=:a"), {"a": account}).mappings().one())


def audit(c, account, key=KEY):
    binding = c.scalar(text("SELECT binding_id FROM of_runtime WHERE account_id=:a"), {"a": account})
    c.execute(text("INSERT INTO of_launch_command(account_id,command_key,binding_id,kind,request_hash,actor,reason,before_json,result_json,created_at)"
                   " VALUES(:a,:k,:b,'commercial_scope',:h,:actor,'guard test','{}','{}',now())"),
              {"a": account, "k": key, "b": binding, "h": "0" * 64, "actor": ACTOR})


def promotion(row, key=KEY, **changes):
    """(config, SET values) of a promotion exactly as the audited command would write them."""
    config = sc.promotion_config(json.loads(row["config_json"]), row["revision"], row["fingerprint"], key, body(**changes))
    return config, {"config_json": sc.encoded(config), "fingerprint": sc.fingerprint(config),
                    "revision": row["revision"] + 1, "transition_command_key": key}


def update(c, account, values, raw=None):
    assignments = ",".join(f"{k}=:{k}" for k in values) + ("," + raw if raw else "")
    c.execute(text(f"UPDATE of_engine_pilot_scope SET {assignments},updated_at=now() WHERE account_id=:account"), {**values, "account": account})


def promote(engine, account, key=KEY, **changes):
    with engine.begin() as c:
        row = scope(c, account)
        config, values = promotion(row, key, **changes)
        audit(c, account, key)
        update(c, account, values, "commercial_since=now()")
    return config


def refused(engine, work):
    """The statement must be refused by the database guard, leaving nothing behind."""
    with pytest.raises(DBAPIError):
        with engine.begin() as c:
            work(c)


def test_legal_promotion_changes_the_contract_in_place_and_nothing_else(database):
    store, account, cfg = setup(database)
    guard = prep.PreparationRequestGuard(store, TENANT, account, 1)
    guard("GET", "https://api.fansapi.com/api/accounts", True)
    engine = database[1]
    with engine.connect() as c:
        before = scope(c, account)
    config = promote(engine, account)
    with engine.connect() as c:
        after = scope(c, account)
        count = c.scalar(text("SELECT count(*) FROM of_engine_pilot_scope"))
    assert count == 1
    for same in ("scope_id", "account_id", "binding_id", "created_at", "provider_requests_used", "provider_credits_reserved", "last_attempt_json"):
        assert after[same] == before[same], same
    assert before["provider_requests_used"] == 1 and before["provider_credits_reserved"] == 2
    assert (after["status"], after["active_since"], after["revision"]) == ("configured", None, before["revision"] + 1)
    assert json.loads(after["config_json"])["contract_version"] == "commercial.v1" and after["config_json"] == sc.encoded(config)
    assert after["transition_command_key"] == KEY and after["commercial_since"] is not None
    assert config["transition"]["from_fingerprint"] == before["fingerprint"]
    with engine.connect() as c:
        check_schema(c)
    store.close()


def test_every_illegal_promotion_is_refused_by_the_database(database):
    store, account, cfg = setup(database)
    prep.PreparationRequestGuard(store, TENANT, account, 1)("GET", "https://api.fansapi.com/api/accounts", True)
    engine = database[1]
    with engine.connect() as c:
        row = scope(c, account)
    good_config, good = promotion(row)

    def attempt(values=None, raw=None, key=KEY, with_audit=True, config=None):
        def work(c):
            if with_audit:
                audit(c, account, key)
            merged = {**good, **(values or {})}
            if config is not None:
                merged.update(config_json=sc.encoded(config), fingerprint=sc.fingerprint(config))
            update(c, account, merged, raw or "commercial_since=now()")
        refused(engine, work)

    def edit(**changes):
        return {**good_config, **changes}

    attempt(values={"revision": row["revision"]})                                   # no revision bump
    attempt(values={"revision": row["revision"] + 2})                               # skipped revision
    attempt(raw="commercial_since=NULL")                                            # no promotion time
    attempt(values={"transition_command_key": None})                                # no audit key
    attempt(values={"transition_command_key": OTHER_KEY}, key=OTHER_KEY)            # key differs from the lineage
    attempt(with_audit=False)                                                       # no audit row exists first
    attempt(values={"status": "active"}, raw="active_since=now(),commercial_since=now()")   # promotion never activates
    attempt(values={"status": "stopped"})                                           # promotion lands configured
    attempt(config=edit(transition={**good_config["transition"], "from_fingerprint": "0" * 64}))
    attempt(config=edit(transition={**good_config["transition"], "from_revision": row["revision"] + 1}))
    attempt(config=edit(transition={**good_config["transition"], "from": "commercial.v1"}))
    attempt(config=edit(provider_account="acct_other"))
    attempt(config=edit(creator_id="999"))
    attempt(config=edit(qualification={"mode": "production", "reference": good_config["qualification"]["reference"]}))
    attempt(config=edit(qualification={"mode": "fixture", "reference": "another reference"}))
    attempt(config=edit(fans=list(range(1, 102))))                                  # audience cap
    attempt(config=edit(limits={**good_config["limits"], "provider_requests": 0}))  # below the one request already used
    attempt(config=edit(limits={**good_config["limits"], "provider_credit_budget": 1}))   # below the 2 credits reserved
    with engine.connect() as c:
        assert scope(c, account) == row                                             # nothing changed
    promote(engine, account)                                                        # and the legal one still works
    store.close()


def test_a_promoted_row_cannot_return_to_preparation_be_deleted_or_lose_counters_or_lineage(database):
    store, account, cfg = setup(database)
    prep.PreparationRequestGuard(store, TENANT, account, 1)("GET", "https://api.fansapi.com/api/accounts", True)
    engine = database[1]
    commercial = promote(engine, account)
    with engine.connect() as c:
        row = scope(c, account)
    preparation_config = json.loads(json.dumps(cfg))

    def bump(c, **values):
        update(c, account, {"revision": row["revision"] + 1, **values})

    refused(engine, lambda c: bump(c, config_json=sc.encoded(preparation_config), fingerprint=sc.fingerprint(preparation_config)))
    refused(engine, lambda c: c.execute(text("DELETE FROM of_engine_pilot_scope")))
    refused(engine, lambda c: c.execute(text("UPDATE of_engine_pilot_scope SET provider_requests_used=0")))
    refused(engine, lambda c: c.execute(text("UPDATE of_engine_pilot_scope SET provider_credits_reserved=0")))
    refused(engine, lambda c: bump(c, transition_command_key=OTHER_KEY))
    refused(engine, lambda c: bump(c, transition_command_key=None))
    refused(engine, lambda c: bump(c, commercial_since=None))
    refused(engine, lambda c: bump(c, commercial_since="2001-01-01"))
    rewritten = {**commercial, "transition": {**commercial["transition"], "from_fingerprint": "1" * 64}}
    refused(engine, lambda c: bump(c, config_json=sc.encoded(rewritten), fingerprint=sc.fingerprint(rewritten)))
    lowered = {**commercial, "limits": {**commercial["limits"], "provider_credit_budget": 1}}
    refused(engine, lambda c: bump(c, config_json=sc.encoded(lowered), fingerprint=sc.fingerprint(lowered)))
    swapped = {**commercial, "qualification": {**commercial["qualification"], "reference": "swapped evidence"}}
    refused(engine, lambda c: bump(c, config_json=sc.encoded(swapped), fingerprint=sc.fingerprint(swapped)))
    refused(engine, lambda c: bump(c, account_id="somebody-else"))
    refused(engine, lambda c: c.execute(text("UPDATE of_engine_pilot_scope SET scope_id=99")))
    refused(engine, lambda c: c.execute(text("UPDATE of_engine_pilot_scope SET revision=revision+1")))   # revision alone
    with engine.connect() as c:
        assert scope(c, account) == row
    # Ordinary reconfiguration of the editable part is allowed and keeps lineage and counters.
    more = {**commercial, "fans": [11, 12]}
    with engine.begin() as c:
        update(c, account, {"revision": row["revision"] + 1, "config_json": sc.encoded(more), "fingerprint": sc.fingerprint(more)})
    store.close()


def test_active_is_reachable_only_for_a_promoted_row_and_active_config_is_frozen(database):
    store, account, cfg = setup(database)
    engine = database[1]
    with engine.connect() as c:
        row = scope(c, account)
    refused(engine, lambda c: update(c, account, {"status": "active", "revision": row["revision"] + 1}, "active_since=now()"))   # preparation
    commercial = promote(engine, account)
    with engine.connect() as c:
        row = scope(c, account)
    with engine.begin() as c:
        c.execute(text("UPDATE of_engine_pilot_scope SET provider_requests_used=1,provider_credits_reserved=2"))
    refused(engine, lambda c: update(c, account, {"status": "active"}, "active_since=now()"))                                 # no revision bump
    refused(engine, lambda c: update(c, account, {"status": "active", "revision": row["revision"] + 1}))                       # check constraint
    with engine.begin() as c:
        update(c, account, {"status": "active", "revision": row["revision"] + 1}, "active_since=now()")
    refused(engine, lambda c: update(c, account, {"revision": row["revision"] + 2, "config_json": sc.encoded({**commercial, "fans": [1]}),
                                                  "fingerprint": sc.fingerprint({**commercial, "fans": [1]})}))                 # active config frozen
    refused(engine, lambda c: c.execute(text("UPDATE of_engine_pilot_scope SET provider_requests_used=0")))
    with engine.begin() as c:
        c.execute(text("UPDATE of_engine_pilot_scope SET provider_requests_used=provider_requests_used+1,provider_credits_reserved=provider_credits_reserved+2"))
        assert c.scalar(text("SELECT provider_requests_used FROM of_engine_pilot_scope")) == 2
        update(c, account, {"status": "stopped", "revision": row["revision"] + 2}, "active_since=NULL")
        update(c, account, {"status": "configured", "revision": row["revision"] + 3})
        update(c, account, {"status": "active", "revision": row["revision"] + 4}, "active_since=now()")      # relaunch
    store.close()


def test_a_commercial_row_cannot_be_inserted_and_legacy_row_one_never_gets_a_contract(database):
    from test_onlyfans_foundation_pg import seed
    store, account, cfg = setup(database)
    engine = database[1]
    fresh_store = seed(database[0], engine, account="fresh-a", tenant=TENANT)
    with engine.connect() as c:
        row = scope(c, account)
    commercial, _ = promotion(row)
    sql = ("INSERT INTO of_engine_pilot_scope(scope_id,account_id,binding_id,revision,status,config_json,fingerprint,"
           "provider_requests_used,provider_credits_reserved,created_at,updated_at{cols}) "
           "SELECT :sid,:a,binding_id,1,'configured',:cfg,:fp,0,0,now(),now(){vals} FROM of_runtime WHERE account_id=:a")

    def insert(sid, config, extra=None):
        extra = extra or {}
        statement = sql.format(cols="".join("," + k for k in extra), vals="".join(",:" + k for k in extra))
        def work(c):
            c.execute(text(statement), {"sid": sid, "a": "fresh-a", "cfg": sc.encoded(config), "fp": sc.fingerprint(config), **extra})
        return work
    with engine.begin() as c:
        audit(c, "fresh-a", OTHER_KEY)
    lineage = {"transition_command_key": OTHER_KEY, "commercial_since": "2026-01-01"}
    refused(engine, insert(50, commercial, lineage))                                  # never created from nothing
    refused(engine, insert(50, cfg, {"transition_command_key": OTHER_KEY}))          # preparation cannot carry lineage
    refused(engine, insert(50, cfg, {"commercial_since": "2026-01-01"}))
    refused(engine, insert(1, commercial, lineage))                                   # row 1 never carries a contract
    refused(engine, insert(1, cfg))
    refused(engine, insert(1, {"fans": [1, 2]}, lineage))
    with engine.begin() as c:
        insert(1, {"fans": [1, 2]})(c)                                                # a legacy-shaped row 1 is still insertable
    with engine.connect() as c:
        legacy = scope(c, "fresh-a")
    assert legacy["scope_id"] == 1 and legacy["transition_command_key"] is None
    for config in ({"contract_version": "preparation.v1", "fans": [1, 2]}, commercial):
        refused(engine, lambda c, config=config: update(c, "fresh-a", {"revision": 2, "config_json": sc.encoded(config), "fingerprint": "1" * 64}))
    refused(engine, lambda c: update(c, "fresh-a", {"revision": 2, "transition_command_key": OTHER_KEY}))
    refused(engine, lambda c: update(c, "fresh-a", {"revision": 2}, "commercial_since=now()"))
    with engine.begin() as c:                                                         # legacy configuration edits still work
        update(c, "fresh-a", {"revision": 2, "config_json": sc.encoded({"fans": [1, 2, 3]}), "fingerprint": "2" * 64})
        update(c, "fresh-a", {"revision": 3, "status": "active"}, "active_since=now()")
    store.close(); fresh_store.close()


def test_pinned_guard_hash_matches_the_installed_function_and_tampering_is_detected(database):
    store, account, cfg = setup(database)
    engine = database[1]
    from db import schema
    source = None
    with engine.connect() as c:
        check_schema(c)
        source = c.scalar(text("SELECT prosrc FROM pg_proc WHERE proname='of_engine_pilot_scope_guard'"))
    digest = hashlib.sha256(" ".join(source.split()).encode()).hexdigest()
    assert digest in open(schema.__file__).read()
    assert HEAD == "0134_commercial_scope_contract" and schema.REVISION_RANK[HEAD] == 134
    # An older guard (0132) under a head database must be rejected by the pin.
    old = migration("20261111_0800_account_preparation.py").GUARD
    with engine.begin() as c:
        c.execute(text(old))
    with engine.connect() as c:
        with pytest.raises(SchemaMismatch, match="pilot scope protection"):
            check_schema(c)
    with engine.begin() as c:
        c.execute(text(migration("20261111_1300_commercial_scope_contract.py").GUARD))
    with engine.connect() as c:
        check_schema(c)
    store.close()


def test_migration_adds_audit_bound_columns_and_downgrade_keeps_evidence(database):
    from alembic import command
    from db.schema import _config
    store, account, cfg = setup(database)
    engine = database[1]
    with engine.connect() as c:
        fks = c.execute(text("SELECT confdeltype::text,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='of_engine_pilot_scope'::regclass AND contype='f' AND conname LIKE '%launch_command'")).all()
    assert len(fks) == 1 and fks[0][0] == "r" and "(account_id, transition_command_key)" in fks[0][1] and "of_launch_command(account_id, command_key)" in fks[0][1]
    with engine.connect() as c:
        with pytest.raises(DBAPIError):                                                                                    # the audit row must exist first
            with c.begin():
                row = scope(c, account)
                update(c, account, promotion(row)[1], "commercial_since=now()")
    # Preparation-only state downgrades cleanly (restoring the 0132 guard pin) and upgrades again.
    with engine.begin() as c:
        command.downgrade(_config(c), "0133_preparation_retry_state")
    with engine.connect() as c:
        check_schema(c, expected="0133_preparation_retry_state")
        assert not c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='of_engine_pilot_scope' AND column_name IN ('transition_command_key','commercial_since')"))
    with engine.begin() as c:
        command.upgrade(_config(c), HEAD)
    with engine.connect() as c:
        check_schema(c)
        assert scope(c, account)["scope_id"] > 1
    promote(engine, account)
    with pytest.raises(RuntimeError, match="Preserve commercial"):
        with engine.begin() as c:
            command.downgrade(_config(c), "0133_preparation_retry_state")
    with engine.connect() as c:
        check_schema(c)
        assert scope(c, account)["transition_command_key"] == KEY
    store.close()
