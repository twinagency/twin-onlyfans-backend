import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import incident_state as m

class IncidentTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.raw=b'{"resolved": false, "detected_utc": "fixed", "failure": "credit"}'
  self.marker=self.root/'outage-open.json';self.marker.write_bytes(self.raw)
  self.h=m.digest(self.raw);self.pin=patch.object(m,'APPROVED_INCIDENT',self.h);self.pin.start()
  self.receipt={'version':'plan-c-monitor-resolution.v1','incident_sha256':self.h,'account_id':m.ACCOUNT,
   'binding_id':m.BINDING,'connection_epoch':1,'scope_revision':12,'classification':'credit_monitor_unavailable',
   'actor':'op-session:'+'a'*64,'audit_command_key':'b'*64,'evidence_sha256':'c'*64,'resolved_at':'2026-11-10T00:00:00Z'}
  self.audit=[]
 def tearDown(self):self.pin.stop();self.tmp.cleanup()
 def test_success_and_replay_keep_original_no_permit(self):
  self.assertTrue(m.unresolved(self.marker))
  self.assertEqual(m.resolve(self.root,self.receipt,self.audit.append),'resolved')
  self.assertFalse(m.unresolved(self.marker));self.assertEqual(self.marker.read_bytes(),self.raw)
  self.assertEqual(m.resolve(self.root,self.receipt,self.audit.append),'replayed')
  self.assertEqual(len(self.audit),1);self.assertFalse((self.root/'budget-state.json').exists())
 def test_wrong_and_stale_evidence_stay_held(self):
  for key,value in [('account_id','other'),('binding_id','other'),('connection_epoch',True),('incident_sha256','d'*64),('actor','other')]:
   with self.subTest(key=key):
    with self.assertRaises(RuntimeError):m.resolve(self.root,{**self.receipt,key:value},self.audit.append)
    self.assertTrue(m.unresolved(self.marker))
  self.assertEqual(self.audit,[])
 def test_crash_after_audit_keeps_hold_and_can_retry(self):
  def crash():raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,self.audit.append,after_audit=crash)
  self.assertTrue(m.unresolved(self.marker))
  # Audit callback must implement the existing immutable command's idempotency.
  self.assertEqual(m.resolve(self.root,self.receipt,self.audit.append),'resolved')
 def test_audit_rejection_never_resolves(self):
  def reject(_):raise RuntimeError('stale DB scope')
  with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,reject)
  self.assertTrue(m.unresolved(self.marker))
 def test_new_failure_after_resolution_is_distinct_and_held(self):
  m.resolve(self.root,self.receipt,self.audit.append)
  new=m.record_failure(self.root,{'failure':'new'})
  self.assertNotEqual(new,self.h);self.assertTrue(m.unresolved(self.marker))
  with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,self.audit.append)
 def test_concurrent_failure_waits_then_keeps_new_hold(self):
  entered=threading.Event();release=threading.Event()
  def audit(_):entered.set();release.wait(3)
  errors=[]
  def resolve():
   try:m.resolve(self.root,self.receipt,audit)
   except Exception as e:errors.append(e)
  t=threading.Thread(target=resolve);t.start();self.assertTrue(entered.wait(2))
  f=threading.Thread(target=lambda:m.record_failure(self.root,{'failure':'concurrent'}));f.start()
  time.sleep(.05);self.assertTrue(f.is_alive())
  release.set();t.join(3);f.join(3)
  self.assertFalse(errors);self.assertTrue(m.unresolved(self.marker))
 def test_missing_immutable_resolution_keeps_hold(self):
  m.resolve(self.root,self.receipt,self.audit.append)
  (self.root/'incident-history'/('resolution-'+self.receipt['audit_command_key']+'.json')).unlink()
  self.assertTrue(m.unresolved(self.marker))
 def test_additional_or_different_failure_cannot_use_original_resolution(self):
  valid=b'pilot_supervisor_stopped:credit_monitor_unavailable'
  self.assertEqual(m.classify_journal(valid),'credit_monitor_unavailable')
  for raw in (b'',valid+b'\n'+valid,b'pilot_supervisor_stopped:unsupported_monitor_failure'):
   with self.assertRaises(RuntimeError):m.classify_journal(raw)
 def test_failure_before_collection_retains_each_original_and_stays_held(self):
  first=m.record_failure(self.root,{'failure':'before_collection'})
  second=m.record_failure(self.root,{'failure':'again'})
  self.assertNotEqual(first,second)
  for h in (self.h,first,second):self.assertTrue((self.root/'incident-history'/h).exists())
  with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,self.audit.append)
  self.assertTrue(m.unresolved(self.marker));self.assertFalse(self.audit)
 def test_failure_after_audit_crash_refuses_explicit_old_evidence_resume(self):
  def crash():raise RuntimeError('after DB audit, before publication')
  with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,self.audit.append,after_audit=crash)
  m.record_failure(self.root,{'failure':'after_audit_crash'})
  with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,self.audit.append)
  self.assertEqual(len(self.audit),1);self.assertTrue(m.unresolved(self.marker))
  self.assertEqual((self.root/'incident-history'/self.h).read_bytes(),self.raw)
 def test_worker_read_only_access_and_unreadable_marker_stays_held(self):
  os.chmod(self.root,0o750);os.chown(self.root,0,65532)
  os.chmod(self.marker,0o600);os.chown(self.marker,0,0)
  m.resolve(self.root,self.receipt,self.audit.append)
  gate=self.root/'budget-state.json';m.atomic(gate,b'{}')
  paths=[self.marker,gate,self.root/'incident-resolution.json',
         self.root/'incident-history'/self.h,
         self.root/'incident-history'/('resolution-'+self.receipt['audit_command_key']+'.json')]
  def worker(expect_held):
   pid=os.fork()
   if pid==0:
    try:
     os.setgroups([]);os.setgid(65532);os.setuid(65532)
     if m.unresolved(self.marker)!=expect_held:os._exit(1)
     for path in paths:
      try:fd=os.open(path,os.O_WRONLY)
      except PermissionError:continue
      else:os.close(fd);os._exit(2)
     os._exit(0)
    except BaseException:os._exit(3)
   _,status=os.waitpid(pid,0);self.assertEqual(os.waitstatus_to_exitcode(status),0)
  worker(False)
  access=json.loads((self.root/'incident-history'/('marker-access-'+self.receipt['audit_command_key']+'.json')).read_bytes())
  self.assertEqual(access['before'],{'uid':0,'gid':0,'mode':0o600})
  self.assertEqual(access['after'],{'uid':0,'gid':65532,'mode':0o640})
  self.assertEqual(access['incident_sha256'],self.h)
  os.chmod(self.marker,0o600);worker(True)
 def test_crash_before_publication_keeps_access_audit_and_replays(self):
  original=m.atomic
  def crash(path,raw):
   if Path(path).name=='incident-resolution.json':raise RuntimeError('publication crash')
   original(path,raw)
  with patch.object(m,'atomic',side_effect=crash):
   with self.assertRaises(RuntimeError):m.resolve(self.root,self.receipt,self.audit.append)
  self.assertTrue(m.unresolved(self.marker))
  access=self.root/'incident-history'/('marker-access-'+self.receipt['audit_command_key']+'.json')
  old=access.read_bytes()
  self.assertEqual(m.resolve(self.root,self.receipt,self.audit.append),'resolved')
  self.assertEqual(access.read_bytes(),old);self.assertFalse(m.unresolved(self.marker))
 def second_case(self):
  m.resolve(self.root,self.receipt,self.audit.append)
  h=m.record_failure(self.root,{'failure':'provider_webhook_health_unavailable'})
  receipt={**self.receipt,'incident_sha256':h,'classification':'provider_webhook_health_unavailable',
           'scope_revision':16,'audit_command_key':'d'*64}
  return h,receipt
 def test_second_case_preserves_prior_history_and_replays_without_new_permit(self):
  h,receipt=self.second_case();original=(self.root/'incident-history'/('resolution-'+self.receipt['audit_command_key']+'.json')).read_bytes()
  with patch.object(m,'WEBHOOK_INCIDENT',h):
   for field,value in [('classification','credit_monitor_unavailable'),('scope_revision',12),('incident_sha256','f'*64)]:
    with self.assertRaises(RuntimeError):m.resolve(self.root,{**receipt,field:value},self.audit.append)
   self.assertEqual(m.resolve(self.root,receipt,self.audit.append),'resolved')
   self.assertEqual(m.resolve(self.root,receipt,self.audit.append),'replayed')
   self.assertFalse(m.unresolved(self.marker));self.assertFalse((self.root/'budget-state.json').exists())
  self.assertEqual((self.root/'incident-history'/('resolution-'+self.receipt['audit_command_key']+'.json')).read_bytes(),original)
  self.assertEqual((self.root/'incident-history'/self.h).read_bytes(),self.raw)
 def test_second_case_crash_then_new_failure_cannot_resume_old_evidence(self):
  h,receipt=self.second_case()
  def crash():raise RuntimeError('after second DB audit')
  with patch.object(m,'WEBHOOK_INCIDENT',h):
   with self.assertRaises(RuntimeError):m.resolve(self.root,receipt,self.audit.append,after_audit=crash)
   self.assertTrue(m.unresolved(self.marker))
   m.record_failure(self.root,{'failure':'unlisted third failure'})
   with self.assertRaises(RuntimeError):m.resolve(self.root,receipt,self.audit.append)
   self.assertTrue(m.unresolved(self.marker))
  self.assertTrue((self.root/'incident-history'/h).exists())
 def test_second_case_crash_can_resume_exact_evidence_and_preserve_old_audit(self):
  h,receipt=self.second_case()
  def crash():raise RuntimeError('publication crash')
  with patch.object(m,'WEBHOOK_INCIDENT',h):
   with self.assertRaises(RuntimeError):m.resolve(self.root,receipt,self.audit.append,after_audit=crash)
   self.assertEqual(m.resolve(self.root,receipt,self.audit.append),'resolved')
   self.assertFalse(m.unresolved(self.marker))
  self.assertTrue((self.root/'incident-history'/('resolution-'+self.receipt['audit_command_key']+'.json')).exists())
 def test_second_case_cannot_overwrite_missing_prior_history(self):
  h,receipt=self.second_case();(self.root/'incident-history'/self.h).unlink()
  with patch.object(m,'WEBHOOK_INCIDENT',h):
   with self.assertRaises(RuntimeError):m.resolve(self.root,receipt,self.audit.append)
   self.assertTrue(m.unresolved(self.marker))
 def test_second_case_journal_classification_is_specific(self):
  raw=b'pilot_supervisor_stopped:provider_webhook_health_unavailable'
  self.assertEqual(m.classify_journal(raw,'provider_webhook_health_unavailable'),'provider_webhook_health_unavailable')
  for bad in (raw+b'\n'+raw,b'pilot_supervisor_stopped:credit_monitor_unavailable'):
   with self.assertRaises(RuntimeError):m.classify_journal(bad,'provider_webhook_health_unavailable')
 def test_resolved_field_alone_is_ineffective(self):
  self.marker.write_text('{"resolved": true}')
  self.assertTrue(m.unresolved(self.marker))

