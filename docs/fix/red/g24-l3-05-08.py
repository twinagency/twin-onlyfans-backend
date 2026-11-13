"""G24 red tests L3-05, L3-06, L3-07, L3-08 (W1 fans-api-adapter FINDINGS.md; vault retry/401/exhaustion/Retry-After cap).

Deferred from G06 (PR branch fix/G06-vault-sync-catalogue): different root cause (sync_vault retry-state ownership,
protocol RETRY_AFTER_MAX), owned by fix group G24. Red at 500a2b66 and at G06 head 4be4e46c (measured).
Removed from the promoted file because premerge counts xfail as skip. Drop into service/tests/ beside
test_w1_fans_api_adapter_pg.py (reuses its setup/listing helpers).
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_vault_models import OfVaultSyncJob
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from test_real_transport_pg import _item
from test_engine_real_worker_pg import answer
from fans_api_adapter.vault_sync import sync_vault
from fans_api_adapter.protocol import parse_retry_after
from onlyfans_runtime.store import Refused
from test_w1_fans_api_adapter_pg import setup, listing


def _job(store):
    with Session(store.engine) as s:
        job = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == ACCOUNT)
                       .order_by(OfVaultSyncJob.requested_at.desc()).limit(1))
        s.expunge(job)
        return job


# L3-05: the job owner persists retry_not_before (Retry-After 120) but the next sync_vault call reads anyway.
def test_w1_l3_05_sync_vault_honours_persisted_retry_after(database):
    state = {"n": 0}

    def script(offset, limit):
        state["n"] += 1
        return answer(429, {"error": "synthetic"}, {"Retry-After": "120"}) if state["n"] == 1 else listing([_item(11)])(offset, limit)
    store, client, calls = setup(database, script)
    first = sync_vault(store, TENANT, ACCOUNT, client)
    assert first["failure"] == "transient" and first["retryNotBefore"]
    sync_vault(store, TENANT, ACCOUNT, client)
    assert calls == [0], "second read issued before the persisted Retry-After due time"
    store.close()


# L3-06: a 401 on the vault read leaves the job pending with no attempt/due -> every call re-reads (retry storm).
def test_w1_l3_06_vault_401_is_terminal_not_repolled(database):
    store, client, calls = setup(database, lambda o, l: answer(401, {"error": "synthetic"}))
    first = sync_vault(store, TENANT, ACCOUNT, client)
    assert first["failure"] == "auth"
    for _ in range(3):
        sync_vault(store, TENANT, ACCOUNT, client)
    assert calls == [0], f"401 re-polled {len(calls)} times"
    assert _job(store).status == "failed"
    store.close()


# L3-07: after 5 transient failures the job is terminal ('give up visibly'), but sync_vault without a
# command_key silently starts a fresh job at offset 0 with attempts reset.
def test_w1_l3_07_exhausted_job_is_not_silently_restarted(database):
    store, client, calls = setup(database, lambda o, l: answer(503, {}))
    for _ in range(5):
        sync_vault(store, TENANT, ACCOUNT, client)
        with Session(store.engine) as s, s.begin():        # make the persisted due time pass
            for job in s.scalars(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == ACCOUNT)):
                job.retry_not_before = None
    assert _job(store).last_error == "vault_retry_exhausted"
    n = len(calls)
    with pytest.raises(Refused):
        sync_vault(store, TENANT, ACCOUNT, client)
    assert len(calls) == n
    store.close()


# L3-08: Retry-After larger than 3600 s is cut to 3600 -> the importer retries before the provider's deadline.
def test_w1_l3_08_retry_after_is_not_shortened():
    assert parse_retry_after("7200") >= 7200
