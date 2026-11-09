"""Timing owner tests. Unchanged host guard_core functions are explicit mocks."""
import importlib.util,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch

class GuardFailure(RuntimeError):pass
spec=importlib.util.spec_from_file_location('timing_subject',Path(__file__).with_name('supervisor.py'))
m=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'guard_core':types.SimpleNamespace(GuardFailure=GuardFailure,credit_estimate=lambda **kw:207,require_health=lambda **kw:None)}):spec.loader.exec_module(m)

class TimingTests(unittest.TestCase):
 def test_request_default_and_explicit_delivery_timeout_and_read_bound(self):
  response=Mock();response.status=200;response.read.return_value=b'{}'
  opener=Mock();opener.open.return_value.__enter__=Mock(return_value=response);opener.open.return_value.__exit__=Mock(return_value=False)
  with patch.object(m,'OPENER',opener):
   m.status_request('https://api.fansapi.com/api/webhooks/test')
   m.status_request('https://api.fansapi.com/api/webhooks/test/deliveries',timeout=12)
  self.assertEqual([c.kwargs['timeout'] for c in opener.open.call_args_list],[6,12])
  self.assertEqual(response.read.call_args.args,(2_000_001,))
 def test_delivery_timeout_does_not_change_subscription_timeout_or_hold_on_error(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'evidence/webhook-enable-v2').mkdir(parents=True)
   (root/'evidence/webhook-enable-v2/put-intent.json').write_text('{}')
   env=root/'env';env.write_text('FANSAPI_BASE=https://api.fansapi.com\nFANSAPI_ACCT=acct_7bf8540de5a84f459058490fee26c47f\nFANSAPI_KEY=mock\n')
   expected={'id':'wh_db5f1a0931514ef9b285c412bede5713','url':m.PUBLIC,'account_ids':['acct_7bf8540de5a84f459058490fee26c47f'],'account_scope':'inclusive','events':['messages.received']}
   show={'data':{**expected,'enabled':True,'has_signing_secret':True},'_meta':{'_credits':{'used':0}}}
   logs={'data':[{'succeeded':True,'status_code':202,'url':m.PUBLIC,'event':'messages.received','payload':{'account_id':expected['account_ids'][0]}}], '_pagination':{'next_page':None},'_meta':{'_credits':{'used':0}}}
   with patch.object(m,'ROOT',root),patch.object(m,'ENV',env),patch.object(m,'readjson',return_value=expected),patch.object(m,'status_request',side_effect=[(200,json.dumps(show).encode()),(200,json.dumps(logs).encode())]) as request:
    self.assertTrue(m.webhook_health())
    self.assertNotIn('timeout',request.call_args_list[0].kwargs)
    self.assertEqual(request.call_args_list[1].kwargs['timeout'],12)
   for changed in ({**logs,'_pagination':{'next_page':'more'}},{**logs,'data':[]},{**logs,'_meta':{'_credits':{'used':1}}}):
    with patch.object(m,'ROOT',root),patch.object(m,'ENV',env),patch.object(m,'readjson',return_value=expected),patch.object(m,'status_request',side_effect=[(200,json.dumps(show).encode()),(200,json.dumps(changed).encode())]):
     with self.assertRaises(GuardFailure):m.webhook_health()
   with patch.object(m,'ROOT',root),patch.object(m,'ENV',env),patch.object(m,'readjson',return_value=expected),patch.object(m,'status_request',side_effect=TimeoutError):
    with self.assertRaisesRegex(GuardFailure,'provider_webhook_health_unavailable'):m.webhook_health()
 def check_at(self,elapsed):
  state={'prior_estimate':207}
  with patch.object(m,'readjson',return_value={'team_credits_observed':0,'team_balance':848}),patch.object(m,'db_state',return_value={'status':'stopped','reserved':92,'revision':20}),patch.object(m,'external_reservations',return_value=0),patch.object(m,'status_request',return_value=(401,b'')),patch.object(m,'intake_readiness',return_value={'ok':True}),patch.object(m,'webhook_health',return_value=True),patch.object(m,'team_usage',return_value=(0,848,0)),patch.object(m.time,'monotonic',side_effect=[100,100+elapsed,100+elapsed]),patch.object(m,'atomic') as atomic:
   try:return m.check(state)
   finally:
    atomic.assert_not_called();self.assertEqual(state,{'prior_estimate':207})
 def test_whole_check_limit_keeps_existing_45_second_wire_lease(self):
  result=self.check_at(30)
  self.assertEqual(result['last_ok_monotonic'],130)
  self.assertNotIn('lease',result)
  with self.assertRaisesRegex(GuardFailure,'monitor_check_too_old'):self.check_at(30.001)
 def test_overlong_run_cannot_publish_state_or_notify_permission(self):
  with patch.object(m,'unresolved',return_value=False),patch.object(m,'readjson',return_value={'prior_estimate':207}),patch.object(m,'db_state',return_value={'status':'stopped'}),patch.object(m,'check',side_effect=GuardFailure('monitor_check_too_old')),patch.object(m,'atomic') as atomic,patch.object(m,'notify') as notify:
   with self.assertRaisesRegex(GuardFailure,'monitor_check_too_old'):m.run()
   atomic.assert_not_called();notify.assert_not_called()

if __name__=='__main__':unittest.main()
