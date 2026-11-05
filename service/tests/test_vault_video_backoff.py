import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


def test_provider_backoff_does_not_become_a_failed_video(monkeypatch):
    import vault_frames
    from onlyfans_runtime.provider_read_budget import ReadDeferred

    async def collect(*args):
        raise ReadDeferred(datetime.utcnow() + timedelta(seconds=10), "provider read backoff")

    monkeypatch.setattr(vault_frames, "_collect", collect)
    item = SimpleNamespace(kind="video", media_id=1)

    with pytest.raises(ReadDeferred, match="provider read backoff"):
        asyncio.run(vault_frames.collect("of-test", item, {}))
