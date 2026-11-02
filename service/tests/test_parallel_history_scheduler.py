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
