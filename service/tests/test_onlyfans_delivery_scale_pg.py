"""Explicit production delivery settings, synthetic PostgreSQL only."""
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings
from onlyfans_runtime.store import Refused

def test_fractional_delivery_policy_requires_explicit_configuration(database,monkeypatch):
    from test_onlyfans_foundation_pg import seed
    from onlyfans_runtime.text_delivery import TextAuthority
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    settings(monkeypatch)
    store=seed(*database)
    authority=TextAuthority(store)
    authority.configure("tenant-a","acc-a",expected_revision=0,enabled=False,
        limit_units=100000,throttle_seconds=0.25,actor="synthetic-operator",reason="Synthetic capacity check")
    with Session(database[1]) as s:
        policy=s.get(OfDeliveryPolicy,"acc-a")
        assert policy.enabled is False and policy.throttle_seconds==0.25
        assert policy.limit_units==100000 and policy.used_units==0
    for value in [True,0,0.01,float("nan"),float("inf")]:
        with pytest.raises(Refused):
            authority.configure("tenant-a","acc-a",expected_revision=1,enabled=False,
                limit_units=100000,throttle_seconds=value,actor="synthetic-operator",reason="Invalid interval")
    store.close()

def test_upgrade_retains_existing_delivery_controls_and_usage(database):
    """Rehearse the 0091 -> 0092 transition with populated controls, not just an empty schema."""
    from alembic import command
    from sqlalchemy import text
    from db.schema import BASELINE, LAUNCH_CONTROLS, HEAD, HERE, _config, check_schema, upgrade
    _, engine = database
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.exec_driver_sql((HERE / "snapshots/0070_postgresql.sql").read_text())
        command.stamp(_config(c), BASELINE)
        command.upgrade(_config(c), LAUNCH_CONTROLS)
        check_schema(c, expected=LAUNCH_CONTROLS)
        c.exec_driver_sql("INSERT INTO accounts(id,is_active_default) VALUES ('of-900000001',false)")
        c.exec_driver_sql("INSERT INTO fans(account_id,fan_id) VALUES ('of-900000001',1000001)")
        c.exec_driver_sql("INSERT INTO of_creator_bindings(id,tenant_id,creator_id,creator_name,account_id,handle,channel) "
                         "VALUES ('synthetic-binding','synthetic-tenant','synthetic-creator','Synthetic','of-900000001','synthetic','onlyfans')")
        c.exec_driver_sql("INSERT INTO of_runtime(account_id,binding_id,enabled,activated_at) "
                         "VALUES ('of-900000001','synthetic-binding',false,CURRENT_TIMESTAMP)")
        c.exec_driver_sql("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state,revision) "
                         "VALUES ('of-900000001',1000001,'paused','effective',6)")
        c.exec_driver_sql("INSERT INTO of_delivery_policy(account_id,revision,enabled,transport_kind,limit_units,used_units,"
                         "queued_count,throttle_seconds,next_dispatch_at,active_intent_id,media_enabled) "
                         "VALUES ('of-900000001',4,true,'native',500,132,0,20,CURRENT_TIMESTAMP+INTERVAL '20 seconds',NULL,false)")
        queries = [
            "SELECT revision,enabled,transport_kind,limit_units,used_units,queued_count,throttle_seconds,next_dispatch_at,active_intent_id,media_enabled FROM of_delivery_policy",
            "SELECT enabled,activated_at FROM of_runtime",
            "SELECT mode,takeover_state,revision FROM of_conversation_state",
        ]
        before = [c.exec_driver_sql(query).all() for query in queries]
        upgrade(c)
        upgrade(c)  # a repeated deployment does not reset any operator choice or counter
        check_schema(c)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert [c.exec_driver_sql(query).all() for query in queries] == before
        assert c.scalar(text("SELECT data_type FROM information_schema.columns WHERE table_schema='public' "
                             "AND table_name='of_delivery_policy' AND column_name='throttle_seconds'")) == "double precision"
