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
