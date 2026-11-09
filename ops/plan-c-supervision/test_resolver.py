"""Manual command boundaries with provider/Docker dependencies mocked explicitly."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import incident_state as st
import resolve_monitor_incident as r

class ResolverTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.sup=self.root/'supervision-v11';self.sup.mkdir()
  (self.root/'evidence').mkdir();(self.root/'config').mkdir()
  self.raw=b'{"resolved":false,"failure":"credit"}';self.h=st.digest(self.raw)
  (self.sup/'outage-open.json').write_bytes(self.raw)
  self.image='sha256:'+'1'*64;self.commit='2'*40
  self.write(self.sup/'reviewed-recovery-release.json',{'image':self.image,'commit':self.commit})
  self.write(self.sup/'budget-state.json',{'prior_estimate':207,'scope_status':'stopped','last_ok_monotonic':0})
  self.write(self.root/'config/pilot-operator-identity.json',{'actor':'op-session:'+'a'*64})
  self.calls=[]
  self.ps=[patch.object(r,'ROOT',self.root),patch.object(r,'SUP',self.sup),patch.object(st,'APPROVED_INCIDENT',self.h),
           patch.object(r,'assert_sender_stopped'),patch.object(r,'command',side_effect=self.fake),
           patch.object(r.sys,'argv',['resolver'])]
  for p in self.ps:p.start()
 def tearDown(self):
  for p in reversed(self.ps):p.stop()
  self.tmp.cleanup()
 def write(self,path,value):path.write_text(json.dumps(value))
 def fake(self,args,**kw):
  self.calls.append(args)
  if args[:3]==['docker','image','inspect']:
   return json.dumps([{'Id':self.image,'Config':{'Labels':{'org.opencontainers.image.revision':self.commit}}}]).encode()
  if args[0]==r.sys.executable:
   target=Path(args[2]);target.mkdir()
   self.write(target/'manifest.json',{'scope_revision':12,'credit_estimate':208})
   (target/'journal.txt').write_text('fixture monitor failure')
   return b'{}'
  if '/opt/validate.py' in args:return b'{"interval_valid_for_bounded_scope":true}'
  if '/opt/audit.py' in args:
   evidence=next(p for p in (self.root/'evidence').iterdir() if p.is_dir())
   proof=json.loads((evidence/'receipt.json').read_bytes())
   return json.dumps({'recorded':True,'sendingEnabled':False,'evidenceSha256':proof['evidence_sha256']}).encode()
  raise AssertionError('unexpected external operation')
 def test_success_stays_stopped_and_replay_makes_no_new_provider_call(self):
  r.main()
  self.assertFalse(st.unresolved(self.sup/'outage-open.json'))
  budget=json.loads((self.sup/'budget-state.json').read_bytes())
  self.assertEqual((budget['prior_estimate'],budget['scope_status']),(208,'stopped'))
  count=len([x for x in self.calls if x[0]==r.sys.executable])
  r.main()
  self.assertEqual(len([x for x in self.calls if x[0]==r.sys.executable]),count)
 def test_crash_after_audit_can_resume_exact_saved_evidence(self):
  original=st.atomic
  def fail(path,raw):
   if Path(path).name=='incident-resolution.json':raise RuntimeError('injected publication crash')
   return original(path,raw)
  with patch.object(st,'atomic',side_effect=fail):
   with self.assertRaises(RuntimeError):r.main()
  self.assertTrue(st.unresolved(self.sup/'outage-open.json'))
  evidence=next(p for p in (self.root/'evidence').iterdir() if p.is_dir())
  with patch.object(r.sys,'argv',['resolver','--resume',str(evidence)]):r.main()
  self.assertFalse(st.unresolved(self.sup/'outage-open.json'))
  self.assertEqual(len([x for x in self.calls if x[0]==r.sys.executable]),1)
 def test_changed_incident_refuses_before_collection(self):
  (self.sup/'outage-open.json').write_text('new failure')
  with self.assertRaises(RuntimeError):r.main()
  self.assertFalse(any(x[0]==r.sys.executable for x in self.calls))
if __name__=='__main__':unittest.main()
