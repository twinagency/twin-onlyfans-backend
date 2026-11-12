"""status_json keeps a sanitized status and body excerpt so a readiness stop is explainable. The pinned host
guard_core is not touched; GuardFailure here is the same plain Exception subclass the host copy defines."""
import importlib.util,json,sys,types,unittest
from pathlib import Path
from unittest.mock import patch

class GuardFailure(RuntimeError):pass
spec=importlib.util.spec_from_file_location('status_json_subject',Path(__file__).with_name('supervisor.py'))
m=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'guard_core':types.SimpleNamespace(GuardFailure=GuardFailure,credit_estimate=lambda **kw:207,require_health=lambda **kw:None)}):spec.loader.exec_module(m)
TOKEN='Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF'

def failure(status,body):
 with patch.object(m,'status_request',return_value=(status,body)):
  with unittest.TestCase().assertRaises(GuardFailure) as caught:m.status_json('http://127.0.0.1:1/readyz')
 return str(caught.exception)

class StatusJsonTests(unittest.TestCase):
 def test_control_ready_body_is_returned_unchanged(self):
  with patch.object(m,'status_request',return_value=(200,b'{"ok":true,"code":"ready"}')):
   self.assertEqual(m.status_json('http://x/readyz'),{'ok':True,'code':'ready'})
 def test_503_keeps_the_status_and_the_readiness_code(self):
  text=failure(503,json.dumps({'ok':False,'mode':'active','code':'loop_failed','consecutiveFailures':3}).encode())
  self.assertTrue(text.startswith('readiness_unavailable'))
  self.assertIn('status=503',text);self.assertIn('loop_failed',text)
 def test_non_json_error_page_is_excerpted_one_line_and_redacted(self):
  text=failure(502,('<html>\n<body>Bad Gateway '+TOKEN+'\x00\x07</body></html>'+'x'*3000).encode())
  self.assertIn('status=502',text);self.assertIn('Bad Gateway',text)
  self.assertNotIn(TOKEN,text);self.assertNotIn('abcdefghijklmnopqrstuvwxyz0123456789',text)
  self.assertNotIn('\n',text);self.assertNotIn('\x00',text);self.assertLessEqual(len(text),400)
 def test_oversized_body_is_flagged_and_still_bounded(self):
  text=failure(200,b'{"pad":"'+b'a'*2_000_001+b'"}')
  self.assertTrue(text.startswith('readiness_unavailable'));self.assertIn('oversize',text);self.assertLessEqual(len(text),400)
 def test_malformed_200_keeps_status_and_excerpt_under_its_own_code(self):
  text=failure(200,b'<<not json>>')
  self.assertTrue(text.startswith('readiness_malformed'));self.assertIn('status=200',text);self.assertIn('<<not json>>',text)
 def test_failure_object_exposes_the_same_evidence(self):
  with patch.object(m,'status_request',return_value=(503,b'{"code":"loop_stale"}')):
   with self.assertRaises(GuardFailure) as caught:m.status_json('http://x/readyz')
  self.assertEqual(caught.exception.status,503);self.assertIn('loop_stale',caught.exception.body_excerpt)

if __name__=='__main__':unittest.main()
