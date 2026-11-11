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
from onlyfans_read.api import router as reads
from fans_api_adapter.vault_sync import sync_vault
CREATOR="11111111-1111-4111-8111-111111111111"
db=database.__wrapped__();owned=next(db)
store,client,calls,state=setup(owned,[_item(11),_item(12,type="video")])
assert sync_vault(store,TENANT,ACCOUNT,client)["complete"]
with Session(store.engine) as s,s.begin():
    runtime=s.get(OfRuntime,ACCOUNT);s.get(OfCreatorBinding,runtime.binding_id).creator_id=CREATOR
    folder=VaultFolder(account_id=ACCOUNT,name="Scenic",created_by="operator");s.add(folder);s.flush();fid=folder.id
    for mid in (11,12):
        s.add(VaultFolderItem(account_id=ACCOUNT,folder_id=fid,media_id=mid))
        s.get(VaultItem,(ACCOUNT,mid)).description="A scenic photograph." if mid==11 else "A scenic video."
os.environ.update(ONLYFANS_PROCESS_ROLE="api",ONLYFANS_API_TOKEN="synthetic-read",ONLYFANS_API_TENANT=TENANT,
    ONLYFANS_CONTENT_TOKEN="synthetic-content",ONLYFANS_CONTENT_TENANT=TENANT)
app=FastAPI();app.state.control_store=store;app.add_middleware(AgencyScopeMiddleware)
app.include_router(router);app.include_router(reads)
@app.get("/fixture")
def fixture():
    with Session(store.engine) as s:
        products=[{"price":p.price_cents,"enabled":p.enabled,"media":json.loads(p.media_ids)} for p in s.scalars(select(CatalogItem))]
    return {"account":ACCOUNT,"folder":str(fid),"creator":CREATOR,"products":products,"providerReads":len(calls)}
@app.post("/fixture/change-asset")
def change():
    with Session(store.engine) as s,s.begin():s.get(VaultItem,(ACCOUNT,11)).description="A newly corrected description."
    return {"ok":True}
if __name__=="__main__":
    try:uvicorn.run(app,host="0.0.0.0",port=8787,log_level="warning")
    finally:store.close();db.close()
