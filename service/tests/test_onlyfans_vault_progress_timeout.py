import unittest
from datetime import datetime, timedelta
from onlyfans_runtime.vault_management import _vault_job_expired

class VaultSyncProgressTimeoutTests(unittest.TestCase):
    now = datetime(2026, 10, 25, 12, 35)

    def test_advancing_import_survives_old_request(self):
        self.assertFalse(_vault_job_expired(
            self.now - timedelta(hours=2), self.now - timedelta(minutes=5), self.now))

    def test_stalled_import_expires(self):
        self.assertTrue(_vault_job_expired(
            self.now - timedelta(hours=2), self.now - timedelta(hours=1, seconds=1), self.now))

    def test_old_import_without_progress_expires(self):
        self.assertTrue(_vault_job_expired(
            self.now - timedelta(hours=2), None, self.now))

    def test_new_queued_import_keeps_its_window(self):
        self.assertFalse(_vault_job_expired(
            self.now - timedelta(minutes=30), None, self.now))

    def test_exact_hour_boundary_is_preserved(self):
        self.assertFalse(_vault_job_expired(
            self.now - timedelta(hours=2), self.now - timedelta(hours=1), self.now))

    def test_older_progress_cannot_shorten_new_request_window(self):
        self.assertFalse(_vault_job_expired(
            self.now - timedelta(minutes=15), self.now - timedelta(hours=2), self.now))

if __name__ == "__main__":
    unittest.main()
