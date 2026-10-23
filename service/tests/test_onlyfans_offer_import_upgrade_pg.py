"""Schema upgrade, append-only protection, v1/v2 content contracts and import API."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import ACTOR, TENANT
from test_onlyfans_offer_import_pg import (ready, refresh, raw, observation, draft, key, history)
from db.models import CatalogItem
from db.schema import OFFER_IMPORT, VAULT_MEDIA, SchemaMismatch, check_schema, upgrade
from onlyfans_commands.content_api import router as v1_router, HEADER as CONTENT_HEADER, SCHEMA as V1
from onlyfans_commands.content_api_v2 import router as v2_router, SCHEMA as V2
from onlyfans_commands.offer_import_api import router as import_router, HEADER, SCHEMA
from onlyfans_runtime.content_management import get_content
from onlyfans_runtime.offer_import import create_draft


def drop_0090(c):
    """Rewind a test database from 0090_launch to 0089 (test databases only)."""
    c.exec_driver_sql("DROP TABLE of_engagement_action, of_engagement_settings_change, of_engagement_settings,"
                      " of_history_memory_correction, of_history_memory_evidence, of_history_memory_receipt, of_history_memory_state,"
                      " of_launch_history_cursor, of_launch_history_scan, of_history_recent_bootstrap,"
                      " of_personality_revision, of_personality_head;"
                      " DROP FUNCTION of_engagement_action_guard(); DROP FUNCTION of_engagement_settings_immutable();"
                      " DROP FUNCTION of_history_memory_evidence_immutable(); DROP FUNCTION of_history_memory_correction_immutable();"
                      " DROP FUNCTION of_personality_immutable();"
                      " ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_origin;"
                      " ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_origin CHECK (origin IN ('automated','manual','campaign'))")
    c.execute(text("UPDATE alembic_version SET version_num=:v"), {"v": OFFER_IMPORT})


def rewind_to_0088(c):
    """Remove every 0090 and 0089 object (test databases only); the caller stamps the revision."""
    drop_0090(c)
    c.exec_driver_sql("DROP TABLE of_content_sync_decision, of_content_sync_conflict, of_content_sync_draft, of_content_sync_asset,"
                      " of_content_sync_operation, of_content_sync_account;"
                      " DROP FUNCTION of_content_sync_decision_immutable(); DROP FUNCTION of_content_sync_asset_frozen(); DROP FUNCTION of_content_sync_conflict_frozen();"
                      " DROP TABLE of_offer_import_decision, of_offer_import_candidate, of_offer_import_cursor, of_offer_import_job, of_offer_observation;"
                      " DROP FUNCTION of_offer_observation_immutable(); DROP FUNCTION of_offer_decision_immutable(); DROP FUNCTION of_offer_import_job_frozen();"
                      " ALTER TABLE catalog_items DROP CONSTRAINT ck_catalog_items_pricing_mode, DROP COLUMN pricing_mode")


def test_populated_0088_upgrade_keeps_existing_items_dynamic_and_unchanged(database):
    _, engine = database
    from db.models import Account
    with Session(engine) as s, s.begin():
        s.add(Account(id="legacy-acc", nickname="Synthetic")); s.flush()
        s.add(CatalogItem(account_id="legacy-acc", kind="image", media_ids="[1]", price_cents=1007, enabled=True))
    with engine.begin() as c:
        before = [{k: v for k, v in r.items() if k != "pricing_mode"} for r in c.execute(text("SELECT * FROM catalog_items")).mappings()]
        rewind_to_0088(c)
        c.execute(text("UPDATE alembic_version SET version_num=:v"), {"v": VAULT_MEDIA})
        check_schema(c, expected=VAULT_MEDIA)
        with pytest.raises(SchemaMismatch, match="not supported"):
            check_schema(c)
        upgrade(c); upgrade(c); check_schema(c)
        after = [dict(r) for r in c.execute(text("SELECT * FROM catalog_items")).mappings()]
        assert [r.pop("pricing_mode") for r in after] == ["dynamic"]
        assert after == before, "no repricing, enabling or other change on upgrade"


def test_populated_0089_upgrade_adds_launch_tables_and_keeps_all_prior_state(database):
    _, engine = database
    populated_sync(database)
    before = snapshot(engine)
    with engine.begin() as c:
        drop_0090(c)
        check_schema(c, expected=OFFER_IMPORT)
        with pytest.raises(SchemaMismatch, match="not supported"):
            check_schema(c)
        upgrade(c); upgrade(c); check_schema(c)
        assert c.execute(text("SELECT count(*) FROM of_engagement_action")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname='ck_of_text_intent_origin' AND pg_get_constraintdef(oid) LIKE '%engagement%'")).scalar() == 1
    assert snapshot(engine) == before


LAUNCH_PROTECTION = [
    ("of_history_memory_evidence", "of_history_memory_evidence_immutable", "append-only protection"),
    ("of_history_memory_correction", "of_history_memory_correction_immutable", "append-only protection"),
    ("of_personality_revision", "of_personality_immutable", "append-only protection"),
    ("of_engagement_settings_change", "of_engagement_settings_immutable", "append-only protection"),
]


@pytest.mark.parametrize("table,trigger,message", LAUNCH_PROTECTION)
@pytest.mark.parametrize("change", ["drop", "disable", "replace_function"])
def test_startup_refuses_weakened_launch_evidence_protection(database, table, trigger, message, change):
    _, engine = database
    with engine.begin() as c:
        if change == "drop":
            c.exec_driver_sql(f'DROP TRIGGER "{trigger}" ON "{table}"')
        elif change == "disable":
            c.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
        else:
            c.exec_driver_sql(f"CREATE OR REPLACE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$")
        with pytest.raises(SchemaMismatch, match=message):
            check_schema(c)


@pytest.mark.parametrize("change", ["drop", "disable", "replace_function"])
def test_startup_refuses_weakened_engagement_action_guard(database, change):
    _, engine = database
    with engine.begin() as c:
        if change == "drop":
            c.exec_driver_sql('DROP TRIGGER of_engagement_action_guard ON of_engagement_action')
        elif change == "disable":
            c.exec_driver_sql('ALTER TABLE of_engagement_action DISABLE TRIGGER of_engagement_action_guard')
        else:
            c.exec_driver_sql("CREATE OR REPLACE FUNCTION of_engagement_action_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
        with pytest.raises(SchemaMismatch, match="frozen-identity protection of_engagement_action"):
            check_schema(c)


@pytest.mark.parametrize("statement,message", [
    ("ALTER TABLE of_engagement_action DROP CONSTRAINT ck_of_engagement_action_status", "missing check constraint"),
    ("ALTER TABLE of_engagement_action DROP CONSTRAINT uq_of_engagement_action_anchor", "missing unique constraint"),
    ("DROP INDEX uq_of_engagement_active_fan", "incompatible index"),
    ("ALTER TABLE of_engagement_action DROP CONSTRAINT fk_of_engagement_action_account_id_of_text_intent", "missing foreign key"),
    ("ALTER TABLE of_personality_revision DROP CONSTRAINT uq_of_personality_command", "missing unique constraint"),
    ("DROP TABLE of_history_recent_bootstrap", "missing table"),
])
def test_check_schema_detects_missing_launch_objects(database, statement, message):
    _, engine = database
    with engine.begin() as c:
        c.exec_driver_sql(statement)
        with pytest.raises(SchemaMismatch, match=message):
            check_schema(c)


@pytest.mark.parametrize("table,trigger", [("of_offer_observation", "of_offer_observation_immutable"),
                                           ("of_offer_import_decision", "of_offer_decision_immutable")])
@pytest.mark.parametrize("change", ["drop", "disable", "replace_function"])
def test_startup_refuses_weakened_offer_evidence_protection(database, table, trigger, change):
    _, engine = database
    with engine.begin() as c:
        if change == "drop":
            c.exec_driver_sql(f'DROP TRIGGER "{trigger}" ON "{table}"')
        elif change == "disable":
            c.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
        else:
            c.exec_driver_sql(f"CREATE OR REPLACE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$")
        with pytest.raises(SchemaMismatch, match="append-only protection"):
            check_schema(c)


def api(store, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN", "synthetic-content-only")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT", TENANT)
    app = FastAPI(); app.state.control_store = store
    for router in (v1_router, v2_router, import_router):
        app.include_router(router)
    return TestClient(app)


def imported(store, account, monkeypatch):
    pages = {(501, None): ([raw(1001, [11, 12], 1999, previews=[11], locked=False)], False)}
    refresh(store, account, [(501, 1001)], pages)
    return create_draft(store, TENANT, account, draft=draft(observation(store, account, 1001), 100), actor=ACTOR)["itemId"]


def test_v1_shape_unchanged_and_refuses_imported_price_or_enable(ready, monkeypatch):
    store, account = ready
    item = imported(store, account, monkeypatch)
    client = api(store, monkeypatch)
    auth = {"authorization": "Bearer synthetic-content-only"}
    v1 = client.get(f"/onlyfans/content/v1/accounts/{account}/items/{item}", headers={**auth, CONTENT_HEADER: V1}).json()
    assert set(v1["item"]) == {"revision", "fingerprint", "label", "labelTruncated", "description", "descriptionTruncated",
                               "priceCents", "enabled", "currency", "isFreeTeaser"}
    current = v1["item"]
    def save(patch, n, version=V1, extra=None):
        body = {"patch": patch, "expectedRevision": current["revision"], "expectedFingerprint": current["fingerprint"],
                "commandKey": key(n), "actor": ACTOR, **(extra or {})}
        path = f"/onlyfans/content/{'v1' if version == V1 else 'v2'}/accounts/{account}/items/{item}"
        return client.post(path, headers={**auth, CONTENT_HEADER: version}, json=body)
    assert save({"priceCents": 2500}, 1).json()["code"] == "version_required"
    assert save({"enabled": True}, 2).json()["code"] == "version_required"
    assert save({"label": "Renamed"}, 3).status_code == 200, "non-commercial labels stay editable in v1"
    current = client.get(f"/onlyfans/content/v1/accounts/{account}/items/{item}", headers={**auth, CONTENT_HEADER: V1}).json()["item"]
    v2 = client.get(f"/onlyfans/content/v2/accounts/{account}/items/{item}", headers={**auth, CONTENT_HEADER: V2}).json()["item"]
    assert v2["pricingMode"] == "fixed" and v2["provenance"]["sourcePriceCents"] == 1999 and v2["priceOverridden"] is False
    assert v2["provenance"]["label"] == "Price from selected OnlyFans offer"
    refused = save({"priceCents": 2500}, 4, V2, {"acknowledgeOverride": False})
    assert refused.status_code == 400 and "overriding" in refused.json()["error"]
    assert save({"priceCents": 2500}, 5, V2, {"acknowledgeOverride": True}).status_code == 200
    v2 = client.get(f"/onlyfans/content/v2/accounts/{account}/items/{item}", headers={**auth, CONTENT_HEADER: V2}).json()["item"]
    assert v2["priceCents"] == 2500 and v2["priceOverridden"] is True and v2["pricingMode"] == "fixed"
    current = v2
    assert save({"enabled": True}, 6, V2, {"acknowledgeOverride": False}).status_code == 200
    assert get_content(store, TENANT, account, item)["enabled"] is True


def test_import_api_contract_scope_and_bounds(ready, monkeypatch):
    store, account = ready
    client = api(store, monkeypatch)
    base = f"/onlyfans/offer-import/v1/accounts/{account}"
    headers = {"authorization": "Bearer synthetic-content-only", HEADER: SCHEMA}
    assert client.post(base + "/jobs", json={}).status_code == 401
    assert client.post(base + "/jobs", headers={"authorization": headers["authorization"]}, json={}).status_code == 400
    body = {"mode": "inventory", "sources": None, "commandKey": key(1), "actor": ACTOR}
    started = client.post(base + "/jobs", headers=headers, json=body)
    assert started.status_code == 200 and started.json()["job"]["status"] == "pending"
    assert client.post(base + "/jobs", headers=headers, json=body).json() == started.json()
    assert client.post(base + "/jobs", headers=headers, json={**body, "commandKey": key(2)}).json()["code"] == "job_active"
    assert client.post(base + "/jobs", headers=headers, json={**body, "priceCents": 1}).status_code == 400
    job = started.json()["job"]["jobId"]
    assert client.get(base + f"/jobs/{job}", headers=headers).json()["job"]["coverage"] == "imported_history"
    assert client.get(base + f"/jobs/{job}/candidates?limit=999", headers=headers).status_code == 400
    assert client.get(base + "/jobs/not-a-job", headers=headers).status_code == 400
    many = {"drafts": [{"commandKey": key(10 + i)} for i in range(21)], "actor": ACTOR}
    assert client.post(base + "/drafts", headers=headers, json=many).status_code == 400
    forged = {"drafts": [{**draft(type("O", (), {"observation_id": "0" * 8 + "-0000-4000-8000-" + "0" * 12, "fingerprint": "0" * 64})(), 50),
                          "priceCents": 1}], "actor": ACTOR}
    assert client.post(base + "/drafts", headers=headers, json=forged).status_code == 400
    assert client.get(base + f"/commands/{key(99)}", headers=headers).json()["command"]["recorded"] is False
    cancelled = client.post(base + f"/jobs/{job}/cancel", headers=headers, json={"expectedRevision": 1, "actor": ACTOR})
    assert cancelled.json()["job"]["status"] == "cancelled"
    assert client.get(f"/onlyfans/offer-import/v1/accounts/other-account/jobs/{job}", headers=headers).status_code == 404


# ── T35: content-sync state across upgrade, and weakened protection ─────

def populated_sync(database):
    from datetime import datetime
    from test_onlyfans_foundation_pg import seed
    url, engine = database
    store = seed(url, engine)
    store.close()
    now = datetime(2026, 10, 23, 12, 0, 0)
    with engine.begin() as c:
        a = "op-session:" + "a" * 64
        c.execute(text("""
            INSERT INTO of_content_sync_account (account_id, binding_id, enabled, status, revision, actor, command_key,
              backlog, failures, created_at, updated_at, covered_through_message_id, reconcile_after_item_id)
            VALUES ('acc-a','binding-acc-a',true,'paused_auth',3,:a,:k1,7,2,now(),now(),555,12);
            INSERT INTO of_content_sync_operation (account_id, operation_id, binding_id, command_key, request_hash, actor, kind,
              status, coverage_json, result_json, attempts, created_at, updated_at)
            VALUES ('acc-a','00000000-0000-4000-8000-000000000001','binding-acc-a',:k2,:k2,:a,'sync_now','queued','{}','{}',0,now(),now()),
                   ('acc-a','00000000-0000-4000-8000-000000000002','binding-acc-a',:k3,:k3,:a,'upload','outcome_unconfirmed','{}','{}',1,now(),now());
            INSERT INTO of_content_sync_asset (account_id, asset_id, binding_id, kind, name, size_bytes, content_hash, upload_token,
              state, attempts, created_at, updated_at, upload_started_at)
            VALUES ('acc-a','00000000-0000-4000-8000-000000000003','binding-acc-a','photo','a.jpg',10,:k4,'token-1','outcome_unconfirmed',1,now(),now(),now());
            INSERT INTO of_content_sync_decision (account_id, command_key, binding_id, action, request_hash, actor, before_json, after_json, result_json, created_at)
            VALUES ('acc-a',:k1,'binding-acc-a','settings',:k1,:a,'{}','{}','{}',now());
        """), {"a": a, "k1": "1" * 64, "k2": "2" * 64, "k3": "3" * 64, "k4": "4" * 64})
    return now


def snapshot(engine):
    with engine.begin() as c:
        return [[dict(r) for r in c.execute(text(f"SELECT * FROM {t} ORDER BY 1, 2")).mappings()] for t in (
            "of_content_sync_account", "of_content_sync_operation", "of_content_sync_asset", "of_content_sync_decision")]


def test_t35_pending_and_unknown_sync_work_survives_repeated_upgrade(database):
    _, engine = database
    populated_sync(database)
    before = snapshot(engine)
    with engine.begin() as c:
        upgrade(c); upgrade(c); check_schema(c)
    assert snapshot(engine) == before, "pending operations, the unknown upload token and the cursors are untouched"
    assert before[2][0]["state"] == "outcome_unconfirmed" and before[1][1]["status"] == "outcome_unconfirmed"
    for statement in ["UPDATE of_content_sync_asset SET upload_token = 'changed'", "UPDATE of_content_sync_asset SET content_hash = 'x'",
                      "DELETE FROM of_content_sync_asset", "UPDATE of_content_sync_decision SET actor = 'x'"]:
        with pytest.raises(Exception), engine.begin() as c:
            c.exec_driver_sql(statement)


@pytest.mark.parametrize("statement,message", [
    ("ALTER TABLE of_content_sync_conflict DROP CONSTRAINT ck_of_content_sync_conflict_resolved", "missing check constraint"),
    ("ALTER TABLE of_content_sync_operation DROP CONSTRAINT uq_of_content_sync_operation_command", "missing unique constraint"),
    ("DROP INDEX uq_of_content_sync_operation_active", "incompatible index"),
    ("DROP INDEX uq_of_content_sync_asset_provider", "incompatible index"),
    ('DROP TRIGGER "of_content_sync_decision_immutable" ON "of_content_sync_decision"', "append-only protection"),
    ('ALTER TABLE "of_content_sync_decision" DISABLE TRIGGER "of_content_sync_decision_immutable"', "append-only protection"),
    ('DROP TRIGGER "of_content_sync_asset_frozen" ON "of_content_sync_asset"', "frozen-identity protection of_content_sync_asset"),
    ('ALTER TABLE "of_content_sync_conflict" DISABLE TRIGGER "of_content_sync_conflict_frozen"', "frozen-identity protection of_content_sync_conflict"),
    ("CREATE OR REPLACE FUNCTION of_offer_import_job_frozen() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
     "frozen-identity protection of_offer_import_job"),
])
def test_t35_check_schema_detects_a_missing_sync_constraint_or_trigger(database, statement, message):
    _, engine = database
    with engine.begin() as c:
        c.exec_driver_sql(statement)
        with pytest.raises(SchemaMismatch, match=message):
            check_schema(c)
