"""Populated 0076 upgrade and startup checks for immutable evidence."""
import pytest
from sqlalchemy import text

from test_onlyfans_foundation_pg import database
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from db.schema import MEMORY,SchemaMismatch,check_schema,upgrade


def test_populated_memory_upgrade_preserves_unknown_delivery_and_model_allowances(database,monkeypatch):
    _,engine=database
    store,authority,claim,planner=ready(database,monkeypatch)
    plan=planner.prepare(TENANT,claim,SyntheticNativeModel(reported_cost_millicents=7))
    identity=planner.queue(TENANT,ACCOUNT,plan)
    assert authority.dispatch(TENANT,ACCOUNT,identity,SyntheticTextTransport(ACCOUNT,outcome="timeout"))=="unknown"
    store.close()
    names=["fans","messages","of_conversation_state","of_delivery_policy","of_text_intent",
        "of_native_plan","of_runtime","grok_calls","grok_daily_cost","of_native_memory_change"]
    def values(c):return {name:[dict(r) for r in c.execute(text(f'SELECT * FROM "{name}"')).mappings()] for name in names}
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE of_configuration_change; DROP FUNCTION of_configuration_immutable(); ALTER TABLE of_runtime DROP COLUMN configuration_revision")
        c.exec_driver_sql("DROP TRIGGER of_control_immutable ON of_control_audit; DROP FUNCTION of_control_immutable(); ALTER TABLE of_control_audit DROP COLUMN action, DROP COLUMN takeover_from, DROP COLUMN takeover_to")
        c.exec_driver_sql("DROP TABLE of_commerce_evidence; DROP FUNCTION of_commerce_immutable()")
        c.exec_driver_sql("ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_media_shape, DROP COLUMN media_json, DROP COLUMN preview_json, DROP COLUMN price_cents, DROP COLUMN catalog_item_id, DROP COLUMN catalog_snapshot_json, DROP COLUMN offer_id")
        c.exec_driver_sql("ALTER TABLE of_delivery_policy DROP COLUMN media_enabled")
        c.exec_driver_sql("ALTER TABLE of_native_plan DROP COLUMN sale_json")
        c.execute(text("UPDATE alembic_version SET version_num=:v"),{"v":MEMORY})
        check_schema(c,expected=MEMORY)
        before=values(c);upgrade(c);upgrade(c);check_schema(c);after=values(c)
        for row in after["of_delivery_policy"]:assert row.pop("media_enabled") is False
        for row in after["of_native_plan"]:assert row.pop("sale_json") is None
        for row in after["of_text_intent"]:
            for key,value in {"media_json":"[]","preview_json":"[]","price_cents":0,"catalog_item_id":None,
                    "catalog_snapshot_json":None,"offer_id":None}.items():assert row.pop(key)==value
        for row in after.get("of_runtime",[]): assert row.pop("configuration_revision")==0
        assert after==before
        assert c.scalar(text("SELECT count(*) FROM of_commerce_evidence"))==0


@pytest.mark.parametrize("table,trigger",[("of_commerce_evidence","of_commerce_immutable"),
    ("of_native_memory_change","of_native_memory_immutable")])
@pytest.mark.parametrize("change",["drop","disable","replace_function"])
def test_startup_refuses_missing_or_weakened_append_only_protection(database,table,trigger,change):
    _,engine=database
    with engine.begin() as c:
        if change=="drop":c.exec_driver_sql(f'DROP TRIGGER "{trigger}" ON "{table}"')
        elif change=="disable":c.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
        else:c.exec_driver_sql(f"CREATE OR REPLACE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$")
        with pytest.raises(SchemaMismatch,match="append-only protection"):check_schema(c)
