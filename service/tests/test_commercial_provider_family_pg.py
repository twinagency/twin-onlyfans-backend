"""Provider family switches use existing owners; real PG, synthetic read boundaries."""
import asyncio
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_vault_pg import sync,queue,page,media
from test_onlyfans_connections_pg import TENANT,ACTOR
from test_onlyfans_provider_adapters import OWNER
from test_engine_real_worker_pg import answer
from test_real_transport_pg import _item
from db.models import VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime.text_delivery import TextAuthority
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.vault_management import process_vault_jobs
from onlyfans_runtime.vault_description_worker import accounts
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.real_transport import build_client,RealFansConfig
from fans_api_adapter.vault_sync import sync_vault


def select_fans(store,account,tenant=TENANT):
    with Session(store.engine) as s:
        policy=s.get(OfDeliveryPolicy,account);revision=policy.revision if policy else 0
    TextAuthority(store).configure(tenant,account,expected_revision=revision,enabled=False,media_enabled=False,
        limit_units=20,throttle_seconds=1,transport_kind="fansapi",actor=ACTOR,reason="Explicit isolated provider family selection")


def fans_client(store,account):
    provider="acct_family_fixture"
    FansWebhookAdapter(store).bind_simulated(TENANT,account,provider,str(OWNER))
    def wire(request):
        if request.url.path=="/api/accounts":
            return answer(200,[{"id":provider,"onlyfans_id":str(OWNER),"is_authenticated":True}])
        return answer(200,{"data":{"list":[_item(11),_item(12)],"hasMore":False}})
    client=build_client(RealFansConfig("synthetic-family",provider,"https://api.fansapi.com",5),
        creator_id=str(OWNER),epoch=1,transport=httpx.MockTransport(wire))
    return client


def import_fans(store,account):
    client=fans_client(store,account)
    assert sync_vault(store,TENANT,account,client)["complete"]
    return client


def test_native_description_scheduler_cannot_adopt_fansapi_observations(database,monkeypatch):
    store,account,_,_=sync(database,monkeypatch)
    select_fans(store,account);import_fans(store,account)
    rows,_=accounts(store,TENANT)
    assert rows==[],"FansAPI media must not schedule the native credential-based caption child"
    store.close()


def test_queued_native_vault_job_cannot_read_after_explicit_fansapi_selection(database,monkeypatch):
    store,account,_,_=sync(database,monkeypatch)
    queue(store,account,"e"*64);select_fans(store,account)
    calls=[]
    def read(self,**kwargs):calls.append(kwargs);return page([media(99)])
    monkeypatch.setattr(NativeProviderConnector,"vault_page",read)
    assert process_vault_jobs(store,TENANT)==0
    assert calls==[],"A retained native credential is not a selected provider"
    with Session(store.engine) as s:assert s.get(VaultItem,(account,99)) is None
    store.close()


def test_native_page_in_flight_is_not_applied_after_family_switch(database,monkeypatch):
    store,account,_,_=sync(database,monkeypatch)
    job=queue(store,account,"e"*64);calls=[]
    def read(self,**kwargs):
        assert store.engine.pool.checkedout()==0
        calls.append(kwargs);select_fans(store,account)
        return page([media(99)])
    monkeypatch.setattr(NativeProviderConnector,"vault_page",read)
    assert process_vault_jobs(store,TENANT)==0
    assert len(calls)==1
    with Session(store.engine) as s:
        assert s.get(VaultItem,(account,99)) is None
        row=s.get(OfVaultSyncJob,(account,job["jobId"]))
        assert row.status=="superseded" and row.last_error=="connection_changed"
    store.close()


def test_native_read_budget_cannot_admit_a_retained_native_credential_for_fansapi(database,monkeypatch):
    from onlyfans_runtime.provider_read_budget import acquire
    from onlyfans_runtime.store import Refused
    store,account,_,_=sync(database,monkeypatch);select_fans(store,account)
    with pytest.raises(Refused,match="native provider family"):
        acquire(store,TENANT,account,connection_revision=1,kind="vault")
    store.close()


def test_explicit_fansapi_sync_supersedes_obsolete_native_job_without_native_read(database,monkeypatch):
    from onlyfans_runtime.vault_management import request_sync
    store,account,_,calls=sync(database,monkeypatch)
    prior=queue(store,account,"e"*64);select_fans(store,account);client=fans_client(store,account)
    queued=request_sync(store,TENANT,account,expected_connection_revision=1,command_key="f"*64,actor=ACTOR)
    assert sync_vault(store,TENANT,account,client)["jobId"]==queued["jobId"]
    assert calls==[(0,24)]
    with Session(store.engine) as s:
        old=s.get(OfVaultSyncJob,(account,prior["jobId"]))
        new=s.get(OfVaultSyncJob,(account,queued["jobId"]))
        assert old.status=="superseded" and old.provider_kind=="native" and old.provider_account is None
        assert new.status=="complete" and new.provider_kind=="fansapi"
    store.close()


def test_stale_native_caption_child_refuses_before_native_client_creation(database,monkeypatch):
    from test_automatic_vault_descriptions_pg import setup,TENANT as CAPTION_TENANT
    from native_pilot.tools import describe_imported_vault as child
    from native_pilot import connection
    from onlyfans_runtime.store import Store,Refused
    args,_,engine,_=setup(database,monkeypatch)
    store=Store(database[0]);select_fans(store,args.account,tenant=CAPTION_TENANT);store.close()
    calls=[]
    def no_native(*a,**k):calls.append(True);raise AssertionError("Must refuse before constructing native client")
    monkeypatch.setattr(connection,"build_client",no_native)
    async def run():
        try:
            with pytest.raises(Refused,match="native provider family"):await child.run(args)
        finally:await engine.dispose()
    asyncio.run(run());assert not calls


def test_native_read_only_cohort_excludes_selected_fansapi_accounts(database,monkeypatch):
    from onlyfans_runtime.connections import load_worker_cohort
    store,account,_,_=sync(database,monkeypatch);select_fans(store,account)
    rows,_,scanned=load_worker_cohort(store,TENANT,include_read_only=True)
    assert rows==[] and scanned==0
    store.close()
