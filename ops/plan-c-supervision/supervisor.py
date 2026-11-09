"""Pilot-only supervisor. Staged; no activation or hold release is performed."""
import datetime,json,os,socket,subprocess,sys,time,urllib.error,urllib.request
from pathlib import Path
from incident_state import unresolved
from urllib.parse import urlencode
from guard_core import GuardFailure,credit_estimate,require_health
ROOT=Path('/srv/projects/twin-plan-c-pilot-20261109')
STATE=ROOT/'supervision-v11'/'budget-state.json'
INCIDENT=ROOT/'supervision-v11'/'outage-open.json'
BASELINE=ROOT/'config'/'pilot-credit-baseline-v1.json'
ENV=ROOT/'config'/'pilot-worker-fansapi.env'
PUBLIC='https://peterson-blacks-quit-ceremony.trycloudflare.com/onlyfans/webhooks/plan-c'
LOCAL='http://127.0.0.1:55462'
SENDER='http://127.0.0.1:55464'
RESERVATIONS=[('mass-queue-observation-v1/mass-queue-observation-v1-reservation.json',1),('transaction-total-v1/transaction-total-v1-reservation.json',1),('fresh-photo-v1/fresh-photo-v1-reservation.json',12),('full-photo-v1/full-photo-v1-reservation.json',20),('bounded-photo-get-v1/bounded-photo-get-v1-reservation.json',20)]
OPTIONAL=[]
RESERVATIONS.append(('provider-photo-v2/provider-photo-v2-reservation.json',10))
RESERVATIONS += [('webhook-readiness-inspection-v1/reservation.json',2),
 ('webhook-enable-v1/reservation.json',3),('webhook-enable-v2/reservation.json',3),
 ('webhook-delivery-check-v1/reservation.json',1),('supervisor-preflight-v10/reservation.json',2)]

SQL="""select json_build_object('status',s.status,'revision',s.revision,'reserved',s.provider_credits_reserved,'runtime',r.enabled,'policy',p.enabled) from of_engine_pilot_scope s join of_runtime r on r.account_id=s.account_id join of_delivery_policy p on p.account_id=r.account_id where s.scope_id=1 and s.account_id='of-323358988';"""
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def readjson(p):
 try:return json.loads(p.read_text())
 except Exception:raise GuardFailure('persistent_state_unavailable') from None
def atomic(p,data):
 tmp=p.with_name(p.name+'.tmp')
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o640)
 os.fchown(fd,0,65532);os.fchmod(fd,0o640)
 with os.fdopen(fd,'w') as f:json.dump(data,f,sort_keys=True);f.flush();os.fsync(f.fileno())
 os.replace(tmp,p)
 d=os.open(p.parent,os.O_RDONLY);os.fsync(d);os.close(d)
def notify(message):
 name=os.environ.get('NOTIFY_SOCKET')
 if not name:raise GuardFailure('systemd_notify_unavailable')
 if name.startswith('@'):name='\0'+name[1:]
 with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:sock.sendto(message.encode(),name)
def status_request(url,method='GET',headers=None,*,timeout=6):
 req=urllib.request.Request(url,headers=headers or {},method=method)
 try:
  with OPENER.open(req,timeout=timeout) as r:return r.status,r.read(2_000_001)
 except urllib.error.HTTPError as e:return e.code,e.read(2_000_001)
 except Exception:raise GuardFailure('network_check_failed') from None
def status_json(url):
 status,body=status_request(url)
 if status!=200 or len(body)>2_000_000:raise GuardFailure('readiness_unavailable')
 try:
  result=json.loads(body)
  if type(result) is not dict:raise ValueError()
  return result
 except Exception:raise GuardFailure('readiness_malformed') from None
def intake_readiness(scope_status):
 status,body=status_request(LOCAL+'/readyz')
 try:
  if len(body)>2_000_000:raise ValueError()
  data=json.loads(body)
  if type(data) is not dict:raise ValueError()
  if scope_status!='active':
   if status!=200 or data.get('ok') is not True or data.get('mode')!='intake_only' or data.get('code')!='ready':raise ValueError()
   return data
  if status!=503 or data.get('mode')!='intake_only' or data.get('code')!='restart_required' or data.get('consecutiveFailures')!=0 or type(data.get('work')) is not dict:raise ValueError()
  return {'ok':True,'code':'active_intake_loop_ready'}
 except Exception:raise GuardFailure('receiver_unavailable') from None
