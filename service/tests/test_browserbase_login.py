"""Browserbase login capture (service/browserbase_login.py + connection_api routes).

No network, no browser: Browserbase REST, Playwright and OnlyFans fetches are faked.
Synthetic ids/cookies only.
"""
import time
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import browserbase_login as bb
import session_bootstrap
from onlyfans_commands.connection_api import router,SCHEMA,HEADER

TENANT='tenant-synthetic'
ACCOUNT='of-123456'
OTHER='of-654321'
API_KEY='bb_synthetic_api_key_0123456789'
CONNECT='wss://connect.example/?signingKey=synthetic-signing-key'
LIVE='https://live.example/view/synthetic'
UID='123456'
SIGN='51234:'+'a'*40+':5f3:6789abcd'
COOKIES=[
    {'name':'auth_id','value':UID,'domain':'.onlyfans.com','path':'/'},
    {'name':'sess','value':'synthetic-sess-value','domain':'onlyfans.com','path':'/'},
    {'name':'other','value':'x','domain':'.example.com','path':'/'},
    {'name':'scoped','value':'y','domain':'.onlyfans.com','path':'/my/'},
]
UA='Mozilla/5.0 synthetic'
CAPTURE={'url':'https://onlyfans.com/api2/v2/users/me','headers':{'user-id':UID,'x-bc':'b'*40,'x-of-rev':'202611120000-abc',
    'sign':SIGN,'time':'1762900000000','user-agent':UA,'x-hash':'synthetic-xhash'},
    'rules':{'static_param':'synthetic-static','start':'51234','end':'6789abcd'}}


class Page:
    def __init__(self,world):self.world=world;self.urls=[]
    def goto(self,url,**_):
        self.urls.append(url)
        if self.world.get('nav_error'):raise RuntimeError('net::ERR '+CONNECT)
    def evaluate(self,js):
        if '__relayCapture' in js:return CAPTURE if self.world.get('logged_in') else None
        if 'userAgent' in js:return UA
    def wait_for_timeout(self,ms):
        self.world['polls']=self.world.get('polls',0)+1
        if self.world['polls']>=self.world.get('login_after',3):self.world['logged_in']=True
        time.sleep(0.005)


class Context:
    def __init__(self,world):self.world=world;self.scripts=[];self.pages=[Page(world)]
    def add_init_script(self,s):self.scripts.append(s)
    def cookies(self,urls=None):
        if self.world.get('hang'):self.world['hang'].wait(10)
        return [dict(c) for c in COOKIES] if self.world.get('logged_in') else []


class Browser:
    def __init__(self,world):self.world=world;self.contexts=[Context(world)]
    def close(self):self.world['closed']=True


@pytest.fixture
def world(monkeypatch):
    w={'calls':[]}
    monkeypatch.setenv('BROWSERBASE_API_KEY',API_KEY);monkeypatch.setenv('BROWSERBASE_PROJECT_ID','proj-synthetic')
    def request(path,method='GET',body=None):
        w['calls'].append((method,path,body))
        if path=='/sessions' and method=='POST':
            w['n']=w.get('n',0)+1
            return w.get('create',{'id':'sess-%d'%w['n'],'connectUrl':CONNECT})
        if path.endswith('/debug'):return {'debuggerFullscreenUrl':LIVE}
        return {}
    monkeypatch.setattr(bb,'_bb_request',request)
    class Chromium:
        def connect_over_cdp(self,url):
            assert url==CONNECT
            if w.get('cdp_error'):raise RuntimeError('cdp refused '+CONNECT+' key '+API_KEY+'\n\tline two \u2014 caf\u00e9'+' x'*400)
            w['browser']=Browser(w);return w['browser']
    class PW:chromium=Chromium()
    @contextmanager
    def playwright():yield PW()
    monkeypatch.setattr(bb,'_sync_playwright',playwright)
    monkeypatch.setattr(bb,'POLL_S',0.001)
    # OnlyFans fetches inside build_connection_capture: chunk + pure derivation stubbed.
    from onlyfans_runtime import browser_capture
    from onlyfans_commands import connection_api
    monkeypatch.setattr(browser_capture,'fetch_asset',lambda kind,**k:'chunk')
    monkeypatch.setattr(connection_api,'get_capture_proxy',lambda *a:None)
    monkeypatch.setattr(session_bootstrap,'derive_rules_from_chunk',lambda code,samples:{'checksum_indexes':[1,2],'checksum_constant':7})
    monkeypatch.setattr(session_bootstrap,'verify_rules',lambda rules,samples:(True,None))
    with bb._jobs_lock:bb._jobs.clear()
    yield w
    if w.get('hang'):w['hang'].set()
    for job in list(bb._jobs.values()):job._cancel.set()
    with bb._jobs_lock:bb._jobs.clear()


