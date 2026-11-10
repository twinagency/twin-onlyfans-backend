import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import supervisor

class QueueCreditReservationTests(unittest.TestCase):
 def test_existing_guard_counts_all_nine_added_read_credits_and_refuses_malformed_reservations(self):
  with tempfile.TemporaryDirectory() as d,patch.object(supervisor,'ROOT',Path(d)),patch.object(supervisor,'RESERVATIONS',[]):
   self.assertEqual(supervisor.external_reservations(),38)
   for rel,amount in supervisor.OPTIONAL:
    p=Path(d)/'evidence'/rel;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps({'credits_reserved':amount}))
   self.assertEqual(supervisor.external_reservations(),47)
   p.write_text(json.dumps({'credits_reserved':999}))
   with self.assertRaises(supervisor.GuardFailure):supervisor.external_reservations()