def db_state():
 try:
  text=subprocess.check_output(['docker','exec','twin-plan-c-pilot-pg-20261109','psql','-X','-qAt','-U','plan_c_pilot','-d','twin_plan_c_pilot','-v','ON_ERROR_STOP=1','-c',SQL],timeout=8,stderr=subprocess.DEVNULL,text=True)
  row=json.loads(text)
  if type(row) is not dict or type(row.get('reserved')) is not int or type(row.get('revision')) is not int:raise ValueError()
  if row.get('status')=='active':
   if row.get('runtime') is not True or row.get('policy') is not True:raise ValueError()
  elif row.get('status') in ('stopped','configured'):
   if row.get('runtime') is not False or row.get('policy') is not False:raise ValueError()
  else:raise ValueError()
  return row
 except Exception:raise GuardFailure('pilot_state_unavailable') from None
def external_reservations():
 total=38
 for rel,amount in RESERVATIONS+OPTIONAL:
  path=ROOT/'evidence'/rel
  if (rel,amount) in OPTIONAL and not path.exists():continue
  entry=readjson(path)
  if type(entry) is not dict or entry.get('credits_reserved')!=amount:raise GuardFailure('reservation_unavailable')
  total+=amount
 return total
def team_usage(baseline):
 try:
  pairs={}
  for line in ENV.read_text().splitlines():
   if not line.strip() or line.lstrip().startswith('#'):continue
   k,sep,v=line.partition('=')
   if not sep or k in pairs:raise ValueError()
   pairs[k]=v
  if pairs.get('FANSAPI_BASE')!='https://api.fansapi.com' or pairs.get('FANSAPI_ACCT')!='acct_7bf8540de5a84f459058490fee26c47f' or not pairs.get('FANSAPI_KEY'):raise ValueError()
  today=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
  query=urlencode({'from':baseline['start'],'to':today,'group_by':'endpoint','include_today':'true'})
  status,body=status_request('https://api.fansapi.com/api/usage/credits?'+query,headers={'Authorization':'Bearer '+pairs['FANSAPI_KEY'],'Accept':'application/json','User-Agent':'Twin-Plan-C/1.0'})
  if status!=200 or len(body)>2_000_000:raise ValueError()
  data=json.loads(body);meta=data['_meta']['_credits']
  return data['data']['totals']['credits'],meta['balance'],meta['used']
 except Exception:raise GuardFailure('credit_monitor_unavailable') from None
def webhook_health():
 # Check the exact dedicated subscription and its complete recent delivery log.
 # A failed/ambiguous delivery causes a stop; no automatic hold is cleared.
 try:
  pairs={}
  for line in ENV.read_text().splitlines():
   if not line.strip() or line.lstrip().startswith('#'):continue
   k,sep,v=line.partition('=')
   if not sep or k in pairs:raise ValueError()
   pairs[k]=v
  if pairs.get('FANSAPI_BASE')!='https://api.fansapi.com' or pairs.get('FANSAPI_ACCT')!='acct_7bf8540de5a84f459058490fee26c47f' or not pairs.get('FANSAPI_KEY'):raise ValueError()
  headers={'Authorization':'Bearer '+pairs['FANSAPI_KEY'],'Accept':'application/json','User-Agent':'Twin-Plan-C/1.0'}
  expected=readjson(ROOT/'evidence'/'webhook-registration.log')
  wid='wh_db5f1a0931514ef9b285c412bede5713'
  if expected.get('id')!=wid or expected.get('url')!=PUBLIC or expected.get('account_ids')!=[pairs['FANSAPI_ACCT']] or expected.get('account_scope')!='inclusive':raise ValueError()
  status,body=status_request('https://api.fansapi.com/api/webhooks/'+wid,headers=headers)
  if status!=200 or len(body)>2000000:raise ValueError()
  obj=json.loads(body);row=obj['data']
  if (type(obj['_meta']['_credits']['used']) is not int or obj['_meta']['_credits']['used']!=0) or row.get('id')!=wid or row.get('url')!=PUBLIC or row.get('enabled') is not True or row.get('has_signing_secret') is not True or row.get('account_scope')!='inclusive' or row.get('account_ids')!=[pairs['FANSAPI_ACCT']] or set(row.get('events',[]))!=set(expected['events']):raise ValueError()
  if 'paused' in row and row['paused'] is not False:raise ValueError()
  intent=ROOT/'evidence'/'webhook-enable-v2'/'put-intent.json'
  start=datetime.datetime.fromtimestamp(intent.stat().st_mtime,datetime.timezone.utc).isoformat()
  query=urlencode({'date_start':start,'limit':100,'offset':0})
  status,body=status_request('https://api.fansapi.com/api/webhooks/'+wid+'/deliveries?'+query,headers=headers,timeout=12)
  if status!=200 or len(body)>2000000:raise ValueError()
  obj=json.loads(body);rows=obj['data'];page=obj['_pagination']
  if (type(obj['_meta']['_credits']['used']) is not int or obj['_meta']['_credits']['used']!=0) or type(rows) is not list or not rows or page.get('next_page') is not None:raise ValueError()
  for item in rows:
   if type(item) is not dict or item.get('succeeded') is not True or type(item.get('status_code')) is not int or not 200<=item['status_code']<300 or item.get('url')!=PUBLIC or item.get('event') not in expected['events'] or type(item.get('payload')) is not dict or item['payload'].get('account_id')!=pairs['FANSAPI_ACCT']:raise ValueError()
  return True
 except Exception:raise GuardFailure('provider_webhook_health_unavailable') from None

