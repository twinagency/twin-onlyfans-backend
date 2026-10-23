"""Native catalogue edits against disposable PostgreSQL, no provider or model."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT
from test_onlyfans_offer_import_upgrade_pg import rewind_to_0088
from db.models import CatalogItem, CatalogScript, OfCreatorBinding
from db.onlyfans_content_models import OfContentChange
from db.schema import SchemaMismatch, check_schema, upgrade
from onlyfans_runtime.content_management import get_content, update_content, validate_patch, item_reference, InvalidContent
from onlyfans_runtime.store import Store, Refused, utcnow
from onlyfans_commands.content_api import router, HEADER, SCHEMA

ACTOR = "op-session:" + "a" * 64


def setup(database, *, free=False):
    store = seed(*database)
    with Session(database[1]) as session, session.begin():
        row = CatalogItem(account_id=ACCOUNT, label="Scenic set", description_for_ai="An approved scenic video.",
            media_ids="[11,12]", preview_media_ids="[11]", price_cents=0 if free else 1000,
            kind="video", duration_sec=63, tags='["native-tag"]', band_lo=300, band_hi=1800,
            tip_unlock_cents=876, is_free_teaser=free, enabled=True)
        session.add(row); session.flush(); identity = row.id
    return store, identity


def save(store, item, data, patch, *, key="b" * 64, **kwargs):
    return update_content(store, TENANT, ACCOUNT, item, patch=patch, expected_revision=data["revision"],
        expected_fingerprint=data["fingerprint"], command_key=key, actor=ACTOR, **kwargs)


def test_partial_edit_preserves_native_owner_media_and_unknown_metadata(database):
    store, item = setup(database)
    data = get_content(store, TENANT, ACCOUNT, item)
    assert data["revision"] == 0 and data["currency"] == "USD" and data["isFreeTeaser"] is False
    revision, fingerprint = save(store, item, data, {"label": "New approved label", "priceCents": 1200})
    assert revision == 1 and fingerprint != data["fingerprint"]
    with Session(database[1]) as session:
        row = session.get(CatalogItem, item)
        assert (row.account_id, row.media_ids, row.preview_media_ids) == (ACCOUNT, "[11,12]", "[11]")
        assert (row.kind, row.duration_sec, row.tags, row.band_lo, row.band_hi, row.tip_unlock_cents) == (
            "video", 63, '["native-tag"]', 300, 1800, 876)
        assert row.is_free_teaser is False and row.description_for_ai == "An approved scenic video."
        audit = session.scalar(select(OfContentChange))
        assert json.loads(audit.before_json) == {"label": "Scenic set", "priceCents": 1000}
        assert json.loads(audit.after_json) == {"label": "New approved label", "priceCents": 1200}
        assert audit.fingerprint_after == fingerprint
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 1
    store.close()


def test_historical_replay_exact_ack_and_native_writer_conflict(database):
    store, item = setup(database); first = get_content(store, TENANT, ACCOUNT, item)
    original_ack = save(store, item, first, {"label": "First label"})
    current = get_content(store, TENANT, ACCOUNT, item)
    assert save(store, item, current, {"label": "Second label"}, key="c" * 64)[0] == 2
    assert save(store, item, first, {"label": "First label"}) == original_ack
    with pytest.raises(Refused, match="idempotency"):
        save(store, item, first, {"label": "Different command"})
    latest = get_content(store, TENANT, ACCOUNT, item)
    with Session(database[1]) as session, session.begin():
        session.get(CatalogItem, item).tags = '["human-edit-to-hidden-field"]'
    with pytest.raises(Refused, match="revision"):
        save(store, item, latest, {"enabled": False}, key="d" * 64)
    with Session(database[1]) as session:
        assert session.get(CatalogItem, item).enabled is True
        assert session.get(CatalogItem, item).label == "Second label"
        assert session.scalar(select(func.count()).select_from(OfContentChange)) == 2
    store.close()


def test_large_and_null_legacy_metadata_are_not_editable_excerpts_or_implicitly_cleared(database):
    store, item = setup(database)
    with Session(database[1]) as session, session.begin():
        row = session.get(CatalogItem, item); row.label = "🔥" * 41; row.description_for_ai = "x" * 5000
    before = get_content(store, TENANT, ACCOUNT, item)
    assert before["label"] is before["description"] is None and before["labelTruncated"] and before["descriptionTruncated"]
    save(store, item, before, {"enabled": False})
    with Session(database[1]) as session:
        row = session.get(CatalogItem, item)
        assert row.label == "🔥" * 41 and row.description_for_ai == "x" * 5000
    current = get_content(store, TENANT, ACCOUNT, item)
    save(store, item, current, {"label": "Full replacement", "description": ""}, key="c" * 64)
    with Session(database[1]) as session:
        audit = session.get(OfContentChange, (ACCOUNT, item, "c" * 64))
        value = json.loads(audit.before_json)["description"]
        assert value["bytes"] == 5000 and len(value["sha256"]) == 64 and value["truncated"]
    with Session(database[1]) as session, session.begin():
        row = session.get(CatalogItem, item); row.label = None; row.description_for_ai = None
    data = get_content(store, TENANT, ACCOUNT, item)
    assert data["label"] == data["description"] == "" and not data["labelTruncated"] and not data["descriptionTruncated"]
    save(store, item, data, {"enabled": True}, key="d" * 64)
    with Session(database[1]) as session:
        row = session.get(CatalogItem, item); assert row.label is row.description_for_ai is None
    store.close()


@pytest.mark.parametrize("free", [False, True])
def test_price_policy_cannot_implicitly_change_free_teaser_or_round_money(database, free):
    store, item = setup(database, free=free); data = get_content(store, TENANT, ACCOUNT, item)
    for patch in ({"priceCents": 300 if free else 0}, {"priceCents": True}, {"priceCents": 3.99},
                  {"priceCents": "300"}, {"priceCents": -1}, {"priceCents": 299}, {"priceCents": 20001}):
        with pytest.raises(InvalidContent): save(store, item, data, patch)
    assert get_content(store, TENANT, ACCOUNT, item) == data
    with Session(database[1]) as session, session.begin(): session.get(CatalogItem, item).price_cents = -1
    malformed = get_content(store, TENANT, ACCOUNT, item); assert malformed["priceCents"] is None
    save(store, item, malformed, {"priceCents": 0 if free else 300})
    fresh = get_content(store, TENANT, ACCOUNT, item)
    assert fresh["isFreeTeaser"] is free and fresh["priceCents"] == (0 if free else 300)
    store.close()


@pytest.mark.parametrize("patch", [{}, {"label": ""}, {"label": "  "}, {"label": "🔥" * 41},
    {"description": "🔥" * 501}, {"description": None}, {"label": None}, {"media_ids": "[]"},
    {"isFreeTeaser": True}, {"account_id": "acc-b"}, {"enabled": 1}, {"kind": "video"},
    {"label": "\ud800"}, {"description": "\x00"}])
def test_strict_patch_boundary(patch):
    with pytest.raises(InvalidContent): validate_patch(patch)


@pytest.mark.parametrize("value", [True, 0, -1, 2147483648, "01", "1.0", "-1", "99999999999999999999"])
def test_native_integer_identity_boundary(value):
    with pytest.raises(InvalidContent): item_reference(value)


def test_content_credential_cannot_alias_new_connection_purpose(monkeypatch):
    from onlyfans_commands.content_api import configured
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN", "synthetic-content-only")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT", TENANT)
    monkeypatch.setenv("ONLYFANS_CONNECTION_TOKEN", "synthetic-connection-only")
    assert configured() == ("synthetic-content-only", TENANT)
    monkeypatch.setenv("ONLYFANS_CONNECTION_TOKEN", "synthetic-content-only")
    assert configured() is None


def test_scope_revocation_legacy_script_filter_and_no_implicit_row(database):
    store, item = setup(database); other = seed(*database, account="acc-b", tenant="tenant-b")
    with Session(database[1]) as session, session.begin():
        script = CatalogScript(account_id=ACCOUNT, name="Retired native sequence")
        session.add(script); session.flush()
        legacy = CatalogItem(account_id=ACCOUNT, script_id=script.id, label="Retired item")
        foreign = CatalogItem(account_id="acc-b", label="Foreign item")
        session.add_all([legacy, foreign]); session.flush(); hidden_ids = (legacy.id, foreign.id, 2147483647)
    data = get_content(store, TENANT, ACCOUNT, item)
    for identity in hidden_ids:
        with pytest.raises(Refused, match="content unavailable"): get_content(store, TENANT, ACCOUNT, identity)
        with pytest.raises(Refused, match="content unavailable"): save(store, identity, data, {"enabled": False})
    with pytest.raises(Refused, match="scope"): get_content(store, "tenant-b", ACCOUNT, item)
    save(store, item, data, {"label": "Before revocation"})
    with Session(database[1]) as session, session.begin():
        session.get(OfCreatorBinding, "binding-" + ACCOUNT).revoked_at = utcnow()
    with pytest.raises(Refused, match="scope"): save(store, item, data, {"label": "Before revocation"})
    store.close(); other.close()


def race(url, item, data, key, queue):
    store = Store(url)
    try: queue.put(("ok", save(store, item, data, {"label": "Concurrent edit"}, key=key)))
    except Refused: queue.put(("refused", None))
    finally: store.close()


@pytest.mark.parametrize("same_key", [False, True])
def test_separate_processes_serialize_edits_and_replays(database, same_key):
    store, item = setup(database); data = get_content(store, TENANT, ACCOUNT, item); store.close()
    context = multiprocessing.get_context("spawn"); queue = context.Queue()
    workers = [context.Process(target=race, args=(database[0], item, data,
        ("b" if same_key or index == 0 else "c") * 64, queue)) for index in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30); assert worker.exitcode == 0
    responses = [queue.get(timeout=2)[0] for _ in workers]
    assert sorted(responses) == (["ok", "ok"] if same_key else ["ok", "refused"])
    with Session(database[1]) as session:
        assert session.scalar(select(func.count()).select_from(OfContentChange)) == 1


@pytest.mark.parametrize("stage", ["queued", "admitted"])
def test_price_edit_invalidates_frozen_send_before_provider_io(database, stage):
    from test_onlyfans_media_pg import manual, intent
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
    store, authority, item = manual(database)
    identity = intent(authority, item)
    grant = authority.admit(TENANT, ACCOUNT, identity) if stage == "admitted" else None
    save(store, item, get_content(store, TENANT, ACCOUNT, item), {"priceCents": 1100})
    wire = SyntheticTextTransport(ACCOUNT)
    if grant:
        with pytest.raises(Refused): wire.send(grant, authority=authority, tenant_id=TENANT)
    else:
        assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "cancelled"
    assert wire.calls == []
    with Session(database[1]) as session:
        assert session.get(OfTextIntent, (ACCOUNT, identity)).wire_started_at is None
        assert session.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
    store.close()


def test_api_auth_version_strict_json_exact_replay_and_scope(database, monkeypatch):
    store, item = setup(database)
    for name, value in {"ONLYFANS_PROCESS_ROLE": "api", "ONLYFANS_API_TOKEN": "synthetic-read",
        "ONLYFANS_CONTROL_TOKEN": "synthetic-control", "ONLYFANS_CONTENT_TOKEN": "synthetic-content",
        "ONLYFANS_CONTENT_TENANT": TENANT}.items(): monkeypatch.setenv(name, value)
    app = FastAPI(); app.include_router(router); app.state.control_store = store; client = TestClient(app)
    path = f"/onlyfans/content/v1/accounts/{ACCOUNT}/items/{item}"
    headers = {"Authorization": "Bearer synthetic-content", HEADER: SCHEMA}
    data = get_content(store, TENANT, ACCOUNT, item)
    body = {"patch": {"description": "API approved description"}, "expectedRevision": 0,
        "expectedFingerprint": data["fingerprint"], "commandKey": "b" * 64, "actor": ACTOR}
    for token in (None, "synthetic-read", "synthetic-control", "wrong"):
        auth = {} if token is None else {"Authorization": "Bearer " + token}
        assert client.get(path, headers={HEADER: SCHEMA, **auth}).status_code == 401
        assert client.post(path, headers={HEADER: SCHEMA, **auth}, json=body).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer synthetic-content"}).status_code == 400
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.json()["item"] == data and "no-store" in response.headers["cache-control"]
    assert response.json()["itemId"] == str(item)
    for bad in ({**body, "expectedRevision": True}, {**body, "actor": "forged"}, {**body, "extra": True}):
        assert client.post(path, headers=headers, json=bad).status_code == 400
    for raw in (json.dumps(body)[:-1] + ',"expectedRevision":0}', json.dumps(body).replace('"expectedRevision": 0', '"expectedRevision": NaN')):
        assert client.post(path, headers={**headers, "Content-Type": "application/json"}, content=raw).status_code == 400
    assert client.post(path, headers={**headers, "Content-Type": "application/json"}, content="x" * 65537).status_code == 413
    ack = client.post(path, headers=headers, json=body)
    assert ack.status_code == 200, ack.text
    assert client.post(path, headers=headers, json=body).json() == ack.json()
    with Session(database[1]) as session, session.begin():
        session.get(OfCreatorBinding, "binding-" + ACCOUNT).revoked_at = utcnow()
    assert client.post(path, headers=headers, json=body).status_code == 404
    assert client.get(path, headers=headers).status_code == 404
    for token in ("synthetic-read", "synthetic-control"):
        monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN", token)
        assert client.get(path, headers=headers).status_code == 503
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN", "synthetic-content"); monkeypatch.setenv("ONLYFANS_CONTENT_TENANT", "deployment")
    assert client.get(path, headers=headers).status_code == 503
    store.close()


@pytest.mark.parametrize("change", ["drop", "disable", "weaken"])
def test_immutable_audit_and_startup_inspection(database, change):
    store, item = setup(database)
    save(store, item, get_content(store, TENANT, ACCOUNT, item), {"enabled": False}); store.close()
    for sql in ("UPDATE of_content_change SET actor='forged'", "DELETE FROM of_content_change"):
        with pytest.raises(DBAPIError, match="append-only"):
            with database[1].begin() as connection: connection.exec_driver_sql(sql)
    with database[1].begin() as connection:
        if change == "drop": connection.exec_driver_sql("DROP TRIGGER of_content_immutable ON of_content_change")
        elif change == "disable": connection.exec_driver_sql("ALTER TABLE of_content_change DISABLE TRIGGER of_content_immutable")
        else: connection.exec_driver_sql("CREATE OR REPLACE FUNCTION of_content_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$")
        with pytest.raises(SchemaMismatch, match="append-only protection"): check_schema(connection)


def test_content_import_does_not_load_provider_model_or_legacy_upload_sender(database):
    store, item = setup(database); store.close()
    service = Path(__file__).resolve().parents[1]
    code = r'''
import importlib.abc,os,sys
sys.path.insert(0,sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in {'llm_client','of_client','automation_registry','automation_executor','server','accounts','curl_cffi','client_pool','vault_upload','vault_mirror'}:
   raise AssertionError('legacy import: '+fullname)
sys.meta_path.insert(0,Guard())
from onlyfans_runtime.store import Store
from onlyfans_runtime.content_management import get_content
store=Store(os.environ['DATABASE_URL'])
assert get_content(store,'tenant-a','acc-a',int(sys.argv[2]))['priceCents']==1000
store.close()
'''
    result = subprocess.run([sys.executable, "-c", code, str(service), str(item)], capture_output=True, text=True,
        timeout=30, env={**os.environ, "ONLYFANS_PROCESS_ROLE": "api", "DATABASE_URL": database[0]})
    assert result.returncode == 0, result.stderr


def test_populated_0081_upgrade_preserves_native_catalogue_and_all_prior_evidence(database):
    store, item = setup(database); store.close()
    with database[1].begin() as connection:
        # Recreate the actual 0081 shape, including removal of every later
        # revision. Merely changing the version row leaves 0083+ tables behind.
        rewind_to_0088(connection)
        connection.exec_driver_sql("DROP TABLE of_vault_media_job; DROP FUNCTION of_vault_media_guard()")      # 0088
        connection.exec_driver_sql("DROP TABLE of_connection_enrollment")                                    # 0087
        connection.exec_driver_sql("DROP TABLE of_vault_edit; DROP FUNCTION of_vault_edit_immutable()")      # 0086
        connection.exec_driver_sql("DROP TABLE of_creator_facts_source")                                     # 0085
        connection.exec_driver_sql("DROP TABLE of_ledger_evidence, of_ledger_scan; DROP FUNCTION of_ledger_immutable()")  # 0084
        connection.exec_driver_sql("DROP TABLE of_vault_product_create, of_vault_observation, of_vault_sync_job; "
            "DROP FUNCTION of_vault_product_immutable(); DROP FUNCTION of_vault_sync_frozen()")
        connection.exec_driver_sql("DROP TABLE of_content_change; DROP FUNCTION of_content_immutable()")
        connection.execute(text("UPDATE alembic_version SET version_num=:head"), {"head": "0081_native_connection"})
        check_schema(connection, expected="0081_native_connection")
        tables = connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename!='alembic_version' ORDER BY tablename")).scalars().all()
        def values():
            return {table: [dict(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{table}"').mappings()] for table in tables}
        before = values(); upgrade(connection); upgrade(connection); check_schema(connection)
        after = values()
        # Later revisions may add columns (0089 adds catalog_items.pricing_mode);
        # every prior value must survive exactly.
        assert {t: [{k: r[k] for k in row} for row, r in zip(rows, after[t])] for t, rows in before.items()} == before
        assert all(len(after[t]) == len(rows) for t, rows in before.items())
        assert connection.scalar(text("SELECT count(*) FROM of_content_change")) == 0
