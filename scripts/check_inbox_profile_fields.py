import sys,json,types
sys.path.insert(0,"/app/service")
from onlyfans_runtime.native_connector import NativeLoopbackConnector
from onlyfans_runtime.native_protocol import NativeProtocolError
from urllib.parse import urlsplit,parse_qs
c=object.__new__(NativeLoopbackConnector);c._handle=types.SimpleNamespace(owner_id=1)
c.verify_identity=lambda:{"id":1}
calls=[]
def get(method,path,body=b""):
    calls.append(path)
    return json.dumps({"list":[{"withUser":{"id":2,"name":"Fan test","username":"fan_test"},"lastMessage":{"id":7}}],"hasMore":False}).encode()
c._call=get
page=c.chats(cursor=None,limit=10)
assert page.fan_ids==[2] and page.heads=={2:7}
assert page.profiles[2]["name"]=="Fan test"
assert "skip_users" not in parse_qs(urlsplit(calls[-1]).query)
c.chats(cursor="10",limit=10)
assert parse_qs(urlsplit(calls[-1]).query)["offset"]==["10"]
for cursor in ["0","-1","x"]:
    try:c.chats(cursor=cursor,limit=10)
    except Exception:pass
    else:raise AssertionError("invalid cursor admitted")
c._call=lambda *args:json.dumps({"list":[{"withUser":{"id":1}}],"hasMore":False}).encode()
try:c.chats(cursor=None,limit=10)
except NativeProtocolError:pass
else:raise AssertionError("owner accepted as fan")
print("profile_read_regressions_passed")
