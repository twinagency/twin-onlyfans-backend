"""Synthetic regressions for slow worker turns and read-only transport retries."""
import threading
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from onlyfans_runtime.native_worker import NativeWorker
# Keep the ORM telemetry owner loaded outside temporary sys.modules fixtures.
from onlyfans_runtime import history_telemetry
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeReadTransientError, NativeAuthenticationError
from onlyfans_runtime.native_protocol import NativeProtocolError
from test_onlyfans_vault_response_limits import session

def run_clock(monkeypatch, *, minimum=1, seconds=15, slow=8):
    import onlyfans_runtime.native_worker as module
    clock=[0.0];seen=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    stop=threading.Event()
    stop.wait=lambda seconds:clock.__setitem__(0,clock[0]+seconds)
    worker=NativeWorker.__new__(NativeWorker)
    def step(_stop):
        clock[0]+=slow;seen.append(clock[0])
        return SimpleNamespace(errors=0,public=lambda:{'synthetic':True})
    worker.step=step
    count=worker.run(stop,max_cycles=30,max_seconds=seconds,minimum_cycles=minimum)
    return count,seen

def test_slow_requests_yield_after_elapsed_refresh_window(monkeypatch):
    count,seen=run_clock(monkeypatch)
    assert count==2 and seen[-1]<30,'Slow I/O must not turn 15 seconds into thirty full network cycles'

def test_yield_still_covers_all_32_accounts(monkeypatch):
    count,_=run_clock(monkeypatch,minimum=8)
    assert count==8,'A time limit must not skip accounts near the end of a cohort'

def test_unspecified_deadline_keeps_existing_cycle_limit(monkeypatch):
    count,_=run_clock(monkeypatch,seconds=None)
    assert count==30

@pytest.mark.parametrize('method,status,transient',[('GET',503,True),('GET',408,True),('POST',503,False),('GET',404,False),('GET',403,False)])
def test_read_retry_never_retries_sends_or_auth(monkeypatch,method,status,transient):
    calls=[]
    class FakeSession:
        def __init__(self,**_):pass
        def __enter__(self):return self
        def __exit__(self,*_):return False
        def request(self,*args,**kwargs):
            calls.append(args)
            kwargs['content_callback'](b'{}')
            return SimpleNamespace(status_code=status,headers={'content-type':'application/json'})
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    with patch.dict('sys.modules',{'curl_cffi':SimpleNamespace(requests=SimpleNamespace(Session=FakeSession))}):
        wire=NativeProviderConnector(session())
        with pytest.raises(NativeProtocolError) as caught:
            wire._signed_request(method,'/api2/v2/chats/99/messages',b'{}' if method=='POST' else b'')
        assert isinstance(caught.value,NativeReadTransientError)==transient
        if status==403:assert isinstance(caught.value,NativeAuthenticationError)
    assert len(calls)==1,'The wire still makes exactly one attempt'

@pytest.mark.parametrize('method',['GET','POST'])
def test_network_failure_redaction_and_read_only_retry(monkeypatch,method):
    class FakeSession:
        def __init__(self,**_):pass
        def __enter__(self):return self
        def __exit__(self,*_):return False
        def request(self,*args,**kwargs):raise TimeoutError('synthetic-private-proxy-credential')
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    with patch.dict('sys.modules',{'curl_cffi':SimpleNamespace(requests=SimpleNamespace(Session=FakeSession))}):
        with pytest.raises(NativeProtocolError) as caught:
            NativeProviderConnector(session())._signed_request(method,'/api2/v2/chats/99/messages',b'{}' if method=='POST' else b'')
        assert isinstance(caught.value,NativeReadTransientError)==(method=='GET')
        assert 'synthetic-private' not in str(caught.value)
