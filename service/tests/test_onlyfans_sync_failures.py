"""Offline wire diagnostics and scoped profile protocol, no external I/O."""
import json
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_onlyfans_provider_adapters import credential, fake_curl
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeReadTransientError
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime import read_failures


@pytest.mark.parametrize('read_seconds,write_seconds', [(10,5),(.25,.75)])
def test_read_deadline_is_independent_and_does_not_retry_writes(monkeypatch,read_seconds,write_seconds):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    records=fake_curl(monkeypatch)
    options={} if (read_seconds,write_seconds)==(10,5) else {'read_timeout_seconds':read_seconds,'timeout_seconds':write_seconds}
    wire=NativeProviderConnector(credential(),**options)
    wire.verify_identity()
    wire._signed_request('POST','/api2/v2/chats/12/messages',b'{}')
    calls=[r for r in records if 'url' in r]
    assert len(calls)==2
    assert calls[0]['options']['timeout']==read_seconds
    assert calls[1]['options']['timeout']==write_seconds
    assert all(r['constructor']['retry']==0 for r in records if 'constructor' in r)


@pytest.mark.parametrize('value',[True,0,11,float('inf'),float('nan')])
def test_read_deadline_remains_bounded(value):
    from onlyfans_runtime.store import Refused
    with pytest.raises(Refused):NativeProviderConnector(credential(),read_timeout_seconds=value)


@pytest.mark.parametrize('code,expected', [(5,'provider_proxy_failed'), (6,'provider_dns_failed'),
    (7,'provider_transport_failed'), (28,'provider_timeout'), (35,'provider_tls_failed'),
    (60,'provider_tls_failed'), (97,'provider_proxy_failed'), (12345,'provider_transport_failed')])
def test_transport_error_has_only_safe_diagnostics(monkeypatch, caplog, code, expected):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    read_failures._LAST.clear()
    failure=RuntimeError('SECRET http://user:password@proxy.example private fan message')
    failure.code=code
    records=fake_curl(monkeypatch,error=failure)
    with caplog.at_level(logging.WARNING), pytest.raises(NativeReadTransientError) as caught:
        NativeProviderConnector(credential()).verify_identity()
    assert read_failures.failure_code(caught.value)==expected
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None
    assert all(secret not in caplog.text + str(caught.value) for secret in ('SECRET','password','proxy.example','fan message'))
    assert '"operation": "identity"' in caplog.text
    assert len([r for r in records if 'url' in r])==1


@pytest.mark.parametrize('status,expected', [(401,'provider_auth_required'), (403,'provider_auth_required'),
    (429,'provider_rate_limited'), (408,'provider_http_transient'), (500,'provider_http_transient'),
    (503,'provider_http_transient'), (302,'provider_protocol_failed')])
def test_http_status_is_retained_without_private_body(monkeypatch, status, expected):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    fake_curl(monkeypatch,status=status,body=b'PRIVATE BODY')
    with pytest.raises(NativeProtocolError) as caught:
        NativeProviderConnector(credential()).verify_identity()
    assert read_failures.failure_code(caught.value)==expected
    assert caught.value.read_failure['httpStatus']==status
    assert 'PRIVATE' not in str(caught.value)


def test_diagnostic_rate_limit_is_per_account_and_operation(monkeypatch,caplog):
    read_failures._LAST.clear()
    monkeypatch.setattr(read_failures.time,'monotonic',lambda:100)
    with caplog.at_level(logging.WARNING):
        for account,path in [('a','/api2/v2/chats'),('a','/api2/v2/chats'),
                             ('b','/api2/v2/chats'),('a','/api2/v2/chats/123/messages?id=SECRET')]:
            read_failures.observe(RuntimeError('SECRET'),account=account,path=path,started=99)
    assert len(caplog.records)==3
    assert '123' not in caplog.text and 'SECRET' not in caplog.text


