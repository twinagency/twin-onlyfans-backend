"""750k synthetic messages; populated pending-source upgrade rehearsal."""
import json,os,time
from pathlib import Path
from sqlalchemy import insert,text
from sqlalchemy.orm import Session
from sqlalchemy.engine import make_url
from test_onlyfans_foundation_pg import database,seed
from onlyfans_upgrade_fixture import restore_pre95_content_vault_shape
from db.models import Message
from db.onlyfans_models import OfRuntime
from db.schema import _config,upgrade,check_schema,PURCHASE_REPLY
from alembic import command
from onlyfans_runtime.content_sync import _fold_query
from onlyfans_runtime.store import utcnow
assert make_url(os.environ['ONLYFANS_TEST_PG_URL']).host=='127.0.0.1'
report={'runtimeSourceCommit':'d6811b124928f8167e6625ce9db5db81bf227fad','rows':750000,'environment':'disposable GitHub Ubuntu runner, PostgreSQL 16, synthetic account, no provider/model calls'}
generator=database.__wrapped__();owned=next(generator);store=seed(*owned)
try:
 start=time.perf_counter();now=utcnow()
 with Session(owned[1]) as s,s.begin():
  for offset in range(0,report['rows'],5000):
   s.execute(insert(Message),[{'account_id':'acc-a','fan_id':101,'message_id':1000000+i,'direction':'out','body':'Synthetic historical source','media_ids':'[11]','media_count':1,'price_cents':100,'created_at':now,'ingested_at':now,'is_unsent':False} for i in range(offset,offset+5000)])
  s.execute(text("UPDATE of_content_sync_source SET folded_at=now(),folded_message_media_json='[11]',folded_message_price_cents=100,folded_message_unsent=false,fold_pending=false"))
 report['populationSeconds']=round(time.perf_counter()-start,3)
 print(json.dumps({'populatedRows':report['rows'],'seconds':report['populationSeconds']}),flush=True)
 with owned[1].begin() as c:
  restore_pre95_content_vault_shape(c);command.stamp(_config(c),PURCHASE_REPLY)
  c.exec_driver_sql('DELETE FROM of_content_sync_source WHERE source_id=1000001')
  c.exec_driver_sql('UPDATE messages SET price_cents=200 WHERE message_id=1000002')
  before=c.execute(text('SELECT enabled,binding_id FROM of_runtime')).all()
  # Release preflight: refresh statistics after a bulk restore, before DDL/backfill.
  for table in ['messages','of_content_sync_source','of_runtime','of_content_sync_account','of_offer_import_job']:
   c.exec_driver_sql('ANALYZE '+table)
  report['releasePreflight']='ANALYZE canonical messages and source/join tables before migration'
  start=time.perf_counter();upgrade(c);check_schema(c);report['upgradeSeconds']=round(time.perf_counter()-start,3)
  assert c.execute(text('SELECT enabled,binding_id FROM of_runtime')).all()==before
  pending=c.execute(text('SELECT source_id FROM of_content_sync_source WHERE fold_pending IS TRUE ORDER BY source_id')).scalars().all()
  assert pending==[1000001,1000002],pending
  assert c.scalar(text('SELECT count(*) FROM of_text_intent'))==0
  assert c.scalar(text('SELECT count(*) FROM transactions'))==0
  c.exec_driver_sql('ANALYZE messages');c.exec_driver_sql('ANALYZE of_content_sync_source')
 with Session(owned[1]) as s:
  runtime=s.get(OfRuntime,'acc-a');q=_fold_query(runtime,None,None).order_by(Message.message_id,Message.fan_id).limit(1000);samples=[]
  for _ in range(30):
   start=time.perf_counter();rows=s.execute(q).all();samples.append((time.perf_counter()-start)*1000)
   assert [r.message_id for r in rows]==[1000001,1000002]
  compiled=q.compile(dialect=owned[1].dialect,compile_kwargs={'render_postcompile':True})
  plan=s.connection().exec_driver_sql('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+str(compiled),compiled.params).scalar()
  assert 'ix_of_content_sync_source_fold_pending' in json.dumps(plan)
  assert plan[0]['Execution Time']<500
  samples.sort();report['discovery']={'samples':30,'rows':len(rows),'p50ms':round(samples[14],3),'p95ms':round(samples[28],3),'maxMs':round(max(samples),3),'postgresExecutionMs':plan[0]['Execution Time'],'partialIndexUsed':True};report['plan']=plan
 report.update(status='passed',runtimeAuthorityPreserved=True,financialRowsCreated=0,sendIntentsCreated=0)
 Path('source-load-evidence').mkdir(exist_ok=True);Path('source-load-evidence/results.json').write_text(json.dumps(report,indent=2))
 print(json.dumps({k:v for k,v in report.items() if k!='plan'}))
finally:
 store.close();generator.close()
