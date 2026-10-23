"""Offline release/import history, full-evidence restore and real demo cleanup."""
from datetime import timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select,text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,seed,event,due
from test_onlyfans_http_pg import synthetic_scenario,port
from test_onlyfans_recovery_pg import KEY,empty_database,pg_bin,rows
from db.models import AccountAiConfig,Fan,Message,OfConversationState
from db.onlyfans_models import OfIngress,OfRuntime,OfWork,OfControlAudit
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfNativeMemoryChange
from db.schema import HEAD,SchemaMismatch,check_schema
from onlyfans_runtime import operations
from onlyfans_runtime.store import Store,utcnow


def test_imported_history_stays_history_and_disabled_runtime_cannot_claim_it(database):
    url,engine=database
    store=seed(url,engine)
    cutoff=utcnow()
    history=event(mid=900,body="Synthetic imported historical inbound")
    from dataclasses import replace
    history=replace(history,created_at=(cutoff-timedelta(days=10)).replace(tzinfo=timezone.utc).isoformat())
    with Session(engine) as s,s.begin():
        runtime=s.get(OfRuntime,"acc-a")
        runtime.enabled=False;runtime.activated_at=cutoff
        state=s.get(OfConversationState,("acc-a",101))
        state.mode="no_contact";state.revision=7;state.unresolved_attempt_id="retained-import-unknown"
        s.add(Message(account_id="acc-a",fan_id=101,message_id=900,direction="in",body=history.body,
            created_at=cutoff-timedelta(days=10),media_ids="[]",price_cents=0))
    assert store.record("tenant-a","acc-a",history,source="catchup")=="accepted"
    assert store.drain("tenant-a","acc-a")==1
    assert store.record("tenant-a","acc-a",history,source="ws")=="duplicate"
    # Both old catch-up and explicit import are history. Import may contain a
    # newer source timestamp, but must still never synthesize reply work.
    for mid,source,stamp in [(901,"catchup",cutoff-timedelta(days=5)),(902,"import",cutoff+timedelta(seconds=2))]:
        imported=replace(event(mid=mid,fan=202),created_at=stamp.replace(tzinfo=timezone.utc).isoformat())
        assert store.record("tenant-a","acc-a",imported,source=source)=="accepted"
        store.drain("tenant-a","acc-a")
    store.close();store=Store(url)
    assert store.claim("tenant-a") is None
    with Session(engine) as s:
        state=s.get(OfConversationState,("acc-a",101))
        assert (state.mode,state.revision,state.unresolved_attempt_id)==("no_contact",7,"retained-import-unknown")
        assert s.query(Message).count()==3 and s.query(OfWork).count()==0
        assert all(row.status=="applied" for row in s.scalars(select(OfIngress)))
        assert s.get(OfRuntime,"acc-a").activated_at==cutoff and s.get(OfRuntime,"acc-a").enabled is False
    fresh=event(mid=903,fan=202,body="Synthetic inbound while disabled")
    store.record("tenant-a","acc-a",fresh);store.drain("tenant-a","acc-a");due(engine)
    assert store.claim("tenant-a") is None
    with Session(engine) as s,s.begin():s.get(OfRuntime,"acc-a").enabled=True
    store.control("tenant-a","acc-a",202,mode="automated",expected_revision=0,command_key="synthetic-new-input-owner",
        actor="synthetic-rehearsal",reason="Explicit offline future-input fixture")
    assert store.claim("tenant-a") is None,"arming must not replay the blocked/imported turn"
    store.record("tenant-a","acc-a",event(mid=904,fan=202,body="Synthetic fresh eligible input"));store.drain("tenant-a","acc-a");due(engine)
    claim=store.claim("tenant-a")
    assert claim is not None and claim.fan_id==202 and claim.target_message_id==904
    store.complete("tenant-a",claim)
    store.close()


