import os,sys,tempfile,sqlite3
from pathlib import Path
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_native_pilot_ledger as L
from native_pilot.ledger import Ledger,Refused
from twin_bridge import store
os.environ['FASTT_FAN_DAILY_REPLY_LIMIT']='2000'
def setup(root,name):
 f=L.Fixture(root,name);f.activate()
 f.sql("CREATE TABLE of_delivery_policy(account_id TEXT PRIMARY KEY,enabled INTEGER,limit_units INTEGER,used_units INTEGER,throttle_seconds INTEGER,next_dispatch_at TEXT,active_intent_id TEXT)")
 f.sql("INSERT INTO of_delivery_policy VALUES(?,1,500,500,0,NULL,NULL)",(L.ACCT,))
 return f,Ledger(f.path,allowance=True)
def seed(f,account,fan,n,when,state='sent',start=1000):
 with sqlite3.connect(f.path) as c:
  for i in range(n):
   aid=start+i
   c.execute("INSERT INTO twin_bridge_send_attempts(id,account_id,fan_id,turn_id,attempt_no,state,owner_token,revision,created_at) VALUES(?,?,?,?,1,?,'seed',1,?)",(aid,account,fan,str(aid),state,when))
   c.execute("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) VALUES(?,'seed',?,1,?,?,?)",(aid,str(aid),state,when,when))
def admission(ledger,fan,when,aid):
 try:
  with ledger._tx() as c:
   ledger._consume_allowance(c,L.ACCT,when,fan_id=fan,daily_limit=ledger.fan_daily_limit)
   c.execute("INSERT INTO twin_bridge_send_attempts(id,account_id,fan_id,turn_id,attempt_no,state,owner_token,revision,created_at) VALUES(?,?,?,?,1,'dispatching','test',1,?)",(aid,L.ACCT,fan,str(aid),when))
   c.execute("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) VALUES(?,'test',?,1,'dispatching',?,?)",(aid,str(aid),when,when))
  return 'admitted'
 except Refused as e:return e.reason
with tempfile.TemporaryDirectory() as d:
 root=Path(d);f,ledger=setup(root,'daily')
 now=L.NOW.isoformat(sep=' ',timespec='microseconds')
 seed(f,L.ACCT,L.FAN,1999,now)
 assert ledger.allowance_status(L.ACCT,L.FAN,now=L.NOW)['used']==1999
 assert ledger.allowance_status(L.ACCT,L.FAN+1,now=L.NOW)['used']==0
 seed(f,'another-creator',L.FAN,1,now,start=5000)
 with ThreadPoolExecutor(2) as pool:
  results=list(pool.map(lambda aid:admission(ledger,L.FAN,now,aid),[7000,7001]))
 assert sorted(results)==['admitted','allowance-exhausted'],results
 assert ledger.allowance_status(L.ACCT,L.FAN,now=L.NOW)['used']==2000
 assert Ledger(f.path,allowance=True).allowance_status(L.ACCT,L.FAN,now=L.NOW)['used']==2000
 assert ledger.allowance_status(L.ACCT,L.FAN,now=L.NOW+timedelta(hours=24,microseconds=-1))['used']==2000
 assert ledger.allowance_status(L.ACCT,L.FAN,now=L.NOW+timedelta(hours=24))['used']==0
 f.sql("UPDATE twin_bridge_send_attempts SET state='sent' WHERE state='dispatching'")
 assert admission(ledger,L.FAN+1,now,8000)=='admitted'
 # Only certain non-delivery gives the allowance back; unknown outcomes count.
 f.sql("UPDATE native_pilot_receipts SET state='not_sent' WHERE attempt_id=1000")
 f.sql("UPDATE native_pilot_receipts SET state='delivery_unknown' WHERE attempt_id=1001")
 assert ledger.allowance_status(L.ACCT,L.FAN,now=L.NOW)['used']==1999
 assert f.sql("SELECT used_units FROM of_delivery_policy")[0][0]==500
 f.sql("UPDATE of_delivery_policy SET enabled=0")
 assert admission(ledger,L.FAN,now,9000)=='allowance-disabled'
with tempfile.TemporaryDirectory() as d:
 f,ledger=setup(Path(d),'adoption')
 f.sql("UPDATE native_pilots SET state='stopped'")
 f.sql("UPDATE twin_bridge_conversations SET owner_token=NULL,owner_expires_at=NULL,send_budget_used=10000,send_budget_limit=10000")
 p=ledger.activate_service(pilot_id='svc-daily-test',account_id=L.ACCT,fan_id=L.FAN,owner_token='daily-owner-0000001',now=L.NOW,ttl_s=120)
 assert p.end_limit==2000010000 and p.baseline_used==10000
 assert f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0]==10000
 p2=ledger.activate_service(pilot_id='svc-daily-test',account_id=L.ACCT,fan_id=L.FAN,owner_token='daily-owner-0000002',now=L.NOW+timedelta(minutes=3),ttl_s=120)
 assert p2.end_limit==p.end_limit and p2.baseline_used==10000
print('PASS daily quota: 2000 boundary, concurrent admission, independent fan/creator, restart persistence, exact 24h expiry, refused/unknown accounting, legacy counters and disable')
