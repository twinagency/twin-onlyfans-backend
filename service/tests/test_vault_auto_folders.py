"""Regression evidence for the misfiled selfie and durable folder repair."""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from sqlalchemy import create_engine, select, delete
from sqlalchemy.orm import Session
from db.models import Base, Account, VaultItem, VaultFolder, VaultFolderItem
from vault_category_evidence import KEY, receipt, verified_categories, proposals
from vault_auto_folders_v3 import assign_sync, CURRENT_MARKER


def evidence(keys=("selfies", "bedroom"), account="a", media_id=1, kind="photo", source="photo", description="A clothed mirror selfie in a bedroom."):
    images = ["image"] if kind == "photo" else ["frame"+str(n) for n in range(6)]
    data = {"description": description, "tags": ["selfie"], "acts": ["posing"],
        "people_count": 1, "confidence": 95,
        "category_evidence": [{"category": k, "confidence": 95, "evidence": "The relevant visual feature is clearly visible.",
            "frames": [1,2] if kind == "video" else [1]} for k in keys]}
    checked = {"description_accurate": True, "checks": [{"category": k, "confirmed": True,
        "confidence": 96, "evidence": "Independently visible feature supports this category."} for k in keys]}
    data[KEY] = receipt(data, checked, images, account=account, media_id=media_id, kind=kind, source=source)
    return data


class EvidenceTests(unittest.TestCase):
    def item(self, data, **extra):
        return SimpleNamespace(account_id="a", media_id=1, kind="photo", ai_fields_json=json.dumps(data), **extra)

    def test_recording_selfie_taxonomy_dump_is_not_authority(self):
        data = {"description": "A mirror selfie", "confidence": 95,
            "primary_folder": "solo_fingering", "acts": ["posing", "tease"],
            "tags": ["solo_fingering", "solo_toy", "ass_twerk", "boobs", "sports bra", "feet", "talking_head"]}
        self.assertEqual(verified_categories(self.item(data)), [])

    def test_two_pass_evidence_allows_real_overlapping_categories(self):
        self.assertEqual(verified_categories(self.item(evidence())), ["bedroom", "selfies"])

    def test_other_creators_receipt_cannot_be_reused(self):
        self.assertEqual(verified_categories(self.item(evidence(account="b"))), [])

    def test_other_media_receipt_cannot_be_reused(self):
        self.assertEqual(verified_categories(self.item(evidence(media_id=2))), [])

    def test_changed_description_invalidates_old_receipt(self):
        data=evidence();data["description"]="Different media description"
        self.assertEqual(verified_categories(self.item(data)), [])

    def test_low_confidence_and_taxonomy_dump_fail_closed(self):
        data=evidence();data["category_evidence"][0]["confidence"]=1
        self.assertNotIn("selfies",[r["category"] for r in proposals(data,kind="photo",source="photo",frame_count=1)])
        data["tags"]=[str(i) for i in range(30)]
        self.assertEqual(proposals(data,kind="photo",source="photo",frame_count=1),[])

    def test_photo_cannot_prove_motion_or_talking(self):
        data=evidence(keys=("dance","talking"))
        self.assertEqual(verified_categories(self.item(data)),[])

    def test_poster_cannot_prove_video_action(self):
        data=evidence(keys=("dance",),kind="video",source="posters")
        item=SimpleNamespace(account_id="a",media_id=1,kind="video",ai_fields_json=json.dumps(data))
        self.assertEqual(verified_categories(item),[])

    def test_action_requires_corroborating_observed_act(self):
        data=evidence(keys=("solo_fingering",))
        self.assertEqual(verified_categories(self.item(data)),[])

    def test_verifier_rejection_never_becomes_verified(self):
        data=evidence();data.pop(KEY)
        checked={"description_accurate":False,"checks":[]}
        data[KEY]=receipt(data,checked,["image"],account="a",media_id=1,kind="photo",source="photo")
        self.assertEqual(verified_categories(self.item(data)),[])

    def test_repeated_video_frame_cannot_prove_motion(self):
        data=evidence(keys=("dance",),kind="video",source="storyboard");data.pop(KEY)
        checked={"description_accurate":True,"checks":[{"category":"dance","confirmed":True,"confidence":99,"evidence":"A repeated frozen frame."}]}
        data[KEY]=receipt(data,checked,["same"]*6,account="a",media_id=1,kind="video",source="storyboard")
        self.assertEqual(data[KEY]["categories"],[])


class FolderRepairTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite://")
        Base.metadata.create_all(self.engine,tables=[Account.__table__,VaultItem.__table__,VaultFolder.__table__,VaultFolderItem.__table__])
        with Session(self.engine) as s,s.begin():
            s.add_all([Account(id="a"),Account(id="b")]);s.flush()
            for account in ("a","b"):
                s.add(VaultItem(account_id=account,media_id=1,kind="photo",created_at=datetime.utcnow(),
                    description="Old unreliable description",tags='["sports bra","boobs"]',
                    ai_fields_json=json.dumps({"confidence":95,"tags":["sports bra","boobs"],"_auto_folders_v2":True})))
                for key in ("fitness","boobs"):
                    f=VaultFolder(account_id=account,name={"fitness":"Fitness","boobs":"Boobs"}[key],created_by="auto-category:"+key)
                    s.add(f);s.flush();s.add(VaultFolderItem(account_id=account,folder_id=f.id,media_id=1))

    def tearDown(self):
        self.engine.dispose()

    def apply(self,account="a"):
        with Session(self.engine) as s,s.begin():
            return assign_sync(s,account,1)

    def folders(self,account="a"):
        with Session(self.engine) as s:
            return set(s.scalars(select(VaultFolder.name).join(VaultFolderItem,VaultFolder.id==VaultFolderItem.folder_id)
                .where(VaultFolderItem.account_id==account,VaultFolderItem.media_id==1)))

    def put(self,data,account="a"):
        with Session(self.engine) as s,s.begin():
            row=s.get(VaultItem,(account,1));old=json.loads(row.ai_fields_json)
            if CURRENT_MARKER in old:data[CURRENT_MARKER]=old[CURRENT_MARKER]
            row.ai_fields_json=json.dumps(data)

    def test_bad_automatic_memberships_removed_without_deleting_media(self):
        result=self.apply()
        self.assertEqual(set(result["removed"]),{"fitness","boobs"})
        self.assertEqual(self.folders(),{"Needs review"})
        with Session(self.engine) as s:self.assertIsNotNone(s.get(VaultItem,("a",1)))

    def test_all_creators_are_repaired_independently(self):
        self.apply("a");self.assertEqual(self.folders("b"),{"Fitness","Boobs"})
        self.apply("b");self.assertEqual(self.folders("b"),{"Needs review"})

    def test_verified_new_analysis_replaces_needs_review(self):
        self.apply();self.put(evidence());self.apply()
        self.assertEqual(self.folders(),{"Selfies","Bedroom"})
        self.assertEqual(self.apply()["status"],"already_categorised")

    def test_operator_folder_is_untouched(self):
        with Session(self.engine) as s,s.begin():
            f=VaultFolder(account_id="a",name="My selection",created_by="operator");s.add(f);s.flush()
            s.add(VaultFolderItem(account_id="a",folder_id=f.id,media_id=1))
        self.apply();self.assertIn("My selection",self.folders())

    def test_renamed_auto_folder_is_retained(self):
        with Session(self.engine) as s,s.begin():
            f=s.scalar(select(VaultFolder).where(VaultFolder.account_id=="a",VaultFolder.name=="Fitness"));f.name="Agency selection"
        self.apply();self.assertIn("Agency selection",self.folders())

    def test_removed_membership_is_not_recreated_on_reanalysis(self):
        self.put(evidence());self.apply()
        with Session(self.engine) as s,s.begin():
            f=s.scalar(select(VaultFolder).where(VaultFolder.account_id=="a",VaultFolder.name=="Selfies"))
            s.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id=="a",VaultFolderItem.folder_id==f.id))
        self.put(evidence(description="A corrected mirror selfie in a bedroom."));self.apply()
        self.assertNotIn("Selfies",self.folders())

    def test_archived_folder_is_not_recreated(self):
        self.put(evidence());self.apply()
        with Session(self.engine) as s,s.begin():
            f=s.scalar(select(VaultFolder).where(VaultFolder.account_id=="a",VaultFolder.name=="Selfies"));f.deleted_at=datetime.utcnow()
        self.put(evidence(description="Corrected description."));self.apply()
        with Session(self.engine) as s:
            self.assertEqual(len(list(s.scalars(select(VaultFolder).where(VaultFolder.account_id=="a",VaultFolder.name=="Selfies")))),1)

    def test_invalid_metadata_is_retained_for_recovery(self):
        with Session(self.engine) as s,s.begin():s.get(VaultItem,("a",1)).ai_fields_json="bad json"
        self.assertEqual(self.apply()["status"],"invalid_metadata")


if __name__ == "__main__":
    unittest.main()