@pytest.fixture
def client(monkeypatch):
    for k,v in {'ONLYFANS_PROCESS_ROLE':'api','ONLYFANS_CONNECTION_TOKEN':'synthetic-connect','ONLYFANS_CONNECTION_TENANT':TENANT,
        'ONLYFANS_CONNECTION_KEY':'42'*32}.items():monkeypatch.setenv(k,v)
    app=FastAPI();app.include_router(router);app.state.control_store=object()
    return TestClient(app)


H={'Authorization':'Bearer synthetic-connect',HEADER:SCHEMA}
def base(account=ACCOUNT):return '/onlyfans/connection/v1/accounts/'+account+'/browserbase-login'


def wait_state(client,job,states,account=ACCOUNT,timeout=5):
    end=time.time()+timeout
    while time.time()<end:
        r=client.get(base(account)+'/'+job,headers=H).json()
        if r['state'] in states:return r
        time.sleep(0.01)
    raise AssertionError(r)


def test_unconfigured_is_503(client,monkeypatch):
    monkeypatch.delenv('BROWSERBASE_API_KEY',raising=False);monkeypatch.delenv('BROWSERBASE_PROJECT_ID',raising=False)
    r=client.post(base(),headers=H,json={})
    assert r.status_code==503 and r.json()['code']=='browserbase_unconfigured'
    assert r.json()['error']=='BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID are not set.'
    assert r.headers[HEADER]==SCHEMA and bb._jobs=={}


def test_auth_and_body_gates(client,world):
    assert client.post(base(),headers={HEADER:SCHEMA,'Authorization':'Bearer wrong'},json={}).status_code==401
    assert client.post(base(),headers=H,json={'email':'x'}).status_code==400
    assert bb._jobs=={}


def test_start_status_capture_matches_paste_curl(client,world):
    r=client.post(base(),headers=H,json={});assert r.status_code==200,r.text
    body=r.json();job=body['jobId']
    assert body['ok'] is True and body['state'] in bb.STATES and 'no-store' in r.headers['cache-control']
    # one active job per (tenant, account)
    again=client.post(base(),headers=H,json={}).json()
    assert again['jobId']==job
    done=wait_state(client,job,{'done','failed'})
    assert done['state']=='done',done
    # same output as POST capture-from-curl for the equivalent pasted curl
    curl=("curl 'https://onlyfans.com/api2/v2/users/me' -H 'user-id: "+UID+"' -H 'x-bc: "+'b'*40+"' -H 'x-of-rev: 202611120000-abc'"
          " -H 'sign: "+SIGN+"' -H 'time: 1762900000000' -H 'user-agent: "+UA+"' -H 'x-hash: synthetic-xhash'"
          " -H 'x-relay-static-param: synthetic-static' -H 'x-relay-sign-start: 51234' -H 'x-relay-sign-end: 6789abcd'"
          " -b 'auth_id="+UID+"; sess=synthetic-sess-value'")
    expected=session_bootstrap.build_connection_capture(curl,account=ACCOUNT)
    assert {k:done[k] for k in ('capture','xHash','ownerId')}==expected
    assert expected['ownerId']==int(UID) and expected['capture']['signing']['rules']['static_param']=='synthetic-static'
    assert [c['name'] for c in done['capture']['cookies']]==['auth_id','sess']  # only onlyfans.com, path /
    assert done['liveViewUrl'] is None  # controls the remote browser: only while live
    # Browserbase: managed proxies, captcha solver off, session released
    create=[c for c in world['calls'] if c[1]=='/sessions'][0][2]
    assert create['proxies'] is True and create['browserSettings']['solveCaptchas'] is False and create['projectId']=='proj-synthetic'
    assert ('POST','/sessions/sess-1',{'projectId':'proj-synthetic','status':'REQUEST_RELEASE'}) in world['calls']
    scripts=world['browser'].contexts[0].scripts
    assert any('__chatterly_capture' in s for s in scripts) and any('webpackChunkof_vue' in s for s in scripts)
    # a finished job does not block a new one
    assert client.post(base(),headers=H,json={}).json()['jobId']!=job


def test_waiting_login_shows_live_view(client,world):
    world['login_after']=10**9
    job=client.post(base(),headers=H,json={}).json()['jobId']
    r=wait_state(client,job,{'waiting_login'})
    assert r['liveViewUrl']==LIVE
    assert client.post(base()+'/'+job+'/cancel',headers=H).json()=={'schemaVersion':SCHEMA,'ok':True,'state':'cancelled'}
    wait=time.time()+5
    while not world.get('closed') and time.time()<wait:time.sleep(0.01)
    time.sleep(0.05)
    assert any(c[2] and c[2].get('status')=='REQUEST_RELEASE' for c in world['calls'])
    r=client.get(base()+'/'+job,headers=H).json()
    assert r['state']=='cancelled' and r['liveViewUrl'] is None and 'capture' not in r


