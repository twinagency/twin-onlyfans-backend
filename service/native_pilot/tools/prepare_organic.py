import asyncio,json,logging,os,sqlite3
from pathlib import Path
from datetime import datetime,timezone
logging.disable(logging.CRITICAL)
ACCOUNT="176774528"; FAN=318022380
ROOT=Path(os.environ["TEMP"])/"of-organic-pilot-20261020"
async def main():
 from of_client import OFClient
 from db.engine import get_session,engine
 from db.models import CatalogItem,VaultItem,ContentOffer
 from automations import ai_chatter as native
 from sqlalchemy import select
 c=OFClient.from_account(ACCOUNT,timeout_s=20)
 try:
  me=await asyncio.to_thread(c.me)
  assert str(me.get("id"))==ACCOUNT and me.get("username")=="niickybabyfree","identity-mismatch"
  db=sqlite3.connect(Path("chatterly.db").resolve().as_uri()+"?mode=ro",uri=True);db.row_factory=sqlite3.Row
  assert not db.execute("SELECT 1 FROM native_pilots WHERE state='active'").fetchone(),"pilot-still-active"
  assert not db.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? AND state IN ('dispatching','delivery_unknown')",(ACCOUNT,FAN)).fetchone(),"delivery-unresolved"
  offers=[dict(x) for x in db.execute("SELECT * FROM content_offers WHERE account_id=? AND status IN ('open','unlocked','paid','delivering')",(ACCOUNT,))]
  assert len(offers)==1 and offers[0]["id"]==2 and offers[0]["fan_id"]==FAN and offers[0]["offer_message_id"]==11708765027621 and offers[0]["mode"]=="ppv","unexpected-offer-obligation"
  hist=await asyncio.to_thread(c.get_messages,FAN,limit=20)
  card=next((r for r in hist.get("list",[]) if r.get("id")==11708765027621),None)
  assert card and card.get("isOpened") is True and card.get("canPurchaseReason")=="opened" and card.get("price")==3 and card.get("fromUser",{}).get("id")==int(ACCOUNT),"purchase-unverified"
  assert [m.get("id") for m in card.get("media",[])]==[4717462861],"purchased-media-mismatch"
  prior=set(x[0] for x in db.execute("SELECT media_id FROM vault_sends WHERE account_id=? AND fan_id=?",(ACCOUNT,FAN)))
  local=set(x[0] for x in db.execute("SELECT media_id FROM vault_items WHERE account_id=?",(ACCOUNT,)))
  db.close()
  assets=[]
  for kind,cents in [("photo",0),("video",300)]:
   page=await asyncio.to_thread(c.vault_media,limit=24,type=kind)
   choice=next((r for r in page.get("list",[]) if r.get("type")==kind and r.get("canView") is True and r.get("isReady") is not False and not r.get("hasError") and int(r.get("id",0)) not in prior|local),None)
   assert choice,"no-fresh-approved-kind"
   exact=await asyncio.to_thread(c.vault_media_by_id,int(choice["id"]))
   assert exact.get("id")==choice["id"] and exact.get("type")==kind and exact.get("canView") is True,"fresh-asset-unavailable"
   assets.append((exact,kind,cents))
  ROOT.mkdir(exist_ok=True)
  assert not (ROOT/"manifest.json").exists(),"already-prepared"
  # Use existing native ownership and offer writers after positive platform evidence.
  assert await native._fastpath_check_opened(c,ACCOUNT,FAN,11708765027621),"purchase-fastpath-failed"
  await native._mark_media_purchased(ACCOUNT,FAN,[4717462861])
  await native._resolve_offer(2,status="delivered",resolved_by="ppv_fastpath")
  evidence={"checked_at":datetime.now(timezone.utc).isoformat(),"message_id":card["id"],"price":card["price"],"isOpened":True,"canPurchaseReason":"opened","media_id":4717462861,"ownership_preserved":True,"platform_sends":0}
  (ROOT/"prior-purchase-check.json").write_text(json.dumps(evidence,sort_keys=True),encoding="utf-8")
  items=[]
  async with get_session() as s:
   for r,kind,cents in assets:
    duration=int(r.get("duration") or 0)
    desc=("One photo." if kind=="photo" else "One video, "+str(duration)+" seconds.")+" No clothing, body-part, activity or personal-origin details have been verified."
    s.add(VaultItem(account_id=ACCOUNT,media_id=int(r["id"]),kind=kind,duration_seconds=duration,description=desc,raw_json=json.dumps(r),default_price_cents=cents,suggested_price_cents=cents,created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    item=CatalogItem(account_id=ACCOUNT,label="Photo preview" if cents==0 else "Video",kind=kind,description_for_ai=desc,media_ids=json.dumps([int(r["id"])]),preview_media_ids="[]",duration_sec=duration,price_cents=cents,is_free_teaser=(cents==0),enabled=True,tags="[]")
    s.add(item);await s.flush()
    items.append({"catalog_id":item.id,"media_id":int(r["id"]),"kind":kind,"price_cents":cents})
  (ROOT/"manifest.json").write_text(json.dumps({"account_id":ACCOUNT,"fan_id":FAN,"items":items},sort_keys=True),encoding="utf-8")
  print(json.dumps({"prepared":True,"prior_purchase_verified":True,"items":items,"history_and_receipts_preserved":True,"both_available_from_start":True,"platform_sends":0,"model_calls":0},sort_keys=True))
 finally:
  c.http.close();await engine.dispose()
asyncio.run(main())
