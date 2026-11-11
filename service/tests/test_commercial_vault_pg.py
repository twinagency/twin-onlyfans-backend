"""Isolated PostgreSQL, explicit mock provider; no paid calls, grants or purchases."""
from datetime import datetime,timedelta,timezone
import json
import httpx
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from db.models import VaultItem,VaultFolder,VaultFolderItem,VaultSend
from db.onlyfans_vault_models import OfVaultSyncJob,OfVaultObservation
from db.onlyfans_connection_models import OfProviderConnection
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import configured,TENANT,ACCOUNT,FAN
from test_real_transport_pg import real_policy,_item
from fake_fans_server import ACCT,CREATOR
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.real_transport import build_client,RealFansConfig
from fans_api_adapter.vault_sync import sync_vault
from onlyfans_runtime.vault_management import list_vault,create_category_drafts
from onlyfans_runtime.vault_editing import get_category_review
from onlyfans_runtime.store import utcnow,Refused
from ownership import owned_media_in_session

from test_engine_real_worker_pg import answer

ACTOR="op-session:"+"a"*64

def setup(database,items):
    store,authority=configured(database,mode="automated")
    real_policy(database,authority);FansWebhookAdapter(store).bind_simulated(TENANT,ACCOUNT,ACCT,CREATOR)
    calls=[];state={"fail":False}
    def handler(request):
        if request.url.path=="/api/accounts":
            return answer(200,[{"id":ACCT,"onlyfans_id":CREATOR,"is_authenticated":True}])
        query=request.url.params;offset=int(query["offset"]);limit=int(query["limit"])
        assert query["field"]=="recent" and query["sort"]=="desc"
        calls.append(offset)
        if state["fail"]:return answer(503,{})
        return answer(200,{"data":{"list":items[offset:offset+limit],"hasMore":offset+limit<len(items)}})
    client=build_client(RealFansConfig("synthetic",ACCT,"https://api.fansapi.com",5),
        creator_id=CREATOR,epoch=1,transport=httpx.MockTransport(handler))
    return store,client,calls,state


def selection(store):
    with Session(store.engine) as s:
        job=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==ACCOUNT,
            OfVaultSyncJob.status=="complete").order_by(OfVaultSyncJob.completed_at.desc()).limit(1))
        return json.loads(job.window_json)["selected"]


def test_combined_unique_window_resumes_ties_and_preserves_old_ownership(database):
    now=datetime(2026,11,1,tzinfo=timezone.utc)
    items=[_item(10000+i,type="video" if i%3 else "photo",
        createdAt=(now-timedelta(seconds=i//5)).isoformat()) for i in range(1050)]
    # Repeated provider rows may overlap pages, without occupying extra selection slots.
    items.insert(23,dict(items[22]))
    store,client,calls,state=setup(database,items)
    with Session(store.engine) as s,s.begin():
        s.add(VaultItem(account_id=ACCOUNT,media_id=1,kind="photo",description="Older human correction",
            created_at=utcnow()-timedelta(days=500),locked_fields_json='["description"]'))
        s.add(VaultSend(account_id=ACCOUNT,fan_id=FAN,media_id=1,message_id=99,
            price_cents=500,was_purchased=True))
    first=sync_vault(store,TENANT,ACCOUNT,client,max_pages=2,command_key="a"*64)
    assert not first["complete"] and first["error"]=="page_budget_exhausted"
    assert calls==[0,24] and selection_pending(store)==[]
    second=sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)
    assert second["complete"] and calls[2]==48
    expected=sorted({(datetime.fromisoformat(r["createdAt"]),r["id"]) for r in items},reverse=True)[:1000]
    assert selection(store)==[str(mid) for _,mid in expected]
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfProviderConnection))==0
        assert s.get(VaultItem,(ACCOUNT,1)).description=="Older human correction"
        assert owned_media_in_session(s,ACCOUNT,FAN)=={1}
        assert s.scalar(select(func.count()).select_from(OfVaultSyncJob))==1
        assert s.scalar(select(func.count()).select_from(OfVaultObservation))>=1000
    before=len(calls)
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)["complete"]
    assert len(calls)==before
    store.close()


def selection_pending(store):
    with Session(store.engine) as s:
        job=s.scalar(select(OfVaultSyncJob));return json.loads(job.window_json)["selected"]


