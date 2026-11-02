import threading
import time
from types import SimpleNamespace

from onlyfans_runtime import parallel_history
from onlyfans_runtime.launch_history_memory import recovery


def test_multiple_fans_overlap_with_bounded_workers_and_single_discovery(monkeypatch):
    stop=threading.Event()
    lock=threading.Lock()
    stats={'active':0,'peak':0,'done':0,'discovery':0,'peakDiscovery':0}
    class History:
        def __init__(self,store):pass
        def request_account(self,*args,**kwargs):pass
        def step(self,*args,archive_only,**kwargs):
            with lock:
                stats['active']+=1;stats['peak']=max(stats['peak'],stats['active'])
                if not archive_only:
                    stats['discovery']+=1;stats['peakDiscovery']=max(stats['peakDiscovery'],stats['discovery'])
            time.sleep(.08)
            with lock:
                stats['active']-=1;stats['done']+=1
                if not archive_only:stats['discovery']-=1
                if stats['done']>=12:stop.set()
            return 1
    monkeypatch.setattr(recovery,'HistoryRecovery',History)
    monkeypatch.setenv('ONLYFANS_IMPORT_CAPACITY','{"of-a":{"rps":64,"inflight":16}}')
    bound=SimpleNamespace(read_connection_revision=1,reader=SimpleNamespace(connection_revision=1))
    timer=threading.Timer(5,stop.set);timer.start()
    try:parallel_history.run(None,'tenant',{'of-a':bound},stop,workers=4)
    finally:timer.cancel()
    assert stats['done']>=12 and 1<stats['peak']<=4 and stats['peakDiscovery']==1


def test_fractional_refill_never_mints_extra_tokens():
    from datetime import datetime,timedelta
    from onlyfans_runtime.import_capacity import refill,Capacity
    start=datetime(2026,1,1);at=start;tokens=0;issued=0
    for tick in range(1,10001):
        tokens,at=refill(tokens,at,start+timedelta(milliseconds=tick),Capacity(64,128))
        if tokens>=2:issued+=1;tokens-=2
    assert 319<=issued<=320


def _coordinator(monkeypatch, *, refresh, initial=None, seed_error=None, until=4):
    """Deterministic coordinator clock, completed tasks, no DB or provider calls."""
    import concurrent.futures
    state={'now':0.,'steps':[],'seeds':[]}
    class Stop:
        halted=False
        def is_set(self):return self.halted or state['now']>=until
        def wait(self,seconds):state['now']+=.25
    class Pool:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def submit(self,fn,*args,**kwargs):
            task=concurrent.futures.Future()
            try:task.set_result(fn(*args,**kwargs))
            except Exception as exc:task.set_exception(exc)
            return task
    class History:
        def __init__(self,store):pass
        def request_account(self,tenant,account,**kwargs):
            state['seeds'].append((state['now'],account))
            if seed_error:seed_error(account)
        def step(self,tenant,account,reader,**kwargs):
            state['steps'].append((state['now'],account,reader));return 1
    monkeypatch.setattr(parallel_history.time,'monotonic',lambda:state['now'])
    monkeypatch.setattr(parallel_history.futures,'ThreadPoolExecutor',Pool)
    monkeypatch.setattr(recovery,'HistoryRecovery',History)
    monkeypatch.setenv('ONLYFANS_IMPORT_CAPACITY','{"of-a":{"rps":2,"inflight":8},"of-b":{"rps":2,"inflight":8}}')
    parallel_history.run(None,'tenant',initial or {},Stop(),workers=2,
        refresh=lambda:refresh(state))
    return state


def _db_timeout(code='55P03'):
    from sqlalchemy.exc import OperationalError
    cause=RuntimeError('private database detail must not be logged');cause.pgcode=code
    return OperationalError('private SQL',{},cause)


def _bound(label):
    return SimpleNamespace(read_connection_revision=1,reader=label)


def test_startup_refresh_retries_lock_timeout_with_bounded_backoff(monkeypatch,capsys):
    calls=[]
    def refresh(state):
        calls.append(state['now'])
        if len(calls)<3:raise _db_timeout()
        return {'of-a':_bound('new')}
    state=_coordinator(monkeypatch,refresh=refresh,until=4)
    assert calls==[0,1,3]
    assert state['steps'] and all(t>=3 for t,_,_ in state['steps'])
    output=capsys.readouterr().out
    assert 'cohort_refresh_deferred' in output and 'private' not in output


def test_failed_refresh_suspends_stale_cohort_until_success(monkeypatch):
    calls=[]
    def refresh(state):
        calls.append(state['now'])
        if len(calls)==1:raise _db_timeout('57014')
        return {'of-a':_bound('renewed')}
    state=_coordinator(monkeypatch,refresh=refresh,initial={'of-a':_bound('old')},until=63)
    assert calls==[60,61]
    assert not any(t>=60 and reader=='old' for t,_,reader in state['steps'])
    assert any(t>=61 and reader=='renewed' for t,_,reader in state['steps'])


def test_discovery_timeout_defers_only_affected_account(monkeypatch):
    tries=[]
    def seed(account):
        if account=='of-a':
            tries.append(account)
            if len(tries)==1:raise _db_timeout()
    state=_coordinator(monkeypatch,initial={'of-a':_bound('a'),'of-b':_bound('b')},
        refresh=lambda _: {},seed_error=seed,until=2)
    assert any(account=='of-b' and t<1 for t,account,_ in state['steps'])
    assert any(account=='of-a' and t>=1 for t,account,_ in state['steps'])
    assert not any(account=='of-a' and t<1 for t,account,_ in state['steps'])


def test_unknown_refresh_failure_is_not_hidden(monkeypatch):
    import pytest
    def refresh(state):raise ValueError('invalid configuration')
    with pytest.raises(ValueError,match='invalid configuration'):
        _coordinator(monkeypatch,refresh=refresh)


def test_shutdown_during_refresh_backoff_does_not_issue_reads(monkeypatch):
    calls=[]
    def refresh(state):calls.append(state['now']);raise _db_timeout()
    state=_coordinator(monkeypatch,refresh=refresh,until=.5)
    assert calls==[0] and state['steps']==[]
