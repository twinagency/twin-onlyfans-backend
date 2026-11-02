"""Vault sync completion: restart recovery, change detection, confirmed removal, folder membership."""
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT, ACTOR
from test_onlyfans_vault_pg import media, page, queue
from db.models import VaultItem
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultSyncJob, OfVaultObservation
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused
from onlyfans_runtime import vault_management as vm
from onlyfans_runtime.vault_management import process_vault_jobs, request_sync, confirm_removals, list_vault, RESUME_OVERLAP


def key(n):
    return f"{n:064x}"


BYID = {"fn": lambda mid: media(mid)}   # provider answer for a by-id existence check (default: present)


def scan(store, account, rows_by_offset, n, monkeypatch):
    """Queue one full scan with synthetic pages of 24 and drain it."""
    monkeypatch.setattr(NativeProviderConnector, "vault_media_by_id", lambda self, mid: BYID["fn"](mid))
    queue(store, account, key(n))
    def read(self, *, offset, limit):
        rows = rows_by_offset(offset)
        return page(rows, more=bool(rows_by_offset(offset + 24)))
    monkeypatch.setattr(NativeProviderConnector, "vault_page", read)
    while True:
        with Session(store.engine) as s:
            active = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.status.in_(("pending", "running"))))
        if active is None:
            return
        assert process_vault_jobs(store, TENANT, limit=4) >= 1


def vault(ids):
    return lambda off: [media(i) for i in ids[off:off + 24]]


