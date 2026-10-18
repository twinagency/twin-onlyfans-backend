"""Owned local HTTP/native-worker scenario for the existing offline demo."""
import threading

from sqlalchemy.orm import Session

from db.models import AccountAiConfig
from test_onlyfans_foundation_pg import seed
from onlyfans_native_protocol_fixture import OWNER,native_message,native_peer
from onlyfans_runtime.native_connector import NativeLoopbackConnector,from_native_server
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_worker import NativeWorker,WorkerAccount
from onlyfans_runtime.text_delivery import TextAuthority

ACCOUNT="acc-worker"
INBOUND="Synthetic inbound processed by the native worker loop"
REPLY="Thanks for asking. How has your day been?"


def run_worker_scenario(database,monkeypatch):
    """Real quiet window, one native call and one HTTP receipt; no live I/O."""
    url,engine=database
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");monkeypatch.setenv("DATABASE_URL",url)
    store=seed(url,engine,account=ACCOUNT)
    try:
        with Session(engine) as s,s.begin():
            s.add(AccountAiConfig(account_id=ACCOUNT,model="grok-4-1-fast-non-reasoning",
                persona="Synthetic Creator enjoys hiking and coffee.",daily_cost_cap_cents=100))
        TextAuthority(store).configure("tenant-a",ACCOUNT,expected_revision=0,enabled=True,limit_units=3,
            throttle_seconds=1,actor="synthetic-worker-demo",reason="Owned local worker acceptance")
        store.begin_scan("tenant-a",ACCOUNT)
        model=SyntheticNativeModel(REPLY,reported_cost_millicents=7)
        stop=threading.Event();reports=[]
        def observe(value):
            reports.append(value)
            if value["accepted"] or value["errors"]:stop.set()
        with native_peer(history={101:[native_message(text=f"<p>{INBOUND}</p>")]}) as peer:
            transport=NativeLoopbackConnector(from_native_server(peer.server,account_id=ACCOUNT,
                owner_id=OWNER,policy_revision=1))
            worker=NativeWorker(store,"tenant-a",(WorkerAccount(ACCOUNT,model,transport,transport),))
            worker.run(stop,max_cycles=50,poll_seconds=.25,observe=observe)
            assert sum(row["accepted"] for row in reports)==1 and not any(row["errors"] for row in reports)
            assert len(model.calls)==len(peer.posts)==1
            assert len([row for row in peer.records if "?" in row["path"]])==2
        return {"accountId":ACCOUNT,"accepted":1,"nativeCalls":1,"httpPosts":1,"providerRequests":0}
    finally:store.close()
