"""Owned PG startup/shutdown and later native polling, without external I/O."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import select as io_select
import signal
import subprocess
import sys

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due
from test_onlyfans_native_connector_pg import fresh, connector
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from onlyfans_native_protocol_fixture import native_message, native_peer
from db.models import Message
from db.onlyfans_models import OfRuntime, OfScan
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
import onlyfans_worker as entry


def configure(database,monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT",TENANT)
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY","ab"*32)
    monkeypatch.setenv("DATABASE_URL",database[0])


def test_valid_empty_database_worker_starts_idle_without_any_provider_account(database,monkeypatch,capsys):
    configure(database,monkeypatch)
    # This test owns startup/intake lifecycle. The independently tested vault
    # job phase is an explicit no-op because this database has no jobs.
    monkeypatch.setattr(entry,"_process_vault_jobs",lambda *_,**__:None)
    assert entry.main(["--once"])==0
    assert json.loads(capsys.readouterr().out)=={
        "schemaVersion":"of-worker.v1","state":"idle","connectedAccounts":0,"scannedAccounts":0}


def test_schema_mismatch_fails_without_healing_or_provider_work(database,monkeypatch,capsys):
    configure(database,monkeypatch)
    with database[1].begin() as connection:
        connection.execute(text("ALTER TABLE messages RENAME COLUMN body TO broken_body"))
    assert entry.main(["--once"])==1
    assert json.loads(capsys.readouterr().out)=={"schemaVersion":"of-worker.v1","state":"startup_unavailable"}
    with database[1].connect() as connection:
        assert "broken_body" in {column["name"] for column in inspect(connection).get_columns("messages")}


def test_actual_worker_process_stops_on_sigterm_while_idle_and_closes_cleanly(database,monkeypatch):
    configure(database,monkeypatch)
    # Keep this signal test independent of the separately tested vault-job
    # service. The real entry, schema check, cohort query and wait loop run.
    code="import onlyfans_worker as w; w._process_vault_jobs=lambda *a,**k:None; raise SystemExit(w.main(['--refresh-seconds','1']))"
    env=dict(os.environ)
    env["PYTHONPATH"]=str(Path(__file__).resolve().parents[1])
    child=subprocess.Popen([sys.executable,"-c",code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        ready,_,_=io_select.select([child.stdout],[],[],15)
        assert ready,"worker did not become idle"
        assert json.loads(child.stdout.readline())["state"]=="idle"
        child.send_signal(signal.SIGTERM)
        output,error=child.communicate(timeout=10)
        assert child.returncode==0,(output,error)
        assert "Traceback" not in error
    finally:
        if child.poll() is None:
            child.kill();child.communicate(timeout=5)


def test_periodic_poll_discovers_later_inbound_and_native_worker_replies_once(database,monkeypatch):
    store,_=fresh(database,monkeypatch)
    historical=native_message(1,text="<p>An old historical input.</p>")
    historical["createdAt"]=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    model=SyntheticNativeModel("A reply to the new input.")
    with native_peer(history={FAN:[historical]}) as peer:
        wire=connector(peer)
        worker=NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,model,wire,wire),))
        for _ in range(3):assert worker.step().errors==0
        assert model.calls==[] and peer.posts==[]
        assert store.request_poll(TENANT,ACCOUNT) is False
        peer.history[FAN].append(native_message(2,text="<p>A new input after the first completed sync.</p>"))
        with database[1].begin() as connection:
            connection.execute(text("UPDATE of_runtime SET last_sync_at=CURRENT_TIMESTAMP-INTERVAL '6 seconds'"))
        assert store.request_poll(TENANT,ACCOUNT) is True
        with Session(database[1]) as session:
            scan=session.get(OfScan,ACCOUNT)
            generation=scan.generation
            assert scan.status=="needed" and not scan.repeat_requested
        # A normal refresh never restarts or requests repetition of unfinished
        # recovery, even when its age exceeds the interval again.
        assert store.request_poll(TENANT,ACCOUNT) is False
        with Session(database[1]) as session:
            scan=session.get(OfScan,ACCOUNT)
            assert scan.generation==generation and not scan.repeat_requested
        for _ in range(3):assert worker.step().errors==0
        due(database[1])
        assert worker.step().accepted==1
        for _ in range(2):assert worker.step().claims==0
        assert len(model.calls)==len(peer.posts)==1
    with Session(database[1]) as session:
        assert session.get(Message,(ACCOUNT,FAN,2)).body=="A new input after the first completed sync."
        assert session.get(Message,(ACCOUNT,FAN,9001)).body=="A reply to the new input."
        assert not session.get(OfRuntime,ACCOUNT).recovery_needed
    store.close()
