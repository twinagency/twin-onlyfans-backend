import sqlite3,json,logging
from pathlib import Path
logging.disable(logging.CRITICAL)
c=sqlite3.connect(Path("chatterly.db").resolve().as_uri()+"?mode=ro",uri=True);c.row_factory=sqlite3.Row
print("messages",json.dumps([dict(r) for r in c.execute("SELECT message_id,direction,body,created_at,is_paid,price_cents FROM messages WHERE account_id=? AND fan_id=? ORDER BY created_at DESC LIMIT 14",("176774528",318022380))],default=str))
from of_client import OFClient
client=OFClient.from_account("176774528",timeout_s=20)
hist=client.get_messages(318022380,limit=20)
rows=hist if isinstance(hist,list) else hist.get("list",[])
for r in rows:
 print("live",json.dumps({k:r.get(k) for k in ["id","createdAt","price","isPaid","isOpened","isFree","canPurchase","canPurchaseReason","isMediaReady","isUnsent","mediaCount"]},default=str))
client.http.close()
