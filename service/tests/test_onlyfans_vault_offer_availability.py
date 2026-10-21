"""Offer identity checks are independent of full-file download/playback support."""
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace

from onlyfans_runtime.store import utcnow
from onlyfans_runtime.vault_management import _ready, digest
from onlyfans_runtime.vault_video import protected_source


class NativeVaultOfferAvailability(unittest.TestCase):
    def fixture(self, **changes):
        raw = {"id": 13, "type": "video", "isReady": True, "canView": True,
               "files": {"full": {"url": None}, "drm": {"manifest": {"dash": "fixture"}}}}
        raw.update(changes)
        row = SimpleNamespace(media_id=13, kind="video", removed_at=None, raw_json=json.dumps(raw))
        observation = SimpleNamespace(raw_hash=digest(row.raw_json))
        job = SimpleNamespace(binding_id="binding-a", owner_id=101, connection_revision=2)
        connection = SimpleNamespace(binding_id="binding-a", owner_id=101, revision=2,
            status="verified", expires_at=utcnow() + timedelta(hours=1))
        return row, observation, job, connection, "binding-a"

    def test_protected_vault_identity_is_available_for_an_offer(self):
        args = self.fixture()
        self.assertEqual(_ready(*args), (True, None))
        self.assertTrue(protected_source(json.loads(args[0].raw_json)))

    def test_progressive_and_url_less_assets_use_provider_readiness(self):
        for files in [None, {}, {"full": {"url": "https://synthetic.invalid/file.mp4"}}]:
            with self.subTest(files=files):
                self.assertEqual(_ready(*self.fixture(files=files)), (True, None))

    def test_visibility_processing_and_readiness_holds_remain(self):
        for patch, reason in [({"isReady": False}, "not_ready"), ({"isProcessing": True}, "not_ready"),
                              ({"canView": False}, "not_ready"), ({"isDeleted": True}, "removed"),
                              ({"isHidden": True}, "removed")]:
            with self.subTest(patch=patch):
                self.assertEqual(_ready(*self.fixture(**patch)), (False, reason))
        args = self.fixture(); raw = json.loads(args[0].raw_json); del raw["isReady"]
        args[0].raw_json = json.dumps(raw); args[1].raw_hash = digest(args[0].raw_json)
        self.assertEqual(_ready(*args), (False, "readiness_unknown"))

    def test_owner_connection_revision_and_raw_identity_holds_remain(self):
        for field, value in [("status", "expired"), ("revision", 3), ("owner_id", 102),
                             ("binding_id", "foreign"), ("expires_at", utcnow() - timedelta(seconds=1))]:
            with self.subTest(field=field):
                args = self.fixture(); setattr(args[3], field, value)
                self.assertEqual(_ready(*args), (False, "unverified_owner"))
        args = self.fixture(); args[1].raw_hash = "0" * 64
        self.assertEqual(_ready(*args), (False, "unverified_owner"))
        self.assertEqual(_ready(*self.fixture(id=14)), (False, "unverified_owner"))
        self.assertEqual(_ready(*self.fixture(type="photo")), (False, "unverified_owner"))


if __name__ == "__main__":
    unittest.main()