def test_cross_account_and_unknown_job_404(client,world):
    world['login_after']=10**9
    job=client.post(base(),headers=H,json={}).json()['jobId']
    for path in (base(OTHER)+'/'+job,base()+'/'+'0'*32,base()+'/not-a-job'):
        assert client.get(path,headers=H).status_code==404
        assert client.post(path+'/cancel',headers=H).status_code==404
    bb.get_job(job).tenant='other-tenant'
    assert client.get(base()+'/'+job,headers=H).status_code==404
    bb.get_job(job)._cancel.set()


def test_expired_window(client,world,monkeypatch):
    world['login_after']=10**9
    monkeypatch.setattr(bb,'JOB_WINDOW_S',0.2);monkeypatch.setattr(bb,'CAPTURE_RESERVE_S',0)
    job=client.post(base(),headers=H,json={}).json()['jobId']
    r=wait_state(client,job,{'expired','failed'})
    assert r['state']=='expired' and r['error']


def test_errors_scrubbed(client,world,caplog):
    world['cdp_error']=True
    job=client.post(base(),headers=H,json={}).json()['jobId']
    r=wait_state(client,job,{'failed'})
    assert r['error'] and API_KEY not in r['error'] and CONNECT not in r['error'] and 'synthetic-signing-key' not in r['error']
    assert len(r['error'])<=300 and all(' '<=ch<='~' for ch in r['error']) and '  ' not in r['error'] and 'line two' in r['error']
    assert API_KEY not in caplog.text and 'synthetic-signing-key' not in caplog.text
    assert any(c[2] and c[2].get('status')=='REQUEST_RELEASE' for c in world['calls'])


def test_scrub():
    assert bb.scrub('a secret b secretsecret',None,'','secret','secretsecret')=='a *** b ***'


def released(world,sid):
    return ('POST','/sessions/'+sid,{'projectId':'proj-synthetic','status':'REQUEST_RELEASE'}) in world['calls']


def test_hung_playwright_job_is_reaped_and_released(client,world,monkeypatch):
    import threading
    world['hang']=threading.Event()
    monkeypatch.setattr(bb,'JOB_WINDOW_S',0.2);monkeypatch.setattr(bb,'CAPTURE_RESERVE_S',0)
    monkeypatch.setattr(bb,'EXPIRY_GRACE_S',0.1);monkeypatch.setattr(bb,'MAX_ACTIVE_JOBS',1)
    job=client.post(base(),headers=H,json={}).json()['jobId']
    assert bb.get_job(job).session_id=='sess-1'
    time.sleep(0.4)  # worker is stuck inside context.cookies()
    r=client.get(base()+'/'+job,headers=H).json()
    assert r['state']=='expired' and r['liveViewUrl'] is None
    end=time.time()+2
    while not released(world,'sess-1') and time.time()<end:time.sleep(0.01)
    assert released(world,'sess-1')
    # the slot is free: a new job for another account is not 429, and this account gets a fresh job
    other=client.post(base(OTHER),headers=H,json={});assert other.status_code==200,other.text
    bb.get_job(other.json()['jobId'])._cancel.set();bb.cancel(bb.get_job(other.json()['jobId']))
    assert client.post(base(),headers=H,json={}).json()['jobId']!=job
    world['hang'].set();time.sleep(0.05)
    assert len([c for c in world['calls'] if c[1]=='/sessions/sess-1' and c[0]=='POST'])==1  # released once


def test_thread_start_failure_fails_job(client,world,monkeypatch):
    def boom(job):raise RuntimeError("can't start new thread")
    monkeypatch.setattr(bb,'_start_thread',boom)
    r=client.post(base(),headers=H,json={}).json()
    assert r['state']=='failed' and r['error']
    monkeypatch.undo()
    assert bb._jobs[r['jobId']].is_terminal()


def test_create_without_connect_url_is_released(client,world):
    world['create']={'id':'sess-orphan'}
    job=client.post(base(),headers=H,json={}).json()['jobId']
    r=wait_state(client,job,{'failed'})
    assert 'connectUrl' in r['error']
    end=time.time()+2
    while not released(world,'sess-orphan') and time.time()<end:time.sleep(0.01)
    assert released(world,'sess-orphan')


def test_empty_cookie_values_skipped():
    rows=bb._of_cookies([{'name':'auth_id','value':UID,'domain':'.onlyfans.com','path':'/'},
        {'name':'empty','value':'','domain':'.onlyfans.com','path':'/'},{'name':'none','value':None,'domain':'onlyfans.com','path':'/'}])
    assert [c['name'] for c in rows]==['auth_id']
