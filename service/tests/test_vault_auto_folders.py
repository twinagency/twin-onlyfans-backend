"""Offline category and real SQLite membership tests. No media/model calls."""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Base, Account, VaultItem, VaultFolder, VaultFolderItem
from vault_auto_folders import assign, categories, MARKER


def metadata(**kwargs):
    return SimpleNamespace(**kwargs)


class ClassificationTests(unittest.TestCase):
    def test_bikini_selfie_overlaps(self):
        self.assertEqual(categories(metadata(tags='["selfie", "bikini"]')), ["selfies", "swimwear"])

    def test_description_adds_category(self):
        self.assertEqual(categories(metadata(tags='["selfie"]', description="Wearing a bikini.")), ["selfies", "swimwear"])

    def test_structured_camera_and_clothes(self):
        self.assertEqual(categories(metadata(ai_fields_json=json.dumps({"camera":"handheld_selfie", "clothing_items":["lingerie"]}))), ["lingerie", "selfies"])

    def test_operator_overrides_win(self):
        self.assertEqual(categories(metadata(tags='["bikini"]', ai_fields_json='{"tags":["lingerie"]}',
            operator_overrides_json='{"tags":["fitness"],"description":null}')), ["fitness"])

    def test_negated_clothing_is_not_a_category(self):
        self.assertEqual(categories(metadata(description="Not wearing lingerie. Without a bikini.")), ["uncategorised"])

    def test_no_partial_word_matches(self):
        self.assertEqual(categories(metadata(description="Selfiesque photograph")), ["uncategorised"])

    def test_video_description_and_fully_nude_conflict(self):
        self.assertEqual(categories(metadata(video_description="A workout at the gym.")), ["fitness"])
        self.assertEqual(categories(metadata(tags='["lingerie","bikini","selfie"]',
            ai_fields_json='{"clothing_state":"fully_nude"}')), ["selfies"])

    def test_unknown_and_malformed_tags(self):
        self.assertEqual(categories(metadata(tags='{"unexpected":true}')), ["uncategorised"])
        self.assertEqual(categories(metadata(tags='casual, streetwear')), ["casual"])


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://")
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[
                Account.__table__, VaultItem.__table__, VaultFolder.__table__, VaultFolderItem.__table__]))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions.begin() as s:
            s.add_all([Account(id="a"), Account(id="b")])
            s.add_all([VaultItem(account_id=account, media_id=1, kind="photo", created_at=datetime.utcnow(),
                tags='["bikini","selfie"]', ai_fields_json='{"description":"Original","_flags_v":"kept"}') for account in ["a","b"]])

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def apply(self, account="a", media=1):
        async with self.sessions.begin() as s:
            return await assign(s, account, media)

    async def test_assignment_and_repeat_are_idempotent(self):
        self.assertEqual((await self.apply())["status"], "categorised")
        self.assertEqual((await self.apply())["status"], "already_categorised")
        async with self.sessions() as s:
            self.assertEqual(len(list(await s.scalars(select(VaultFolder)))), 2)
            self.assertEqual(len(list(await s.scalars(select(VaultFolderItem)))), 2)
            row = await s.get(VaultItem, ("a", 1))
            self.assertEqual(json.loads(row.ai_fields_json)["_flags_v"], "kept")
            self.assertEqual(row.tags, '["bikini","selfie"]')

    async def test_same_media_id_in_other_account_is_untouched(self):
        await self.apply()
        async with self.sessions() as s:
            row = await s.get(VaultItem, ("b", 1))
            self.assertNotIn(MARKER, json.loads(row.ai_fields_json))
            self.assertEqual(list(await s.scalars(select(VaultFolder).where(VaultFolder.account_id=="b"))), [])

    async def test_manual_removal_rename_archive_are_preserved(self):
        await self.apply()
        async with self.sessions.begin() as s:
            rows = list(await s.scalars(select(VaultFolder).where(VaultFolder.account_id=="a").order_by(VaultFolder.id)))
            rows[0].name = "Agency selection"
            rows[1].deleted_at = datetime.utcnow()
            await s.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id=="a"))
        await self.apply()
        async with self.sessions() as s:
            self.assertEqual(list(await s.scalars(select(VaultFolderItem))), [])
            self.assertEqual(len(list(await s.scalars(select(VaultFolder)))), 2)
            self.assertEqual((await s.get(VaultFolder, 1)).name, "Agency selection")

    async def test_operator_folder_name_is_not_adopted(self):
        async with self.sessions.begin() as s:
            manual = VaultFolder(account_id="a", name="Selfies", created_by="operator")
            s.add(manual)
            await s.flush()
            manual_id = manual.id
        await self.apply()
        async with self.sessions() as s:
            self.assertEqual(list(await s.scalars(select(VaultFolderItem).where(VaultFolderItem.folder_id==manual_id))), [])

    async def test_archived_auto_folder_not_recreated_for_new_item(self):
        await self.apply()
        async with self.sessions.begin() as s:
            folder = await s.scalar(select(VaultFolder).where(VaultFolder.created_by=="auto-category:selfies"))
            folder.deleted_at = datetime.utcnow()
            s.add(VaultItem(account_id="a", media_id=2, kind="video", created_at=datetime.utcnow(), tags='["selfie"]'))
        await self.apply(media=2)
        async with self.sessions() as s:
            self.assertEqual(len(list(await s.scalars(select(VaultFolder)))), 2)
            self.assertEqual(list(await s.scalars(select(VaultFolderItem).where(VaultFolderItem.media_id==2))), [])

    async def test_missing_removed_and_pending_metadata_are_skipped(self):
        self.assertEqual((await self.apply(media=999))["status"], "skipped")
        async with self.sessions.begin() as s:
            row = await s.get(VaultItem, ("a", 1))
            row.tags = "[]"; row.ai_fields_json = "{}"
        self.assertEqual((await self.apply())["status"], "awaiting_metadata")
        async with self.sessions.begin() as s:
            row = await s.get(VaultItem, ("a", 1)); row.removed_at = datetime.utcnow()
        self.assertEqual((await self.apply())["status"], "skipped")

    async def test_pending_moves_to_real_categories_and_keeps_operator_membership(self):
        async with self.sessions.begin() as s:
            row = await s.get(VaultItem, ("a", 1)); row.tags = "[]"; row.ai_fields_json = "{}"
        self.assertEqual((await self.apply())["status"], "awaiting_metadata")
        async with self.sessions.begin() as s:
            pending = await s.scalar(select(VaultFolder).where(VaultFolder.created_by == "auto-category:pending"))
            self.assertIsNotNone(await s.get(VaultFolderItem, ("a", pending.id, 1)))
            manual = VaultFolder(account_id="a", name="Pinned", created_by="operator")
            s.add(manual); await s.flush()
            s.add(VaultFolderItem(account_id="a", folder_id=manual.id, media_id=1)); manual_id=manual.id
            row = await s.get(VaultItem, ("a", 1)); row.description="A selfie outdoors at the beach."
        self.assertEqual((await self.apply())["categories"], ["beach", "outdoors", "selfies"])
        async with self.sessions() as s:
            self.assertIsNone(await s.get(VaultFolderItem, ("a", pending.id, 1)))
            self.assertIsNotNone(await s.get(VaultFolderItem, ("a", manual_id, 1)))
        self.assertEqual((await self.apply())["status"], "already_categorised")

    async def test_v1_migration_adds_new_categories_without_readding_manual_removals(self):
        await self.apply()
        async with self.sessions.begin() as s:
            row=await s.get(VaultItem, ("a", 1))
            fields=json.loads(row.ai_fields_json);fields.pop("_auto_folders_v2");fields["setting"]="bedroom"
            row.ai_fields_json=json.dumps(fields)
            await s.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id=="a"))
        self.assertEqual((await self.apply())["categories"], ["bedroom"])
        async with self.sessions() as s:
            entries=list(await s.scalars(select(VaultFolderItem).where(VaultFolderItem.account_id=="a")))
            self.assertEqual(len(entries),1)
            self.assertEqual((await s.get(VaultFolder,entries[0].folder_id)).name,"Bedroom")

    async def test_invalid_metadata_does_not_get_overwritten(self):
        async with self.sessions.begin() as s:
            row = await s.get(VaultItem, ("a", 1)); row.ai_fields_json = "bad JSON"
        self.assertEqual((await self.apply())["status"], "invalid_metadata")

    async def test_folder_limit_rolls_back_without_marking_complete(self):
        async with self.sessions.begin() as s:
            s.add_all([VaultFolder(account_id="a", name=str(i)) for i in range(200)])
        with self.assertRaises(ValueError):
            await self.apply()
        async with self.sessions() as s:
            self.assertNotIn(MARKER, json.loads((await s.get(VaultItem, ("a",1))).ai_fields_json))
            self.assertEqual(list(await s.scalars(select(VaultFolderItem))), [])


if __name__ == "__main__":
    unittest.main()

