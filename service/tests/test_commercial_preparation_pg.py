"""Disabled preparation: real PostgreSQL, mocked wire, no grants or purchases."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
import httpx
import pytest
from sqlalchemy import select,text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database,seed
from test_engine_real_worker_pg import answer
from test_real_transport_pg import _item
from conversation_engine import preparation as prep
from db.onlyfans_models import OfRuntime
from db.onlyfans_engine_models import OfEnginePilotScope,OfFansConnection
from db.onlyfans_fans_intake_models import OfFansLink
from db.onlyfans_vault_models import OfVaultSyncJob
from db.models import VaultItem
from onlyfans_runtime.store import Store,Refused
from onlyfans_runtime.text_delivery import TextAuthority
from fans_api_adapter.real_transport import RealFansConfig,build_client
from db.schema import check_schema
TENANT="tenant-a"
ACTOR="op-session:"+"a"*64

def config(i=1,requests=10,credits=20,per=2):
    return {"contract_version":"preparation.v1","provider_account":"acct_prep_"+str(i),"creator_id":str(100+i),
        "qualification":{"mode":"fixture","reference":"isolated mocked qualification"},
        "limits":{"provider_requests":requests,"provider_credit_budget":credits,"provider_credits_per_request":per}}
def setup(database,i=1,**limits):
    url,engine=database;account="prep-"+str(i)
    store=seed(url,engine,account=account,tenant=TENANT)
    with Session(engine) as s,s.begin():s.get(OfRuntime,account).enabled=False
    TextAuthority(store).configure(TENANT,account,expected_revision=0,enabled=False,media_enabled=False,
        limit_units=0,throttle_seconds=1,transport_kind="fansapi",actor=ACTOR,reason="Preparation only")
    cfg=config(i,**limits)
    prep.configure(store,TENANT,account,cfg,expected_revision=0,command_key="a"*64,actor=ACTOR,reason="Explicit fixture read ceiling")
    return store,account,cfg
def factory(wire):
    def make(cfg,epoch,guard):
        return build_client(RealFansConfig("synthetic-preparation",cfg["provider_account"],"https://api.fansapi.com",5),
            creator_id=cfg["creator_id"],epoch=epoch,transport=httpx.MockTransport(wire),request_guard=guard)
    return make
def identity(cfg):return answer(200,[{"id":cfg["provider_account"],"onlyfans_id":cfg["creator_id"],"is_authenticated":True}])
def page(items,more=False):return answer(200,{"data":{"list":items,"hasMore":more}})

def test_account_scoped_ten_creator_connect_and_metadata_sync_remain_disabled(database):
    stores=[]
    try:
        for i in range(1,11):
            store,account,cfg=setup(database,i);stores.append(store);calls=[]
            def wire(req):
                assert store.engine.pool.checkedout()==1,"only session advisory lock may span HTTP"
                calls.append((req.method,req.url.path))
                return identity(cfg) if req.url.path=="/api/accounts" else page([_item(501)])
            assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="complete"
            assert len(calls)==2 and all(method=="GET" for method,_ in calls)
            # Completed preparation performs no polling/identity spend on restart.
            assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="complete"
            assert len(calls)==2
            state=prep.view(store,TENANT,account)
            assert state["connected"] and state["vault"]["selected"]==1
            assert state["providerRequestsUsed"]==2 and state["providerCreditsReserved"]==4
            assert not state["automaticRepliesEnabled"] and not state["salesEnabled"]
            assert state["historyCoverage"]==state["ownershipCoverage"]=="pending"
        with Session(database[1]) as s:
            assert len(s.scalars(select(OfEnginePilotScope)).all())==10
            for i in range(1,11):
                a="prep-"+str(i);conn=s.get(OfFansConnection,(a,1))
                assert conn.provider_creator_id==str(100+i)
                assert s.get(OfFansLink,(a,str(100+i))).send_blocked
                assert s.get(VaultItem,(a,501)) is not None
        with pytest.raises(Refused,match="scope"):prep.view(stores[0],"another-tenant","prep-1")
    finally:
        for store in stores:store.close()

def test_guard_reserves_failures_and_rejects_posts_downloads_wrong_creator_and_wire(database):
    store,account,cfg=setup(database,requests=2,credits=3,per=2)
    guard=prep.PreparationRequestGuard(store,TENANT,account,1)
    for method,url,mock in (
        ("POST","https://api.fansapi.com/api/accounts",True),
        ("GET","https://api.fansapi.com/api/acct_other/media/vault?limit=24&offset=0&field=recent&sort=desc",True),
        ("GET","https://api.fansapi.com/api/"+cfg["provider_account"]+"/media/123/download",True),
        ("GET","https://api.fansapi.com/api/accounts",False)):
        with pytest.raises(Refused):guard(method,url,mock)
    assert prep.view(store,TENANT,account)["providerRequestsUsed"]==0
    def wire(req):raise TimeoutError("synthetic failed identity read")
    with pytest.raises(Refused):prep.prepare_one(store,TENANT,account,client_factory=factory(wire))
    state=prep.view(store,TENANT,account)
    assert state["providerRequestsUsed"]==1 and state["providerCreditsReserved"]==2 and not state["connected"]
    with pytest.raises(Refused,match="budget"):guard("GET","https://api.fansapi.com/api/accounts",True)
    assert prep.view(store,TENANT,account)["providerRequestsUsed"]==1
    store.close()

def test_identity_assertion_mismatch_does_not_create_connection(database):
    store,account,cfg=setup(database)
    with pytest.raises(Refused):
        prep.prepare_one(store,TENANT,account,client_factory=factory(lambda req:identity(config(2))))
    with Session(database[1]) as s:assert s.get(OfFansConnection,(account,1)) is None
    assert prep.view(store,TENANT,account)["providerRequestsUsed"]==1
    store.close()

def test_stop_during_page_retains_cursor_and_rejects_stale_apply_and_retry(database):
    store,account,cfg=setup(database);calls=[]
    def wire(req):
        calls.append(req.url.path)
        if req.url.path=="/api/accounts":return identity(cfg)
        prep.stop(store,TENANT,account,expected_revision=1,command_key="b"*64,actor=ACTOR,reason="Stop in-flight fixture read")
        return page([_item(777)])
    result=prep.prepare_one(store,TENANT,account,client_factory=factory(wire))
    assert result["vault"]["error"]=="preparation_authorization_changed"
    with Session(database[1]) as s:
        assert s.get(VaultItem,(account,777)) is None
        job=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account))
        assert job.page_offset==0 and job.pages_read==0 and job.lease_token is None and job.status=="pending"
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="stopped"
    assert len(calls)==2
    with pytest.raises(Refused):prep.PreparationRequestGuard(store,TENANT,account,1)("GET","https://api.fansapi.com/api/accounts",True)
    store.close()

def test_overlapping_workers_do_not_duplicate_reads_or_hold_other_accounts(database):
    first,account,cfg=setup(database);second,other,other_cfg=setup(database,2)
    entered=threading.Event();release=threading.Event();calls=[]
    def wire(req):
        calls.append(req.url.path)
        if req.url.path=="/api/accounts":return identity(cfg)
        entered.set();assert release.wait(10);return page([_item(701)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        running=pool.submit(prep.prepare_one,first,TENANT,account,client_factory=factory(wire))
        assert entered.wait(10)
        try:
            assert prep.prepare_one(first,TENANT,account,client_factory=factory(wire))["state"]=="busy"
            def other_wire(req):return identity(other_cfg) if req.url.path=="/api/accounts" else page([_item(702)])
            assert prep.prepare_one(second,TENANT,other,client_factory=factory(other_wire))["state"]=="complete"
        finally:release.set()
        assert running.result(timeout=10)["state"]=="complete"
    assert len(calls)==2
    with Session(database[1]) as s:
        assert s.get(VaultItem,(account,702)) is None and s.get(VaultItem,(other,701)) is None
    first.close();second.close()

def test_resume_same_job_and_budget_after_restart_and_failed_page(database):
    store,account,cfg=setup(database);offsets=[];failed=False
    def wire(req):
        nonlocal failed
        if req.url.path=="/api/accounts":return identity(cfg)
        offset=int(req.url.params["offset"]);offsets.append(offset)
        if offset==0:return page([_item(800-i) for i in range(24)],True)
        if not failed:failed=True;return answer(503,{})
        return page([_item(500)])
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="partial"
    job_id=prep.view(store,TENANT,account)["vault"]["jobId"]
    store.close();store=Store(database[0])
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["vault"]["error"]=="vault_read_failed"
    # R1: the failed page persisted a due time; an immediate pass must not read again. Move the clock past it.
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="delayed" and offsets==[0,24]
    from datetime import timedelta
    from onlyfans_runtime.store import utcnow
    with Session(database[1]) as s,s.begin():
        s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account)).retry_not_before=utcnow()-timedelta(seconds=1)
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="complete"
    state=prep.view(store,TENANT,account)
    assert offsets==[0,24,24] and state["vault"]["jobId"]==job_id and state["vault"]["selected"]==25
    assert state["providerRequestsUsed"]==6 and state["providerCreditsReserved"]==12
    store.close()

def test_preparation_hold_blocks_actual_delivery_admission_and_policy_enable(database):
    store,account,cfg=setup(database)
    with pytest.raises(Refused,match="preparation"):
        TextAuthority(store).configure(TENANT,account,expected_revision=1,enabled=True,media_enabled=False,
            limit_units=1,throttle_seconds=1,transport_kind="fansapi",actor=ACTOR,reason="Must stay disabled")
    with Session(database[1]) as s,s.begin():
        # Exercise common send admission even if runtime were accidentally enabled.
        s.get(OfRuntime,account).enabled=True;s.flush()
        with pytest.raises(Refused,match="preparation"):store._delivery_runtime(s,TENANT,account)
    prep.stop(store,TENANT,account,expected_revision=1,command_key="b"*64,actor=ACTOR,reason="Stop preparation")
    with Session(database[1]) as s,s.begin():
        with pytest.raises(Refused,match="preparation"):store._delivery_runtime(s,TENANT,account)
    store.close()

def test_database_guard_retains_counters_identity_and_prohibits_activation(database):
    store,account,cfg=setup(database)
    guard=prep.PreparationRequestGuard(store,TENANT,account,1);guard("GET","https://api.fansapi.com/api/accounts",True)
    for sql in (
        "UPDATE of_engine_pilot_scope SET provider_requests_used=0",
        "UPDATE of_engine_pilot_scope SET status='active',active_since=now(),revision=revision+1",
        "UPDATE of_engine_pilot_scope SET config_json=replace(config_json,'acct_prep_1','acct_prep_2'),revision=revision+1",
        "DELETE FROM of_engine_pilot_scope"):
        with pytest.raises(DBAPIError):
            with database[1].begin() as c:c.execute(text(sql))
    with database[1].connect() as c:check_schema(c)
    replay=prep.configure(store,TENANT,account,cfg,expected_revision=0,command_key="a"*64,actor=ACTOR,reason="Explicit fixture read ceiling")
    assert replay["replayed"]
    reduced=copy.deepcopy(cfg);reduced["limits"]["provider_credit_budget"]=1;reduced["limits"]["provider_credits_per_request"]=1
    with pytest.raises(Refused,match="reservations"):
        prep.configure(store,TENANT,account,reduced,expected_revision=1,command_key="c"*64,actor=ACTOR,reason="Cannot reset spent credits")
    assert prep.view(store,TENANT,account)["providerRequestsUsed"]==1
    store.close()


def test_preparation_evidence_refuses_lossy_schema_downgrade(database):
    from alembic import command
    from db.schema import _config
    store,account,cfg=setup(database)
    with pytest.raises(RuntimeError,match="Preserve preparation"):
        with database[1].begin() as c:
            command.downgrade(_config(c),"0131_manual_category_prices")
    with database[1].connect() as c:check_schema(c)
    store.close()


def test_real_preparation_requires_private_explicit_ten_account_registry_without_io(tmp_path,monkeypatch):
    from conversation_engine.preparation import approved_entries,production_factory
    from fans_api_adapter import real_transport
    from unittest.mock import Mock
    with pytest.raises(Refused,match="disabled"):approved_entries({})
    with pytest.raises(Refused,match="registry"):approved_entries({"TWIN_FANSAPI_PREPARATION_REAL":"1"})
    path=tmp_path/"read-registry.json"
    rows=[{"tenant":TENANT,"account":"prep-"+str(i),"provider_account":"acct_prep_"+str(i),"creator_id":str(100+i),
        "env_file":str(tmp_path/("secret-"+str(i))),"limits":config(i)["limits"]} for i in range(1,11)]
    path.write_text(json.dumps(rows));path.chmod(0o600)
    env={"TWIN_FANSAPI_PREPARATION_REAL":"1","TWIN_FANSAPI_PREPARATION_REGISTRY":str(path)}
    assert len(approved_entries(env))==10
    path.chmod(0o644)
    with pytest.raises(Refused,match="private"):approved_entries(env)
    path.chmod(0o600);path.write_text(json.dumps(rows+[rows[0]]))
    with pytest.raises(Refused,match="ten"):approved_entries(env)
    path.write_text(json.dumps([rows[0],{**rows[1],"creator_id":rows[0]["creator_id"]}]))
    with pytest.raises(Refused,match="duplicate"):approved_entries(env)
    no_secret=Mock(side_effect=AssertionError("No secret or wire on unapproved configuration"))
    monkeypatch.setattr(real_transport,"load_real_config",no_secret)
    cfg=config();cfg["qualification"]["mode"]="production"
    entry=rows[0]
    cfg["limits"]["provider_credit_budget"]+=1
    with pytest.raises(Refused,match="ceilings"):production_factory(entry)(cfg,1,None)
    cfg=config()
    with pytest.raises(Refused,match="approval"):production_factory(entry)(cfg,1,None)
    no_secret.assert_not_called()

def test_new_disabled_account_selects_fansapi_explicitly_without_native_credentials(database):
    url,engine=database
    store=seed(url,engine,account="fresh",tenant=TENANT)
    with Session(engine) as s,s.begin():s.get(OfRuntime,"fresh").enabled=False
    assert prep.view(store,TENANT,"fresh")=={"configured":False,"canPrepare":True}
    result=prep.configure(store,TENANT,"fresh",config(),expected_revision=0,command_key="a"*64,actor=ACTOR,reason="Explicit read-only FansAPI selection")
    assert result["configured"] and result["revision"]==1
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    with Session(engine) as s:
        policy=s.get(OfDeliveryPolicy,"fresh")
        assert policy.transport_kind=="fansapi" and not policy.enabled and not policy.media_enabled and policy.limit_units==0
    store.close()


def test_existing_worker_preparation_mode_defaults_off_without_native_key_or_sender(database,monkeypatch):
    import onlyfans_worker as worker
    store,account,cfg=setup(database);store.close()
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");monkeypatch.setenv("ONLYFANS_WORKER_TENANT",TENANT)
    monkeypatch.setenv("DATABASE_URL",database[0]);monkeypatch.delenv("ONLYFANS_CONNECTION_KEY",raising=False)
    monkeypatch.delenv("TWIN_FANSAPI_PREPARATION_REAL",raising=False)
    emitted=[];monkeypatch.setattr(worker,"_emit",emitted.append)
    assert worker.main(["--once","--preparation-only"])==1
    assert emitted==[{"schemaVersion":"of-preparation.v1","state":"unavailable"}]
    with Session(database[1]) as s:
        row=s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id==account))
        assert row.provider_requests_used==0 and s.get(OfFansConnection,(account,1)) is None


def test_explicit_refresh_is_not_hidden_by_an_older_completed_import(database):
    from onlyfans_runtime.vault_management import request_sync
    store,account,cfg=setup(database);calls=[];mid=501
    def wire(req):
        calls.append(req.url.path)
        return identity(cfg) if req.url.path=="/api/accounts" else page([_item(mid)])
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="complete"
    old=prep.view(store,TENANT,account)["vault"]["jobId"]
    queued=request_sync(store,TENANT,account,expected_connection_revision=1,command_key="e"*64,actor=ACTOR)
    mid=502
    result=prep.prepare_one(store,TENANT,account,client_factory=factory(wire))
    assert result["state"]=="complete" and result.get("vault",{}).get("jobId")==queued["jobId"]
    assert queued["jobId"]!=old and len(calls)==4
    with Session(database[1]) as s:
        assert s.get(VaultItem,(account,501)) is not None
        job=s.get(OfVaultSyncJob,(account,queued["jobId"]))
        assert json.loads(job.window_json)["selected"]==["502"]
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="complete"
    assert len(calls)==4
    store.close()

def test_failed_import_needs_explicit_new_sync_command_instead_of_automatic_read_loop(database):
    from onlyfans_runtime.vault_management import request_sync
    store,account,cfg=setup(database);calls=[];bad=True
    def wire(req):
        calls.append(req.url.path)
        if req.url.path=="/api/accounts":return identity(cfg)
        if bad:
            a,b=_item(501),_item(502)
            a["createdAt"]="2026-01-01T00:00:00Z";b["createdAt"]="2026-02-01T00:00:00Z"
            return page([a,b])
        return page([_item(502)])
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["vault"]["error"]=="vault_order_or_replay"
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["state"]=="held"
    assert len(calls)==2
    queued=request_sync(store,TENANT,account,expected_connection_revision=1,command_key="e"*64,actor=ACTOR)
    bad=False
    assert prep.prepare_one(store,TENANT,account,client_factory=factory(wire))["vault"]["jobId"]==queued["jobId"]
    assert len(calls)==4
    store.close()
