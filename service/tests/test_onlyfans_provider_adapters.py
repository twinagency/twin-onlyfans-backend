"""Explicit credentials and bounded wire behavior, with no real network access."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_client import LLMConfigError
from llm_models import MODELS
from onlyfans_runtime.provider_connector import NativeProviderConnector, NativeProviderSession, session_from_capture
from onlyfans_runtime.provider_model import NativeProviderModel, _decode
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.store import Refused

OWNER = 7001
MODEL = "grok-4-1-fast-non-reasoning"


def expires():
    return datetime.now(timezone.utc) + timedelta(hours=1)


def capture():
    return {"headers": {"user_id":str(OWNER), "x_bc":"synthetic-bc", "x_of_rev":"synthetic-rev", "user_agent":"Synthetic Test"},
        "signing":{"rules":{"static_param":"synthetic-static", "start":"fixture", "end":"offline",
            "checksum_indexes":list(range(32)), "checksum_constant":17}},
        "cookies":[{"name":"sess", "value":"synthetic-session", "domain":".onlyfans.com", "path":"/"}]}


def credential(**overrides):
    values=dict(account_id="acc-a", owner_id=OWNER, policy_revision=2, capture=capture(),
                x_hash="synthetic-hash", proxy_url=None, expires_at=expires())
    values.update(overrides)
    return session_from_capture(**values)


def model(**overrides):
    values=dict(account_id="acc-a", model_id=MODEL, api_key="synthetic-key", expires_at=expires())
    values.update(overrides)
    return NativeProviderModel(**values)


@pytest.mark.parametrize("change", ["owner", "foreign_cookie", "cookie_path", "duplicate_cookie", "header_injection", "invalid_rules"])
def test_session_conversion_refuses_ambiguous_or_foreign_credentials(change):
    data=capture()
    if change=="owner":data["headers"]["user_id"]="9999"
    if change=="foreign_cookie":data["cookies"][0]["domain"]="example.com"
    if change=="cookie_path":data["cookies"][0]["path"]="/api2"
    if change=="duplicate_cookie":data["cookies"].append(dict(data["cookies"][0]))
    if change=="header_injection":data["headers"]["x_bc"]="synthetic\r\nAuthorization: bearer"
    if change=="invalid_rules":data["signing"]["rules"]["checksum_indexes"][0]=True
    with pytest.raises(Refused):credential(capture=data)


@pytest.mark.parametrize("overrides", [
    {"owner_id":True}, {"policy_revision":0}, {"account_id":"../other"}, {"x_hash":""},
    {"proxy_url":"https://proxy.example"}, {"proxy_url":"http://proxy.example:8080/path"},
    {"expires_at":datetime.now(timezone.utc)-timedelta(seconds=1)},
    {"expires_at":datetime.now()}, {"expires_at":datetime.now(timezone.utc)+timedelta(days=2)},
])
def test_explicit_session_scope_expiry_and_proxy_are_required(overrides):
    with pytest.raises(Refused):credential(**overrides)


def test_credentials_have_no_secret_bearing_repr_and_no_capture_reference():
    original=capture()
    session=credential(capture=original, proxy_url="http://user:synthetic-pass@proxy.example:8080")
    original["headers"]["x_bc"]="changed"
    original["signing"]["rules"]["checksum_indexes"][0]=39
    original["cookies"][0]["value"]="changed"
    assert session.x_bc=="synthetic-bc" and session.rules()["checksum_indexes"][0]==0
    assert session.cookie_header=="sess=synthetic-session"
    assert all(value not in repr(session) for value in ("synthetic-pass","synthetic-session","synthetic-bc","synthetic-hash"))
    assert "synthetic-key" not in repr(model())


def fake_curl(monkeypatch, *, body=b'{"id":7001}', status=200, headers=None, error=None):
    records=[]
    class Session:
        def __init__(self, **options):
            records.append({"constructor":options})
        def __enter__(self):return self
        def __exit__(self,*_):return False
        def request(self, method, url, **options):
            records.append(dict(method=method,url=url,options=options))
            if error:raise error
            options["content_callback"](body)
            return SimpleNamespace(status_code=status,headers={"content-type":"application/json",**(headers or {})})
    monkeypatch.setitem(sys.modules,"curl_cffi",SimpleNamespace(requests=SimpleNamespace(Session=Session)))
    return records


def test_https_identity_uses_only_explicit_session_no_hidden_warmup_retry_or_environment_proxy(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    monkeypatch.setenv("HTTPS_PROXY","http://untrusted-environment.example:8080")
    records=fake_curl(monkeypatch)
    wire=NativeProviderConnector(credential(proxy_url="http://fixed.example:8080"))
    wire.verify_identity();wire.verify_identity()
    requests=[row for row in records if "url" in row]
    assert len(requests)==2 and {row["url"] for row in requests}=={"https://onlyfans.com/api2/v2/users/me"}
    assert all(row["constructor"]=={"impersonate":"chrome","trust_env":False,"retry":0} for row in records if "constructor" in row)
    for row in requests:
        options=row["options"]
        assert options["proxy"]=="http://fixed.example:8080" and options["allow_redirects"] is False and options["verify"] is True
        assert options["discard_cookies"] is True and options["accept_encoding"]=="identity"
        assert options["headers"]["Cookie"]=="sess=synthetic-session"
        assert options["headers"]["User-ID"]==str(OWNER) and options["headers"]["X-Hash"]=="synthetic-hash"


def test_ledger_get_uses_selected_identity_frozen_utc_window_and_no_write(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    wire = NativeProviderConnector(credential())
    calls = []
    monkeypatch.setattr(wire, "verify_identity", lambda: calls.append("identity"))
    def call(method, path, body=b""):
        assert calls == ["identity"] and method == "GET" and body == b""
        parsed = urlsplit(path)
        assert parsed.path == "/api2/v2/payouts/transactions"
        assert parse_qs(parsed.query) == {"limit": ["20"], "offset": ["40"],
            "startDate": ["2026-10-01 00:00:00"], "endDate": ["2026-10-08 00:00:00"]}
        return b'{"list":[],"hasMore":false}'
    monkeypatch.setattr(wire, "_call", call)
    assert wire.transaction_page(offset=40, start=datetime(2026,10,1), end=datetime(2026,10,8))
    for overrides in [{"offset": -1}, {"offset": True}, {"limit": 21},
                      {"end": datetime(2027,10,8)}, {"start": datetime(2026,10,1,tzinfo=timezone.utc)}]:
        args = dict(offset=40, start=datetime(2026,10,1), end=datetime(2026,10,8)); args.update(overrides)
        with pytest.raises(Refused): wire.transaction_page(**args)


def test_ledger_marker_is_opaque_exact_and_replaces_the_offset(monkeypatch):
    from urllib.parse import parse_qs,urlsplit
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    wire=NativeProviderConnector(credential())
    calls=[]
    monkeypatch.setattr(wire,"verify_identity",lambda:None)
    monkeypatch.setattr(wire,"_call",lambda method,path: calls.append((method,path)) or b'{"list":[],"hasMore":false}')
    for marker in (12345678901234567890,"opaque+/=&value"):
        wire.transaction_page(offset=520,start=datetime(2026,10,1),end=datetime(2026,10,8),marker=marker)
        method,path=calls[-1];query=parse_qs(urlsplit(path).query)
        assert method=="GET" and query["marker"]==[str(marker)] and "offset" not in query
    count=len(calls)
    for marker in (True,1.5,"",-1,"x"*2049,"bad\nmarker"):
        with pytest.raises(Refused):
            wire.transaction_page(offset=0,start=datetime(2026,10,1),end=datetime(2026,10,8),marker=marker)
    assert len(calls)==count


@pytest.mark.parametrize("options", [
    {"status":302}, {"status":401}, {"status":429}, {"status":500},
    {"headers":{"content-type":"text/html"}}, {"headers":{"content-encoding":"gzip"}},
    {"body":b"x"*65537}, {"body":b'{"id":9999}'},
])
def test_uncertain_https_response_never_retries_or_follows_redirects(monkeypatch,options):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    records=fake_curl(monkeypatch,**options)
    with pytest.raises(NativeProtocolError):NativeProviderConnector(credential()).verify_identity()
    assert len([row for row in records if "url" in row])==1


def test_https_errors_are_redacted_and_never_retry(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    records=fake_curl(monkeypatch,error=RuntimeError("secret-proxy-pass secret-session"))
    with pytest.raises(NativeProtocolError) as result:NativeProviderConnector(credential()).verify_identity()
    assert "secret" not in str(result.value) and result.value.__suppress_context__
    assert len([row for row in records if "url" in row])==1


@pytest.mark.parametrize("role", [None,"","read","commands","configuration","legacy"])
def test_provider_io_refuses_nonworker_roles_before_constructing_http(monkeypatch,role):
    monkeypatch.delenv("ONLYFANS_PROCESS_ROLE",raising=False)
    if role is not None:monkeypatch.setenv("ONLYFANS_PROCESS_ROLE",role)
    records=fake_curl(monkeypatch)
    with pytest.raises(Refused):NativeProviderConnector(credential()).verify_identity()
    assert records==[]


@pytest.mark.parametrize("method,path,body", [
    ("GET","https://example.com/api2/v2/users/me",b""),
    ("GET","/api2/v2/../admin",b""), ("GET","/api2/v2/users/me#fragment",b""),
    ("POST","/api2/v2/chats/101/messages?other=1",b"{}"),
    ("DELETE","/api2/v2/chats/101/messages",b"{}"), ("POST","/api2/v2/users/me",b"{}"),
])
def test_provider_target_is_fixed_and_narrow_before_any_network(monkeypatch,method,path,body):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");records=fake_curl(monkeypatch)
    with pytest.raises(Refused):NativeProviderConnector(credential())._call(method,path,body)
    assert records==[]


def test_expired_session_stops_before_network(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");records=fake_curl(monkeypatch)
    session=credential();object.__setattr__(session,"expires_at",datetime.now(timezone.utc)-timedelta(seconds=1))
    with pytest.raises(Refused):NativeProviderConnector(session).verify_identity()
    assert records==[]


def test_native_vault_page_pins_identity_then_reads_only_the_bounded_native_endpoint(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    records=[]
    def call(self,method,path,body=b""):
        records.append((method,path,body))
        return b'{"id":7001}' if path.endswith("/users/me") else b'{"list":[],"hasMore":false}'
    monkeypatch.setattr(NativeProviderConnector,"_call",call)
    wire=NativeProviderConnector(credential())
    assert wire.vault_page(offset=24)==b'{"list":[],"hasMore":false}'
    assert records==[("GET","/api2/v2/users/me",b""),
        ("GET","/api2/v2/vault/media?limit=24&offset=24&sort=desc&field=recent&list=all",b"")]


@pytest.mark.parametrize("offset,limit",[(True,24),(-1,24),(1000001,24),(0,True),(0,0),(0,25)])
def test_invalid_vault_page_never_opens_any_request(monkeypatch,offset,limit):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");records=fake_curl(monkeypatch)
    with pytest.raises(Refused):NativeProviderConnector(credential()).vault_page(offset=offset,limit=limit)
    assert records==[]


def test_model_uses_saved_registry_wire_contract_and_requires_committed_plan_before_spend(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    adapter=model();messages=[{"role":"user","content":"Synthetic prompt"}]
    adapter.configure_call(account_id="acc-a",chosen_model=MODEL,config={},messages=messages)
    body=json.loads(adapter._body)
    assert body["model"]==MODELS[MODEL].wire_model() and body["messages"]==messages
    for key,value in MODELS[MODEL].reasoning_keys().items():assert body[key]==value
    assert body["response_format"]=={"type":"json_object"} and body["stream"] is False and body["max_tokens"]==4096
    calls=[]
    async def post(self,body):calls.append(body);raise AssertionError("must not reach HTTP")
    monkeypatch.setattr(NativeProviderModel,"_post_once",post)
    with pytest.raises(LLMConfigError,match="committed reservation"):asyncio.run(adapter.generate(messages))
    assert calls==[]
    with pytest.raises(LLMConfigError):adapter.configure_call(account_id="other",chosen_model=MODEL,config={},messages=messages)
    with pytest.raises(LLMConfigError):adapter.configure_call(account_id="acc-a",chosen_model="deepseek-chat",config={},messages=messages)


@pytest.mark.parametrize("raw",[b'[]',b'{"choices":[],"choices":[]}',b'{"usage":NaN}',b'\xff',b'{'])
def test_model_json_refuses_ambiguous_response(raw):
    with pytest.raises(LLMConfigError):_decode(raw)


def fake_model_http(monkeypatch, *, status=200, body=b'{"choices":[]}', headers=None, error=None):
    records=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body
    async def handle(request):
        records.append(request)
        if error:raise error
        return httpx.Response(status,headers={"content-type":"application/json",**(headers or {})},stream=Stream())
    options=[]
    def transport(**kw):
        options.append(kw)
        return httpx.MockTransport(handle)
    monkeypatch.setattr(httpx,"AsyncHTTPTransport",transport)
    return records,options


def test_model_http_uses_fixed_registered_endpoint_explicit_key_and_zero_retry(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    records,options=fake_model_http(monkeypatch)
    adapter=model()
    assert asyncio.run(adapter._post_once(b'{"model":"synthetic"}'))==b'{"choices":[]}'
    assert len(records)==1 and str(records[0].url)=="https://api.x.ai/v1/chat/completions"
    assert records[0].method=="POST" and records[0].headers["Authorization"]=="Bearer synthetic-key"
    assert options[0]["retries"]==0 and options[0]["trust_env"] is False


@pytest.mark.parametrize("options,error",[
    ({"status":302},TimeoutError),({"status":401},TimeoutError),({"status":429},TimeoutError),({"status":500},TimeoutError),
    ({"headers":{"content-type":"text/html"}},LLMConfigError),
    ({"headers":{"content-encoding":"gzip"}},LLMConfigError),
    ({"body":b"x"*131073},LLMConfigError),
    ({"error":RuntimeError("synthetic-secret")},TimeoutError),
])
def test_model_http_uncertainty_is_bounded_and_never_retries_or_follows_redirects(monkeypatch,options,error):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    records,_=fake_model_http(monkeypatch,**options)
    with pytest.raises(error) as result:asyncio.run(model()._post_once(b'{}'))
    assert len(records)==1 and "synthetic-secret" not in str(result.value)
