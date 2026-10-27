"""Concurrent category writes in an isolated PostgreSQL schema, no provider calls."""
import asyncio
import os
import unittest
import uuid
from datetime import datetime
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Base, Account, VaultItem, VaultFolder, VaultFolderItem
from vault_auto_folders import assign


@unittest.skipUnless(os.environ.get("ONLYFANS_TEST_PG_URL"), "disposable PostgreSQL URL required")
class ConcurrentFoldersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "vault_categories_" + uuid.uuid4().hex
        url = make_url(os.environ["ONLYFANS_TEST_PG_URL"]).set(drivername="postgresql+asyncpg")
        self.admin = create_async_engine(url)
        async with self.admin.begin() as c:
            await c.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.engine = create_async_engine(url, connect_args={"server_settings":{"search_path":self.schema}})
        async with self.engine.begin() as c:
            await c.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[
                Account.__table__, VaultItem.__table__, VaultFolder.__table__, VaultFolderItem.__table__]))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions.begin() as s:
            s.add(Account(id="category-test"))
            await s.flush()
            s.add_all([VaultItem(account_id="category-test", media_id=i, kind="photo",
                created_at=datetime.utcnow(), tags='["selfie","bikini"]') for i in range(1, 9)])

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin.begin() as c:
            await c.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        await self.admin.dispose()

    async def test_eight_parallel_imports_create_two_folders_and_sixteen_memberships(self):
        async def one(media_id):
            async with self.sessions.begin() as s:
                return await assign(s, "category-test", media_id)
        results = await asyncio.gather(*(one(i) for i in range(1, 9)))
        self.assertTrue(all(r["status"] == "categorised" for r in results))
        async with self.sessions() as s:
            self.assertEqual(len(list(await s.scalars(select(VaultFolder)))), 2)
            self.assertEqual(len(list(await s.scalars(select(VaultFolderItem)))), 16)
        results = await asyncio.gather(*(one(i) for i in range(1, 9)))
        self.assertTrue(all(r["status"] == "already_categorised" for r in results))


if __name__ == "__main__":
    unittest.main()
