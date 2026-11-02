"""Large real-shape vault metadata remains bounded and keeps exact cursors."""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT, OWNER
from test_onlyfans_vault_pg import media, page, queue
from db.models import VaultItem
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.vault_management import process_vault_jobs, parse_vault_page
from onlyfans_runtime.store import Refused


def test_configured_small_pages_import_oversized_vault_without_skips(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    queued = queue(store, account)
    monkeypatch.setenv('ONLYFANS_VAULT_PAGE_SIZES', json.dumps({account: 6}))
    calls = []

    def read(self, *, offset, limit):
        calls.append((offset, limit))
        assert store.engine.pool.checkedout() == 0
        # Twenty-four such items exceed the unchanged 1 MiB wire budget.
        count = limit if offset == 0 else 1
        return page([media(offset + i + 1, metadata='x' * 90000)
                     for i in range(count)], more=offset == 0)

    monkeypatch.setattr(NativeProviderConnector, 'vault_page', read)
    try:
        assert process_vault_jobs(store, TENANT) == 1
        with Session(store.engine) as s:
            job = s.get(OfVaultSyncJob, (account, queued['jobId']))
            assert (job.status, job.page_offset, job.items_seen) == ('pending', 6, 6)
        assert process_vault_jobs(store, TENANT) == 1
        with Session(store.engine) as s:
            job = s.get(OfVaultSyncJob, (account, queued['jobId']))
            assert (job.status, job.page_offset, job.items_seen) == ('complete', 7, 7)
            assert s.scalar(select(func.count()).select_from(VaultItem)) == 7
        assert calls == [(0, 6), (6, 6)]
    finally:
        store.close()


def test_short_page_cannot_claim_more_unless_it_matches_requested_limit():
    raw = page([media(i + 1) for i in range(6)], more=True)
    with pytest.raises(NativeProtocolError):
        parse_vault_page(raw, owner_id=OWNER)
    rows, more = parse_vault_page(raw, owner_id=OWNER, page_size=6)
    assert len(rows) == 6 and more is True
    with pytest.raises(NativeProtocolError):
        parse_vault_page(page([media()], more=True), owner_id=OWNER, page_size=6)
    with pytest.raises(NativeProtocolError):
        parse_vault_page(page([media(userId=OWNER + 1)]), owner_id=OWNER, page_size=6)


@pytest.mark.parametrize('size', [True, 0, -1, 25, 1.5, '6', None])
def test_invalid_page_sizes_are_refused(size):
    with pytest.raises(NativeProtocolError):
        parse_vault_page(page([media()]), owner_id=OWNER, page_size=size)


def test_oversize_failure_is_distinct_and_does_not_advance(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    queued = queue(store, account)

    def too_large(*args, **kwargs):
        error = NativeProtocolError('native HTTPS response exceeded its byte budget')
        error.read_failure = {'category': 'response_limit', 'failure': 'provider_protocol_failed'}
        raise error

    monkeypatch.setattr(NativeProviderConnector, 'vault_page', too_large)
    try:
        assert process_vault_jobs(store, TENANT) == 0
        with Session(store.engine) as s:
            job = s.get(OfVaultSyncJob, (account, queued['jobId']))
            assert job.status == 'failed' and job.last_error == 'vault_response_too_large'
            assert job.page_offset == job.pages_read == job.items_seen == 0
            assert s.scalar(select(func.count()).select_from(VaultItem)) == 0
    finally:
        store.close()


def test_other_accounts_keep_default_page_size(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    queue(store, account)
    monkeypatch.setenv('ONLYFANS_VAULT_PAGE_SIZES', json.dumps({'other-account': 6}))
    calls = []
    def read(self, *, offset, limit):
        calls.append(limit)
        return page([media()])
    monkeypatch.setattr(NativeProviderConnector, 'vault_page', read)
    try:
        assert process_vault_jobs(store, TENANT) == 1
        assert calls == [24]
    finally:
        store.close()