def sender_identity():
 try:
  import re
  data=readjson(ROOT/'supervision-v11'/'sender-process.json')
  pid=data.get('pid');cid=data.get('container_id')
  if type(pid) is not int or pid<=1 or type(cid) is not str or not re.fullmatch('[0-9a-f]{64}',cid):raise ValueError()
  if cid not in Path('/proc').joinpath(str(pid),'cgroup').read_text():raise ValueError()
 except Exception:raise GuardFailure('sender_identity_unavailable') from None
def check(state):
 started=time.monotonic()
 baseline=readjson(BASELINE);row=db_state();outside=external_reservations()
 public,_=status_request(PUBLIC,'POST');local,_=status_request(LOCAL+'/onlyfans/webhooks/plan-c','POST')
 intake=intake_readiness(row['status'])
 pending=False;transition_start=None
 if row['status']=='active':
  try:sender=status_json(SENDER+'/readyz')
  except GuardFailure:
   if state.get('scope_status') not in ('stopped','configured','transition'):raise
   transition_start=state.get('transition_start',time.monotonic())
   if type(transition_start) not in (int,float) or time.monotonic()-transition_start>30:raise GuardFailure('sender_start_timeout')
   if public!=401 or local!=401 or intake.get('ok') is not True:raise GuardFailure('ingress_unavailable')
   pending=True;sender=None
  if not pending:
   sender_identity()
   require_health(public_status=public,local_status=local,intake=intake,scope_status='active',active_sender=sender)
 else:require_health(public_status=public,local_status=local,intake=intake,scope_status=row['status'],active_sender=None)
 webhook_health()
 total,balance,cost=team_usage(baseline)
 estimate=credit_estimate(baseline_total=baseline['team_credits_observed'],baseline_balance=baseline['team_balance'],team_total=total,team_balance=balance,usage_cost=cost,scope_reserved=row['reserved'],external_reserved=outside,prior_estimate=state['prior_estimate'])
 # Bound proof age before the caller can publish STATE; request timeouts are
 # socket/request limits, not a whole-check wall-clock guarantee.
 if time.monotonic()-started>30:raise GuardFailure('monitor_check_too_old')
 result={'prior_estimate':estimate,'last_ok_utc':now(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'last_ok_monotonic':time.monotonic(),'scope_status':'transition' if pending else row['status'],'scope_revision':row['revision']}
 if pending:result['transition_start']=transition_start
 return result
def run():
 if unresolved(INCIDENT):raise GuardFailure('outage_reconciliation_required')
 state=readjson(STATE)
 if type(state) is not dict or type(state.get('prior_estimate')) not in (int,float):raise GuardFailure('persistent_state_malformed')
 if db_state()['status']=='active':raise GuardFailure('restart_requires_stopped_scope')
 while True:
  state=check(state);atomic(STATE,state)
  notify('READY=1\nWATCHDOG=1\nSTATUS=Pilot checks passed')
  time.sleep(10)
if __name__=='__main__':
 try:run()
 except GuardFailure as exc:
  print('pilot_supervisor_stopped:'+str(exc),file=sys.stderr)
  raise SystemExit(1)
 except Exception:
  print('pilot_supervisor_stopped:unexpected_failure',file=sys.stderr)
  raise SystemExit(1)
