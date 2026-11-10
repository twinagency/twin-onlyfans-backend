import copy,hashlib,json,unittest
import test_validate_interval as base

class QueueIntervalTests(unittest.TestCase):
 setUp=base.IntervalTests.setUp
 tearDown=base.IntervalTests.tearDown
 run_case=base.IntervalTests.run_case
 def setup_queue(self):
  body={'event':'chat_queue.finished','account_id':self.payload['account_id'],'payload':{
    'id':123,'date':'2026-11-09T20:00:00+00:00','canUnsend':False,'hasError':False,
    'isCanceled':False,'isCouplePeopleMedia':False,'isDone':True,'isReady':True,
    'pending':0,'total':100,'unsendSeconds':0}}
  second=copy.deepcopy(body);second['payload']['isCanceled']=True
  bodies=[json.dumps(x) for x in (body,second)]
  hashes=[hashlib.sha256(x.encode()).hexdigest() for x in bodies]
  self.rows.append({**self.row,'idempotency_key':'queue','event':body['event'],'payload':body})
  self.local['digests']=[{'key':'queue','hash':h,'hold':True} for h in hashes]
  self.local['queue_reviews']=[{'before':{'eventKey':'queue','providerAccount':body['account_id'],
    'payloadSha256s':hashes,'rawBodies':bodies},'result':{'outcome':'queue_event_group_reviewed','resolved':True}}]
 def test_exact_reviewed_queue_group_passes_without_modifying_other_evidence(self):
  self.setup_queue()
  r=self.run_case()
  self.assertTrue(r['interval_valid_for_bounded_scope'])
  self.assertFalse(r['full_event_continuity_claimed'])
  self.assertEqual(r['outside_scope_limitations'][0]['disposition'],'audited_queue_group_review')
 def test_missing_review_new_variant_changed_payload_and_provider_refuse(self):
  for bad in ('review','new_variant','payload','provider','event_label','raw_hash'):
   original_rows=copy.deepcopy(self.rows);original_local=copy.deepcopy(self.local)
   try:
    self.setup_queue()
    if bad=='review':self.local['queue_reviews']=[]
    if bad=='new_variant':self.local['digests'].append({'key':'queue','hash':'ee'*32,'hold':True})
    if bad=='payload':self.rows[-1]['payload']['payload']['total']=101
    if bad=='provider':self.rows[-1]['payload']['account_id']='acct_other'
    if bad=='event_label':self.rows[-1]['event']='accounts.disconnected'
    if bad=='raw_hash':self.local['queue_reviews'][0]['before']['rawBodies'][0]+=' '
    with self.assertRaises(RuntimeError,msg=bad):self.run_case()
   finally:self.rows=original_rows;self.local=original_local
