import base64,hashlib,json,tempfile,unittest
from unittest.mock import patch
import copy
from pathlib import Path
import validate_interval as v

class IntervalTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.p=Path(self.t.name)
  self.pin=patch.object(v,'REQUIRED_PILOT_RECEIPT','key');self.pin.start()
  self.payload={'event':'messages.received','account_id':v.PROVIDER,'payload':{'fromUser':{'id':318022380},'text':'fixture'}}
  raw=json.dumps(self.payload).encode()
  self.row={'idempotency_key':'key','event':'messages.received','payload':self.payload,'succeeded':True,'status_code':202,'url':v.URL}
  self.local={'receipts':[{'key':'key','raw':base64.b64encode(raw).decode(),'hash':hashlib.sha256(raw).hexdigest(),'status':'accepted','projection':'projected'}],'digests':[]}
  self.rows=[self.row];self.page={'next_page':None}
 def tearDown(self):self.pin.stop();self.t.cleanup()
 def run_case(self):
  for name,obj in [('manifest.json',{'provider_delivery_count':len(self.rows),**getattr(self,'manifest_extra',{})}),('deliveries.json',{'data':self.rows,'_pagination':self.page}),('local-receipts.json',self.local)]:
   (self.p/name).write_text(json.dumps(obj))
  if hasattr(self,'recent'):(self.p/'incident-deliveries.json').write_text(json.dumps(self.recent))
  return v.validate(self.p)
 def test_matches_exact_receipt(self):
  self.assertTrue(self.run_case()['interval_valid_for_bounded_scope'])
 def test_nonpilot_digest_is_retained_as_explicit_limitation(self):
  other=copy.deepcopy(self.row);other['idempotency_key']='other'
  other['payload']['payload']['fromUser']['id']=12345
  self.rows.append(other);self.local['digests']=[{'key':'other','hash':'a'*64,'hold':False}]
  r=self.run_case();self.assertFalse(r['full_event_continuity_claimed'])
  self.assertEqual(r['outside_scope_limitations'][0]['fan'],12345)
 def test_pilot_digest_cannot_replace_receipt(self):
  self.local={'receipts':[],'digests':[{'key':'key','hash':'a'*64,'hold':False}]}
  with self.assertRaises(RuntimeError):self.run_case()
 def test_changed_payload_cannot_match_id_alone(self):
  self.payload['payload']['text']='changed'
  with self.assertRaises(RuntimeError):self.run_case()
 def test_failed_delivery_and_incomplete_page_refuse(self):
  self.row['succeeded']=False
  with self.assertRaises(RuntimeError):self.run_case()
  self.row['succeeded']=True;self.page['next_page']='more'
  with self.assertRaises(RuntimeError):self.run_case()
 def test_empty_listing_cannot_erase_known_postincident_receipt(self):
  self.rows=[]
  with self.assertRaises(RuntimeError):self.run_case()
 def test_dropped_known_receipt_is_not_a_complete_interval(self):
  self.row['idempotency_key']='other'
  self.local['receipts'][0]['key']='other'
  with self.assertRaises(RuntimeError):self.run_case()
 def test_conflict_digest_refuses(self):
  self.payload['payload']['fromUser']['id']=12345
  self.local={'receipts':[],'digests':[{'key':'key','hash':'a'*64,'hold':True}]}
  with self.assertRaises(RuntimeError):self.run_case()
 def second_case(self):
  self.manifest_extra={'incident_sha256':v.WEBHOOK_INCIDENT,'classification':'provider_webhook_health_unavailable','scope_revision':16,'incident_delivery_count':0}
  self.recent={'data':[],'_pagination':{'next_page':None},'_meta':{'_credits':{'used':0}}}
 def test_second_empty_incident_window_needs_full_prior_anchor(self):
  self.second_case();r=self.run_case()
  self.assertEqual(r['reported_incident_window_count'],0)
  self.assertIn('predates',r['anchor_context']);self.assertEqual(r['original_failure_subcause'],'not retained')
  self.assertFalse(r['full_event_continuity_claimed'])
  self.rows=[]
  with self.assertRaises(RuntimeError):self.run_case()
 def test_second_window_cannot_hide_new_or_changed_delivery(self):
  self.second_case();row=copy.deepcopy(self.row);row['idempotency_key']='not-in-full'
  self.recent['data']=[row];self.manifest_extra['incident_delivery_count']=1
  with self.assertRaises(RuntimeError):self.run_case()
 def test_second_window_page_class_scope_and_hash_mixups_refuse(self):
  self.second_case()
  for field,value in [('classification','credit_monitor_unavailable'),('scope_revision',12),('incident_sha256','f'*64)]:
   original=self.manifest_extra[field];self.manifest_extra[field]=value
   with self.assertRaises(RuntimeError):self.run_case()
   self.manifest_extra[field]=original
  self.recent['_pagination']['next_page']='more'
  with self.assertRaises(RuntimeError):self.run_case()
if __name__=='__main__':unittest.main()
