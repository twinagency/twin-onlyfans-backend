"""Populated release recovery: synthetic database, no provider/model calls."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys

import httpx
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_http_pg import synthetic_scenario, port, process, wait_http
from test_onlyfans_recovery_pg import KEY, empty_database, pg_bin, rows
from db.onlyfans_models import OfRuntime
from db.onlyfans_content_sync_progress_models import OfContentSyncSource, OfContentPostProgress
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime import operations
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.launch_operations import account_state, control_account
from managed_proxy.store import Store as ProxyStore
from managed_proxy.provider import Candidate, ProxyError, address_hash


def proxy_rows(engine):
    with engine.begin() as c:
        c.exec_driver_sql("SET LOCAL timezone='UTC'")
        return {name:c.scalar(text(f'SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM onlyfans_proxy."{name}" t'))
                for name in inspect(c).get_table_names(schema="onlyfans_proxy")}


def populated(database, monkeypatch):
    url, engine=database
    store,other,*_=synthetic_scenario(database,monkeypatch)
    broker=ProxyStore(url.replace("postgresql+psycopg2:","postgresql:"),"42"*32)
    broker.initialize()
    candidates=[Candidate(address_hash=address_hash(ip),country="US",order_id=123,
        expires_at=datetime.now(timezone.utc)+timedelta(days=10),
        url=f"http://synthetic:never-real@{ip}:12323") for ip in ("8.8.4.4","8.8.8.8")]
    broker.sync(candidates)
    actor="op-session:"+"a"*64
    used=broker.assign("tenant-a","synthetic-used","synthetic_used","US",actor)
    assert broker.begin_use("tenant-a","synthetic-used",used,actor)
    broker.assign("tenant-a","synthetic-unused","synthetic_unused","US",actor)
    reservation=next(r for r in broker.reservations("tenant-a") if r["creatorId"]=="synthetic-unused")
    assert broker.reclaim("tenant-a",creator="synthetic-unused",address=reservation["addressHash"],
        reservation=reservation["reservationId"],command="b"*64,actor=actor,reason="Offline unused-reservation rehearsal")
    with Session(engine) as s,s.begin():
        s.add(OfContentSyncSource(account_id="acc-a",binding_id="binding-acc-a",source_kind="message",
            source_id=700,fan_id=101,occurrence=1,fold_pending=True))
        s.add(OfContentPostProgress(account_id="acc-a",binding_id="binding-acc-a",connection_revision=1,
            history_marker="synthetic-frozen-marker",history_complete=False))
        s.add(OfVaultSyncJob(account_id="acc-a",job_id="synthetic-retained-vault",binding_id="binding-acc-a",
            command_key="c"*64,request_hash="d"*64,actor=actor,connection_revision=1,owner_id=77,
            max_pages=200,status="pending",page_offset=4824,pages_read=201,items_seen=4824,
            requested_at=utcnow(),updated_at=utcnow()))
    for account in ("acc-a","acc-b"):
        with Session(engine) as s:
            fingerprint=account_state(s,s.get(OfRuntime,account))["fingerprint"]
        control_account(store if account=="acc-a" else other,
            "tenant-a" if account=="acc-a" else "tenant-b",account,action="emergency_stop",
            expected_fingerprint=fingerprint,command_key=("e" if account=="acc-a" else "f")*64,
            actor=actor,reason="Isolated populated release rehearsal")
    before=rows(engine)
    assert before["of_text_intent"] and before["of_commerce_evidence"]
    assert any(r["status"]=="unknown" for r in before["of_text_intent"])
    assert any(r["status"]=="accepted" for r in before["of_text_intent"])
    assert all(not r["enabled"] for r in before["of_runtime"])
    return store,other,broker


def test_full_scope_backup_retains_broker_fences_and_all_crm_evidence(database,tmp_path,pg_bin,monkeypatch):
    url,engine=database
    store,other,broker=populated(database,monkeypatch)
    try:
        before,proxy_before=rows(engine),proxy_rows(engine)
        archive=tmp_path/"crm-and-proxy.twinof"
        metadata=operations.backup(url,archive,key=KEY,pg_bin=pg_bin,include_proxy=True)
        assert metadata["scope"]=="public+onlyfans_proxy"
        assert b"never-real" not in archive.read_bytes()
        with empty_database() as (target,restored):
            result=operations.restore(target,archive,key=KEY,pg_bin=pg_bin)
            assert result["releasePermitted"] is False
            expected=dict(before);expected.pop("alembic_version")
            after=rows(restored);after.pop("alembic_version")
            assert after==expected and proxy_rows(restored)==proxy_before
            restored_broker=ProxyStore(target.replace("postgresql+psycopg2:","postgresql:"),"42"*32)
            try:
                with pytest.raises(ProxyError,match="restore_quarantined"):restored_broker.check()
            finally:restored_broker.close()
        assert rows(engine)==before and proxy_rows(engine)==proxy_before
    finally:
        broker.close();store.close();other.close()


def test_schema_compatible_rollback_preserves_evidence_with_commands_closed(database,tmp_path,monkeypatch):
    legacy=Path(os.environ["ONLYFANS_COMPATIBLE_ROLLBACK_DIR"]).resolve()
    assert (legacy/"service/onlyfans_app.py").is_file()
    url,engine=database
    store,other,broker=populated(database,monkeypatch)
    before,proxy_before=rows(engine),proxy_rows(engine)
    try:
        # Read projections use existing authority row locks. Reuse that owner;
        # close HTTP mutation capabilities and keep every background writer off.
        env={k:v for k,v in os.environ.items() if k in {"PATH","SystemRoot","SYSTEMROOT","WINDIR","TEMP","TMP","PATHEXT","COMSPEC"}}
        env.update(PYTHON_DOTENV_DISABLED="1",PYTHONDONTWRITEBYTECODE="1",PYTHONUTF8="1",
            ONLYFANS_PROCESS_ROLE="api",DATABASE_URL=url,PYTHONPATH=str(legacy/"service"),
            ONLYFANS_API_TOKEN="synthetic-rollback-read-token",ONLYFANS_API_TENANT="tenant-a")
        api_port=port();base=f"http://127.0.0.1:{api_port}"
        headers={"Authorization":"Bearer synthetic-rollback-read-token","X-OnlyFans-Schema-Version":"of-preview.v2"}
        with process([sys.executable,"-m","uvicorn","onlyfans_app:app","--app-dir","service",
                      "--host","127.0.0.1","--port",str(api_port)],cwd=legacy,env=env,log=tmp_path/"rollback-api.log") as child:
            wait_http(base+"/healthz",child)
            with httpx.Client(trust_env=False,timeout=20) as client:
                for path in ("/ready","/accounts","/accounts/acc-a/workspace/sales","/accounts/acc-a/operations"):
                    response=client.get(base+"/onlyfans/api/v1"+path,headers=headers)
                    assert response.status_code==200,(path,response.text)
                assert client.get(base+"/onlyfans/api/v1/accounts/acc-b/operations",headers=headers).status_code in (403,404)
                assert client.post(base+"/onlyfans/api/v1/accounts/acc-a/campaigns",headers=headers,json={}).status_code in (400,401,403,404,405)
        assert rows(engine)==before and proxy_rows(engine)==proxy_before
    finally:
        broker.close();store.close();other.close()



from contextlib import contextmanager
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from alembic import command as alembic_command
from db.models import Account, Fan, Message
from db.schema import BASELINE, HEAD, LEDGER_HISTORY, SchemaMismatch, _config, check_schema, upgrade


@contextmanager
def populated_preupgrade_database():
    # Start empty, install the frozen baseline and replay only through 0092.
    # Never stamp a current database backwards or apply broker recovery-upgrade.sql.
    with empty_database() as (url, engine):
        with engine.begin() as c:
            c.exec_driver_sql((Path(__file__).resolve().parents[1]/"db/snapshots/0070_postgresql.sql").read_text())
            alembic_command.stamp(_config(c), BASELINE)
            alembic_command.upgrade(_config(c), LEDGER_HISTORY)
            check_schema(c, expected=LEDGER_HISTORY)
            c.exec_driver_sql((Path(__file__).resolve().parents[1]/"managed_proxy/schema.sql").read_text())
        with Session(engine) as s, s.begin():
            # Use the actual historical columns. Current ORM inserts include
            # later fields and fail before this populated upgrade can run.
            s.execute(text("INSERT INTO accounts (id,nickname,is_active_default) VALUES ('preupgrade','Retained synthetic account',false)"))
            s.execute(text("""INSERT INTO fans (
                account_id,fan_id,is_name_real,recent_events,likes_boobs,likes_ass,
                family_names,pets,communication_style,tags,custom_fields,
                lifetime_spend_cents,is_bot,turn_counter,is_followed,
                deep_convo_state,deep_convo_skip_level,deep_convo_skip_remaining,source
            ) VALUES ('preupgrade',1,true,'[]',false,false,'{}','[]','{}','[]','{}',
                      0,false,0,false,'missing',0,0,'onlyfans')"""))
            s.execute(text("""INSERT INTO messages (
                account_id,fan_id,message_id,direction,sender_name,body,media_ids,
                media_count,price_cents,is_tip,is_unsent,is_promise,created_at
            ) VALUES ('preupgrade',1,9,'out','','PREUPGRADE PRIVATE HISTORY','[]',
                      0,0,false,false,false,:now)"""), {"now": utcnow()})
            s.execute(text("INSERT INTO of_conversation_state (account_id,fan_id,mode,revision,unresolved_attempt_id) VALUES ('preupgrade',1,'no_contact',4,'unknown-preupgrade')"))
            s.execute(text("INSERT INTO of_message_delivery (account_id,fan_id,message_id,outcome,attempt_id,evidence_ref) VALUES ('preupgrade',1,9,'unknown','unknown-preupgrade','retained-evidence')"))
        nonce=bytes(range(12)); address="7"*64
        sealed=base64.b64encode(nonce+AESGCM(bytes.fromhex("42"*32)).encrypt(nonce,b"http://synthetic:never-real@8.8.4.4:12323",("twin-managed-proxy.v1:"+address).encode())).decode()
        with engine.begin() as c:
            c.execute(text("""INSERT INTO onlyfans_proxy.inventory
                (address_hash,country,order_id,expires_at,sealed_url,state,owner_key,tenant_id,creator_id,handle,account_id,assigned_at,observed_at)
                VALUES (:address,'US',123,CURRENT_TIMESTAMP+INTERVAL '10 days',:sealed,'assigned',:owner,'tenant-a','synthetic-used','synthetic_used','preupgrade',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""),{"address":address,"sealed":sealed,"owner":"8"*64})
            c.execute(text("INSERT INTO onlyfans_proxy.audit(address_hash,tenant_id,creator_id,handle,actor,action) VALUES (:address,'tenant-a','synthetic-used','synthetic_used',:actor,'assign')"),{"address":address,"actor":"op-session:"+"a"*64})
        yield url, engine


