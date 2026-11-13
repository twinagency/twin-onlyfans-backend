"""CVA-01 (G07): an item that becomes commercially "uncertain" leaves its internal category folders.

SQLite in-memory only; no model/provider calls, no network.
"""
import json
import unittest
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db.models import Base, Account, VaultItem, VaultFolder, VaultFolderItem
from vault_auto_folders import assign_sync, OWNER_PREFIX, MARKER, CURRENT_MARKER, FILED
from vault_describe_state import stamp_of

ACCOUNT = "acct-w1"
MID = 11
MISC = OWNER_PREFIX + "miscellaneous"
SELFIES = OWNER_PREFIX + "selfies"
SWIMWEAR = OWNER_PREFIX + "swimwear"
CASUAL = OWNER_PREFIX + "casual"
FITNESS = OWNER_PREFIX + "fitness"


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

    def fields(self):
        return json.loads(self.item().ai_fields_json)

    def folder(self, owner):
        return self.s.scalar(select(VaultFolder).where(VaultFolder.account_id == ACCOUNT, VaultFolder.created_by == owner))

    def unfile(self, owner):
        self.s.delete(self.s.get(VaultFolderItem, (ACCOUNT, self.folder(owner).id, MID)))
        self.s.flush()

    def snapshot(self):
        return self.item().ai_fields_json, self.owners()

    def test_already_uncertain_current_item_drops_stale_filings_once(self):
        """D1: deployed rows are already uncertain+current with stale filings; the unchanged-evidence fast
        return must not keep them. Operator removals stay removed; a second unchanged pass writes nothing."""
        self.analyse("A casual selfie in a bikini.", confidence=99)
        self.unfile(SWIMWEAR)                                   # operator removal
        item = self.item()
        fields = self.fields()
        fields.update({"confidence": 40, "_commercial_classification_v1": {
            "status": "uncertain", "reason": "low_confidence", "content_stamp": stamp_of(item),
            "categories": ["miscellaneous"], "sale_approved": False}})
        item.ai_fields_json = json.dumps(fields)                # pre-fix uncertain state, FILED kept
        self.s.flush()
        self.assertTrue(fields[CURRENT_MARKER])
        self.assertTrue({SELFIES, CASUAL} <= set(self.owners()), self.owners())   # control: stale filings

        assign_sync(self.s, ACCOUNT, MID, commercial=True)
        leftover = [o for o in self.owners() if o.startswith(OWNER_PREFIX) and o != MISC]
        self.assertEqual(leftover, [], "already-uncertain item kept stale internal category filings")
        self.assertIn(MISC, self.owners())
        self.assertIn("swimwear", self.fields()[FILED])        # operator removal still remembered
        state = self.snapshot()
        self.assertEqual(assign_sync(self.s, ACCOUNT, MID, commercial=True), {"status": "already_categorised"})
        self.assertEqual(self.snapshot(), state)

        self.analyse("A casual selfie in a bikini.", confidence=95)
        self.assertTrue({SELFIES, CASUAL} <= set(self.owners()), self.owners())
        self.assertNotIn(SWIMWEAR, self.owners(), "operator-removed category was re-filed")

    def test_operator_added_membership_in_auto_folder_survives_uncertain(self):
        """O1: an operator may add an item to any folder, including an auto folder the classifier never filed."""
        self.analyse("A casual selfie in a bikini.", confidence=99)
        fitness = VaultFolder(account_id=ACCOUNT, created_by=FITNESS, name="Fitness")
        self.s.add(fitness); self.s.flush()
        self.s.add(VaultFolderItem(account_id=ACCOUNT, folder_id=fitness.id, media_id=MID)); self.s.flush()
        self.analyse("A casual selfie in a bikini.", confidence=40)
        self.assertEqual(self.cls()["status"], "uncertain")    # control
        self.assertNotIn(SELFIES, self.owners())               # control: classifier filings withdrawn
        self.assertIn(FITNESS, self.owners(), "operator-added membership withdrawn")
        self.analyse("A casual selfie in a bikini.", confidence=95)
        self.assertIn(FITNESS, self.owners(), "operator-added membership lost after regaining confidence")

    def test_v1_item_uncertain_while_pending_keeps_legacy_filings(self):
        """O2: a genuine V1 item analysed while pending must not lose V1 filings for good."""
        selfies = VaultFolder(account_id=ACCOUNT, created_by=SELFIES, name="Selfies")
        self.s.add(selfies); self.s.flush()
        self.s.add(VaultFolderItem(account_id=ACCOUNT, folder_id=selfies.id, media_id=MID))
        self.item().ai_fields_json = json.dumps({MARKER: True})
        self.s.flush()
        assign_sync(self.s, ACCOUNT, MID, commercial=True)
        self.assertEqual(self.cls()["reason"], "analysis_pending")   # control
        self.analyse("A selfie.", confidence=99)
        self.assertEqual(self.cls()["status"], "proposed")            # control
        self.assertIn(SELFIES, self.owners(), "V1 filing lost after an uncertain pending pass")

    def precommercial_selfie(self):
        """Legacy v2 row: filed by a non-commercial pass, so it has no FILED and no classification."""
        item = self.item()
        item.ai_fields_json = json.dumps({"description": "A selfie.", "confidence": 99, "_content_stamp": stamp_of(item)})
        self.s.flush()
        assign_sync(self.s, ACCOUNT, MID)
        fields = self.fields()
        self.assertTrue(fields[CURRENT_MARKER])                              # control
        self.assertNotIn(FILED, fields); self.assertNotIn("_commercial_classification_v1", fields)
        self.assertIn(SELFIES, self.owners())                                # control

    def assert_kept_and_stable(self):
        self.assertIn(MISC, self.owners())
        self.assertIn(SELFIES, self.owners(), "unevidenced live membership withdrawn (may be an operator add)")
        state = self.snapshot()
        self.assertEqual(assign_sync(self.s, ACCOUNT, MID, commercial=True), {"status": "already_categorised"})
        self.assertEqual(self.snapshot(), state)

    def test_precommercial_item_with_evidenced_filing_drops_it(self):
        """F1: a pre-commercial filing still backed by current evidence is withdrawn when uncertain."""
        self.precommercial_selfie()
        self.analyse("A selfie.", confidence=40)
        self.assertEqual(self.cls()["status"], "uncertain")                 # control
        leftover = [o for o in self.owners() if o.startswith(OWNER_PREFIX) and o != MISC]
        self.assertEqual(leftover, [], "evidenced pre-commercial filing survived the uncertain pass")

    def test_precommercial_item_with_changed_evidence_keeps_unevidenced_membership(self):
        """G07c: evidence now only Casual; without FILED the live Selfies membership cannot be told apart from
        an operator addition, so it is kept (never withdrawn)."""
        self.precommercial_selfie()
        self.analyse("A casual photo.", confidence=40)
        self.assertEqual(self.cls()["status"], "uncertain")                 # control
        self.assert_kept_and_stable()

    def test_precommercial_item_with_vanished_metadata_keeps_unevidenced_membership(self):
        """G07c: metadata gone (pending) on the first commercial pass; no evidence says who filed Selfies."""
        self.precommercial_selfie()
        item = self.item()
        fields = self.fields(); fields.pop("description")
        item.ai_fields_json = json.dumps(fields); self.s.flush()
        assign_sync(self.s, ACCOUNT, MID, commercial=True)
        self.assertEqual(self.cls()["reason"], "analysis_pending")          # control
        self.assert_kept_and_stable()

    def test_precommercial_operator_added_membership_survives_uncertain(self):
        """G07c: a pre-commercial item has no FILED record, so a live auto-folder membership outside its
        current evidence may be an operator addition; the uncertain pass must not withdraw it."""
        self.precommercial_selfie()
        fitness = VaultFolder(account_id=ACCOUNT, created_by=FITNESS, name="Fitness")
        self.s.add(fitness); self.s.flush()
        self.s.add(VaultFolderItem(account_id=ACCOUNT, folder_id=fitness.id, media_id=MID)); self.s.flush()
        self.analyse("A selfie.", confidence=40)
        self.assertEqual(self.cls()["status"], "uncertain")                 # control
        self.assertNotIn(SELFIES, self.owners())                            # control: evidenced filing withdrawn
        self.assertIn(FITNESS, self.owners(), "operator-added membership withdrawn")
        self.analyse("A selfie.", confidence=95)
        self.assertIn(FITNESS, self.owners(), "operator-added membership lost after regaining confidence")


if __name__ == "__main__":
    unittest.main()
