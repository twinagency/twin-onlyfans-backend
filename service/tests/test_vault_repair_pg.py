import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_automatic_vault_descriptions_pg import setup, ACCOUNT, MID
from db.models import VaultItem, OfCreatorBinding
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.vault_description_worker import pending
from native_pilot.tools import describe_imported_vault as child, caption_vault


@pytest.mark.parametrize('payload', [
    {'tags': ['outdoors']}, {'description': '  ', 'tags': ['outdoors']},
    {'description': {'invented': 'object'}, 'tags': ['outdoors']},
])
def test_model_without_description_is_not_success(database, monkeypatch, payload):
    args, calls, async_engine, _ = setup(database, monkeypatch)
    import llm_client
    async def response(**kwargs):
        return SimpleNamespace(content=json.dumps(payload), call_id=None, cost_cents=1)
    monkeypatch.setattr(llm_client, 'chat', response)
    async def run():
        try:
            result = await child.run(args)
            assert result['described'] == 0 and result['failed'] == 1
        finally:
            await async_engine.dispose()
    asyncio.run(run())
    with Session(database[1]) as s:
        item = s.get(VaultItem, (ACCOUNT, MID))
        assert item.describe_status == 'refused'
        assert not (item.description or item.video_description)
        assert json.loads(item.ai_fields_json)['_describe_failure']['category'] == 'model'


