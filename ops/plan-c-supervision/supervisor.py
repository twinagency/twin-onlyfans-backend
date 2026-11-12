"""Pilot-only supervisor. Staged; no activation or hold release is performed."""
import datetime,json,os,socket,subprocess,sys,time,urllib.error,urllib.request
from pathlib import Path
from incident_state import unresolved,resolved_intervals
import delivery_history
from urllib.parse import urlencode
from guard_core import GuardFailure,credit_estimate,require_health
class TransientFailure(GuardFailure):
 """A lookup that could not complete. Tolerated a bounded number of times in a row, never as success."""
MAX_TRANSIENT=2
# Inert import defaults support pure tests; a running supervisor must load
# an explicit reviewed account configuration before it can inspect anything.
ROOT=Path('/unconfigured-twin-supervisor')
STATE=ROOT/'supervision-v11'/'budget-state.json'
INCIDENT=ROOT/'supervision-v11'/'outage-open.json'
BASELINE=ROOT/'config'/'pilot-credit-baseline-v1.json'
ENV=ROOT/'config'/'pilot-worker-fansapi.env'
PUBLIC='https://unconfigured.invalid/onlyfans/webhooks/plan-c'
LOCAL='http://127.0.0.1:1'
SENDER='http://127.0.0.1:2'
PROVIDER_ACCOUNT=WEBHOOK_ID=DATABASE_CONTAINER=DATABASE_ROLE=DATABASE_NAME=''
CREDIT_STOP=300
INITIAL_EXTERNAL=0
RESERVATIONS=[]
OPTIONAL=[]
SQL=''
CONFIG=None


def configure(value):
 from operation_config import validate
 global ROOT,STATE,INCIDENT,BASELINE,ENV,PUBLIC,LOCAL,SENDER,PROVIDER_ACCOUNT,WEBHOOK_ID
 global DATABASE_CONTAINER,DATABASE_ROLE,DATABASE_NAME,CREDIT_STOP,INITIAL_EXTERNAL,RESERVATIONS,OPTIONAL,SQL,CONFIG
 CONFIG=validate(value)
 ROOT=CONFIG['root'];STATE=ROOT/'supervision-v11'/'budget-state.json';INCIDENT=STATE.with_name('outage-open.json')
 BASELINE=ROOT/'config'/'pilot-credit-baseline-v1.json';ENV=ROOT/'config'/'pilot-worker-fansapi.env'
 PUBLIC=CONFIG['publicUrl'];LOCAL=CONFIG['receiverUrl'];SENDER=CONFIG['senderUrl']
 PROVIDER_ACCOUNT=CONFIG['providerAccount'];WEBHOOK_ID=CONFIG['webhookId']
 DATABASE_CONTAINER=CONFIG['databaseContainer'];DATABASE_ROLE=CONFIG['databaseRole'];DATABASE_NAME=CONFIG['databaseName']
 CREDIT_STOP=CONFIG['creditStop'];INITIAL_EXTERNAL=CONFIG['initialExternalReserved']
 RESERVATIONS=[(r['path'],r['amount']) for r in CONFIG['reservations'] if not r['optional']]
 OPTIONAL=[(r['path'],r['amount']) for r in CONFIG['reservations'] if r['optional']]
 # Identities are validated ASCII values without SQL quotes. Current bindings
 # are joined, and shared reservation totals include every explicitly named account.
 team=','.join("'" + a + "'" for a in CONFIG['teamAccounts'])
 SQL=("select json_build_object('status',s.status,'revision',s.revision,'reserved',s.provider_credits_reserved,"
      "'team_reserved',(select coalesce(sum(t.provider_credits_reserved),0) from of_engine_pilot_scope t "
      "join of_runtime tr on tr.account_id=t.account_id and tr.binding_id=t.binding_id where t.account_id in ("+team+")),"
      "'runtime',r.enabled,'policy',p.enabled) from of_engine_pilot_scope s "
      "join of_runtime r on r.account_id=s.account_id and r.binding_id=s.binding_id "
      "join of_delivery_policy p on p.account_id=r.account_id where s.account_id='"+CONFIG['account']+"';")
 return CONFIG

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
def retain_readiness_failure(status,body,data=None):
 import hashlib,re
 record={'at':now(),'http_status':status,'body_bytes':len(body),'body_sha256':hashlib.sha256(body).hexdigest()}
 if type(data) is dict:
  for key in ('code','mode'):
   value=data.get(key)
   if type(value) is str and re.fullmatch('[a-z_]{1,64}',value):record[key]=value
  for key in ('ticks','failures','consecutiveFailures'):
   value=data.get(key)
   if type(value) is int and 0<=value<10**9:record[key]=value
  phase=data.get('inFlight')
  if type(phase) is dict and phase.get('phase') in ('tick','model','model_identity','provider'):
   record['inFlight']={k:v for k,v in phase.items() if k=='phase' or k in ('ageSeconds','deadlineInSeconds') and type(v) in (int,float) and 0<=v<=3600}
 # The journal record survives sender-container removal. The private host file
 # also survives monitor restarts; no response prose or identifiers are retained.
 print(json.dumps({'readinessFailure':record},sort_keys=True),file=sys.stderr,flush=True)
 try:atomic(ROOT/'supervision-v11'/'readiness-last-failure.json',record)
 except (OSError,ValueError):pass
 return record

