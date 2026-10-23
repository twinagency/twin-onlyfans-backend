import asyncio,json,os,sqlite3,sys,logging
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path.cwd()))
os.environ["W7_WEBHOOK_DISPATCH_DISABLED"]="1"
logging.disable(logging.CRITICAL)
from of_client import OFClient
from of_ws import OFWebSocket
from event_transcoder import _transcode_chat_message
from db.engine import engine
PILOT="of-content-20261020-0714"
ACCOUNT="176774528"
FAN=318022380
DB=Path("chatterly.db").resolve()
def emit(event,**kw):
 print(json.dumps(dict(event=event,**kw),sort_keys=True),flush=True)
def row():
 c=sqlite3.connect(DB.as_uri()+"?mode=ro",uri=True)
 try:
  c.execute("PRAGMA query_only=ON")
  r=c.execute("SELECT state,cutoff,deadline FROM native_pilots WHERE pilot_id=? AND account_id=? AND fan_id=?",(PILOT,ACCOUNT,FAN)).fetchone()
  return r
 finally:c.close()
def active():
 r=row()
 return bool(r and r[0]=="active" and datetime.now(timezone.utc).replace(tzinfo=None)<datetime.fromisoformat(r[2]))
def exists(mid):
 c=sqlite3.connect(DB.as_uri()+"?mode=ro",uri=True)
 try:return c.execute("SELECT 1 FROM messages WHERE account_id=? AND fan_id=? AND message_id=?",(ACCOUNT,FAN,mid)).fetchone() is not None
 finally:c.close()
async def main():
 r=row()
 if not active():raise RuntimeError("pilot-inactive")
 cutoff=datetime.fromisoformat(r[1])
 client=OFClient.from_account(ACCOUNT,timeout_s=20)
 me=await asyncio.to_thread(client.me)
 if str(me.get("id"))!=ACCOUNT or me.get("username")!="niickybabyfree":raise RuntimeError("identity-mismatch")
 ws=OFWebSocket(client)
 async def watch():
  while active():await asyncio.sleep(1)
  ws.stop()
 watchdog=asyncio.create_task(watch())
 emit("intake-starting",account_id=ACCOUNT,fan_id=FAN,deadline_utc=r[2])
 try:
  async for e in ws.events():
   if not active():break
   if isinstance(e,dict) and e.get("connected") is True:
    emit("intake-connected",account_id=ACCOUNT,fan_id=FAN)
   m=e.get("api2_chat_message") if isinstance(e,dict) else None
   if not isinstance(m,dict) or str((m.get("fromUser") or {}).get("id"))!=str(FAN):continue
   mid=m.get("id")
   if type(mid) is not int or mid<=0 or exists(mid):continue
   try:
    created=datetime.fromisoformat(m["createdAt"].replace("Z","+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
   except Exception:continue
   if created<=cutoff:continue
   if m.get("media") or m.get("mediaCount") or m.get("giphyId") or m.get("isTip") or m.get("price") or m.get("lockedText"):
    emit("unsupported-input",message_id=mid);continue
   await _transcode_chat_message(ACCOUNT,m)
   emit("inbound-persisted",message_id=mid,verified=exists(mid))
 finally:
  ws.stop()
  watchdog.cancel()
  await asyncio.gather(watchdog,return_exceptions=True)
  await engine.dispose()
  emit("intake-stopped")
try:asyncio.run(main())
except Exception as e:emit("intake-error",error_type=type(e).__name__)
