"""Real private HTTP handler authentication; inventory/provider I/O is synthetic."""
import io,json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"service"))
from types import SimpleNamespace
import pytest
from managed_proxy import api

@pytest.fixture
def handler(monkeypatch):
    calls=[];captured={}
    class Broker:
        def check(self):pass
        def sync(self,rows):pass
        def stock(self):return []
        def reservations(self,tenant):return [{"creatorId":"creator-a","reservationId":"a"*64}]
        def reclaim(self,tenant,**kw):calls.append((tenant,kw));return True
        def begin_use(self,*args):calls.append(args);return True
    class StopLoop(Exception):pass
    class Thread:
        def __init__(self,target,daemon):self.target=target
        def start(self):
            try:self.target()
            except StopLoop:pass
    class Server:
        def __init__(self,address,klass):captured["class"]=klass
        def serve_forever(self):pass
    def sleep(seconds):raise StopLoop()
    for key,value in {"ONLYFANS_MANAGED_PROXY_TOKEN":"a"*40,"ONLYFANS_MANAGED_PROXY_MANAGEMENT_TOKEN":"b"*40,
        "ONLYFANS_MANAGED_PROXY_TENANT":"tenant-a","ONLYFANS_MANAGED_PROXY_DATABASE_URL":"synthetic",
        "ONLYFANS_CONNECTION_KEY":"42"*32,"IPROYAL_API_TOKEN":"synthetic"}.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(api,"Store",lambda *args:Broker())
    monkeypatch.setattr(api,"IPRoyal",lambda *args:SimpleNamespace(inventory=lambda:[]))
    monkeypatch.setattr(api,"ThreadingHTTPServer",Server)
    monkeypatch.setattr(api,"threading",SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(api,"time",SimpleNamespace(monotonic=lambda:100,sleep=sleep))
    monkeypatch.setattr(api.argparse.ArgumentParser,"parse_args",lambda self:SimpleNamespace(init_schema=False))
    api.main()
    def request(path,token,body=None):
        obj=object.__new__(captured["class"])
        raw=json.dumps(body or {}).encode()
        obj.path=path;obj.headers={"Authorization":"Bearer "+token,"Content-Type":"application/json","Content-Length":str(len(raw))}
        obj.rfile=io.BytesIO(raw);answer=[]
        obj.reply=lambda code,data:answer.append((code,data))
        (obj.do_GET if body is None else obj.do_POST)()
        return answer[0]
    return request,calls

def test_reclaim_requires_separate_management_credential(handler):
    request,calls=handler
    body={"creatorId":"creator-a","addressHash":"a"*64,"reservationId":"b"*64,
        "commandKey":"c"*64,"actor":"op-session:"+"d"*64,"reason":"Verified unused reservation"}
    assert request("/v1/reclaim","a"*40,body)[0]==401
    assert calls==[]
    assert request("/v1/reclaim","b"*40,{**body,"proxyUrl":"forged"})[0]==400
    assert calls==[]
    assert request("/v1/reclaim","b"*40,body)==(200,{"ok":True,"reclaimed":True})
    assert calls[0][0]=="tenant-a"
    assert calls[0][1]["reason"]==body["reason"]
    assert request("/v1/begin-use","b"*40,{"creatorId":"creator-a","proxyUrl":"synthetic","actor":body["actor"]})[0]==401

def test_reservation_management_does_not_expose_proxy_credentials(handler):
    request,_=handler
    assert request("/v1/reservations","a"*40)[0]==401
    status,body=request("/v1/reservations","b"*40)
    assert status==200 and body["ok"]
    assert "proxyUrl" not in json.dumps(body)
