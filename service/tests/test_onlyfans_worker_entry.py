"""Worker process lifecycle and cohort fairness, with no provider/DB I/O."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import onlyfans_worker as entry


def configured(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT","tenant-a")
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY","ab"*32)
    monkeypatch.setenv("DATABASE_URL","postgresql://synthetic:synthetic-password@127.0.0.1:65432/synthetic")


@pytest.mark.parametrize("name,value",[
    ("ONLYFANS_PROCESS_ROLE",None),("ONLYFANS_PROCESS_ROLE","api"),("ONLYFANS_PROCESS_ROLE",""),
    ("ONLYFANS_WORKER_TENANT",None),("ONLYFANS_WORKER_TENANT","deployment"),("ONLYFANS_WORKER_TENANT","../other"),
    ("ONLYFANS_CONNECTION_KEY",None),("ONLYFANS_CONNECTION_KEY","short"),
    ("DATABASE_URL",None),("DATABASE_URL","sqlite:///synthetic.db"),("DATABASE_URL","postgresql://host"),
])
def test_invalid_startup_configuration_refuses_before_store_or_provider(monkeypatch,capsys,name,value):
    configured(monkeypatch)
    import onlyfans_runtime.store
    if value is None:monkeypatch.delenv(name,raising=False)
    else:monkeypatch.setenv(name,value)
    monkeypatch.setattr(onlyfans_runtime.store,"Store",lambda *_:pytest.fail("must not construct a store"))
    with pytest.raises(SystemExit) as result:entry.main(["--once"])
    assert result.value.code==2
    assert "synthetic-password" not in capsys.readouterr().err


@pytest.mark.parametrize("value",["nan","inf","0","61"])
def test_invalid_refresh_interval_refuses_before_startup(monkeypatch,value):
    configured(monkeypatch)
    with pytest.raises(SystemExit) as result:entry.main(["--refresh-seconds",value])
    assert result.value.code==2


def test_schema_or_db_startup_failure_is_redacted_and_exits_nonzero(monkeypatch,capsys):
    configured(monkeypatch)
    import onlyfans_runtime.store
    def failure(url):raise RuntimeError("synthetic-password private-capture")
    monkeypatch.setattr(onlyfans_runtime.store,"Store",failure)
    before={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    assert entry.main(["--once"])==1
    output=capsys.readouterr()
    assert json.loads(output.out)=={"schemaVersion":"of-worker.v1","state":"startup_unavailable"}
    assert "private-capture" not in output.out+output.err and "synthetic-password" not in output.out+output.err
    assert {sig:signal.getsignal(sig) for sig in before}==before


def test_empty_configured_accounts_are_idle_and_do_not_construct_a_native_worker():
    stop=threading.Event();reports=[]
    def load(*_,**kw):
        assert kw=={"limit":32,"after_account":None,"stop":stop}
        return [],None,0
    def worker(*_):pytest.fail("no native worker or provider should be constructed")
    assert entry.run_loop(object(),"tenant-a",stop,once=True,load_cohort=load,worker_type=worker,observe=reports.append)==0
    assert reports==[{"schemaVersion":"of-worker.v1","state":"idle","connectedAccounts":0,"scannedAccounts":0}]


def test_ledger_phase_runs_without_outgoing_accounts_and_honours_stop():
    stop = threading.Event(); seen = []
    def ledger(store, tenant, *, limit, stop):
        assert tenant == "tenant-a" and limit == 1
        seen.append("ledger")
    def load(*_, **__): seen.append("accounts"); return [], None, 0
    assert entry.run_loop(object(), "tenant-a", stop, once=True, load_cohort=load,
        ledger_jobs=ledger, observe=lambda _:None) == 0
    assert seen == ["ledger", "accounts"]
    seen.clear()
    def stopped(*_, stop, **__): seen.append("ledger"); stop.set()
    assert entry.run_loop(object(), "tenant-a", stop, once=True, load_cohort=load,
        ledger_jobs=stopped, observe=lambda _:None) == 0
    assert seen == ["ledger"]


def test_once_runtime_failure_is_redacted_and_exits_nonzero():
    reports=[]
    def load(*_,**__):raise RuntimeError("private-proxy-password")
    assert entry.run_loop(object(),"tenant-a",threading.Event(),once=True,load_cohort=load,observe=reports.append)==1
    assert reports==[{"schemaVersion":"of-worker.v1","state":"unavailable"}]


def test_one_account_becoming_unavailable_does_not_abort_other_account_polls():
    from onlyfans_runtime.store import Refused
    polled=[];serviced=[]
    accounts=[SimpleNamespace(account_id="unavailable"),SimpleNamespace(account_id="ready")]
    class Store:
        def request_poll(self,tenant,account):
            polled.append(account)
            if account=="unavailable":raise Refused("scope unavailable")
    class Worker:
        def __init__(self,store,tenant,cohort):serviced.extend(item.account_id for item in cohort)
        def run(self,*_,**__):pass
    assert entry.run_loop(Store(),"tenant-a",threading.Event(),once=True,
        load_cohort=lambda *_,**__: (accounts,None,2),worker_type=Worker,observe=lambda _:None)==0
    assert polled==serviced==["unavailable","ready"]


def test_paged_cohorts_service_accounts_after_first_32_and_cover_each_cohort_before_refresh():
    stop=threading.Event();seen=[];cursors=[]
    class Store:
        def request_poll(self,tenant,account):assert tenant=="tenant-a"
    def load(*_,after_account=None,limit=32,stop=None):
        cursors.append(after_account)
        if after_account is None:return [SimpleNamespace(account_id=i) for i in range(32)],"account-32",32
        assert after_account=="account-32"
        return [SimpleNamespace(account_id=i) for i in (32,33)],None,2
    class Worker:
        def __init__(self,store,tenant,accounts):self.accounts=accounts
        def run(self,current_stop,*,max_cycles,poll_seconds,observe):
            assert poll_seconds==.5
            assert max_cycles>=max(1,(len(self.accounts)+3)//4)
            seen.extend(item.account_id for item in self.accounts)
            if any(item.account_id==33 for item in self.accounts):current_stop.set()
    assert entry.run_loop(Store(),"tenant-a",stop,refresh_seconds=1,load_cohort=load,worker_type=Worker,observe=lambda _:None)==0
    assert seen==list(range(34)) and cursors==[None,"account-32"]


def test_cursor_advances_across_empty_invalid_credential_cohort():
    class Stop:
        stopped=False
        def is_set(self):return self.stopped
        def wait(self,seconds):assert seconds==1
    stop=Stop();cursors=[]
    def load(*_,after_account=None,**__):
        cursors.append(after_account)
        if after_account is None:return [],"invalid-last",32
        stop.stopped=True
        return [],None,0
    assert entry.run_loop(object(),"tenant-a",stop,refresh_seconds=1,load_cohort=load,observe=lambda _:None)==0
    assert cursors==[None,"invalid-last"]


def test_stop_after_credential_refresh_does_not_start_a_new_worker_phase():
    stop=threading.Event()
    def load(*_,**__):stop.set();return [object()],None,1
    def worker(*_):pytest.fail("stop must be honored between refresh and a new worker phase")
    assert entry.run_loop(object(),"tenant-a",stop,load_cohort=load,worker_type=worker)==0


def test_explicit_vault_jobs_run_without_outgoing_accounts_and_stop_between_phases():
    stop=threading.Event();seen=[]
    def vault(store,tenant,*,limit,stop):
        assert tenant=="tenant-a" and limit==1
        seen.append("vault")
    def load(*_,**__):seen.append("accounts");return [],None,0
    assert entry.run_loop(object(),"tenant-a",stop,once=True,load_cohort=load,
        vault_jobs=vault,observe=lambda _:None)==0
    assert seen==["vault","accounts"]
    seen.clear()
    def stopped_vault(*_,stop,**__):seen.append("vault");stop.set()
    def unexpected_load(*_,**__):pytest.fail("stop must be honored before another credential phase")
    assert entry.run_loop(object(),"tenant-a",stop,load_cohort=unexpected_load,vault_jobs=stopped_vault)==0
    assert seen==["vault"]


def test_main_bounds_connection_startup_and_closes_store_and_restores_signals(monkeypatch):
    configured(monkeypatch)
    import onlyfans_runtime.store
    urls=[];closed=[]
    class Store:
        def __init__(self,url):urls.append(url)
        def close(self):closed.append(True)
    monkeypatch.setattr(onlyfans_runtime.store,"Store",Store)
    monkeypatch.setattr(entry,"run_loop",lambda store,tenant,stop,**kw:0)
    before={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    assert entry.main(["--once"])==0
    assert "connect_timeout=5" in urls[0] and closed==[True]
    assert {sig:signal.getsignal(sig) for sig in before}==before


def test_importing_worker_entry_never_imports_legacy_server_or_executor():
    source="import onlyfans_worker,sys; assert 'server' not in sys.modules; assert 'automations.executor' not in sys.modules"
    env=dict(os.environ)
    env["PYTHONPATH"]=str(Path(__file__).resolve().parents[1])
    result=subprocess.run([sys.executable,"-c",source],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
