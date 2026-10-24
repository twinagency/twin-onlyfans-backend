"""Canonical native workspace reads, disposable PostgreSQL and neutral fixtures."""
import asyncio
from datetime import timedelta
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select,text,event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,seed
from test_onlyfans_media_pg import shelf
from onlyfans_runtime.store import utcnow
from onlyfans_read.workspace import read_workspace,cursor_for
from onlyfans_read.errors import BadRequest,Unavailable
from db.models import AccountAiConfig,CatalogItem,CatalogScript,ContentOffer,Transaction,OfCreatorBinding


def read(database,section,**kwargs):
    async def run():
        engine=create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"))
        try:
            async with async_sessionmaker(engine)() as s:
                async with s.begin():
                    return await read_workspace(s,tenant_id=kwargs.pop("tenant_id","tenant-a"),
                        account_id=kwargs.pop("account_id","acc-a"),section=section,**kwargs)
        finally:await engine.dispose()
    return asyncio.run(run())


def test_catalogue_uses_native_terms_and_bounded_account_cursor_without_writes(database):
    store=seed(*database);other=seed(*database,account="acc-b",tenant="tenant-b")
    item=shelf(database[1]);shelf(database[1],account="acc-b")
    with Session(database[1]) as s,s.begin():
        for i in range(31):
            s.add(CatalogItem(account_id="acc-a",label=f"Synthetic catalogue {i}",kind="image",media_ids="[11]",
                preview_media_ids="[]",price_cents=500,description_for_ai="A neutral scenic still image.",enabled=False))
        legacy=CatalogScript(account_id="acc-a",name="Retained legacy sequence",status="disabled")
        s.add(legacy);s.flush();s.add(CatalogItem(account_id="acc-a",script_id=legacy.id,media_ids="[11]",price_cents=500,label="Hidden legacy sequence"))
    first=read(database,"content")
    assert first["schemaVersion"]=="of-workspace.v2" and first["providerConnectionVerified"] is False
    assert first["historyRecoveryRequired"] is True and first["lastCompleteSyncAt"] is None
    assert len(first["items"])==25 and first["items"][0]["itemId"]==str(item)
    assert first["items"][0]["readiness"]=="terms_checked"
    assert (first["items"][0]["mediaCount"],first["items"][0]["previewCount"],first["items"][0]["priceCents"])==(3,1,1000)
    second=read(database,"content",cursor=first["nextCursor"])
    assert len(second["items"])==7 and second["nextCursor"] is None
    assert set(x["itemId"] for x in first["items"]).isdisjoint(x["itemId"] for x in second["items"])
    assert "Hidden legacy sequence" not in json.dumps([first,second])
    with pytest.raises(BadRequest):read(database,"content",cursor=cursor_for("acc-b","content",1))
    with pytest.raises(BadRequest):read(database,"content",cursor=cursor_for("acc-a","sales",1))
    with Session(database[1]) as s:
        assert s.get(CatalogItem,item).price_cents==1000
        assert s.scalar(select(Transaction.id)) is None
    store.close();other.close()


def test_catalogue_invalid_metadata_is_visible_review_not_free_price_or_valid_terms(database):
    store=seed(*database);item=shelf(database[1])
    with Session(database[1]) as s,s.begin():
        row=s.get(CatalogItem,item);row.price_cents=-1;row.media_ids='[11,11]';row.description_for_ai="x"*2100
    result=read(database,"content")["items"][0]
    assert result["readiness"]=="needs_review" and result["priceCents"] is None and result["mediaCount"] is None
    assert result["descriptionTruncated"] is True and len(result["description"])==2000
    store.close()


