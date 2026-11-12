"""Supervisor webhook_health + run() loop with the real checkpoint and a stateful fake provider.

guard_core is a host-only file; it is replaced here by an explicit stub with the same three
names (GuardFailure, credit_estimate, require_health). Nothing here weakens or reimplements it.
"""
import datetime,importlib.util,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
import delivery_history as h
from test_delivery_history import row,at,T0,ACCT,EVENTS

class GuardFailure(RuntimeError):pass
spec=importlib.util.spec_from_file_location('loop_subject',Path(__file__).with_name('supervisor.py'))
m=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'guard_core':types.SimpleNamespace(GuardFailure=GuardFailure,credit_estimate=lambda **kw:207,require_health=lambda **kw:None)}):spec.loader.exec_module(m)
from test_operation_config import fixture_config
m.configure(fixture_config())

WID='wh_db5f1a0931514ef9b285c412bede5713'
class Stop(Exception):pass

def listing(rows,start,limit,offset):
 keep=sorted([r for r in rows if h.parse_ts(r['created_at'])>=h.parse_ts(start)],key=lambda r:r['created_at'],reverse=True)
 page=keep[offset:offset+limit]
 return {'data':page,'_pagination':{'next_page':'more' if offset+limit<len(keep) else None,'total':len(keep)},'_meta':{'_credits':{'used':0}}}

class World:
 """Provider + files for supervisor.webhook_health."""
 def __init__(self,case):
  self.case=case;self.root=Path(case.tmp.name)
  (self.root/'evidence/webhook-enable-v2').mkdir(parents=True);(self.root/'supervision-v11').mkdir()
  intent=self.root/'evidence/webhook-enable-v2/put-intent.json';intent.write_text('{}')
  import os;os.utime(intent,(T0.timestamp(),T0.timestamp()))
  self.env=self.root/'env';self.env.write_text('FANSAPI_BASE=https://api.fansapi.com\nFANSAPI_ACCT=%s\nFANSAPI_KEY=mock\n'%'acct_7bf8540de5a84f459058490fee26c47f')
  self.acct='acct_7bf8540de5a84f459058490fee26c47f'
  self.expected={'id':WID,'url':m.PUBLIC,'account_ids':[self.acct],'account_scope':'inclusive','events':['messages.received']}
  self.show={'data':{**self.expected,'enabled':True,'has_signing_secret':True},'_meta':{'_credits':{'used':0}}}
  self.rows=[];self.requests=[];self.down=0
 def mk(self,i,minutes,ok=True,key=None):
  r=row(i,minutes,ok=ok,key=key);r['url']=m.PUBLIC;r['payload']['account_id']=self.acct;return r
 def status_request(self,url,method='GET',headers=None,*,timeout=6):
  self.requests.append(url)
  if self.down>0:
   self.down-=1;raise GuardFailure('network_check_failed')
  if '/deliveries?' in url:
   from urllib.parse import urlparse,parse_qs
   q=parse_qs(urlparse(url).query)
   return 200,json.dumps(listing(self.rows,q['date_start'][0],int(q['limit'][0]),int(q['offset'][0]))).encode()
  return 200,json.dumps(self.show).encode()
 def patches(self):
  return [patch.object(m,'ROOT',self.root),patch.object(m,'ENV',self.env),
          patch.object(m,'readjson',side_effect=lambda p:self.expected if p.name=='webhook-registration.log' else json.loads(p.read_text())),
          patch.object(m,'status_request',side_effect=self.status_request),
          patch.object(m,'GuardFailure',GuardFailure)]
 def health(self,**kw):
  ps=self.patches()
  for p in ps:p.start()
  try:return m.webhook_health(**kw)
  finally:
   for p in reversed(ps):p.stop()
 def cp(self):return json.loads((self.root/'supervision-v11/delivery-checkpoint.json').read_bytes())

class HealthTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.w=World(self)
 def tearDown(self):self.tmp.cleanup()
 def test_150_plus_deliveries_over_multiple_pages_pass_through_the_real_supervisor_path(self):
  self.w.rows=[self.w.mk(i,1+i*0.1) for i in range(230)]
  self.assertTrue(self.w.health())
  pages=[u for u in self.w.requests if '/deliveries?' in u]
  self.assertEqual(len(pages),3);self.assertIn('offset=200',pages[2])
  self.assertEqual(self.w.cp()['accounted_count'],230)
 def test_new_failed_delivery_stops_permission_old_failure_stays_held(self):
  self.w.rows=[self.w.mk(i,1+i) for i in range(5)]
  self.assertTrue(self.w.health())
  before=(self.w.root/'supervision-v11/delivery-checkpoint.json').read_bytes()
  self.w.rows.append(self.w.mk(50,3,ok=False))   # failed delivery dated older than the cursor
  for _ in range(2):
   with self.assertRaisesRegex(GuardFailure,'provider_webhook_health_unavailable'):self.w.health()
  self.assertEqual((self.w.root/'supervision-v11/delivery-checkpoint.json').read_bytes(),before)
 def test_changed_url_or_subscription_rejects_the_old_checkpoint(self):
  self.w.rows=[self.w.mk(i,1+i) for i in range(5)];self.assertTrue(self.w.health())
  self.w.show['data']['events']=['messages.received','extra.event'];self.w.expected['events']=['messages.received','extra.event']
  with self.assertRaisesRegex(GuardFailure,'provider_webhook_health_unavailable'):self.w.health()
 def test_two_transient_lookup_failures_then_success_publish_nothing_and_lose_no_gap(self):
  self.w.rows=[self.w.mk(i,1+i) for i in range(5)];self.assertTrue(self.w.health())
  before=(self.w.root/'supervision-v11/delivery-checkpoint.json').read_bytes()
  self.w.rows+=[self.w.mk(100+i,10+i) for i in range(4)]   # deliveries arrive during the provider outage
  for _ in range(2):
   self.w.down=1
   with self.assertRaises(m.TransientFailure):self.w.health()
   self.assertEqual((self.w.root/'supervision-v11/delivery-checkpoint.json').read_bytes(),before)   # no advance on uncertainty
  self.assertTrue(self.w.health())
  self.assertEqual(self.w.cp()['accounted_count'],9)   # the gap since the checkpoint was read in full
 def test_http_recovery_does_not_prove_the_missed_interval(self):
  self.w.rows=[self.w.mk(i,1+i) for i in range(5)];self.assertTrue(self.w.health())
  self.w.rows.append(self.w.mk(200,9,ok=False))    # a delivery failed while we could not look
  self.w.down=1
  with self.assertRaises(m.TransientFailure):self.w.health()
  with self.assertRaisesRegex(GuardFailure,'provider_webhook_health_unavailable'):self.w.health()   # lookups work again: still held
 def test_http_5xx_and_429_are_transient_but_identity_mismatch_is_not(self):
  for code in (429,500,503):
   with patch.object(m,'status_request',return_value=(code,b'')):
    with self.assertRaises(m.TransientFailure):m.lookup('https://x')
  with patch.object(m,'status_request',return_value=(401,b'')):self.assertEqual(m.lookup('https://x'),(401,b''))
  self.w.rows=[self.w.mk(1,2),self.w.mk(2,3)];self.w.show['data']['url']='https://elsewhere.example/'
  with self.assertRaises(GuardFailure) as caught:self.w.health()
  self.assertNotIsInstance(caught.exception,m.TransientFailure)

class LoopTests(unittest.TestCase):
 def run_loop(self,checks):
  published=[];notified=[];sleeps=[]
  def sleep(n):
   sleeps.append(n)
   if len(sleeps)>=len(checks):raise Stop()
  patches=[patch.object(m,'unresolved',return_value=False),patch.object(m,'readjson',return_value={'prior_estimate':207}),
   patch.object(m,'db_state',return_value={'status':'stopped'}),patch.object(m,'check',side_effect=checks),
   patch.object(m,'atomic',side_effect=lambda p,d:published.append(d)),patch.object(m,'notify',side_effect=notified.append),
   patch.object(m.time,'sleep',side_effect=sleep),patch.object(m,'GuardFailure',GuardFailure)]
  for p in patches:p.start()
  try:
   try:m.run();error=None
   except (GuardFailure,Stop) as e:error=e
  finally:
   for p in reversed(patches):p.stop()
  return published,notified,error
 def ok(self,n):return {'prior_estimate':207,'last_ok_utc':'t%d'%n}
 def test_two_transient_failures_then_success_publish_state_only_after_success(self):
  t=m.TransientFailure
  published,notified,error=self.run_loop([t('provider_webhook_health_unavailable'),t('provider_webhook_health_unavailable'),self.ok(1)])
  self.assertIsInstance(error,Stop)
  self.assertEqual([p['last_ok_utc'] for p in published],['t1'])    # nothing was published for the two uncertain checks
  self.assertEqual(len(notified),1)
 def test_third_consecutive_transient_failure_stops_with_the_ordinary_failure_class(self):
  t=m.TransientFailure
  published,notified,error=self.run_loop([t('provider_webhook_health_unavailable')]*3)
  self.assertIsInstance(error,GuardFailure);self.assertNotIsInstance(error,t)
  self.assertEqual(str(error),'provider_webhook_health_unavailable')
  self.assertEqual((published,notified),([],[]))
 def test_success_resets_the_consecutive_counter(self):
  t=m.TransientFailure('credit_monitor_unavailable')
  published,notified,error=self.run_loop([t,t,self.ok(1),t,t,self.ok(2)])
  self.assertIsInstance(error,Stop);self.assertEqual(len(published),2)
 def test_fatal_failure_is_never_tolerated(self):
  published,notified,error=self.run_loop([GuardFailure('credit_monitor_unavailable')])
  self.assertIsInstance(error,GuardFailure);self.assertEqual((published,notified),([],[]))

if __name__=='__main__':unittest.main()
