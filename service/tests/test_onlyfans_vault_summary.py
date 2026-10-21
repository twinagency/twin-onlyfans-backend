"""Real SQL aggregation on a disposable SQLite database; no provider/network."""
import unittest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from db.models import VaultItem
from onlyfans_runtime.vault_management import _library_summary

class VaultSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE vault_items (account_id TEXT, media_id BIGINT, kind TEXT, description TEXT)"))
            conn.execute(text("CREATE TABLE vault_folders (account_id TEXT, deleted_at TEXT)"))
            conn.execute(text("INSERT INTO vault_items VALUES ('a',1,'photo','100% coast'),('a',2,'photo','1000 coast'),('a',3,'video','forest'),('a',4,'audio','other'),('b',5,'video','private')"))
            conn.execute(text("INSERT INTO vault_folders VALUES ('a',NULL),('a','archived'),('b',NULL)"))
        self.base = select(VaultItem).where(VaultItem.account_id == "a", VaultItem.media_id > 0)
    def tearDown(self):
        self.engine.dispose()
    def summary(self, query=None, account="a"):
        with Session(self.engine) as session:
            return _library_summary(session, account, self.base, query if query is not None else self.base)
    def test_counts_every_type_and_only_active_account_folders(self):
        self.assertEqual(self.summary(), dict(total=4, photos=2, videos=1, other=1, matching=4, folders=1))
    def test_media_filter_does_not_change_totals(self):
        result = self.summary(self.base.where(VaultItem.kind == "video"))
        self.assertEqual(result["matching"], 1)
        self.assertEqual(result["total"], 4)
    def test_search_treats_percent_as_literal(self):
        result = self.summary(self.base.where(VaultItem.description.contains("100%", autoescape=True)))
        self.assertEqual(result["matching"], 1)
    def test_no_matches_and_empty_account_are_zero(self):
        self.assertEqual(self.summary(self.base.where(VaultItem.media_id > 999))["matching"], 0)
        self.base = select(VaultItem).where(VaultItem.account_id == "empty")
        self.assertEqual(self.summary(account="empty"), dict(total=0, photos=0, videos=0, other=0, matching=0, folders=0))

if __name__ == "__main__":
    unittest.main()