def test_sales_preserves_original_currency_status_and_records_no_offer_as_payment(database):
    store=seed(*database);other=seed(*database,account="acc-b",tenant="tenant-b")
    item=shelf(database[1])
    with Session(database[1]) as s,s.begin():
        for index in range(28):s.add(ContentOffer(account_id="acc-a",fan_id=101,item_id=item,mode="ppv",price_cents=800,status="open"))
        for currency,status,amount in [("USD","cleared",1000),("USD","pending",2000),("USD","refunded",500),
                ("EUR","cleared",700),("USD","refund_pending",800)]:
            s.add(Transaction(account_id="acc-a",fan_id=101,kind="ppv_message",amount_cents=amount,currency=currency,
                status=status,occurred_at=utcnow(),raw_json='{"private":"must not cross the read boundary"}',provider_transaction_id="private-provider-"+currency+status))
        s.add(Transaction(account_id="acc-b",kind="tip",amount_cents=999999,currency="USD",status="cleared",occurred_at=utcnow()))
    result=read(database,"sales",limit=2)
    groups={(x["currency"],x["status"]):x["grossCents"] for x in result["totals"]}
    assert groups=={("USD","cleared"):1000,("USD","pending"):2000,("USD","refunded"):500,("EUR","cleared"):700,("USD","refund_pending"):800}
    assert len(result["transactions"])==2
    assert result["openOfferCount"]==28 and len(result["openOffers"])==25 and result["moreOpenOffers"] is True
    assert all(row["messageId"] is None and row["priceCents"]==800 and row["tipUnlockCents"]==0 for row in result["openOffers"])
    remaining=read(database,"sales",limit=2,cursor=result["nextCursor"])
    last=read(database,"sales",limit=2,cursor=remaining["nextCursor"])
    assert len(last["transactions"])==1 and last["nextCursor"] is None
    assert len({x["transactionId"] for page in [result,remaining,last] for x in page["transactions"]})==5
    assert "private" not in json.dumps(result) and "999999" not in json.dumps(result)
    store.close();other.close()


def test_sales_lists_newest_first_even_when_backfill_inserted_newest_rows_first(database):
    store=seed(*database);other=seed(*database,account="acc-b",tenant="tenant-b")
    base=utcnow()
    with Session(database[1]) as s,s.begin():
        # The ledger backfill walks the provider newest-first, so row ids ascend as sales get older.
        for days in range(5):
            s.add(Transaction(account_id="acc-a",fan_id=101,kind="tip",amount_cents=100+days,currency="USD",status="cleared",
                occurred_at=base-timedelta(days=days),provider_transaction_id=f"synthetic-{days}"))
        s.add(Transaction(account_id="acc-b",kind="tip",amount_cents=999,currency="USD",status="cleared",occurred_at=base+timedelta(days=1)))
    pages=[read(database,"sales",limit=2)]
    while pages[-1]["nextCursor"]:pages.append(read(database,"sales",limit=2,cursor=pages[-1]["nextCursor"]))
    rows=[x for page in pages for x in page["transactions"]]
    assert [x["grossCents"] for x in rows]==[100,101,102,103,104]
    assert len({x["transactionId"] for x in rows})==5
    store.close();other.close()


def test_sales_page_and_totals_share_one_snapshot_during_a_concurrent_refund(database):
    store=seed(*database)
    with Session(database[1]) as s,s.begin():
        s.add(Transaction(account_id="acc-a",fan_id=101,kind="ppv_message",amount_cents=1000,currency="USD",
            status="cleared",occurred_at=utcnow()))
    changed=[]
    async def run():
        engine=create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"))
        def after_page(connection,cursor,statement,parameters,context,many):
            if not changed and "FROM transactions" in statement and "ORDER BY transactions.occurred_at DESC, transactions.id DESC" in statement:
                with database[1].begin() as c:c.execute(text("UPDATE transactions SET status='refunded' WHERE account_id='acc-a'"))
                changed.append(True)
        event.listen(engine.sync_engine,"after_cursor_execute",after_page)
        try:
            async with async_sessionmaker(engine)() as s:
                async with s.begin():return await read_workspace(s,tenant_id="tenant-a",account_id="acc-a",section="sales")
        finally:await engine.dispose()
    result=asyncio.run(run())
    assert changed==[True]
    assert result["transactions"][0]["status"]=="cleared" and result["totals"]==[{"currency":"USD","status":"cleared","count":1,"grossCents":1000}]
    assert read(database,"sales")["transactions"][0]["status"]=="refunded"
    store.close()


