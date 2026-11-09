import base64,hashlib,json,tempfile,unittest
from pathlib import Path
import validate_interval as v

class IntervalTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.p=Path(self.t.name)
  self.payload={'event':'messages.received','account_id':v.PROVIDER,'payload':{'fromUser':{'id':318022380},'text':'fixture'}}
  raw=json.dumps(self.payload).encode()
  self.row={'idempotency_key':'key','event':'messages.received','payload':self.payload,'succeeded':True,'status_code':202,'url':v.URL}
  self.local={'receipts':[{'key':'key','raw':base64.b64encode(raw).decode(),'hash':hashlib.sha256(raw).hexdigest(),'status':'accepted','projection':'projected'}],'digests':[]}
  self.rows=[self.row];self.page={'next_page':None}
 def tearDown(self):self.t.cleanup()
 def run_case(self):
  for name,obj in [('manifest.json',{'provider_delivery_count':len(self.rows)}),('deliveries.json',{'data':self.rows,'_pagination':self.page}),('local-receipts.json',self.local)]:
   (self.p/name).write_text(json.dumps(obj))
  return v.validate(self.p)
 def test_matches_exact_receipt(self):
  self.assertTrue(self.run_case()['interval_valid_for_bounded_scope'])
 def test_nonpilot_digest_is_retained_as_explicit_limitation(self):
  self.payload['payload']['fromUser']['id']=12345
  self.local={'receipts':[],'digests':[{'key':'key','hash':'a'*64,'hold':False}]}
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
 def test_conflict_digest_refuses(self):
  self.payload['payload']['fromUser']['id']=12345
  self.local={'receipts':[],'digests':[{'key':'key','hash':'a'*64,'hold':True}]}
  with self.assertRaises(RuntimeError):self.run_case()
if __name__=='__main__':unittest.main()
