import json
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
 def test_resolved_field_alone_is_ineffective(self):
  self.marker.write_text('{"resolved": true}')
  self.assertTrue(m.unresolved(self.marker))

if __name__=='__main__':unittest.main()
