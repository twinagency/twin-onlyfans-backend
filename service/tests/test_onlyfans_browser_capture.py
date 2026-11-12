"""Synthetic browser proof exercises the real existing signer and conversion."""
import copy
from datetime import datetime, timedelta, timezone
import pytest
from of_signer import sign
from onlyfans_runtime import browser_capture as browser
from onlyfans_runtime.store import Refused

OWNER = 123
RULES = dict(static_param="synthetic-static", start="synthetic", end="test", checksum_indexes=list(range(32)), checksum_constant=17)
CHUNK = " + ".join(f"W[{10000+i},W[x]](0)" for i in range(32))

def payload():
    now = datetime.now(timezone.utc)
    samples = []
    for i, path in enumerate(["/api2/v2/users/me", "/api2/v2/chats?limit=10"]):
        url = "https://onlyfans.com" + path
        samples.append(dict(url=url, **sign(url, OWNER, RULES, int(now.timestamp()*1000)+i)))
    return {"ownerId":OWNER,"expiresAt":(now+timedelta(hours=6)).isoformat(),"browser":{
        "schemaVersion":"of-browser.v1", "headers":{"user_id":str(OWNER),"user_agent":"Synthetic browser","x_bc":"synthetic-bc","x_of_rev":"synthetic-rev","x_hash":None},
        "staticParam":RULES["static_param"],"samples":samples,"cookies":[{"name":"auth_id","value":str(OWNER),"domain":".onlyfans.com","path":"/"},{"name":"sess","value":"synthetic-session","domain":".onlyfans.com","path":"/"}]}}

def assets(monkeypatch):
    calls = []
    def asset(kind, **kwargs):
        calls.append((kind, kwargs)); return CHUNK if kind == "rules" else "synthetic-hash"
    monkeypatch.setattr(browser,"fetch_asset",asset)
    return calls

def test_real_signer_conversion_and_fixed_provider_inputs(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");calls=assets(monkeypatch);p=payload()
    browser.validate_browser_payload(p)
    value=browser.normalize_browser_payload(p,proxy="http://synthetic.proxy:8080")
    assert value["capture"]["signing"]["rules"]==RULES
    assert value["xHash"]=="synthetic-hash" and "browser" not in value
    assert [c[0] for c in calls]==["rules","hash"]
    assert all(c[1]["owner"]==OWNER and c[1]["rev"]=="synthetic-rev" and c[1]["proxy"]=="http://synthetic.proxy:8080" for c in calls)
    p["browser"]["headers"]["x_hash"]="captured-hash";calls.clear()
    assert browser.normalize_browser_payload(p,proxy=None)["xHash"]=="captured-hash"
    assert len(calls)==1

def test_wrong_static_parameter_and_mixed_samples_fail_closed(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");assets(monkeypatch)
    p=payload();p["browser"]["staticParam"]="wrong"
    with pytest.raises(Refused):browser.normalize_browser_payload(p,proxy=None)
    p=payload();p["browser"]["samples"][1]["url"]="https://onlyfans.com/api2/v2/users/different"
    with pytest.raises(Refused):browser.normalize_browser_payload(p,proxy=None)

def test_browser_shape_owner_cookies_freshness_and_target_restrictions(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    changes=[lambda p:p.update(ownerId=True),lambda p:p.update(proxyUrl="http://bad:8080"),lambda p:p["browser"]["headers"].update(x_of_rev="../../private"),
        lambda p:p["browser"]["headers"].update(user_id="999"),lambda p:p["browser"]["cookies"][0].update(value="999"),
        lambda p:p["browser"]["cookies"].append(copy.deepcopy(p["browser"]["cookies"][0])),lambda p:p["browser"]["samples"][0].update(url="https://localhost/api2/v2/users/me"),
        lambda p:p["browser"]["samples"][0].update(time="1000000000000"),lambda p:p["browser"]["samples"].pop(),
        lambda p:p["browser"]["cookies"][1].update(value="bad\r\nsecret"),lambda p:p["browser"]["cookies"][1].update(path="/other")]
    for change in changes:
        p=payload();change(p)
        with pytest.raises(Refused):browser.validate_browser_payload(p)
    with pytest.raises(Refused):browser.normalize_browser_payload(payload(),proxy=None)

def test_asset_loader_has_no_redirect_retry_cookie_or_arbitrary_url(monkeypatch):
    import sys,types
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    calls=[]
    class Client:
        def __init__(self,**kwargs):assert kwargs==dict(impersonate="chrome",trust_env=False,retry=0)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            calls.append((url,kwargs));kwargs["content_callback"](b"synthetic")
            return types.SimpleNamespace(status_code=200,headers={"content-encoding":"identity"})
    monkeypatch.setitem(sys.modules,"curl_cffi",types.SimpleNamespace(requests=types.SimpleNamespace(Session=Client)))
    assert browser.fetch_asset("hash",owner=123,rev="rev",user_agent="agent",proxy=None)=="synthetic"
    url,kw=calls[0];assert url=="https://cdn2.onlyfans.com/hash/?u=123"
    assert kw["allow_redirects"] is False and kw["discard_cookies"] is True and "Cookie" not in kw["headers"]
    with pytest.raises(Refused):browser.fetch_asset("arbitrary",owner=123,rev="rev",user_agent="agent",proxy=None)
