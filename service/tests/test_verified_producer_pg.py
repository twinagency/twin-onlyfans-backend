"""Run the hash-pinned dedicated producer through real DB persistence; model/media are synthetic."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_automatic_vault_descriptions_pg import setup, ACCOUNT, MID
from db.models import VaultItem


@pytest.mark.parametrize('case', ['confirmed_no_category', 'verification_rejected', 'tags_only'])
def test_dedicated_producer_distinguishes_verified_text_review_and_empty_output(database, monkeypatch, case):
    import llm_client
    args, calls, async_engine, _ = setup(database, monkeypatch)
    root=Path(__file__).resolve().parents[2]/'compat'
    monkeypatch.syspath_prepend(str(root/'baseline/generic'))
    family=os.environ.get('VAULT_TEST_VARIANT','candidate')
    path=root/family/'generic/vault_ai_api.py'
    spec=importlib.util.spec_from_file_location('verified_producer_fixture',path)
    api=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    replies=[]
    async def chat(**kwargs):
        assert kwargs['account_id']==ACCOUNT and kwargs['purpose']=='describe_media'
        replies.append(kwargs['model'])
        if case=='tags_only': data={'tags':['portrait']}
        elif len(replies)==1: data={'description':'A portrait beside a plain blue wall.','tags':[]}
        else: data={'description_accurate':case=='confirmed_no_category','checks':[]}
        return SimpleNamespace(content=json.dumps(data),call_id=None,cost_cents=1)
    monkeypatch.setattr(llm_client,'chat',chat)
    async def run():
        try:
            return await api._describe_one(ACCOUNT,MID,model='openrouter/qwen3-vl-30b',prompt_version='v3')
        finally:
            await async_engine.dispose()
    result=asyncio.run(run())
    with Session(database[1]) as s:
        v=s.get(VaultItem,(ACCOUNT,MID))
        if case=='confirmed_no_category':
            assert result['ok'] is True and v.describe_status=='described'
            assert v.description=='A portrait beside a plain blue wall.'
            assert len(replies)==2   # Description verification runs even without folder proposals.
        elif case=='verification_rejected':
            assert result['ok'] is False and result['status']=='needs_review'
            assert v.describe_status=='needs_review' and v.review_state=='needs_review'
            receipt=json.loads(v.ai_fields_json)['_folder_evidence_v1']
            assert receipt['status']=='needs_review' and receipt['rejected_analysis']['description']
            assert v.description==''
        else:
            assert result['ok'] is False and v.describe_status=='refused'
            assert not v.description
