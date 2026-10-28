"""Real imported-row discovery with synthetic SQLite data; no provider/model calls."""
import json
import os
from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest
from db.models import Account, AccountAiConfig, OfCreatorBinding, VaultItem
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from onlyfans_runtime import vault_description_worker as jobs
from onlyfans_runtime.store import utcnow


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS", raising=False)
    engine = create_engine("sqlite://")
    for model in (Account, AccountAiConfig, OfCreatorBinding, OfRuntime,
                  OfProviderConnection, VaultItem, OfVaultSyncJob, OfVaultObservation):
        model.__table__.create(engine)
    yield SimpleNamespace(engine=engine)
    engine.dispose()


def imported(store, number, *, tenant="tenant-a", **item_values):
    now = utcnow()
    account, binding = f"of-{number}", f"binding-{number}"
    with Session(store.engine) as s, s.begin():
        s.add(Account(id=account))
        s.add(OfCreatorBinding(id=binding, tenant_id=tenant, creator_id=f"creator-{number}",
            creator_name="Synthetic", account_id=account, handle="synthetic", channel="onlyfans"))
        s.add(OfRuntime(account_id=account, binding_id=binding, enabled=False, activated_at=now))
        s.add(OfProviderConnection(account_id=account, binding_id=binding, owner_id=number,
            revision=1, sealed_config="synthetic-encrypted", expires_at=now+timedelta(hours=1),
            verified_at=now, status="verified", updated_at=now))
        s.add(OfVaultSyncJob(account_id=account, job_id="synthetic-job", binding_id=binding,
            command_key=str(number).zfill(64), request_hash="a"*64, actor="worker-import:synthetic",
            connection_revision=1, owner_id=number, max_pages=200, status="complete",
            page_offset=0, pages_read=0, items_seen=0, requested_at=now, updated_at=now))
        s.add(VaultItem(account_id=account, media_id=number, kind="photo", created_at=now,
            raw_json="{}", **item_values))
        s.add(OfVaultObservation(account_id=account, media_id=number, job_id="synthetic-job",
            raw_hash="a"*64, observed_at=now))
    return account


def ids(store, after=None):
    rows, cursor = jobs.accounts(store, "tenant-a", after)
    return [row[0] for row in rows], cursor


def test_new_import_is_discovered_without_another_trigger_and_ai_can_stay_off(store):
    assert ids(store) == ([], None)
    account = imported(store, 1001)
    assert ids(store)[0] == [account]
    # Completed work disappears from the queue; a later import is picked up.
    with Session(store.engine) as s, s.begin():
        row = s.get(VaultItem, (account, 1001)); row.description = "Synthetic photo"; row.describe_status = "described"
    assert ids(store)[0] == []
    assert ids(store)[0] == []  # restart/rescan does not re-caption completed work
    second = imported(store, 1002)
    assert ids(store)[0] == [second]


def test_foreign_expired_rebound_unobserved_removed_and_unsupported_media_are_excluded(store):
    imported(store, 1001, tenant="tenant-b")
    expired = imported(store, 1002)
    rebound = imported(store, 1003)
    unobserved = imported(store, 1004)
    imported(store, 1005, removed_at=utcnow())
    unsupported = imported(store, 1006)
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, expired).expires_at = utcnow()-timedelta(seconds=1)
        s.get(OfVaultSyncJob, (rebound, "synthetic-job")).binding_id = "old-binding"
        s.delete(s.get(OfVaultObservation, (unobserved, 1004)))
        s.get(VaultItem, (unsupported, 1006)).kind = "audio"
    assert ids(store)[0] == []


def test_manual_and_video_descriptions_are_preserved_and_failure_retries_are_bounded(store):
    now = utcnow()
    imported(store, 1001, description="Operator description")
    imported(store, 1002, video_description="Existing video description")
    imported(store, 1003, describe_status="refused")
    imported(store, 1004, describe_status="failed", describe_generated_at=now)
    imported(store, 1005, describe_status="blocked_drm", describe_generated_at=now-timedelta(days=1))
    imported(store, 1006, describe_status="fetch_failed", describe_generated_at=now-timedelta(hours=1))
    assert ids(store)[0] == ["of-1006"]


def test_holds_are_respected_without_starving_later_creators(store, monkeypatch):
    for i in range(1000, 1033):
        account = imported(store, i)
        if i < 1032:
            with Session(store.engine) as s, s.begin():
                s.add(AccountAiConfig(account_id=account, vault_ai_config_json=json.dumps({"auto_describe_on_import": False})))
    first, cursor = ids(store)
    assert first == [] and cursor == "of-1031"
    assert ids(store, cursor)[0] == ["of-1032"]
    monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS", "of-1032")
    assert ids(store, cursor)[0] == []


@pytest.mark.parametrize("enabled,once,expected", [(True, False, ["start", "join"]),
    (False, False, []), (True, True, [])])
def test_worker_starts_and_stops_background_descriptions_without_changing_role(monkeypatch, enabled, once, expected):
    import onlyfans_worker as entry
    import onlyfans_runtime.store
    seen = []
    class FakeStore:
        def __init__(self, url): pass
        def close(self): pass
    class Background:
        def __init__(self, store, tenant, stop, **kwargs):
            assert tenant == "tenant-a"
        def start(self): seen.append("start")
        def join(self): seen.append("join")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT", "tenant-a")
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY", "ab"*32)
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic:synthetic@127.0.0.1:65432/test")
    monkeypatch.setenv("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED", "1")
    monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTIONS_ENABLED", "1" if enabled else "0")
    for flag in ("ONLYFANS_VAULT_MEDIA_ENABLED", "ONLYFANS_LEDGER_ENABLED", "ONLYFANS_TWIN_FACTS_ENABLED"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setattr(onlyfans_runtime.store, "Store", FakeStore)
    monkeypatch.setattr(jobs, "VaultDescriptionWorker", Background)
    monkeypatch.setattr(entry, "run_loop", lambda *args, **kwargs: 0)
    assert entry.main(["--once"] if once else []) == 0
    assert seen == expected
    assert os.environ["ONLYFANS_PROCESS_ROLE"] == "worker"
