"""Stored caption status must agree with usable content; no paid/network calls."""
import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import VaultItem
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.vault_description_worker import pending
from test_onlyfans_foundation_pg import database
from test_vault_description_coverage_pg import Rig,ACCOUNT,MID

@pytest.mark.parametrize('locked,exhausted,expected',[(False,False,True),(True,False,False),(False,True,False)])
def test_blank_described_row_is_retryable_unless_operator_locked_or_exhausted(database,monkeypatch,locked,exhausted,expected):
    rig=Rig(database,monkeypatch,limit=1)
    rig.update(describe_status='described',description=' ',video_description='',
        describe_generated_at=utcnow(),content_hash=None,
        locked_fields_json=json.dumps(['description']) if locked else '[]',
        ai_fields_json=json.dumps({'_describe_attempts':{'exhausted':True,'stamp':None}}) if exhausted else '{}')
    with Session(database[1]) as s:
        selected=s.scalar(select(VaultItem.media_id).where(VaultItem.account_id==ACCOUNT,pending(utcnow())))
    assert (selected==MID)==expected
    if expected:
        result=rig.run()
        assert result['described']==1
        assert rig.row().description.strip()
        assert rig.run()['described']==0


def test_caption_permission_is_task_local_and_cannot_send_to_fans(monkeypatch):
    import llm_client
    import asyncio
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    def stop_at_provider(model):
        raise LookupError("provider boundary reached")
    monkeypatch.setattr(llm_client, "_resolve", stop_at_provider)
    kwargs = dict(model="openrouter/qwen3-vl-30b", messages=[], purpose="describe_media", account_id=ACCOUNT)
    async def run():
        async def unprivileged():
            with pytest.raises(RuntimeError, match="Legacy model"):
                await llm_client.chat(**kwargs)
        task = asyncio.create_task(unprivileged())
        token = llm_client._vault_caption_account.set(ACCOUNT)
        try:
            with pytest.raises(LookupError, match="provider boundary"):
                await llm_client.chat(**kwargs)
            for change in ({"account_id": "of-900009"}, {"purpose": "fan_reply"}, {"fan_id": 12}, {"model": "grok-4-1-fast-non-reasoning"}):
                with pytest.raises(RuntimeError, match="Legacy model"):
                    await llm_client.chat(**{**kwargs, **change})
            await task
        finally:
            llm_client._vault_caption_account.reset(token)
        await unprivileged()
    asyncio.run(run())
