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

class ThirdIntervalTests(unittest.TestCase):
 setUp=IntervalTests.setUp
 tearDown=IntervalTests.tearDown
 run_case=IntervalTests.run_case
 def third_case(self):
  IntervalTests.second_case(self)
  self.manifest_extra.update(incident_sha256=v.REPEATED_WEBHOOK_INCIDENT,scope_revision=20)
 def test_full_anchor_and_separate_incident_window_for_stopped20(self):
  self.third_case();r=self.run_case()
  self.assertEqual(r['reported_incident_window_count'],0)
  self.assertEqual(r['original_failure_subcause'],'not retained')
  self.rows=[]
  with self.assertRaises(RuntimeError):self.run_case()
 def test_stale18_or_wrong_case_and_unmatched_incident_refuse(self):
  self.third_case();self.manifest_extra['scope_revision']=18
  with self.assertRaises(RuntimeError):self.run_case()
  self.manifest_extra['scope_revision']=20
  row=copy.deepcopy(self.row);row['idempotency_key']='new'
  self.recent['data']=[row];self.manifest_extra['incident_delivery_count']=1
  with self.assertRaises(RuntimeError):self.run_case()

class SharedClassificationTests(unittest.TestCase):
 """Recovery uses the same classification contract as intake: no unguarded fan extraction."""
 setUp=IntervalTests.setUp
 tearDown=IntervalTests.tearDown
 run_case=IntervalTests.run_case
 def add(self,event,payload,key,hold=False,digest=True):
  body={'event':event,'account_id':v.PROVIDER,'payload':payload}
  self.rows.append({'idempotency_key':key,'event':event,'payload':body,'succeeded':True,'status_code':202,'url':v.URL})
  if digest:self.local['digests'].append({'key':key,'hash':'b'*64,'hold':hold})
 def test_the_five_safe_counters_pass_recovery(self):
  for i,event in enumerate(('messages.unread_count_updated','messages.priority_count_updated','messages.unread_tips_count_updated','calls.count_request_updated','notifications.count_updated')):
   self.add(event,{'count':i},'ctr%d'%i)
  r=self.run_case();self.assertTrue(r['interval_valid_for_bounded_scope'])
  self.assertEqual({f['disposition'] for f in r['outside_scope_limitations']},{'validated_irrelevant'})
 def test_validated_presence_and_like_events_pass_but_need_a_clean_digest(self):
  self.add('users.typing',{'id':318022380},'typing')
  self.add('users.online',{'fan':{'id':601164546}},'online')
  self.add('messages.liked',{'id':5,'chat_id':None,'fan_id':None,'fan':None},'liked')
  self.assertTrue(self.run_case()['interval_valid_for_bounded_scope'])
  self.local['digests'][0]['hold']=True
  with self.assertRaises(RuntimeError):self.run_case()
 def test_bad_counters_and_unverified_types_refuse_without_crashing(self):
  for event,payload in (('messages.unread_count_updated',{'count':-1}),('messages.unread_count_updated',{'count':True}),
          ('messages.unread_count_updated',{'count':1,'extra':1}),('chat_queue.finished',{'id':1,'total':3}),
          ('chat_queue.updated',{'id':1}),('posts.new',{'id':1}),('users.typing',{'user':{'id':1}})):
   self.rows=self.rows[:1];self.local['digests']=[]
   self.add(event,payload,'x')
   with self.assertRaises(RuntimeError,msg=event):self.run_case()
 def test_material_tip_for_a_pilot_fan_without_a_receipt_refuses_but_other_fan_is_a_limitation(self):
  tip=lambda fan:{'id':'t','amountGross':5,'fan':{'id':fan},'user_id':1}
  self.add('tips.received',tip(318022380),'tip')
  with self.assertRaises(RuntimeError):self.run_case()
  self.rows=self.rows[:1];self.local['digests']=[]
  self.add('tips.received',tip(12345),'tip2')
  r=self.run_case();self.assertEqual(r['outside_scope_limitations'][0]['disposition'],'outside_approved_fans')