def status_json(url):
 try:status,body=status_request(url)
 except GuardFailure:
  retain_readiness_failure(None,b'')
  raise
 data=None
 try:
  if len(body)<=2_000_000:data=json.loads(body)
 except Exception:pass
 if status!=200 or len(body)>2_000_000:
  retain_readiness_failure(status,body,data)
  raise GuardFailure('readiness_unavailable')
 if type(data) is not dict:
  retain_readiness_failure(status,body,data)
  raise GuardFailure('readiness_malformed')
 return data

def lookup(url,headers=None,**kw):
 # Network errors, 429 and 5xx are transient: nothing was learned, and nothing is published.
 try:status,body=status_request(url,headers=headers,**kw)
 except Exception:raise TransientFailure('lookup_incomplete') from None
 if status==429 or status>=500:raise TransientFailure('lookup_incomplete')
 return status,body
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
  text=subprocess.check_output(['docker','exec',DATABASE_CONTAINER,'psql','-X','-qAt','-U',DATABASE_ROLE,'-d',DATABASE_NAME,'-v','ON_ERROR_STOP=1','-c',SQL],timeout=8,stderr=subprocess.DEVNULL,text=True)
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
 total=INITIAL_EXTERNAL
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
  if pairs.get('FANSAPI_BASE')!='https://api.fansapi.com' or pairs.get('FANSAPI_ACCT')!=PROVIDER_ACCOUNT or not pairs.get('FANSAPI_KEY'):raise ValueError()
  today=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
  query=urlencode({'from':baseline['start'],'to':today,'group_by':'endpoint','include_today':'true'})
  status,body=lookup('https://api.fansapi.com/api/usage/credits?'+query,headers={'Authorization':'Bearer '+pairs['FANSAPI_KEY'],'Accept':'application/json','User-Agent':'Twin-Plan-C/1.0'})
  if status!=200 or len(body)>2_000_000:raise ValueError()
  data=json.loads(body);meta=data['_meta']['_credits']
  return data['data']['totals']['credits'],meta['balance'],meta['used']
 except TransientFailure:raise TransientFailure('credit_monitor_unavailable') from None
 except Exception:raise GuardFailure('credit_monitor_unavailable') from None