def test_settings_use_saved_fields_and_do_not_expose_unknown_config_or_provider_secrets(database):
    store=seed(*database)
    with Session(database[1]) as s,s.begin():
        s.add(AccountAiConfig(account_id="acc-a",persona="Approved synthetic creator persona.",language="es",model="saved-default",
            model_by_purpose='{"ai_chatter":"saved-reply","secret":"hidden-provider-secret"}',
            persona_facts_json='{"age":"28","home_city":"Synthetic city","unknown":"hidden-private-field"}',
            ai_chatter_config_json='{"pack_send_enabled":true,"pack_on_ask_enabled":false,"qualification_gate_enabled":"true","secret":"hidden-provider-secret"}',daily_cost_cap_cents=123))
    result=read(database,"settings");cfg=result["configuration"]
    assert cfg["savedDefaultModel"]=="saved-default" and cfg["savedReplyModelOverride"]=="saved-reply"
    assert cfg["salesPermissions"]=={"pack_send_enabled":True,"pack_on_ask_enabled":False,"qualification_gate_enabled":None}
    assert cfg["creatorFacts"]==[{"label":"Age","value":"28"},{"label":"Lives in","value":"Synthetic city"}]
    assert cfg["language"]=="es" and cfg["dailyModelCapCents"]==123
    assert "hidden" not in json.dumps(result) and result["deliveryPolicy"] is None
    with Session(database[1]) as s,s.begin():
        row=s.get(AccountAiConfig,"acc-a");row.model_by_purpose='{"ai_chatter":null}';row.persona_facts_json='["invalid"]'
    cfg=read(database,"settings")["configuration"]
    assert cfg["issues"]==["creator_facts_invalid","model_configuration_invalid"]
    assert cfg["savedReplyModelOverride"] is None and cfg["creatorFacts"]==[]
    store.close()


@pytest.mark.parametrize("section",["content","sales","settings"])
def test_every_section_refuses_cross_tenant_unknown_and_revoked_binding(database,section):
    store=seed(*database)
    assert read(database,section,tenant_id="other-tenant") is None
    assert read(database,section,account_id="unknown") is None
    with Session(database[1]) as s,s.begin():s.get(OfCreatorBinding,"binding-acc-a").revoked_at=utcnow()
    assert read(database,section) is None
    store.close()


def test_cursor_and_page_limits_refuse_instead_of_guessing(database):
    store=seed(*database)
    for kwargs in [{"limit":0},{"limit":101},{"limit":True},{"cursor":"malformed=="}]:
        with pytest.raises(BadRequest):read(database,"content",**kwargs)
    with pytest.raises(BadRequest):read(database,"settings",cursor=cursor_for("acc-a","settings",1))
    with pytest.raises(BadRequest):read(database,"unknown")
    store.close()


def test_reads_do_not_import_any_legacy_sender_or_model_client(database):
    store=seed(*database);shelf(database[1]);store.close()
    with Session(database[1]) as s,s.begin():s.add(AccountAiConfig(account_id="acc-a",persona="Synthetic",persona_facts_json='{"age":"28"}'))
    service=Path(__file__).resolve().parents[1]
    script=r'''
import asyncio,importlib.abc,os,sys
sys.path.insert(0,sys.argv[1])
forbidden={"llm_client","of_client","automation_registry","automation_executor","server","accounts","curl_cffi","client_pool"}
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split(".")[0] in forbidden:raise AssertionError("legacy import: "+fullname)
sys.meta_path.insert(0,Guard())
from db.engine import get_session,engine
from onlyfans_read.workspace import read_workspace
async def run():
 for section in ("content","sales","settings"):
  async with get_session() as s:
   result=await read_workspace(s,tenant_id="tenant-a",account_id="acc-a",section=section)
   assert result["section"]==section
 await engine.dispose()
asyncio.run(run())
'''
    result=subprocess.run([sys.executable,"-c",script,str(service)],capture_output=True,text=True,
        env={**os.environ,"ONLYFANS_PROCESS_ROLE":"api","DATABASE_URL":database[0]},timeout=20)
    assert result.returncode==0,result.stderr