def test_failed_page_keeps_cursor_and_does_not_invent_readiness(database):
    store,client,calls,state=setup(database,[_item(11),_item(12,isReady=False)])
    state["fail"]=True
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)["error"]=="vault_read_failed"
    assert selection_pending(store)==[]
    state["fail"]=False
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)["complete"]
    assert calls==[0,0]
    result=list_vault(store,TENANT,ACCOUNT)
    assert [(r["mediaId"],r["ready"]) for r in result["items"]]==[("11",True),("12",False)]
    assert result["sync"]["scope"]["combined"]==1000
    store.close()


def test_fansapi_shared_provenance_supports_reviewed_disabled_drafts_without_native_proof(database):
    store,client,calls,state=setup(database,[_item(11),_item(12,type="video")])
    assert sync_vault(store,TENANT,ACCOUNT,client)["complete"]
    with Session(store.engine) as s,s.begin():
        folder=VaultFolder(account_id=ACCOUNT,name="Scenic",created_by=ACTOR);s.add(folder);s.flush()
        fid=folder.id;s.add_all([VaultFolderItem(account_id=ACCOUNT,folder_id=fid,media_id=mid) for mid in (11,12)])
    from onlyfans_runtime.vault_editing import get_category_price,set_category_price
    current=get_category_price(store,TENANT,ACCOUNT,fid)
    set_category_price(store,TENANT,ACCOUNT,fid,price_cents=1200,expected_fingerprint=current["fingerprint"],
        command_key="0"*64,actor=ACTOR)
    review=get_category_review(store,TENANT,ACCOUNT,fid,["11","12"])
    category={"folderId":fid,"priceCents":1200,"categoryPriceRevision":1,"classificationConfirmed":True,"rightsConfirmed":True,
        "reviewedState":review,"products":[{"label":"Mixed scenic set","description":"An approved photograph and video.",
            "mediaIds":["11","12"],"previewMediaIds":[],"approvedTotalCents":2000}]}
    result=create_category_drafts(store,TENANT,ACCOUNT,category=category,command_key="b"*64,actor=ACTOR)
    assert len(result["items"])==1 and result["items"][0]["item"]["enabled"] is False
    from onlyfans_runtime.content_management import get_content_v2,update_content
    from onlyfans_runtime.media_delivery import catalog_terms
    item=int(result["items"][0]["itemId"]);current=get_content_v2(store,TENANT,ACCOUNT,item)
    update_content(store,TENANT,ACCOUNT,item,patch={"enabled":True},expected_revision=current["revision"],
        expected_fingerprint=current["fingerprint"],command_key="c"*64,actor=ACTOR,version=2)
    with Session(store.engine) as s:
        terms=catalog_terms(s,ACCOUNT,item)
        assert terms["media_ids"]==[11,12] and terms["price_cents"]==2000
        job=s.scalar(select(OfVaultSyncJob))
        assert job.provider_kind=="fansapi" and job.provider_account==ACCT
        assert s.scalar(select(func.count()).select_from(OfProviderConnection))==0
    store.close()


def test_out_of_order_provider_page_cannot_qualify_a_selection(database):
    store,client,calls,state=setup(database,[_item(11,createdAt="2026-10-01T00:00:00+00:00"),_item(12)])
    result=sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)
    assert not result["complete"] and result["error"]=="vault_order_or_replay"
    with Session(store.engine) as s:assert s.scalar(select(func.count()).select_from(VaultItem))==0
    store.close()


def test_real_unbudgeted_wire_is_refused_before_any_read(database):
    from fans_api_adapter.https_transport import FansHttpsTransport
    store,client,calls,state=setup(database,[_item(11)])
    client._http=FansHttpsTransport(enabled=True)
    with pytest.raises(Refused,match="budgeted"):sync_vault(store,TENANT,ACCOUNT,client)
    assert not calls
    store.close()


def test_bff_sync_command_queues_the_same_fansapi_job_and_cannot_switch_provider(database):
    from onlyfans_runtime.vault_management import request_sync
    from sqlalchemy.exc import DBAPIError
    store,client,calls,state=setup(database,[_item(11)])
    queued=request_sync(store,TENANT,ACCOUNT,expected_connection_revision=1,command_key="d"*64,actor=ACTOR)
    assert queued["status"]=="pending" and calls==[]
    completed=sync_vault(store,TENANT,ACCOUNT,client)
    assert completed["jobId"]==queued["jobId"] and completed["complete"]
    with pytest.raises(DBAPIError),store.engine.begin() as c:
        c.execute(text("UPDATE of_vault_sync_job SET provider_kind='native',provider_account=NULL"))
    assert request_sync(store,TENANT,ACCOUNT,expected_connection_revision=1,command_key="d"*64,actor=ACTOR)["status"]=="complete"
    store.close()


