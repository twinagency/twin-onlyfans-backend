"""Accepted media persists once; a delayed history row cannot erase a purchase."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, patch

root = Path(tempfile.mkdtemp(prefix="of-message-media-"))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + (root / "test.db").as_posix()
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update
from db.engine import engine, get_session
from db.models import Base, Message
import attribution

async def main():
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[Message.__table__]))
    stamp = datetime(2026, 10, 25)
    async def write(message_id, **kwargs):
        await attribution.write_outbound_attribution(account_id="account", fan_id=1,
            message_id=message_id, sent_by_employee_id=None, body="accepted caption",
            price_cents=500, created_at=stamp, automation_kind="pack_send", **kwargs)
    async def read(message_id):
        async with get_session() as session:
            return await session.get(Message, ("account", 1, message_id))
    with patch("employees.get_automation_employee_id", AsyncMock(return_value=None)), \
         patch("chatters.get_request_chatter", return_value=None), \
         patch.object(attribution, "_advance_chat_preview", AsyncMock()):
        await write(10, media_ids=[101, 102, 103])
        first = await read(10)
        assert json.loads(first.media_ids) == [101, 102, 103] and first.media_count == 3
        async with get_session() as session:
            await session.execute(update(Message).where(Message.message_id == 10).values(is_paid=True))
        await write(10, media_ids=[999])
        duplicate = await read(10)
        assert duplicate.is_paid is True and json.loads(duplicate.media_ids) == [101, 102, 103]
        async with get_session() as session:
            await session.execute(update(Message).where(Message.message_id == 10).values(media_ids="[]", media_count=0))
        await write(10, media_ids=[101, 102, 103])
        recovered = await read(10)
        assert recovered.is_paid is True and recovered.media_count == 3
        await write(11)
        text = await read(11)
        assert text.media_count == 0 and text.media_ids == "[]"
        async with get_session() as session:
            assert len((await session.scalars(select(Message))).all()) == 2
    await engine.dispose()
    print("PASS accepted attachment metadata, idempotent writes, late history recovery, purchase preservation and old text callers")

asyncio.run(main())