if __name__=='__main__':unittest.main()

class ThirdIncidentTests(unittest.TestCase):
 setUp=IncidentTests.setUp
 tearDown=IncidentTests.tearDown
 def test_three_histories_and_audits_survive_third_then_unlisted_failure(self):
  m.resolve(self.root,self.receipt,self.audit.append)
  h2=m.record_failure(self.root,{'failure':'provider_webhook_health_unavailable','case':2})
  r2={**self.receipt,'incident_sha256':h2,'classification':'provider_webhook_health_unavailable','scope_revision':16,'audit_command_key':'d'*64}
  with patch.object(m,'WEBHOOK_INCIDENT',h2):
   m.resolve(self.root,r2,self.audit.append)
   h3=m.record_failure(self.root,{'failure':'provider_webhook_health_unavailable','case':3})
   r3={**r2,'incident_sha256':h3,'scope_revision':20,'audit_command_key':'e'*64}
   with patch.object(m,'REPEATED_WEBHOOK_INCIDENT',h3):
    for field,value in [('scope_revision',18),('scope_revision',16),('classification','credit_monitor_unavailable')]:
     with self.assertRaises(RuntimeError):m.resolve(self.root,{**r3,field:value},self.audit.append)
    def crash():raise RuntimeError('after third DB audit')
    with self.assertRaises(RuntimeError):m.resolve(self.root,r3,self.audit.append,after_audit=crash)
    self.assertTrue(m.unresolved(self.marker))
    m.resolve(self.root,r3,self.audit.append)
    self.assertEqual(m.resolve(self.root,r3,self.audit.append),'replayed')
    self.assertFalse(m.unresolved(self.marker));self.assertFalse((self.root/'budget-state.json').exists())
    for receipt in (self.receipt,r2,r3):
     self.assertTrue((self.root/'incident-history'/receipt['incident_sha256']).exists())
     self.assertEqual((self.root/'incident-history'/('resolution-'+receipt['audit_command_key']+'.json')).read_bytes(),m.encoded(receipt))
    m.record_failure(self.root,{'failure':'unlisted fourth'})
    with self.assertRaises(RuntimeError):m.resolve(self.root,r3,self.audit.append)
    self.assertTrue(m.unresolved(self.marker))