def webhook_health(deadline=None):
 # Check the exact dedicated subscription and every unaccounted delivery since a durable
 # verified checkpoint. A failed/ambiguous delivery causes a stop; no hold is cleared.
 try:
  pairs={}
  for line in ENV.read_text().splitlines():
   if not line.strip() or line.lstrip().startswith('#'):continue
   k,sep,v=line.partition('=')
   if not sep or k in pairs:raise ValueError()
   pairs[k]=v
  if pairs.get('FANSAPI_BASE')!='https://api.fansapi.com' or pairs.get('FANSAPI_ACCT')!=PROVIDER_ACCOUNT or not pairs.get('FANSAPI_KEY'):raise ValueError()
  headers={'Authorization':'Bearer '+pairs['FANSAPI_KEY'],'Accept':'application/json','User-Agent':'Twin-Plan-C/1.0'}
  expected=readjson(ROOT/'evidence'/'webhook-registration.log')
  wid=WEBHOOK_ID
  if expected.get('id')!=wid or expected.get('url')!=PUBLIC or expected.get('account_ids')!=[pairs['FANSAPI_ACCT']] or expected.get('account_scope')!='inclusive':raise ValueError()
  status,body=lookup('https://api.fansapi.com/api/webhooks/'+wid,headers=headers)
  if status!=200 or len(body)>2000000:raise ValueError()
  obj=json.loads(body);row=obj['data']
  if (type(obj['_meta']['_credits']['used']) is not int or obj['_meta']['_credits']['used']!=0) or row.get('id')!=wid or row.get('url')!=PUBLIC or row.get('enabled') is not True or row.get('has_signing_secret') is not True or row.get('account_scope')!='inclusive' or row.get('account_ids')!=[pairs['FANSAPI_ACCT']] or set(row.get('events',[]))!=set(expected['events']):raise ValueError()
  if 'paused' in row and row['paused'] is not False:raise ValueError()
  def page(start,limit,offset):
   status,body=lookup('https://api.fansapi.com/api/webhooks/'+wid+'/deliveries?'+urlencode({'date_start':start,'limit':limit,'offset':offset}),headers=headers,timeout=12)
   if status!=200 or len(body)>2000000:raise delivery_history.HistoryError('delivery_page_unavailable')
   return json.loads(body)
  sup=ROOT/'supervision-v11'
  delivery_history.verify_history(page,boundary_path=sup/'delivery-enable-boundary.json',checkpoint_path=sup/'delivery-checkpoint.json',
   intent_path=ROOT/'evidence'/'webhook-enable-v2'/'put-intent.json',webhook_id=wid,url=PUBLIC,subscription=row,events=set(expected['events']),
   account_id=pairs['FANSAPI_ACCT'],resolved_intervals=resolved_intervals(sup),deadline=deadline)
  return True
 except TransientFailure:raise TransientFailure('provider_webhook_health_unavailable') from None
 except delivery_history.TransientHistoryError:raise TransientFailure('provider_webhook_health_unavailable') from None
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
 webhook_health(started+25)
 total,balance,cost=team_usage(baseline)
 estimate=credit_estimate(baseline_total=baseline['team_credits_observed'],baseline_balance=baseline['team_balance'],team_total=total,team_balance=balance,usage_cost=cost,scope_reserved=row.get('team_reserved',row['reserved']),external_reserved=outside,prior_estimate=state['prior_estimate'],credit_limit=CREDIT_STOP)
 # Bound proof age before the caller can publish STATE; request timeouts are
 # socket/request limits, not a whole-check wall-clock guarantee.
 if time.monotonic()-started>30:raise GuardFailure('monitor_check_too_old')
 result={'prior_estimate':estimate,'last_ok_utc':now(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'last_ok_monotonic':time.monotonic(),'scope_status':'transition' if pending else row['status'],'scope_revision':row['revision']}
 if CONFIG is not None:
  result.update(account_id=CONFIG['account'],binding_id=CONFIG['binding'],connection_epoch=CONFIG['epoch'])
 if pending:result['transition_start']=transition_start
 return result
def run():
 if unresolved(INCIDENT):raise GuardFailure('outage_reconciliation_required')
 state=readjson(STATE)
 if type(state) is not dict or type(state.get('prior_estimate')) not in (int,float):raise GuardFailure('persistent_state_malformed')
 if db_state()['status']=='active':raise GuardFailure('restart_requires_stopped_scope')
 failures=0
 while True:
  try:fresh=check(state)
  except TransientFailure as exc:
   # Incomplete evidence: no STATE, no lease, no READY/WATCHDOG. The sender's 45s limit lapses on its own.
   failures+=1
   if failures>MAX_TRANSIENT:raise GuardFailure(str(exc)) from None
   time.sleep(10);continue
  failures=0;state=fresh;atomic(STATE,state)
  notify('READY=1\nWATCHDOG=1\nSTATUS=Pilot checks passed')
  time.sleep(10)
def retain_sender_logs():
 if CONFIG is None:return False
 try:
  import uuid
  raw=subprocess.check_output(['docker','logs','--timestamps','--tail','200',CONFIG['senderContainer']],stderr=subprocess.STDOUT,timeout=3)
  directory=ROOT/'evidence';directory.mkdir(exist_ok=True)
  path=directory/('sender-before-stop-'+uuid.uuid4().hex+'.log')
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as stream:
   stream.write(raw[:262144]);stream.flush();os.fsync(stream.fileno())
  print('sender_logs_retained:'+str(path),file=sys.stderr,flush=True)
  return True
 except Exception:return False

if __name__=='__main__':
 try:
  from operation_config import load
  configure(load())
  context=readjson(STATE)
  if (context.get('account_id'),context.get('binding_id'),context.get('connection_epoch')) != (CONFIG['account'],CONFIG['binding'],CONFIG['epoch']):
   raise GuardFailure('persistent_state_scope_mismatch')
  run()
 except GuardFailure as exc:
  retain_sender_logs()
  print('pilot_supervisor_stopped:'+str(exc),file=sys.stderr)
  raise SystemExit(1)
 except Exception:
  retain_sender_logs()
  print('pilot_supervisor_stopped:unexpected_failure',file=sys.stderr)
  raise SystemExit(1)
