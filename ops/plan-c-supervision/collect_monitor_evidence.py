import base64,datetime,hashlib,json,os,subprocess,sys
from pathlib import Path
from urllib.parse import urlencode
ROOT=Path('/srv/projects/twin-plan-c-pilot-20261109')
sys.path.insert(0,str(ROOT/'supervision-v11'))
import supervisor
import delivery_history
from incident_state import classify_journal,journal_class,incident_spec,WEBHOOK_INCIDENT,REPEATED_WEBHOOK_INCIDENT
from guard_core import credit_estimate
if len(sys.argv)!=2:raise SystemExit('one exclusive collection directory is required')
d=Path(sys.argv[1]).resolve()
if d.parent!=ROOT/'evidence' or not d.name.startswith('monitor-incident-recovery-'):
 raise SystemExit('invalid evidence directory')
d.mkdir(mode=0o700)
def sha(b):return hashlib.sha256(b).hexdigest()
def save(name,raw):
 fd=os.open(d/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 return sha(raw)
def sql(query):
 cmd=['docker','exec','twin-plan-c-pilot-pg-20261109','psql','-X','-qAt','-U','plan_c_pilot','-d','twin_plan_c_pilot','-v','ON_ERROR_STOP=1','-c',query]
 return subprocess.check_output(cmd,timeout=15)
out={'state':'blocked','incident_sha256':None}
try:
 marker=(ROOT/'supervision-v11/outage-open.json').read_bytes();out['incident_sha256']=sha(marker)
 spec=incident_spec(out['incident_sha256'],marker)
 if spec is None:raise RuntimeError('incident changed')
 incident=json.loads(marker);save('original-incident.json',marker)
 state=supervisor.db_state()
 if state['status']!='stopped' or (spec['scope_revision'] is not None and state['revision']!=spec['scope_revision']):raise RuntimeError('stopped revision changed')
 if subprocess.check_output(['docker','ps','-q','--filter','name=twin-plan-c-pilot-active-sender']).strip():raise RuntimeError('sender running')
 journal=subprocess.check_output(['journalctl','-u','plan-c-pilot-supervisor.service','--since',incident['last_confirmed_healthy_utc'],'--no-pager','-o','cat'],timeout=10)
 if spec['pinned']:classify_journal(journal,spec['classification'])
 else:spec['classification']=journal_class(journal);spec['scope_revision']=state['revision']
 out['journal_sha256']=save('journal.txt',journal)
 env={}
 for line in supervisor.ENV.read_text().splitlines():
  if line.strip() and not line.lstrip().startswith('#'):
   k,sep,v=line.partition('=')
   if not sep or k in env:raise RuntimeError('credential configuration invalid')
   env[k]=v
 if env.get('FANSAPI_BASE')!='https://api.fansapi.com' or env.get('FANSAPI_ACCT')!='acct_7bf8540de5a84f459058490fee26c47f':raise RuntimeError('provider identity changed')
 h={'Authorization':'Bearer '+env['FANSAPI_KEY'],'Accept':'application/json','User-Agent':'Twin-Plan-C/1.0'}
 def get(path,name):
  status,raw=supervisor.status_request('https://api.fansapi.com'+path,headers=h,timeout=12 if path.startswith('/api/webhooks/'+wid+'/deliveries?') else 6)
  save(name,raw)
  if status!=200:raise RuntimeError('provider interval proof unavailable HTTP '+str(status))
  j=json.loads(raw)
  if type(j['_meta']['_credits']['used']) is not int or j['_meta']['_credits']['used']!=0:raise RuntimeError('unexpected monitored request cost')
  return j
 wid='wh_db5f1a0931514ef9b285c412bede5713'
 show=get('/api/webhooks/'+wid,'subscription.json')['data']
 expected=supervisor.readjson(ROOT/'evidence/webhook-registration.log')
 if any(show.get(k)!=expected.get(k) for k in ('id','url','account_ids','account_scope')) or show.get('enabled') is not True or show.get('has_signing_secret') is not True or set(show.get('events',[]))!=set(expected['events']) or show.get('paused',False) is not False:raise RuntimeError('dedicated webhook changed')
 interval_start=incident['last_confirmed_healthy_utc']
 pinned_webhook=out['incident_sha256'] in (WEBHOOK_INCIDENT,REPEATED_WEBHOOK_INCIDENT)
 if pinned_webhook:
  intent=ROOT/'evidence/webhook-enable-v2/put-intent.json'
  interval_start=datetime.datetime.fromtimestamp(intent.stat().st_mtime,datetime.timezone.utc).isoformat()
 elif not spec['pinned']:
  # Never earlier than the recorded enable boundary; the interval start is the last confirmed healthy instant.
  boundary=supervisor.readjson(ROOT/'supervision-v11/delivery-enable-boundary.json')
  if boundary.get('url')!=supervisor.PUBLIC or boundary.get('webhook_id')!=wid:raise RuntimeError('enable boundary identity changed')
  interval_start=max(delivery_history.parse_ts(interval_start),delivery_history.parse_ts(boundary['boundary_utc'])).isoformat()
 def pages(start,prefix,merged_name):
  def one(start_iso,limit,offset,name):
   return get('/api/webhooks/'+wid+'/deliveries?'+urlencode({'date_start':start_iso,'limit':limit,'offset':offset}),name)
  merged,window=delivery_history.collect_pages(one,start=start,prefix=prefix)
  save(merged_name,json.dumps(merged,sort_keys=True).encode())
  return merged,window
 deliveries,window=pages(interval_start,'deliveries-page','deliveries.json')
 rows=deliveries['data']
 if type(rows) is not list:raise RuntimeError('invalid interval listing')
 if pinned_webhook:
  recent,_=pages(incident['last_confirmed_healthy_utc'],'incident-deliveries-page','incident-deliveries.json')
  full={delivery_history.canon(r) for r in rows}
  for r in recent['data']:
   if delivery_history.canon(r) not in full:
    raise RuntimeError('incident window changed during collection')
  out.update(incident_delivery_count=len(recent['data']),anchor_context='known prior receipt predates this incident',original_failure_subcause='not retained')
 if not spec['pinned']:
  out.update(resolution_kind='class_based',incident_id=spec['incident_id'])
 out.update(delivery_url=supervisor.PUBLIC,delivery_url_sha256=delivery_history.sha(supervisor.PUBLIC.encode()),config_version=delivery_history.config_version(show),
  delivery_page_count=window['pages'],delivery_duplicates_folded=window['duplicates_folded'])
 out.update(delivery_interval_start=interval_start,incident_interval_start=incident['last_confirmed_healthy_utc'])
 local=sql("select json_build_object('receipts',(select coalesce(json_agg(json_build_object('key',w.event_key,'hash',w.payload_sha256,'raw',encode(w.raw_body,'base64'),'status',w.status,'projection',p.status)),'[]'::json) from of_fans_webhook w left join of_fans_projection p using(account_id,epoch,event_key) where w.account_id='of-323358988' and w.epoch=1),'digests',(select coalesce(json_agg(json_build_object('key',event_key,'hash',payload_sha256,'hold',safety_hold)),'[]'::json) from of_engine_pilot_webhook_digest where account_id='of-323358988' and epoch=1));")
 local=json.loads(local)
 queue_reviews=sql("select coalesce(json_agg(json_build_object('before',c.before_json::json,'result',c.result_json::json)),'[]'::json) from of_launch_command c join of_runtime r on r.account_id=c.account_id and r.binding_id=c.binding_id where c.account_id='of-323358988' and c.kind='plan_c_queue_review';")
 local['queue_reviews']=json.loads(queue_reviews)
 save('local-receipts.json',json.dumps(local,sort_keys=True).encode())
 receipts={r['key']:r for r in local['receipts']};digests={}
 for r in local['digests']:digests.setdefault(r['key'],[]).append(r)
 matched=[];missing=[]
 ok_index=delivery_history.retry_index(rows,lambda r:delivery_history.success(r,url=supervisor.PUBLIC,events=set(expected['events']),account_id=env['FANSAPI_ACCT']))
 for row in rows:
  key=row.get('idempotency_key');payload=row.get('payload')
  if row.get('succeeded') is not True or not 200<=row.get('status_code',0)<300 or row.get('url')!=supervisor.PUBLIC or type(payload) is not dict or payload.get('account_id')!=env['FANSAPI_ACCT']:
   if delivery_history.retry_state(row,ok_index)=='resolved':continue   # same event/destination/payload retried successfully; that success is reconciled below
   missing.append({'key':key,'reason':'failed_or_ambiguous_delivery'});continue
  # Collection only identifies a candidate exact audit. The image-owned
  # validator independently checks the full queue schema and current digest set.
  queue_candidate=False
  hashes={x['hash'] for x in local['digests'] if x['key']==key}
  for audit in local['queue_reviews']:
   proof,result=audit.get('before',{}),audit.get('result',{})
   if (proof.get('eventKey')!=key or proof.get('providerAccount')!=env['FANSAPI_ACCT']
       or set(proof.get('payloadSha256s',[]))!=hashes or result.get('resolved') is not True
       or result.get('outcome')!='queue_event_group_reviewed'):continue
   originals=proof.get('rawBodies',[])
   if type(originals) is not list or not 1<=len(originals)<=16:continue
   try:
    bodies=[x.encode('utf-8') for x in originals]
    queue_candidate=({sha(x) for x in bodies}==hashes and any(json.loads(x)==payload for x in bodies))
   except (ValueError,TypeError,AttributeError):queue_candidate=False
   if queue_candidate:break
  if queue_candidate:
   matched.append({'key':key,'local_kind':'audited_queue_group_review'});continue
  rec=receipts.get(key)
  if rec:
   raw=base64.b64decode(rec['raw'])
   if sha(raw)!=rec['hash'] or json.loads(raw)!=payload:
    missing.append({'key':key,'reason':'receipt_payload_mismatch'});continue
   matched.append({'key':key,'local_kind':'receipt','raw_sha256':rec['hash'],'projection':rec['projection'],'status':rec['status']})
  else:
   candidates={sha(json.dumps(payload,ensure_ascii=a,sort_keys=s,separators=sep).encode()) for a in (True,False) for s in (True,False) for sep in (None,(',',':'))}
   found=[v for v in digests.get(key,[]) if v['hash'] in candidates]
   if len(found)!=1:missing.append({'key':key,'reason':'exact_digest_bytes_unproven'});continue
   matched.append({'key':key,'local_kind':'digest','raw_sha256':found[0]['hash'],'hold':found[0]['hold']})
 baseline=supervisor.readjson(supervisor.BASELINE)
 total,balance,cost=supervisor.team_usage(baseline);outside=supervisor.external_reservations()
 previous=supervisor.readjson(supervisor.STATE)['prior_estimate']
 estimate=credit_estimate(baseline_total=baseline['team_credits_observed'],baseline_balance=baseline['team_balance'],team_total=total,team_balance=balance,usage_cost=cost,scope_reserved=state['reserved'],external_reserved=outside,prior_estimate=previous)
 if supervisor.db_state()!=state:raise RuntimeError('scope changed during collection')
 out.update(state='evidence_collected' if not missing else 'blocked',classification=spec['classification'],scope_revision=state['revision'],provider_delivery_count=len(rows),matched=matched,missing=missing,credit_estimate=estimate,external_reservations=outside,scope_reserved=state['reserved'],collected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),continuity_claim='supported delivery records reconciled only; no inferred full event completeness')
except Exception as e:
 out['error_type']=type(e).__name__;out['reason']=str(e)[:160] if isinstance(e,RuntimeError) else 'inspect protected source evidence'
finally:
 save('manifest.json',json.dumps(out,sort_keys=True).encode())
print(json.dumps(out,sort_keys=True))