def test_restart_resumes_from_committed_cursor_not_zero(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    job = queue(store, account, key(1))
    with Session(store.engine) as s, s.begin():
        row = s.get(OfVaultSyncJob, (account, job["jobId"]))
        row.pages_read, row.items_seen, row.page_offset = 300, 4776, 4776
    # Expiry/crash ends the job; the next request continues, it does not wipe.
    from onlyfans_runtime.vault_management import _terminal
    with Session(store.engine) as s, s.begin():
        _terminal(s.get(OfVaultSyncJob, (account, job["jobId"])), "failed", "vault_job_expired")
    again = queue(store, account, key(2))
    assert again["pageOffset"] == 4776 - RESUME_OVERLAP and again["pagesRead"] == 300
    offsets = []
    def read(self, *, offset, limit):
        offsets.append(offset)
        return page([media(offset + i + 1) for i in range(24)], more=offset < 4800)
    monkeypatch.setattr(NativeProviderConnector, "vault_page", read)
    assert process_vault_jobs(store, TENANT) == 1
    assert offsets == [4752]
    store.close()


def test_unsupported_and_repeated_page_failures_restart_from_zero(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    job = queue(store, account, key(1))
    with Session(store.engine) as s, s.begin():
        row = s.get(OfVaultSyncJob, (account, job["jobId"]))
        row.pages_read, row.items_seen, row.page_offset = 10, 240, 240
        vm._terminal(row, "failed", "unsupported_vault_pagination")
    assert queue(store, account, key(2))["pageOffset"] == 0
    store.close()


def test_revision_change_supersedes_but_next_sync_resumes_at_cursor(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    job = queue(store, account, key(1))
    monkeypatch.setattr(NativeProviderConnector, "vault_page", lambda self, **kw: page([media(i) for i in range(1, 25)], True))
    assert process_vault_jobs(store, TENANT) == 1
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).revision += 1   # e.g. a session refresh
    process_vault_jobs(store, TENANT)
    with Session(store.engine) as s:
        assert s.get(OfVaultSyncJob, (account, job["jobId"])).status == "superseded"
    resumed = request_sync(store, TENANT, account, expected_connection_revision=2, command_key=key(2), actor=ACTOR)
    assert resumed["pageOffset"] == 0 and resumed["pagesRead"] == 1     # 24 read, overlap of 24 re-read
    store.close()


def test_owner_change_still_supersedes(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    job = queue(store, account, key(1))
    with Session(store.engine) as s, s.begin():
        c = s.get(OfProviderConnection, account)
        c.revision += 1
        c.owner_id += 1
    monkeypatch.setattr(NativeProviderConnector, "vault_page", lambda self, **kw: page([media(1)], False))
    process_vault_jobs(store, TENANT)
    with Session(store.engine) as s:
        assert s.get(OfVaultSyncJob, (account, job["jobId"])).status == "superseded"
    store.close()


def test_new_changed_media_propagate_and_curated_fields_survive(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    scan(store, account, vault([1, 2, 3]), 1, monkeypatch)
    with Session(store.engine) as s, s.begin():
        s.get(VaultItem, (account, 2)).description = "operator note"
        s.get(VaultItem, (account, 2)).notes = "keep me"
    changed = lambda off: [media(4), media(1), media(2, width=1280), media(3)] if off == 0 else []
    scan(store, account, changed, 2, monkeypatch)
    with Session(store.engine) as s:
        assert s.get(VaultItem, (account, 4)) is not None                       # new upload
        two = s.get(VaultItem, (account, 2))
        assert two.width == 1280 and json.loads(two.raw_json)["width"] == 1280   # changed media
        assert two.description == "operator note" and two.notes == "keep me"     # curated fields preserved
    store.close()


def test_incomplete_scan_never_removes_and_complete_scan_alone_is_not_proof(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    called = []
    BYID["fn"] = lambda mid: called.append(mid) or media(mid)
    scan(store, account, vault(list(range(1, 31))), 1, monkeypatch)
    called.clear()
    # Second scan omits media 25-30 and has not finished: no candidate, no provider call, no removal.
    queue(store, account, key(2))
    monkeypatch.setattr(NativeProviderConnector, "vault_page", lambda self, **kw: page([media(i) for i in range(1, 25)], True))
    assert process_vault_jobs(store, TENANT) == 1
    assert confirm_removals(store, TENANT) == 0 and called == []
    # Scan finishes without them, but the provider still serves them by id: they stay on sale.
    monkeypatch.setattr(NativeProviderConnector, "vault_page", lambda self, **kw: page([media(1)], False))
    for _ in range(40):
        process_vault_jobs(store, TENANT)
    assert called and set(called) <= set(range(2, 31))
    with Session(store.engine) as s:
        assert all(r.removed_at is None for r in s.scalars(select(VaultItem)))
    store.close()


def test_provider_404_confirms_removal_and_preserves_history(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    BYID["fn"] = lambda mid: media(mid)
    scan(store, account, vault([1, 2, 3]), 1, monkeypatch)
    with Session(store.engine) as s, s.begin():
        s.get(VaultItem, (account, 3)).description = "sold before"
    # A failed existence check proves nothing.
    def boom(mid):
        raise vm.NativeProtocolError("unsupported vault response")
    BYID["fn"] = boom
    scan(store, account, vault([1, 2]), 2, monkeypatch)
    with Session(store.engine) as s:
        assert s.get(VaultItem, (account, 3)).removed_at is None
    vm._RECHECK_AFTER.clear()
    BYID["fn"] = lambda mid: None if mid == 3 else media(mid)
    assert confirm_removals(store, TENANT) == 1
    with Session(store.engine) as s:
        gone = s.get(VaultItem, (account, 3))
        assert gone.removed_at is not None and gone.description == "sold before"   # row and history kept
        assert s.get(VaultItem, (account, 1)).removed_at is None
    ids = [i["mediaId"] for i in list_vault(store, TENANT, account)["items"] if i["ready"]]
    assert "3" not in ids and "1" in ids
    # A later sighting in the provider vault lifts the removal.
    scan(store, account, vault([1, 2, 3]), 3, monkeypatch)
    with Session(store.engine) as s:
        assert s.get(VaultItem, (account, 3)).removed_at is None
    store.close()


def test_provider_deleted_flag_removes_from_sale_before_send(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    scan(store, account, lambda off: [media(1), media(2)] if off == 0 else [], 1, monkeypatch)
    scan(store, account, lambda off: [media(1), media(2, isDeleted=True)] if off == 0 else [], 2, monkeypatch)
    with Session(store.engine) as s:
        assert s.get(VaultItem, (account, 2)).removed_at is not None
    from onlyfans_runtime.vault_management import create_product
    with pytest.raises(Refused):
        create_product(store, TENANT, account, product={"label": "x", "description": "two", "priceCents": 1000,
            "enabled": True, "mediaIds": ["1", "2"], "previewMediaIds": ["1"]}, command_key=key(9), actor=ACTOR)
    store.close()


def test_folder_membership_uses_has_media_not_folder_presence(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    states = lambda a, b: [{"id": 7, "type": "custom", "hasMedia": a}, {"id": 8, "type": "custom", "hasMedia": b}]
    scan(store, account, lambda off: [media(1, listStates=states(True, False)),
        media(2, listStates=states(False, True))] if off == 0 else [], 1, monkeypatch)
    with Session(store.engine) as s:
        assert json.loads(s.get(VaultItem, (account, 1)).of_folder_ids) == [7]
        assert json.loads(s.get(VaultItem, (account, 2)).of_folder_ids) == [8]
    scan(store, account, lambda off: [media(1, listStates=states(False, False)),
        media(2, listStates=states(True, True))] if off == 0 else [], 2, monkeypatch)
    with Session(store.engine) as s:
        assert json.loads(s.get(VaultItem, (account, 1)).of_folder_ids) == []
        assert json.loads(s.get(VaultItem, (account, 2)).of_folder_ids) == [7, 8]
    store.close()


def test_pages_per_pass_setting_default_and_bounds(monkeypatch):
    import onlyfans_worker as w
    seen = []
    monkeypatch.setattr("onlyfans_runtime.vault_management.process_vault_jobs",
        lambda store, tenant, limit, stop: seen.append(limit) or 0)
    monkeypatch.delenv("ONLYFANS_VAULT_PAGES_PER_PASS", raising=False)
    w._process_vault_jobs(None, "t", limit=1, stop=None)
    for value in ["3", "9", "0", "x"]:
        monkeypatch.setenv("ONLYFANS_VAULT_PAGES_PER_PASS", value)
        w._process_vault_jobs(None, "t", limit=1, stop=None)
    assert seen == [1, 3, 4, 1, 1]


def test_allowance_n_reads_up_to_n_pages_per_pass_and_deferral_stops_it(database, monkeypatch):
    from onlyfans_runtime import provider_read_budget as budget
    store, account, _ = verified(database, monkeypatch)
    job = queue(store, account, key(1))
    offsets = []
    def read(self, *, offset, limit):
        offsets.append(offset)
        return page([media(offset + i + 1) for i in range(24)], more=True)
    monkeypatch.setattr(NativeProviderConnector, "vault_page", read)
    held = budget.acquire(store, TENANT, account, connection_revision=1, kind="recent")
    assert process_vault_jobs(store, TENANT, limit=3) == 0 and offsets == []   # deferral ends the pass
    budget.release(store, TENANT, held)
    import time
    from datetime import timedelta
    from onlyfans_runtime.store import utcnow
    later = utcnow() + timedelta(seconds=3)
    monkeypatch.setattr("onlyfans_runtime.provider_read_budget.utcnow", lambda: later)
    monkeypatch.setattr("onlyfans_runtime.vault_management.utcnow", lambda: later)
    assert process_vault_jobs(store, TENANT, limit=3) == 3
    assert offsets == [0, 24, 48]
    with Session(store.engine) as s:
        row = s.get(OfVaultSyncJob, (account, job["jobId"]))
        assert row.page_offset == 72 and row.pages_read == 3
    store.close()