def test_preupgrade_0092_encrypted_full_scope_restore_then_migrate(tmp_path,pg_bin):
    with populated_preupgrade_database() as (url,engine):
        before,proxy_before=rows(engine),proxy_rows(engine)
        assert before["alembic_version"]==[{"version_num":LEDGER_HISTORY}]
        assert "used_at" not in {v["name"] for v in inspect(engine).get_columns("inventory",schema="onlyfans_proxy")}
        archive=tmp_path/"before-migration.twinof"
        with pytest.raises(SchemaMismatch):operations.backup(url,archive,key=KEY,pg_bin=pg_bin,include_proxy=True)
        assert not archive.exists()
        metadata=operations.backup(url,archive,key=KEY,pg_bin=pg_bin,include_proxy=True,source_revision=LEDGER_HISTORY)
        assert metadata["schema"]==LEDGER_HISTORY and metadata["scope"]=="public+onlyfans_proxy"
        encrypted=archive.read_bytes()
        assert b"PREUPGRADE PRIVATE HISTORY" not in encrypted and b"never-real" not in encrypted
        assert rows(engine)==before and proxy_rows(engine)==proxy_before
        with empty_database() as (target,restored):
            result=operations.restore(target,archive,key=KEY,pg_bin=pg_bin)
            assert result["schema"]==LEDGER_HISTORY and result["quarantined"] and not result["releasePermitted"]
            expected=dict(before);expected.pop("alembic_version")
            after=rows(restored);after.pop("alembic_version")
            assert after==expected and proxy_rows(restored)==proxy_before
            with restored.connect() as c:
                assert c.scalar(text("SELECT status FROM onlyfans_restore.fence"))=="quarantined"
                assert c.scalar(text("SELECT version_num FROM alembic_version"))==operations.QUARANTINE_REVISION
                with pytest.raises(SchemaMismatch,match="quarantined"):check_schema(c,expected=LEDGER_HISTORY)
                with pytest.raises(SchemaMismatch,match="quarantined"):check_schema(c)
            broker=ProxyStore(target.replace("postgresql+psycopg2:","postgresql:"),"42"*32)
            try:
                with pytest.raises(ProxyError,match="restore_quarantined"):broker.check()
            finally:broker.close()
            with pytest.raises(operations.RecoveryRefused,match="empty"):operations.restore(target,archive,key=KEY,pg_bin=pg_bin)
        # Only after the encrypted backup and isolated restore have succeeded,
        # rehearse the documented preflight/upgrade on this disposable source copy.
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'")
            for table in ("messages","of_content_sync_source","of_runtime","of_content_sync_account","of_offer_import_job"):
                c.exec_driver_sql("ANALYZE public."+table)
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'")
            upgrade(c);check_schema(c)
            assert c.scalar(text("SELECT version_num FROM alembic_version"))==HEAD
            assert c.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname='of_content_fold_pending' AND NOT tgisinternal"))==1
            assert c.scalar(text("SELECT to_regclass('ix_of_content_sync_source_fold_pending')"))
        assert rows(engine)["messages"]==before["messages"] and proxy_rows(engine)==proxy_before
        assert rows(engine)["of_message_delivery"]==before["of_message_delivery"]