def test_blank_legacy_success_recovers_and_restart_does_not_pay_again(database, monkeypatch):
    import vault_ai_api as api
    args, calls, async_engine, _ = setup(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        v = s.get(VaultItem, (ACCOUNT, MID))
        v.describe_status = 'described'
        v.description = '   '
    async def run():
        try:
            assert (await caption_vault.candidates(api, ACCOUNT, 4)) == [MID]
            assert (await child.run(args))['described'] == 1
            assert (await child.run(args))['described'] == 0
            assert (await caption_vault.candidates(api, ACCOUNT, 4)) == []
        finally:
            await async_engine.dispose()
    asyncio.run(run())
    assert calls == [(ACCOUNT, MID)]


@pytest.mark.parametrize('case,expected', [
    ('verification_rejected', False), ('receipt_only_rejection', False),
    ('operator_blank_lock', False), ('valid_description', False),
    ('exhausted', False), ('recent_fetch_failure', False), ('due_fetch_failure', True),
])
def test_recovery_selection_respects_review_locks_and_backoff(database, monkeypatch, case, expected):
    args, calls, async_engine, _ = setup(database, monkeypatch)
    now = utcnow()
    with Session(database[1]) as s, s.begin():
        v = s.get(VaultItem, (ACCOUNT, MID))
        v.describe_status = 'described'
        if case in ('verification_rejected', 'receipt_only_rejection'):
            v.ai_fields_json = json.dumps({'_folder_evidence_v1': {'status': 'needs_review', 'rejected_analysis': {'description': 'rejected'}}})
            if case == 'verification_rejected':
                v.review_state = 'needs_review'
        elif case == 'operator_blank_lock':
            v.locked_fields_json = '["description"]'
        elif case == 'valid_description':
            v.description = 'Approved portrait'
        elif case == 'exhausted':
            v.ai_fields_json = json.dumps({'_describe_failure': {'exhausted': True, 'stamp': None}})
        else:
            v.describe_status = 'fetch_failed'
            v.describe_generated_at = now - timedelta(minutes=14 if case == 'recent_fetch_failure' else 31)
    with Session(database[1]) as s:
        assert bool(s.scalar(select(VaultItem.media_id).where(VaultItem.account_id == ACCOUNT, pending(now)))) is expected
    asyncio.run(async_engine.dispose())


@pytest.mark.parametrize('hours,enabled,complete,state,attention', [
    (23,True,False,'in_progress',False), (24,True,False,'needs_attention',True),
    (25,False,False,'paused',False), (60,True,True,'complete',False),
])
def test_connection_checkpoint_is_read_only_and_does_not_stop_import(database, monkeypatch, hours, enabled, complete, state, attention):
    args, calls, async_engine, _ = setup(database, monkeypatch)
    from onlyfans_runtime.content_sync import _vault_coverage
    now = utcnow()
    with Session(database[1]) as s, s.begin():
        s.get(OfCreatorBinding, args.binding).created_at = now - timedelta(hours=hours)
        job = s.get(OfVaultSyncJob, (ACCOUNT, 'caption-sync'))
        job.status = 'complete' if complete else 'pending'
        job.completed_at = now-timedelta(hours=1) if complete else None
        # Recent refresh/progress must not reset the original connection clock.
        job.updated_at = now
    with Session(database[1]) as s:
        s.execute(text('SET TRANSACTION READ ONLY'))
        result = _vault_coverage(s, ACCOUNT, enabled, now)['initialImport']
        assert result['state'] == state and result['needsAttention'] is attention
        assert result['checkpointHours'] == 24
        assert s.get(OfVaultSyncJob, (ACCOUNT, 'caption-sync')).status == ('complete' if complete else 'pending')
    asyncio.run(async_engine.dispose())


def test_unrelated_pending_item_does_not_block_existing_approved_sale(database, monkeypatch):
    from test_moxy_integrated_pg import setup as moxy_setup
    from test_moxy_media_descriptions_pg import offer
    from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT as M_ACCOUNT
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    engine, store, service, authority, claim, settings = moxy_setup(database, monkeypatch)
    try:
        with Session(engine) as s, s.begin():
            s.add(VaultItem(account_id=M_ACCOUNT, media_id=9999991, kind='video', created_at=utcnow()))
        identity, model = offer(service, engine, 'unrelated-pending')
        intent = service.queue(TENANT, M_ACCOUNT, identity)
        transport = SyntheticTextTransport(M_ACCOUNT)
        assert authority.dispatch(TENANT, M_ACCOUNT, intent, transport) == 'accepted'
        assert len(transport.calls) == 1
    finally:
        store.close()


@pytest.mark.parametrize('case,expected', [
    ('rejected', 'needs_review'), ('blank_legacy', 'pending'),
    ('valid_text_folder_review', 'described'), ('human_blank', 'needs_review'),
    ('ai_status_override', 'pending')])
def test_existing_rows_report_effective_description_state_without_rewriting(database, monkeypatch, case, expected):
    args, calls, async_engine, _ = setup(database, monkeypatch)
    import vault_ai_api as api
    from vault_ai_effective import description_status
    with Session(database[1]) as s, s.begin():
        v=s.get(VaultItem,(ACCOUNT,MID));v.describe_status='described'
        if case in ('rejected','valid_text_folder_review'):
            v.review_state='needs_review'
            v.ai_fields_json=json.dumps({'_folder_evidence_v1':{'status':'needs_review',
                **({'rejected_analysis':{'description':'rejected'}} if case=='rejected' else {})}})
        if case=='valid_text_folder_review': v.description='A verified portrait with no supported folder category.'
        if case=='human_blank':v.locked_fields_json='["description"]'
        if case=='ai_status_override':v.ai_fields_json='{"describe_status":"described"}'
    with Session(database[1]) as s:
        s.execute(text('SET TRANSACTION READ ONLY'))
        v=s.get(VaultItem,(ACCOUNT,MID))
        original=v.ai_fields_json
        assert description_status(v)==expected
        assert api._overlay(v)['_ai']['describe_status']==expected
        assert v.describe_status=='described' and v.ai_fields_json==original
    asyncio.run(async_engine.dispose())


def test_checkpoint_remembers_completed_import_after_many_failed_refreshes(database, monkeypatch):
    args, calls, async_engine, _ = setup(database, monkeypatch)
    from onlyfans_runtime.content_sync import _vault_coverage
    now=utcnow()
    with Session(database[1]) as s,s.begin():
        s.get(OfCreatorBinding,args.binding).created_at=now-timedelta(days=3)
        old=s.get(OfVaultSyncJob,(ACCOUNT,'caption-sync'));old.completed_at=now-timedelta(hours=2)
        for n in range(11):
            s.add(OfVaultSyncJob(account_id=ACCOUNT,job_id='refresh-'+str(n),binding_id=args.binding,
                command_key=f'{n:064x}',request_hash='a'*64,actor='worker-import:synthetic',
                connection_revision=1,owner_id=900001,max_pages=200,status='failed',page_offset=0,
                pages_read=0,items_seen=0,requested_at=now+timedelta(seconds=n+1),updated_at=now))
    with Session(database[1]) as s:
        result=_vault_coverage(s,ACCOUNT,True,now)
        assert result['initialImport']['state']=='complete'
        assert result['initialImport']['needsAttention'] is False
        assert result['checkedAt'] is not None
    asyncio.run(async_engine.dispose())