def test_complete_workspace_evidence_restores_only_to_quarantine(database,tmp_path,pg_bin,monkeypatch):
    url,engine=database
    store,other,*_=synthetic_scenario(database,monkeypatch)
    store.control("tenant-a","acc-a",101,mode="no_contact",expected_revision=1,command_key="rehearsal-stop",
        actor="synthetic-rehearsal",reason="Offline restore evidence",action="mark_no_contact")
    from onlyfans_runtime.configuration import get_configuration,update_configuration
    configuration=get_configuration(store,"tenant-a","acc-a")
    update_configuration(store,"tenant-a","acc-a",patch={"persona":"Synthetic approved configuration for recovery."},
        expected_revision=configuration["configurationRevision"],expected_fingerprint=configuration["fingerprint"],
        command_key="a"*64,actor="op-session:"+"b"*64)
    # Include real same-call native correction evidence, in a separate synthetic
    # account so the intentionally unresolved sale account stays held throughout.
    memory_store=seed(url,engine,account="acc-memory")
    from onlyfans_runtime.native_planning import NativeTextPlanner,SyntheticNativeModel
    from onlyfans_runtime.text_delivery import TextAuthority
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");monkeypatch.setenv("DATABASE_URL",url)
    with Session(engine) as s,s.begin():
        runtime=s.get(OfRuntime,"acc-memory");runtime.recovery_needed=False;runtime.last_sync_at=utcnow()
        s.add(AccountAiConfig(account_id="acc-memory",model="grok-4-1-fast-non-reasoning",persona="Synthetic Creator enjoys hiking.",daily_cost_cap_cents=100))
        s.get(Fan,("acc-memory",101)).home_city="Brisbane"
    TextAuthority(memory_store).configure("tenant-a","acc-memory",expected_revision=0,enabled=True,limit_units=5,
        throttle_seconds=1,actor="synthetic-rehearsal",reason="Offline correction fixture")
    memory_store.record("tenant-a","acc-memory",event(body="I live in Sydney now."));memory_store.drain("tenant-a","acc-memory");due(engine)
    claim=memory_store.claim("tenant-a");assert claim.account_id=="acc-memory"
    model=SyntheticNativeModel("How is Sydney treating you?",fact_updates=[{"field":"home_city","value":"Sydney","quote":"I live in Sydney now"}],reported_cost_millicents=7)
    NativeTextPlanner(memory_store).prepare("tenant-a",claim,model)
    assert len(model.calls)==1
    with Session(engine) as s:assert s.scalar(select(OfNativeMemoryChange.change_id)) is not None
    memory_store.close()
    # Back up actual 0091 operator command evidence as well as the old native
    # controls. Stopping the runtime must retain the ambiguous send verbatim.
    from onlyfans_runtime.launch_operations import account_state,control_account
    unknown_intents=[row for row in rows(engine)["of_text_intent"]
        if row["account_id"]=="acc-a" and row["status"]=="unknown"]
    assert unknown_intents
    with Session(engine) as s:
        fingerprint=account_state(s,s.get(OfRuntime,"acc-a"))["fingerprint"]
    stopped=control_account(store,"tenant-a","acc-a",action="emergency_stop",
        expected_fingerprint=fingerprint,command_key="e"*64,actor="op-session:"+"f"*64,
        reason="Synthetic release backup evidence")
    assert stopped["recorded"] is True and stopped["enabled"] is False
    assert stopped["unresolvedOutgoing"]==len(unknown_intents)
    before=rows(engine)
    assert before["of_commerce_evidence"] and before["grok_calls"] and before["of_control_audit"]
    assert before["of_configuration_change"]
    assert before["of_launch_command"]
    assert [row for row in before["of_text_intent"]
        if row["account_id"]=="acc-a" and row["status"]=="unknown"]==unknown_intents
    archive=tmp_path/"synthetic-release.twinof"
    operations.backup(url,archive,key=KEY,pg_bin=pg_bin)
    with empty_database() as (target,restored):
        result=operations.restore(target,archive,key=KEY,pg_bin=pg_bin)
        assert result["releasePermitted"] is False
        after=rows(restored);after.pop("alembic_version")
        expected=dict(before);expected.pop("alembic_version")
        assert after==expected
        # Even the exact current head cannot release the independent restore fence.
        with restored.begin() as c:c.execute(text("UPDATE alembic_version SET version_num=:head"),{"head":HEAD})
        with restored.connect() as c:
            with pytest.raises(SchemaMismatch,match="quarantined"):check_schema(c)
        with pytest.raises(SchemaMismatch,match="quarantined"):Store(target)
    assert rows(engine)==before, "restore cannot mutate the newer source evidence"
    with pytest.raises(operations.RecoveryRefused,match="empty"):
        operations.restore(url,archive,key=KEY,pg_bin=pg_bin)
    assert rows(engine)==before
    store.close();other.close()


