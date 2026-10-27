import asyncio,json,logging,os
from pathlib import Path
from datetime import datetime,timezone
logging.disable(logging.CRITICAL)
from of_client import OFClient
ACCOUNT="176774528";FAN=318022380
ROOT=Path(os.environ["TEMP"])/"of-content-pilot-20261020"
async def main():
 from db.engine import get_session,engine
 from db.models import CatalogItem,VaultItem
 from sqlalchemy import select,func
 manifest=ROOT/"manifest.json"
 if manifest.exists():raise RuntimeError("manifest-already-staged")
 c=OFClient.from_account(ACCOUNT,timeout_s=20)
 me=await asyncio.to_thread(c.me)
 if str(me.get("id"))!=ACCOUNT or me.get("username")!="niickybabyfree":raise RuntimeError("identity-mismatch")
 assets=[]
 for mid,kind,cents in [(4747380180,"photo",0),(4717462861,"video",300)]:
  r=await asyncio.to_thread(c.vault_media_by_id,mid)
  if str(r.get("id"))!=str(mid) or r.get("type")!=kind or r.get("canView") is not True:raise RuntimeError("asset-unavailable")
  assets.append((r,kind,cents))
 async with get_session() as s:
  for model in [CatalogItem,VaultItem]:
   if (await s.execute(select(func.count()).select_from(model).where(model.account_id==ACCOUNT))).scalar_one()!=0:raise RuntimeError("catalog-not-empty")
  items=[]
  for r,kind,cents in assets:
   duration=int(r.get("duration") or 0)
   description=("One photo." if kind=="photo" else "One video, "+str(duration)+" seconds.")+" No clothing, body-part, activity or personal-origin details have been verified."
   s.add(VaultItem(account_id=ACCOUNT,media_id=int(r["id"]),kind=kind,duration_seconds=duration,description=description,raw_json=json.dumps(r),default_price_cents=cents,suggested_price_cents=cents,created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
   item=CatalogItem(account_id=ACCOUNT,label="Test photo preview" if cents==0 else "Test video",kind=kind,description_for_ai=description,media_ids=json.dumps([int(r["id"])]),preview_media_ids="[]",duration_sec=duration,price_cents=cents,is_free_teaser=(cents==0),enabled=(cents==0),tags="[]")
   s.add(item);await s.flush()
   items.append({"catalog_id":item.id,"media_id":int(r["id"]),"kind":kind,"price_cents":cents})
 with manifest.open("x",encoding="utf-8") as f:json.dump({"account_id":ACCOUNT,"fan_id":FAN,"items":items},f,sort_keys=True);f.flush();os.fsync(f.fileno())
 print(json.dumps({"staged":True,"account_id":ACCOUNT,"fan_id":FAN,"items":items,"account_automation_enabled":False,"platform_sends":0,"model_calls":0}))
 c.http.close();await engine.dispose()
try:asyncio.run(main())
except Exception as e:print(json.dumps({"staging_failed":True,"error_type":type(e).__name__,"database_reason":str(getattr(e,"orig",""))[:160]}));raise SystemExit(1)
