"""Counterexamples from retained provider interval: repeated keys are not row identity."""
import copy
from test_validate_interval import ThirdIntervalTests
class RepeatedDeliveryWindowTests(ThirdIntervalTests):
 def test_two_exact_attempts_with_one_key_are_both_preserved(self):
  self.third_case()
  self.row['id']='attempt-a'
  retry=copy.deepcopy(self.row);retry['id']='attempt-b'
  self.rows.append(retry)
  self.recent['data']=copy.deepcopy(self.rows);self.manifest_extra['incident_delivery_count']=2
  self.assertTrue(self.run_case()['interval_valid_for_bounded_scope'])
 def test_same_key_changed_payload_or_status_is_not_window_evidence(self):
  self.third_case()
  self.row['id']='attempt-a'
  for field,value in [('status_code',500),('url',self.row['url']+'/other')]:
   changed=copy.deepcopy(self.row);changed[field]=value
   self.recent['data']=[changed];self.manifest_extra['incident_delivery_count']=1
   with self.assertRaises(RuntimeError):self.run_case()