def paired_dashboard():
    paired=os.environ.get("ONLYFANS_TEST_DASHBOARD")
    assert paired,"Set ONLYFANS_TEST_DASHBOARD to the exact installed dashboard candidate"
    dashboard=Path(paired).resolve()
    assert Path(paired).is_absolute(),"The paired dashboard checkout must be explicit and absolute"
    for required in ("package.json","node_modules/next/dist/bin/next",
                     "preview/onlyfans-integration/package.json","scripts/check-onlyfans-http-browser.mjs"):
        assert (dashboard/required).is_file(),f"Paired dashboard checkout incomplete: {required}; install its lockfile dependencies"
    return dashboard


def test_one_command_demo_smoke_runs_current_screens_and_removes_its_database(database):
    url,engine=database
    backend=Path(__file__).resolve().parents[2]
    dashboard=paired_dashboard()
    with engine.connect() as c:
        before=set(c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'of_demo_%'")).scalars())
    env={**os.environ,"ONLYFANS_TEST_PG_URL":url,
        "ONLYFANS_API_TOKEN":"inherited-real-token-must-not-be-used",
        "ONLYFANS_CONTROL_TOKEN":"inherited-real-control-must-not-be-used",
        "DASHBOARD_SESSION_SECRET":"inherited-real-session-must-not-be-used",
        "OPENAI_API_KEY":"inherited-real-model-key-must-not-be-used"}
    result=subprocess.run([sys.executable,"service/tests/onlyfans_demo.py","--dashboard",str(dashboard),"--port",str(port()),"--smoke"],
        cwd=backend,env=env,capture_output=True,text=True,timeout=330)
    with engine.connect() as c:
        after=set(c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'of_demo_%'")).scalars())
    assert after==before
    assert result.returncode==0,result.stdout+result.stderr
    assert "Synthetic demo smoke passed" in result.stdout
    assert "native catalogue paging" in result.stdout
    assert "inherited-real" not in result.stdout+result.stderr


def test_one_command_demo_removes_its_database_after_frontend_start_failure(database,tmp_path):
    url,engine=database
    backend=Path(__file__).resolve().parents[2]
    dashboard=paired_dashboard()
    with engine.connect() as c:
        before=set(c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'of_demo_%'")).scalars())
    # Fail after the owned database, worker scenario and API have started.
    # Exercise real ExitStack/finally cleanup rather than replacing the cleanup.
    missing_node=str(tmp_path/"missing-synthetic-node")
    result=subprocess.run([sys.executable,"service/tests/onlyfans_demo.py","--dashboard",str(dashboard),"--port",str(port()),"--smoke"],
        cwd=backend,env={**os.environ,"ONLYFANS_TEST_PG_URL":url,"CODEX_PRIMARY_RUNTIME_NODE":missing_node},
        capture_output=True,text=True,timeout=60)
    with engine.connect() as c:
        after=set(c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'of_demo_%'")).scalars())
    assert after==before,"failed frontend startup leaked its owned demo database"
    assert result.returncode!=0 and "missing-synthetic-node" in result.stderr
    assert "Synthetic demo smoke passed" not in result.stdout
