import subprocess,json
code=r'''
import os,json
from sqlalchemy import create_engine,text,select
from sqlalchemy.orm import Session
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.launch_operations import _ledger_core
e=create_engine(os.environ['DATABASE_URL'],connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=15000'})
q="""SELECT t.account_id,b.created_at AS binding_created_at,count(*) FILTER(WHERE t.occurred_at>=b.created_at) AS current_binding_unlinked,count(*) FILTER(WHERE t.occurred_at<b.created_at) AS historical_unlinked FROM transactions t JOIN of_runtime r ON r.account_id=t.account_id JOIN of_creator_bindings b ON b.id=r.binding_id WHERE t.source='ledger' AND t.kind='ppv_message' AND t.message_id IS NULL AND t.status IN ('pending','cleared') GROUP BY t.account_id,b.created_at ORDER BY t.account_id"""
with e.connect() as c:rows=[dict(r) for r in c.execute(text(q)).mappings()]
with Session(e) as s:
 gates=[{'account':r.account_id,'enabled':r.enabled,**_ledger_core(s,r)}for r in s.scalars(select(OfRuntime).order_by(OfRuntime.account_id))]
print(json.dumps({'unlinked':rows,'gates':gates},default=str))
'''
p=subprocess.run(['docker','exec','twin-onlyfans-worker-1','python','-c',code],text=True,capture_output=True,timeout=60)
assert p.returncode==0,p.stderr
print(p.stdout)