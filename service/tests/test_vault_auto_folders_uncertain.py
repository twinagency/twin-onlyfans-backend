"""CVA-01 (G07): an item that becomes commercially "uncertain" leaves its internal category folders.

SQLite in-memory only; no model/provider calls, no network.
"""
import json
import unittest
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db.models import Base, Account, VaultItem, VaultFolder, VaultFolderItem
from vault_auto_folders import assign_sync, OWNER_PREFIX
from vault_describe_state import stamp_of

ACCOUNT = "acct-w1"
MID = 11
MISC = OWNER_PREFIX + "miscellaneous"
SELFIES = OWNER_PREFIX + "selfies"
SWIMWEAR = OWNER_PREFIX + "swimwear"


class UncertainStaysInCategory(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[
            Account.__table__, VaultItem.__table__, VaultFolder.__table__, VaultFolderItem.__table__])
        self.s = Session(self.engine)
        self.s.add(Account(id=ACCOUNT))
        self.s.add(VaultItem(account_id=ACCOUNT, media_id=MID, kind="photo", created_at=datetime.utcnow(),
                             tags="[]", content_hash="a" * 64, content_hash_kind="thumb/sha256",
                             ai_fields_json="{}"))
        self.s.flush()

    def tearDown(self):
        self.s.close()
        self.engine.dispose()

    def item(self):
        return self.s.get(VaultItem, (ACCOUNT, MID))

    def analyse(self, text, *, confidence=99):
        item = self.item()
        fields = json.loads(item.ai_fields_json or "{}")
        fields.update({"description": text, "confidence": confidence, "_content_stamp": stamp_of(item)})
        item.ai_fields_json = json.dumps(fields)
        return assign_sync(self.s, ACCOUNT, MID, commercial=True)

    def cls(self):
        return json.loads(self.item().ai_fields_json)["_commercial_classification_v1"]

    def owners(self):
        return sorted(self.s.scalars(select(VaultFolder.created_by).join(
            VaultFolderItem, (VaultFolderItem.account_id == VaultFolder.account_id)
            & (VaultFolderItem.folder_id == VaultFolder.id)).where(
            VaultFolderItem.account_id == ACCOUNT, VaultFolderItem.media_id == MID)).all())

    def test_uncertain_removes_internal_category_memberships(self):
        """A proposed item that drops to low confidence becomes "uncertain" (Miscellaneous only,
        never sellable) and must not keep its automatic category memberships."""
        self.analyse("A casual selfie in a bikini.", confidence=99)
        # control: fixture produces a proposed classification filed in Selfies + Swimwear
        self.assertEqual(self.cls()["status"], "proposed")
        self.assertTrue({SELFIES, SWIMWEAR} <= set(self.owners()), self.owners())

        self.analyse("A casual selfie in a bikini.", confidence=40)
        # control: classification really is uncertain and filed in Miscellaneous
        self.assertEqual(self.cls()["status"], "uncertain")
        self.assertIn(MISC, self.owners())
        leftover = [o for o in self.owners() if o.startswith(OWNER_PREFIX) and o != MISC]
        self.assertEqual(leftover, [], "uncertain item still in internal category folders")

    def test_regained_confidence_refiles_categories_dropped_while_uncertain(self):
        """Memberships withdrawn by the uncertain pass are not operator removals: they come back."""
        self.analyse("A casual selfie in a bikini.", confidence=99)
        self.analyse("A casual selfie in a bikini.", confidence=40)
        self.analyse("A casual selfie in a bikini.", confidence=95)
        self.assertEqual(self.cls()["status"], "proposed")
        self.assertTrue({SELFIES, SWIMWEAR} <= set(self.owners()), self.owners())
        self.assertNotIn(MISC, self.owners())


if __name__ == "__main__":
    unittest.main()