def test_uncertain_classification_has_reason_and_no_automatic_sales_approval(database):
    store,client,calls,state=setup(database,[_item(11)])
    assert sync_vault(store,TENANT,ACCOUNT,client)["complete"]
    with Session(store.engine) as s,s.begin():
        item=s.get(VaultItem,(ACCOUNT,11))
        saved=json.loads(item.ai_fields_json)["_commercial_classification_v1"]
        assert saved["status"]=="uncertain" and saved["reason"]=="analysis_pending"
        assert saved["sale_approved"] is False
        folder=s.scalar(select(VaultFolder))
        assert folder.name=="Miscellaneous"
        folder.name="Renamed without reclassifying";fid=folder.id
    with pytest.raises(Refused,match="uncertain category"):
        get_category_review(store,TENANT,ACCOUNT,fid,["11"])
    store.close()


def test_populated_native_upgrade_preserves_legacy_job_metadata_and_ownership(database,monkeypatch):
    from test_onlyfans_vault_pg import sync
    from alembic import command
    from db.schema import _config,check_schema,upgrade,RECONNECT_RECEIPT
    store,account,job,calls=sync(database,monkeypatch)
    with Session(store.engine) as s,s.begin():
        item=s.get(VaultItem,(account,11));item.description="Preserved operator correction"
        s.add(VaultSend(account_id=account,fan_id=101,media_id=11,message_id=90,
            price_cents=500,was_purchased=True))
        folder=VaultFolder(account_id=account,name="Existing native folder",created_by=ACTOR)
        s.add(folder);s.flush();fid=folder.id
    # The two new migrations can leave a legacy-only DB, then upgrade that populated old revision.
    with store.engine.begin() as c:
        command.downgrade(_config(c),RECONNECT_RECEIPT)
        check_schema(c,expected=RECONNECT_RECEIPT)
        upgrade(c);check_schema(c)
    with Session(store.engine) as s:
        assert s.get(VaultItem,(account,11)).description=="Preserved operator correction"
        assert owned_media_in_session(s,account,101)=={11}
        legacy=s.get(OfVaultSyncJob,(account,job["jobId"]))
        assert legacy.provider_kind=="native" and legacy.provider_account is None and legacy.status=="complete"
        folder=s.get(VaultFolder,fid)
        assert folder.price_revision==0 and folder.single_price_cents is None
    store.close()


def test_rollback_refuses_to_discard_recorded_fansapi_provenance(database):
    from alembic import command
    from db.schema import _config,check_schema,RECONNECT_RECEIPT
    store,client,calls,state=setup(database,[_item(11)])
    assert sync_vault(store,TENANT,ACCOUNT,client)["complete"]
    with pytest.raises(RuntimeError,match="Preserve provider observations"),store.engine.begin() as c:
        command.downgrade(_config(c),RECONNECT_RECEIPT)
    with store.engine.begin() as c:check_schema(c)
    store.close()


@pytest.mark.parametrize("confidence,stamped,reason",[(None,True,"classification_confidence_unknown"),
    (40,True,"low_confidence"),(99,False,"analysis_version_unbound"),(99,True,"metadata_evidence")])
def test_description_refresh_keeps_commercial_classification_contract(database,confidence,stamped,reason):
    from vault_auto_folders import assign_sync
    from vault_describe_state import stamp_of
    store,client,calls,state=setup(database,[_item(11)])
    assert sync_vault(store,TENANT,ACCOUNT,client)["complete"]
    with Session(store.engine) as s,s.begin():
        item=s.get(VaultItem,(ACCOUNT,11));fields=json.loads(item.ai_fields_json)
        fields.update({"description":"A bikini selfie.","confidence":confidence,
            "_content_stamp":stamp_of(item) if stamped else "prior-content"})
        item.ai_fields_json=json.dumps(fields)
        assign_sync(s,ACCOUNT,11)
        classification=json.loads(item.ai_fields_json)["_commercial_classification_v1"]
        assert classification["reason"]==reason and classification["sale_approved"] is False
        assert classification["status"]==("proposed" if reason=="metadata_evidence" else "uncertain")
        folders=list(s.scalars(select(VaultFolder.name).join(VaultFolderItem,
            (VaultFolderItem.account_id==VaultFolder.account_id)&(VaultFolderItem.folder_id==VaultFolder.id))
            .where(VaultFolderItem.account_id==ACCOUNT,VaultFolderItem.media_id==11)))
        assert ("Miscellaneous" in folders)==(reason!="metadata_evidence")
    store.close()