def test_preupgrade_backup_refuses_unsupported_and_incompatible_sources(tmp_path,pg_bin,monkeypatch):
    with populated_preupgrade_database() as (url,engine):
        before,proxy_before=rows(engine),proxy_rows(engine)
        for source in ("0091_launch_controls","0093_ledger_markers","restore_quarantine_0096"):
            with pytest.raises(operations.RecoveryRefused,match="Unsupported backup source"):
                operations.backup(url,tmp_path/(source+".twinof"),key=KEY,pg_bin=pg_bin,include_proxy=True,source_revision=source)
        # Partial migration / falsified source identity must not get to pg_dump.
        with engine.begin() as c:c.exec_driver_sql("ALTER TABLE content_offers ADD COLUMN post_buy_started_at timestamp")
        with pytest.raises(operations.RecoveryRefused,match="structure differs"):
            operations.backup(url,tmp_path/"partial.twinof",key=KEY,pg_bin=pg_bin,include_proxy=True,source_revision=LEDGER_HISTORY)
        with engine.begin() as c:c.exec_driver_sql("ALTER TABLE content_offers DROP COLUMN post_buy_started_at")
        with engine.begin() as c:c.exec_driver_sql("ALTER TABLE messages RENAME COLUMN body TO broken_body")
        with pytest.raises(SchemaMismatch,match="messages.body"):
            operations.backup(url,tmp_path/"broken.twinof",key=KEY,pg_bin=pg_bin,include_proxy=True,source_revision=LEDGER_HISTORY)
        with engine.begin() as c:c.exec_driver_sql("ALTER TABLE messages RENAME COLUMN broken_body TO body")
        assert rows(engine)==before and proxy_rows(engine)==proxy_before
        assert not list(tmp_path.glob("*.twinof"))
