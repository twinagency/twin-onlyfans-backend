"""Actual Next handlers/session -> TS adapter -> FastAPI -> synthetic PG.

The isolated Next app imports production handlers, auth/identity code, and
pages. Only the external Supabase identity service is a local synthetic HTTP
server. No production auth bypass or function alias is installed.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy.orm import Session
from sqlalchemy import select

from test_onlyfans_foundation_pg import database, seed, event
from db.models import Chat, Message, OfCreatorBinding, AccountAiConfig, GrokCall, GrokDailyCost, CatalogItem, Transaction, ContentOffer
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority


MANUAL_REPLY = "Synthetic manual reply through the shared authority"
AMBIGUOUS_REPLY = "Synthetic ambiguous reply without a provider receipt"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


class IdentityStub(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        path = self.path.split("?")[0]
        status = 200
        if path == "/auth/v1/user":
            if self.headers.get("Authorization") != "Bearer synthetic-agency-token":
                status, value = 401, {"message":"Invalid synthetic identity"}
            else: value = {"id":"00000000-0000-4000-8000-000000000001", "aud":"authenticated", "role":"authenticated"}
        elif path == "/rest/v1/agency_memberships": value = [{"agency_id":"synthetic-agency"}]
        elif path == "/rest/v1/partner_agencies": value = [{"id":"synthetic-agency", "name":"Synthetic agency"}]
        elif path == "/rest/v1/agency_creators": value = []
        elif path == "/rest/v1/clones":
            creator={"id":"creator-acc-a","name":"Synthetic Creator"}
            wanted=parse_qs(urlsplit(self.path).query).get("id", [None])[0]
            value=[creator] if wanted in (None,"eq.creator-acc-a") else []
        else: status, value = 500, {"error":"Unexpected identity dependency"}
        if "vnd.pgrst.object" in self.headers.get("Accept","") and isinstance(value,list):
            value = value[0] if value else None
        body=json.dumps(value).encode()
        self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)


@contextmanager
def process(args, *, cwd, env, log):
    with open(log,"w") as output:
        child=subprocess.Popen(args,cwd=cwd,env=env,stdout=output,stderr=output)
        try: yield child
        finally:
            child.terminate()
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()


def wait_http(url, child):
    deadline=time.monotonic()+55
    while time.monotonic()<deadline:
        if child.poll() is not None:raise AssertionError("test server exited; inspect its log")
        try:
            result=httpx.get(url,timeout=3,trust_env=False)
            if result.status_code < 500:return
        except httpx.HTTPError:pass
        time.sleep(.15)
    raise AssertionError("test server did not become ready")


def synthetic_scenario(database, monkeypatch):
    """One shared neutral scenario for HTTP acceptance and the offline demo."""
    url, engine=database
    store=seed(url,engine)
    other=seed(url,engine,"acc-b","tenant-b")
    incoming=event(body="Synthetic PostgreSQL message through the real read path")
    store.record("tenant-a","acc-a",incoming);store.drain("tenant-a","acc-a")
    store.record("tenant-a","acc-a",event(mid=4,fan=202,body="Synthetic second conversation"))
    store.drain("tenant-a","acc-a")
    with Session(engine) as s,s.begin():
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(AccountAiConfig(account_id="acc-a",daily_cost_cap_cents=100))
        s.add(GrokDailyCost(day=now.strftime("%Y-%m-%d"),account_id="acc-a",provider="grok",cost_cents=7,call_count=1))
        s.add(GrokDailyCost(day=now.strftime("%Y-%m-%d"),account_id="acc-b",provider="grok",cost_cents=99999999,call_count=99))
        s.add(GrokCall(purpose="ai_chatter",account_id="acc-a",fan_id=101,model="synthetic/native-text",
            provider="grok",endpoint="synthetic://onlyfans-native-text",prompt_json="private model prompt must never cross read API",
            status="unknown",cost_cents=7,was_dry_run=True,called_at=now))
        s.add(Chat(account_id="acc-a",fan_id=999,last_message_at=None,unread_count=0))
        s.add(Message(account_id="acc-a",fan_id=101,message_id=2,direction="out",body="Retracted synthetic message",created_at=datetime.now(timezone.utc).replace(tzinfo=None),is_unsent=True))
        # This explicit fixture certifies only synthetic history for dispatch.
        # The account is marked unsynchronized again before any read is served.
        runtime=s.get(OfRuntime,"acc-a")
        runtime.recovery_needed=False
        runtime.last_sync_at=datetime.now(timezone.utc).replace(tzinfo=None)
    manual_revision=store.control("tenant-a","acc-a",101,mode="human_controlled",expected_revision=0,
                                  command_key="http-manual-owner",actor="synthetic-operator",reason="Offline HTTP integration")
    unknown_revision=store.control("tenant-a","acc-a",202,mode="human_controlled",expected_revision=0,
                                   command_key="http-unknown-owner",actor="synthetic-operator",reason="Offline uncertainty integration")
    authority=TextAuthority(store)
    from test_onlyfans_media_pg import shelf
    media_item=shelf(engine)
    with Session(engine) as s,s.begin():
        s.get(CatalogItem,media_item).label="Scenic test collection"
        for i in range(31):s.add(CatalogItem(account_id="acc-a",label=f"Synthetic saved item {i+1}",kind="image",
            description_for_ai="A neutral scenic still image.",media_ids="[11]",preview_media_ids="[]",price_cents=500,enabled=False))
        cfg=s.get(AccountAiConfig,"acc-a")
        cfg.persona="Saved synthetic creator persona for settings."
        cfg.persona_facts_json='{"age":"28","home_city":"Synthetic Harbor","secret":"private creator config field"}'
        cfg.model="synthetic/native-text"
        s.add(ContentOffer(account_id="acc-a",fan_id=101,item_id=media_item,mode="ppv",price_cents=800,status="open"))
        for currency,status,amount in [("USD","pending",2500),("USD","refunded",600),("EUR","cleared",700)]:
            s.add(Transaction(account_id="acc-a",fan_id=101,kind="ppv_message",amount_cents=amount,currency=currency,
                status=status,occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),raw_json='{"private":"private transaction source"}'))
    authority.configure("tenant-a","acc-a",expected_revision=0,enabled=True,limit_units=5,
                        throttle_seconds=1,actor="synthetic-operator",reason="Local synthetic transport only",media_enabled=True)
    accepted_intent=authority.prepare("tenant-a","acc-a",101,command_key="http-manual-text",
                                      expected_revision=manual_revision,body=MANUAL_REPLY,origin="manual",actor="synthetic-operator",catalog_item_id=media_item)
    accepted_transport=SyntheticTextTransport("acc-a",message_id=3)
    assert authority.dispatch("tenant-a","acc-a",accepted_intent,accepted_transport)=="accepted"
    assert len(accepted_transport.calls)==1
    from onlyfans_runtime.commerce import CommerceIntake, SyntheticCommerceEvent
    from dataclasses import replace
    with monkeypatch.context() as commerce_context:
        commerce_context.setenv("ONLYFANS_PROCESS_ROLE","worker")
        intake=CommerceIntake(store)
        paid=SyntheticCommerceEvent(account_id="acc-a",fan_id=101,message_id=3,
            event_key="http-synthetic-payment",provider_transaction_id="http-synthetic-tx",provider_revision=1,
            kind="purchase",amount_cents=1000,currency="USD",occurred_at=datetime.now(timezone.utc).isoformat(),
            evidence_ref="synthetic-http-purchase")
        assert intake.record("tenant-a","acc-a",paid)=="applied"
        assert intake.record("tenant-a","acc-a",replace(paid,kind="refund"))=="conflict"
    with Session(engine) as s,s.begin():
        # Advance only the synthetic policy's pacing fixture; no waiting or
        # production throttle override is installed in application code.
        s.get(OfDeliveryPolicy,"acc-a").next_dispatch_at=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1)
        accepted_evidence=s.get(OfTextIntent,("acc-a",accepted_intent)).evidence_ref
    unknown_intent=authority.prepare("tenant-a","acc-a",202,command_key="http-ambiguous-text",
                                     expected_revision=unknown_revision,body=AMBIGUOUS_REPLY,origin="manual",actor="synthetic-operator")
    unknown_transport=SyntheticTextTransport("acc-a",outcome="timeout")
    assert authority.dispatch("tenant-a","acc-a",unknown_intent,unknown_transport)=="unknown"
    assert len(unknown_transport.calls)==1
    with Session(engine) as s,s.begin():
        unresolved=s.get(OfTextIntent,("acc-a",unknown_intent))
        unknown_attempt=unresolved.attempt_id
        assert unresolved.provider_message_id is None
        assert s.scalar(select(Message.message_id).where(Message.account_id=="acc-a",Message.fan_id==202,Message.direction=="out")) is None
        s.get(OfRuntime,"acc-a").recovery_needed=True
    return store, other, incoming, manual_revision, accepted_evidence, unknown_attempt, accepted_transport, unknown_transport


def test_real_authenticated_read_path(database, tmp_path, monkeypatch):
    url, engine=database
    store, other, incoming, manual_revision, accepted_evidence, unknown_attempt, accepted_transport, unknown_transport = synthetic_scenario(database, monkeypatch)
    from onlyfans_worker_fixture import run_worker_scenario
    assert run_worker_scenario(database,monkeypatch)["providerRequests"]==0
    from onlyfans_operator_fixture import seed_operator, operator_dispatcher, ACCOUNT as OPERATOR_ACCOUNT, MANUAL as OPERATOR_MESSAGE
    operator_store=seed_operator(database)
    from onlyfans_vault_preview_fixture import seed_preview
    preview_root=tmp_path/"vault-images"
    preview_bytes=seed_preview(engine,preview_root,monkeypatch)
    backend=Path(__file__).resolve().parents[2]
    dashboard=Path(os.environ.get("ONLYFANS_TEST_DASHBOARD",str(backend.parent/"twin-dashboard"))).resolve()
    assert (dashboard/"node_modules/next/dist/bin/next").exists(), "Run npm ci in the dashboard first"
    harness=dashboard/"preview/onlyfans-integration"
    assert not list(harness.glob(".env*")), "Integration harness must not load an environment file"
    api_port,next_port=port(),port()
    identity=ThreadingHTTPServer(("127.0.0.1",0),IdentityStub)
    identity_thread=threading.Thread(target=identity.serve_forever,daemon=True);identity_thread.start()
    api=f"http://127.0.0.1:{api_port}"
    web=f"http://127.0.0.1:{next_port}"
    env={**os.environ,"ONLYFANS_PROCESS_ROLE":"api","DATABASE_URL":url,"ONLYFANS_API_TOKEN":"synthetic-of-token","ONLYFANS_API_TENANT":"tenant-a","ONLYFANS_API_BASE_URL":api,"VAULT_IMAGE_DIR":str(preview_root),
         "ONLYFANS_CONTROL_TOKEN":"synthetic-controls-only-token","ONLYFANS_CONTROL_TENANT":"tenant-a",
         "ONLYFANS_MANUAL_ENABLED":"1","ONLYFANS_TEST_OPERATOR":"1",
         "ONLYFANS_CAMPAIGN_TOKEN":"synthetic-campaign-only-token","ONLYFANS_CAMPAIGN_TENANT":"tenant-a",
         "ONLYFANS_CONTENT_TOKEN":"synthetic-content-only-token","ONLYFANS_CONTENT_TENANT":"tenant-a","ONLYFANS_TEST_CONTENT":"1",
         "ONLYFANS_CONNECTION_TOKEN":"synthetic-connection-only-token","ONLYFANS_CONNECTION_TENANT":"tenant-a",
         "ONLYFANS_CONNECTION_KEY":"17"*32,"ONLYFANS_TEST_CONNECTIONS":"1",
         "ONLYFANS_CONFIGURATION_TOKEN":"synthetic-configuration-only-token","ONLYFANS_CONFIGURATION_TENANT":"tenant-a","ONLYFANS_TEST_CONFIGURATION":"1","ONLYFANS_TEST_WORKER":"1",
         "DASHBOARD_SESSION_SECRET":"synthetic-local-session-secret","SUPABASE_URL":f"http://127.0.0.1:{identity.server_port}","SUPABASE_SERVICE_KEY":"synthetic-service-key","NEXT_TELEMETRY_DISABLED":"1","TZ":"UTC"}
    node=os.environ.get("CODEX_PRIMARY_RUNTIME_NODE","node")
    log_dir=Path(os.environ.get("ONLYFANS_TEST_EVIDENCE",str(tmp_path)))
    log_dir.mkdir(parents=True,exist_ok=True)
    try:
        with process([sys.executable,"-m","uvicorn","onlyfans_app:app","--app-dir","service","--host","127.0.0.1","--port",str(api_port),"--limit-concurrency","32"],cwd=backend,env=env,log=log_dir/"api.log") as api_proc:
            wait_http(api+"/healthz",api_proc)
            credential={"Authorization":"Bearer synthetic-of-token","X-OnlyFans-Schema-Version":"of-preview.v2"}
            with httpx.Client(timeout=20,trust_env=False) as client:
                assert client.get(api+"/onlyfans/api/v1/ready",headers=credential).status_code==200
                assert client.get(api+"/onlyfans/api/v1/accounts").status_code==401
                assert client.get(api+"/onlyfans/api/v1/accounts",headers={**credential,"X-OnlyFans-Schema-Version":"of-preview.v1"}).status_code==400
                for path in ("/api/of/accounts","/admin","/events","/sse","/ws","/vault_thumbs/test","/secrets.json"):
                    response=client.get(api+path,headers=credential)
                    assert response.status_code==404
                    assert "no-store" in response.headers["cache-control"]
                assert client.get(api+"/onlyfans/api/v1/accounts/acc-b/conversations",headers=credential).status_code in (403,404)
                for section in ("content","sales","settings"):
                    native_path=api+f"/onlyfans/api/v1/accounts/acc-a/workspace/{section}"
                    assert client.get(native_path).status_code==401
                    native=client.get(native_path,headers=credential)
                    assert native.status_code==200,native.text
                    assert native.json()["workspace"]["section"]==section
                    assert native.json()["workspace"]["providerConnectionVerified"] is False
                    assert "no-store" in native.headers["cache-control"]
                    assert client.get(native_path.replace("acc-a","acc-b"),headers=credential).status_code==404
                    assert "private transaction source" not in native.text and "private creator config field" not in native.text
                operations_path=api+"/onlyfans/api/v1/accounts/acc-a/operations"
                assert client.get(operations_path).status_code == 401
                for denied_account in ("acc-b", "nonexistent"):
                    assert client.get(api+f"/onlyfans/api/v1/accounts/{denied_account}/operations",headers=credential).status_code in (403,404)
                operations=client.get(operations_path,headers=credential)
                assert operations.status_code == 200, operations.text
                assert "no-store" in operations.headers["cache-control"]
                diagnostics=operations.json()["operations"]
                assert diagnostics["schemaVersion"] == "of-operations.v3"
                assert diagnostics["modelCosts"]["unit"] == "usd_millicents"
                assert diagnostics["modelCosts"]["spentMillicents"] == 7
                assert diagnostics["modelCosts"]["capPerProviderMillicents"] == 10000
                assert diagnostics["modelCosts"]["unresolvedCalls"] == 1
                assert "private model prompt" not in operations.text and "99999999" not in operations.text
                assert diagnostics["delivery"]["unknown"] == 1
                assert diagnostics["policy"]["usedUnits"] == 2
                assert diagnostics["providerConnectionVerified"] is False
                assert "delivery_unresolved" in diagnostics["reasons"]
                assert "commerce_evidence_conflict" in diagnostics["reasons"]
                assert "http-synthetic-payment" not in operations.text and "http-synthetic-tx" not in operations.text
                assert MANUAL_REPLY not in operations.text and AMBIGUOUS_REPLY not in operations.text
                assert "synthetic-of-token" not in operations.text and unknown_attempt not in operations.text
                first=client.get(api+"/onlyfans/api/v1/accounts/acc-a/conversations?limit=1",headers=credential).json()
                # PG's default DESC puts NULL first; the explicit NULLS LAST
                # order must preserve the cursor's total order across pages.
                page=first.get("page",first)
                assert "acc-a:101" in json.dumps(page)
            sessions=json.loads(subprocess.check_output([node,"--import","tsx","scripts/onlyfans-test-sessions.ts"],cwd=dashboard,env=env,text=True))
            with process([node,str(dashboard/"node_modules/next/dist/bin/next"),"dev","--hostname","127.0.0.1","--port",str(next_port)],cwd=harness,env=env,log=log_dir/"next.log") as next_proc:
                wait_http(web+"/api/onlyfans/accounts",next_proc)
                with httpx.Client(timeout=40,trust_env=False) as client:
                    path=web+"/api/onlyfans/accounts"
                    assert client.get(path).status_code==401
                    for invalid in ("malformed", sessions["operator"]+".extra", "9999999999999."+"!"*43):
                        assert client.get(path,headers={"Cookie":"allie_admin_session="+invalid}).status_code==401
                    cookie="allie_admin_session="+sessions["operator"]
                    preview_path=web+"/api/onlyfans-content/accounts/acc-a/vault/previews/11"
                    assert client.get(preview_path).status_code==401
                    assert client.get(preview_path,headers={"Cookie":cookie+"; twin_agency_access=synthetic-agency-token"}).status_code==403
                    picture=client.get(preview_path,headers={"Cookie":cookie})
                    assert picture.status_code==200 and picture.content==preview_bytes
                    assert picture.headers["content-type"]=="image/png" and "no-store" in picture.headers["cache-control"]
                    assert client.get(preview_path+"?url=https://caller.example",headers={"Cookie":cookie}).status_code==400
                    assert client.get(preview_path.replace("acc-a","acc-b"),headers={"Cookie":cookie}).status_code==404
                    response=client.get(path,headers={"Cookie":cookie})
                    assert response.status_code==200,response.text
                    assert "no-store" in response.headers["cache-control"]
                    body=response.json()
                    assert "acc-a" in json.dumps(body) and "acc-b" not in json.dumps(body)
                    assert '"asOf": null' in json.dumps(body), "unsynchronized account must not claim a fresh sync"
                    detail=client.get(web+"/api/onlyfans/accounts/acc-a/conversations/acc-a:101",headers={"Cookie":cookie})
                    assert detail.status_code==200,detail.text
                    assert incoming.body in detail.text
                    assert '"retracted"' in detail.text
                    manual=next(message for message in detail.json()["conversation"]["messages"] if message["body"]==MANUAL_REPLY)
                    assert (manual["messageId"],manual["origin"],manual["delivery"],manual["evidenceRef"])==("3","human","sent",accepted_evidence)
                    ambiguous=client.get(web+"/api/onlyfans/accounts/acc-a/conversations/acc-a:202",headers={"Cookie":cookie})
                    assert ambiguous.status_code==200,ambiguous.text
                    uncertain=ambiguous.json()["conversation"]
                    assert uncertain["deliveryIncident"]["attemptId"]==unknown_attempt
                    assert uncertain["deliveryIncident"]["outcome"]=="unknown"
                    assert uncertain["summary"]["requiresAttention"] is True
                    assert all(message["body"]!=AMBIGUOUS_REPLY and message["direction"]!="outbound" for message in uncertain["messages"])
                    assert AMBIGUOUS_REPLY not in ambiguous.text, "an ambiguous intent cannot fabricate a provider message"
                    assert client.get(path,headers={"Cookie":cookie+"; twin_agency_access=synthetic-agency-token"}).status_code==403
                    assert client.get(path,headers={"Cookie":cookie+"; twin_agency_access=expired"}).status_code==401
                    assert client.get(path,headers={"Cookie":cookie+"; twin_admin_view_as="+sessions["viewAs"]}).status_code==403
                    command_path=web+"/api/onlyfans-controls/accounts/acc-a/conversations/acc-a:101"
                    command_headers={"Cookie":cookie,"Origin":web,"Content-Type":"application/json","Sec-Fetch-Site":"same-origin"}
                    command_body={"action":"request_takeover","expectedRevision":manual_revision}
                    assert client.post(command_path,json=command_body,headers={"Origin":web}).status_code==401
                    assert client.post(command_path,json=command_body,headers={"Cookie":cookie}).status_code==403
                    assert client.post(command_path,json=command_body,headers={**command_headers,"Origin":"https://untrusted.invalid"}).status_code==403
                    assert client.post(command_path,json=command_body,headers={**command_headers,"Sec-Fetch-Site":"cross-site"}).status_code==403
                    assert client.post(command_path,json={**command_body,"actor":"forged"},headers=command_headers).status_code==400
                    assert client.post(command_path,json={**command_body,"action":"send_manual_message"},headers=command_headers).status_code==400
                    assert client.post(command_path,content="x"*65537,headers=command_headers).status_code==413
                    for denied in (cookie+"; twin_agency_access=synthetic-agency-token",cookie+"; twin_admin_view_as="+sessions["viewAs"]):
                        assert client.post(command_path,json=command_body,headers={**command_headers,"Cookie":denied}).status_code==403
                    assert client.post(command_path,json=command_body,headers={**command_headers,"Cookie":cookie+"; twin_agency_access=expired"}).status_code==401
                    assert client.post(command_path.replace("acc-a","acc-b"),json=command_body,headers=command_headers).status_code==404
                    configuration_path=web+"/api/onlyfans-configuration/accounts/acc-a"
                    assert client.get(configuration_path).status_code==401
                    configuration=client.get(configuration_path,headers={"Cookie":cookie})
                    assert configuration.status_code==200,configuration.text
                    assert "no-store" in configuration.headers["cache-control"]
                    metadata=configuration.json()["configuration"]
                    configuration_body={"patch":{"persona":"Should not be saved by a refused request."},
                        "expectedRevision":metadata["configurationRevision"],"expectedFingerprint":metadata["fingerprint"]}
                    assert client.post(configuration_path,headers={"Origin":web},json=configuration_body).status_code==401
                    assert client.post(configuration_path,headers={"Cookie":cookie},json=configuration_body).status_code==403
                    for altered in ({"Origin":"https://untrusted.invalid"},{"Sec-Fetch-Site":"cross-site"}):
                        assert client.post(configuration_path,headers={**command_headers,**altered},json=configuration_body).status_code==403
                    for denied_cookie in (cookie+"; twin_agency_access=synthetic-agency-token",cookie+"; twin_admin_view_as="+sessions["viewAs"]):
                        assert client.get(configuration_path,headers={"Cookie":denied_cookie}).status_code==403
                        assert client.post(configuration_path,headers={**command_headers,"Cookie":denied_cookie},json=configuration_body).status_code==403
                    assert client.get(configuration_path,headers={"Cookie":cookie+"; twin_agency_access=expired"}).status_code==401
                    assert client.post(configuration_path,headers={**command_headers,"Cookie":cookie+"; twin_agency_access=expired"},json=configuration_body).status_code==401
                    assert client.get(configuration_path.replace("acc-a","acc-b"),headers={"Cookie":cookie}).status_code==404
                    assert client.post(configuration_path.replace("acc-a","acc-b"),headers=command_headers,json=configuration_body).status_code==404
                    assert client.post(configuration_path,headers=command_headers,json={**configuration_body,"actor":"forged"}).status_code==400
                    assert client.post(configuration_path,headers=command_headers,json={**configuration_body,"patch":{"dailyCostCap":0}}).status_code==400
                    assert client.post(configuration_path,headers=command_headers,content="x"*65537).status_code==413
                    assert client.get(web+"/api/onlyfans/accounts/acc-b/conversations",headers={"Cookie":cookie}).status_code in (403,404)
                    ops_route=web+"/api/onlyfans/accounts/acc-a/operations"
                    assert client.get(ops_route).status_code==401
                    ops_response=client.get(ops_route,headers={"Cookie":cookie})
                    assert ops_response.status_code==200,ops_response.text
                    assert ops_response.json()["operations"]["policy"]["usedUnits"]==2
                    assert "no-store" in ops_response.headers["cache-control"]
                    for denied_cookie in (cookie+"; twin_agency_access=synthetic-agency-token",cookie+"; twin_admin_view_as="+sessions["viewAs"]):
                        assert client.get(ops_route,headers={"Cookie":denied_cookie}).status_code==403
                    assert client.get(web+"/api/onlyfans/accounts/acc-b/operations",headers={"Cookie":cookie}).status_code in (403,404)
                    for section in ("content","sales","settings"):
                        native_route=web+f"/api/onlyfans/accounts/acc-a/workspace/{section}"
                        assert client.get(native_route).status_code==401
                        response=client.get(native_route,headers={"Cookie":cookie})
                        assert response.status_code==200,response.text
                        assert "no-store" in response.headers["cache-control"]
                        for denied_cookie in (cookie+"; twin_agency_access=synthetic-agency-token",cookie+"; twin_admin_view_as="+sessions["viewAs"]):
                            assert client.get(native_route,headers={"Cookie":denied_cookie}).status_code==403
                        assert client.get(native_route.replace("acc-a","acc-b"),headers={"Cookie":cookie}).status_code==404
                        scoped=client.get(web+f"/onlyfans/{section}?creator=creator-acc-b&account=acc-a",headers={"Cookie":cookie})
                        assert "data-of-native-section" not in scoped.text
                    for query in ("?creator=creator-acc-b&account=acc-a", "?account=acc-a&account=acc-b", "?creator=creator-acc-a&creator=creator-acc-b"):
                        scoped_page=client.get(web+"/onlyfans/operations"+query,headers={"Cookie":cookie})
                        assert scoped_page.status_code==200,scoped_page.text
                        assert "data-of-cap" not in scoped_page.text, "invalid or mismatched scope must not choose another account"
                with httpx.Client(timeout=40,trust_env=False) as combined_client:
                    combined_path=web+"/api/combined-earnings?period=1M"
                    assert combined_client.get(combined_path).status_code==401
                    assert combined_client.get(combined_path,headers={"Cookie":cookie+"; twin_agency_access=synthetic-agency-token"}).status_code==403
                    combined=combined_client.get(combined_path,headers={"Cookie":cookie})
                    assert combined.status_code==200,combined.text
                    combined_data=combined.json()
                    assert combined_data["telegram"]["status"]=="unavailable"  # stub has no revenue service
                    assert combined_data["telegram"]["netStars"] is None
                    assert combined_data["onlyfans"]["status"]=="unavailable"
                    assert combined_data["onlyfans"]["clearedGrossCents"] is None
                    assert combined_data["onlyfans"]["window"]==combined_data["window"]
                    assert combined_client.get(combined_path+"&clone=other",headers={"Cookie":cookie}).status_code==400
                if os.environ.get("ONLYFANS_TEST_CLIENT") == "http":
                    from onlyfans_operator_fixture import check_campaign_http
                    with httpx.Client(timeout=40, trust_env=False) as client:
                        check_campaign_http(client, web, cookie, engine)
                    return
                # Exercise the actual client-rendered page in the same isolated
                # network namespace as both servers when Playwright is supplied.
                # A real source projection, not a browser response override.
                # Seed immediately before the browser so the freshness window
                # exercises the real inheritance POST and canonical read path.
                from db.onlyfans_creator_facts_models import OfCreatorFactsSource
                from onlyfans_runtime.creator_facts import SOURCE_COLUMNS
                from onlyfans_runtime.native_context import canonical
                import hashlib
                with Session(engine) as s,s.begin():
                    source=dict.fromkeys(key for key in SOURCE_COLUMNS if key!="updated_at")
                    source.update(id="creator-acc-a",current_city="Shared Twin city",tattoo_details=[],turn_ons=[],hard_limits=[])
                    now=datetime.now(timezone.utc).replace(tzinfo=None)
                    s.add(OfCreatorFactsSource(account_id="acc-a",binding_id="binding-acc-a",source_json=canonical(source),
                        source_hash=hashlib.sha256(canonical(source).encode()).hexdigest(),source_updated_at=now,checked_at=now,
                        overrides_json=s.get(AccountAiConfig,"acc-a").persona_facts_json))
                browser_check=dashboard/"scripts/check-onlyfans-http-browser.mjs"
                with operator_dispatcher(operator_store) as operator_peer:
                    subprocess.run([node,str(browser_check)],cwd=dashboard,env={**env,"ONLYFANS_TEST_WEB_URL":web,"ONLYFANS_TEST_COOKIE":sessions["operator"],"ONLYFANS_TEST_EVIDENCE":str(log_dir)},check=True,timeout=240)
                    assert len(operator_peer.posts)==1, "manual lost acknowledgement retry must not send twice"
                from db.models import OfConversationState
                from db.onlyfans_models import OfControlAudit
                with Session(engine) as s:
                    state=s.get(OfConversationState,("acc-a",101))
                    assert (state.mode,state.revision,state.takeover_state)==("no_contact",6,"none")
                    pending=s.get(OfConversationState,("acc-a",202))
                    assert pending.takeover_state=="requested" and pending.unresolved_attempt_id==unknown_attempt
                    assert s.get(OfDeliveryPolicy,"acc-a").used_units==2
                    audits=s.scalars(select(OfControlAudit).where(OfControlAudit.account_id=="acc-a",OfControlAudit.action.is_not(None))).all()
                    assert len(audits)==6, "lost acknowledgement retry must not create another control event"
                    assert all(x.actor.startswith("op-session:") and sessions["operator"] not in x.actor for x in audits)
                    assert len(accepted_transport.calls)==len(unknown_transport.calls)==1
                    from db.onlyfans_models import OfConfigurationChange,OfRuntime
                    changes=s.scalars(select(OfConfigurationChange).where(OfConfigurationChange.account_id=="acc-a")).all()
                    assert len(changes)==5 and s.get(OfRuntime,"acc-a").configuration_revision==5
                    config=s.get(AccountAiConfig,"acc-a")
                    assert config.persona=="Updated approved synthetic creator persona."
                    assert json.loads(config.persona_facts_json)["home_city"]=="Concurrent Synthetic Harbor"
                    assert json.loads(config.persona_facts_json)["secret"]=="private creator config field"
                    assert config.daily_cost_cap_cents==100 and config.model=="grok-4-1-fast-non-reasoning"
                    assert all(row.actor.startswith("op-session:") and sessions["operator"] not in row.actor for row in changes)
                    operator_intents=s.scalars(select(OfTextIntent).where(OfTextIntent.account_id==OPERATOR_ACCOUNT)).all()
                    assert len(operator_intents)==1 and operator_intents[0].status=="accepted"
                    assert operator_intents[0].body==OPERATOR_MESSAGE and operator_intents[0].origin=="manual"
                    assert s.get(OfDeliveryPolicy,OPERATOR_ACCOUNT).used_units==1
                    assert s.scalar(select(Message.body).where(Message.account_id==OPERATOR_ACCOUNT,Message.direction=="out"))==OPERATOR_MESSAGE
                    from db.onlyfans_campaign_models import OfCampaign, OfCampaignRecipient, OfCampaignChange
                    operator_campaigns=s.scalars(select(OfCampaign).where(OfCampaign.account_id==OPERATOR_ACCOUNT)).all()
                    assert len(operator_campaigns)==1 and operator_campaigns[0].status=="cancelled"
                    operator_recipients=s.scalars(select(OfCampaignRecipient).where(OfCampaignRecipient.account_id==OPERATOR_ACCOUNT)).all()
                    assert len(operator_recipients)==1 and operator_recipients[0].status=="cancelled"
                    operator_changes=s.scalars(select(OfCampaignChange).where(OfCampaignChange.account_id==OPERATOR_ACCOUNT)).all()
                    assert len(operator_changes)==4, "create retry must preserve one campaign and one audit event"
    finally:
        identity.shutdown();identity.server_close();store.close();other.close();operator_store.close()
