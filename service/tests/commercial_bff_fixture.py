"""Loopback-only real BFF/backend qualification against a uniquely owned PG DB."""
import json,os
import pytest,uvicorn
from fastapi import FastAPI
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_vault_pg import setup
from test_real_transport_pg import _item
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.models import OfCreatorBinding,VaultFolder,VaultFolderItem,VaultItem,CatalogItem
from db.onlyfans_models import OfRuntime
from onlyfans_read.agency_scope import AgencyScopeMiddleware
from onlyfans_commands.vault_api import router
from onlyfans_commands.configuration_api import router as configuration_router
from onlyfans_read.api import router as reads
from fans_api_adapter.vault_sync import sync_vault
CREATOR="11111111-1111-4111-8111-111111111111"
db=database.__wrapped__();owned=next(db)
store,client,calls,state=setup(owned,[_item(11),_item(12,type="video")])
assert sync_vault(store,TENANT,ACCOUNT,client)["complete"]
with Session(store.engine) as s,s.begin():
    runtime=s.get(OfRuntime,ACCOUNT);runtime.enabled=False;s.get(OfCreatorBinding,runtime.binding_id).creator_id=CREATOR
    folder=VaultFolder(account_id=ACCOUNT,name="Scenic",created_by="operator");s.add(folder);s.flush();fid=folder.id
    for mid in (11,12):
        s.add(VaultFolderItem(account_id=ACCOUNT,folder_id=fid,media_id=mid))
        s.get(VaultItem,(ACCOUNT,mid)).description="A scenic photograph." if mid==11 else "A scenic video."
from onlyfans_runtime.text_delivery import TextAuthority
from db.onlyfans_delivery_models import OfDeliveryPolicy
with Session(store.engine) as s:policy=s.get(OfDeliveryPolicy,ACCOUNT);revision=policy.revision;units=policy.limit_units
TextAuthority(store).configure(TENANT,ACCOUNT,expected_revision=revision,enabled=False,media_enabled=False,
    limit_units=units,throttle_seconds=1,transport_kind="fansapi",actor="op-session:"+"a"*64,reason="Disabled isolated BFF preparation fixture")
os.environ.update(ONLYFANS_PROCESS_ROLE="api",ONLYFANS_API_TOKEN="synthetic-read",ONLYFANS_API_TENANT=TENANT,
    ONLYFANS_CONTENT_TOKEN="synthetic-content",ONLYFANS_CONTENT_TENANT=TENANT,
    ONLYFANS_CONFIGURATION_TOKEN="synthetic-configuration",ONLYFANS_CONFIGURATION_TENANT=TENANT)
app=FastAPI();app.state.control_store=store;app.add_middleware(AgencyScopeMiddleware)
app.include_router(router);app.include_router(reads);app.include_router(configuration_router)
preparation_reads=[]
@app.post("/fixture/prepare")
def prepare():
    import httpx
    from conversation_engine.preparation import prepare_one
    from fans_api_adapter.real_transport import build_client,RealFansConfig
    from test_engine_real_worker_pg import answer
    def factory(config,epoch,guard):
        def wire(req):
            preparation_reads.append((req.method,req.url.path))
            if req.url.path=="/api/accounts":
                return answer(200,[{"id":config["provider_account"],"onlyfans_id":config["creator_id"],"is_authenticated":True}])
            return answer(200,{"data":{"list":[_item(11),_item(12,type="video")],"hasMore":False}})
        return build_client(RealFansConfig("synthetic-preparation",config["provider_account"],"https://api.fansapi.com",5),
            creator_id=config["creator_id"],epoch=epoch,transport=httpx.MockTransport(wire),request_guard=guard)
    return prepare_one(store,TENANT,ACCOUNT,client_factory=factory)
@app.get("/fixture")
def fixture():
    with Session(store.engine) as s:
        products=[{"price":p.price_cents,"enabled":p.enabled,"media":json.loads(p.media_ids)} for p in s.scalars(select(CatalogItem))]
    return {"account":ACCOUNT,"folder":str(fid),"creator":CREATOR,"providerAccount":client.account,"providerCreator":client.creator_id,"products":products,"providerReads":len(calls)+len(preparation_reads)}
@app.post("/fixture/change-asset")
def change():
    with Session(store.engine) as s,s.begin():s.get(VaultItem,(ACCOUNT,11)).description="A newly corrected description."
    return {"ok":True}
if __name__=="__main__":
    try:uvicorn.run(app,host="0.0.0.0",port=8787,log_level="warning")
    finally:store.close();db.close()
