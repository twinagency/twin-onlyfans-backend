import os,json,hashlib,inspect
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from onlyfans_runtime.profile_learning import worker,model
e=create_engine(make_url(os.environ['DATABASE_URL']).set(drivername='postgresql+psycopg2'),connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=20000 -c lock_timeout=3000'})
queries={
"receipts":"""SELECT p.account_id,p.direction,count(*) AS messages,count(DISTINCT p.fan_id) AS fans FROM of_profile_receipts p JOIN of_runtime r ON p.account_id=r.account_id AND p.binding_id=r.binding_id WHERE p.fan_id>0 GROUP BY p.account_id,p.direction""",
"calls":"""SELECT account_id,status,count(*) AS calls,count(response_json) AS with_response,count(DISTINCT fan_id) AS fans,max(called_at) AS latest FROM grok_calls WHERE purpose='gen_info' AND called_at>'2026-10-29 02:45:00' GROUP BY account_id,status ORDER BY account_id,status""",
"coverage":"""SELECT m.account_id,count(DISTINCT m.fan_id) AS fans_with_inbound,count(*) AS inbound_messages FROM messages m JOIN of_runtime r ON m.account_id=r.account_id WHERE m.direction='in' AND NOT m.is_unsent AND length(trim(m.body))>0 AND EXISTS (SELECT 1 FROM of_ingress i WHERE i.account_id=m.account_id AND i.binding_id=r.binding_id AND i.fan_id=m.fan_id AND i.message_id=m.message_id AND i.status='applied') GROUP BY m.account_id""",
"jobs":"""SELECT p.account_id,count(*) AS total,count(*) FILTER(WHERE p.checked_at IS NOT NULL) AS checked,count(*) FILTER(WHERE p.processed_messages>0) AS with_receipts,count(*) FILTER(WHERE p.error_code IS NOT NULL) AS errors,min(p.queued_at) AS oldest,max(p.checked_at) AS latest FROM of_profile_learning p JOIN of_runtime r ON p.account_id=r.account_id AND p.binding_id=r.binding_id WHERE p.fan_id>0 GROUP BY p.account_id""",
"errors":"""SELECT p.account_id,p.fan_id=0 AS creator_draft,p.error_code,count(*) AS jobs,min(p.next_at) AS next_retry FROM of_profile_learning p JOIN of_runtime r ON p.account_id=r.account_id AND p.binding_id=r.binding_id WHERE p.error_code IS NOT NULL GROUP BY p.account_id,p.fan_id=0,p.error_code""",
"config":"""SELECT account_id,model,daily_cost_cap_cents FROM account_ai_config WHERE account_id IN (SELECT account_id FROM of_runtime) ORDER BY account_id""",
"memories":"""SELECT p.account_id,p.status,count(*) AS facts,count(DISTINCT p.fan_id) AS fans FROM of_profile_memories p JOIN of_runtime r ON p.account_id=r.account_id AND p.binding_id=r.binding_id WHERE p.fan_id>0 GROUP BY p.account_id,p.status"""}
out={'source_hashes':{x.__name__:hashlib.sha256(inspect.getsource(x).encode()).hexdigest() for x in (worker,model)}}
for key,sql in queries.items():
 try:
  with e.connect() as c:
   c.exec_driver_sql('BEGIN READ ONLY')
   out[key]=[dict(r) for r in c.execute(text(sql)).mappings()]
   c.rollback()
 except Exception as ex: out[key]={'error_type':type(ex).__name__}
out['model_response_shapes']=[]
with e.connect() as c:
 c.exec_driver_sql('BEGIN READ ONLY')
 for r in c.execute(text("SELECT id,account_id,status,called_at,response_json FROM grok_calls WHERE purpose='gen_info' AND called_at>'2026-10-29 02:45:00' ORDER BY id DESC LIMIT 15")).mappings():
  item={k:r[k] for k in ('id','account_id','status','called_at')}
  if r['response_json']:
   try:
    obj=json.loads(r['response_json'])
    item['json_type']=type(obj).__name__
    if type(obj) is dict:
     item['keys']=sorted(obj.keys())
     item['list_counts']={k:len(v) for k,v in obj.items() if type(v) is list}
   except ValueError: item['json_parse']='failed'
  out['model_response_shapes'].append(item)
 c.rollback()
e.dispose()
print(json.dumps(out,default=str))