def test_profile_batch_uses_requested_ids_and_fixed_proxy(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    records=fake_curl(monkeypatch,body=b'{"12":{"id":12,"name":"Synthetic Fan"}}')
    wire=NativeProviderConnector(credential(proxy_url='http://fixed.example:8080'))
    checked=[]
    monkeypatch.setattr(wire,'verify_identity',lambda:checked.append(True))
    assert wire.profiles([12,13])=={12:{'id':12,'name':'Synthetic Fan'}}
    requests=[r for r in records if 'url' in r]
    assert len(requests)==1 and checked==[True]
    assert parse_qs(urlsplit(requests[0]['url']).query)=={'m[]':['12','13']}
    assert requests[0]['options']['proxy']=='http://fixed.example:8080'


@pytest.mark.parametrize('body',[b'{"99":{"id":99,"name":"Foreign"}}',
    b'{"12":{"id":13}}',b'{"12":{"id":true}}',b'{"12":null}'])
def test_profile_batch_refuses_mismatched_provider_identity(monkeypatch,body):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    fake_curl(monkeypatch,body=body)
    wire=NativeProviderConnector(credential())
    monkeypatch.setattr(wire,'verify_identity',lambda:None)
    with pytest.raises(NativeProtocolError):wire.profiles([12])


@pytest.mark.parametrize('ids',[[],[12]*2,[True],[7001],list(range(1,22))])
def test_profile_scope_refused_before_http(monkeypatch,ids):
    from onlyfans_runtime.store import Refused
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    records=fake_curl(monkeypatch)
    with pytest.raises(Refused):NativeProviderConnector(credential()).profiles(ids)
    assert not records


def test_wrapped_response_limit_is_not_reported_as_proxy_failure(monkeypatch):
    import sys
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    class Session:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def request(self,*args,**kw):
            try:kw['content_callback'](b'x'*65537)
            except NativeProtocolError:
                error=RuntimeError('SECRET proxy URL');error.code=23;raise error
    monkeypatch.setitem(sys.modules,'curl_cffi',SimpleNamespace(requests=SimpleNamespace(Session=Session)))
    with pytest.raises(NativeProtocolError) as caught:NativeProviderConnector(credential()).verify_identity()
    assert read_failures.failure_code(caught.value)=='provider_protocol_failed'
    assert caught.value.read_failure['category']=='response_limit'
    assert not isinstance(caught.value,NativeReadTransientError)


def test_history_and_profiles_rotate_across_worker_reconstruction(monkeypatch):
    from onlyfans_runtime.native_worker import NativeWorker, CycleReport
    from onlyfans_runtime.launch_history_memory import recovery
    from onlyfans_runtime import profile_sync
    from onlyfans_runtime.store import Refused
    calls=[]
    class Recovery:
        def __init__(self,store):pass
        def request_account(self,*args,**kw):pass
        def step(self,tenant,account,reader,**kw):calls.append((account,'archive'))
    monkeypatch.setattr(recovery,'HistoryRecovery',Recovery)
    def profiles(store,tenant,account,reader,**kw):calls.append((account,'profiles'))
    monkeypatch.setattr(profile_sync,'step',profiles)
    store=SimpleNamespace()
    for _ in range(2):
        worker=object.__new__(NativeWorker)
        worker.store=store;worker.tenant_id='tenant';worker.history_recovery=True;worker.history_memory=False
        for account in ('a','b','c','d','e'):
            worker._history_turn(SimpleNamespace(account_id=account,reader=object()),CycleReport(),revision=1)
    assert calls[:2]==[('a','archive'),('a','profiles')]
    assert calls[10:12]==[('a','profiles'),('a','archive')]
    assert {account for account,lane in calls}=={'a','b','c','d','e'}
    # A broken archive read is visible as an error and cannot skip profiles.
    calls.clear()
    monkeypatch.setattr(Recovery,'step',lambda *a,**k:(_ for _ in ()).throw(NativeProtocolError('synthetic')))
    report=CycleReport()
    worker._history_turn(SimpleNamespace(account_id='a',reader=object()),report,revision=1)
    assert report.errors==1 and ('a','profiles') in calls


def test_recent_protocol_failure_does_not_report_success_or_run_planning(monkeypatch):
    import threading
    from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
    from onlyfans_runtime import worker as reads, delivery_resolution_jobs
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    monkeypatch.setattr(delivery_resolution_jobs,'poll',lambda *a:None)
    monkeypatch.setattr(reads,'read_step',lambda *a,**k:(_ for _ in ()).throw(NativeProtocolError('synthetic')))
    worker=object.__new__(NativeWorker)
    worker.store=SimpleNamespace();worker.tenant_id='tenant';worker._cursor=0
    worker.history_recovery=True
    worker.accounts_per_cycle=4;worker._lock=threading.Lock()
    worker.accounts=tuple(WorkerAccount(a,None,None,None,1) for a in ('a','b'))
    calls=[]
    worker._history_turn=lambda account,report,**kw:calls.append(account.account_id)
    worker._heartbeat=lambda *a,**k:None
    worker._deliver=lambda *a:pytest.fail('read failure cannot start sending')
    report=worker.step()
    assert report.errors==2 and calls==['a','b']
