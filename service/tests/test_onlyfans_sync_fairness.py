"""Fair import turns are opt-in, scoped, bounded and survive cohort refresh."""
from types import SimpleNamespace
import pytest
from onlyfans_runtime import sync_fairness as fair


def lanes(monkeypatch, calls, fail=False):
    from onlyfans_runtime import vault_management, offer_import_worker, offer_post_source
    for module, name, lane in [(vault_management, 'process_vault_jobs', 'vault'),
        (offer_import_worker, 'process_refresh_jobs', 'offer'),
        (offer_post_source, 'process_posts', 'posts')]:
        def run(store, tenant, *, limit, account_id, lane=lane):
            calls.append((lane, tenant, account_id, limit))
            if fail: raise RuntimeError('synthetic deferral')
        monkeypatch.setattr(module, name, run)
    monkeypatch.setenv('ONLYFANS_OFFER_IMPORT_ENABLED', '1')
    monkeypatch.setenv('ONLYFANS_AUTOMATIC_IMPORTS_ENABLED', '1')


def test_default_off_and_exact_account_allowlist(monkeypatch):
    calls=[];lanes(monkeypatch,calls)
    monkeypatch.delenv('ONLYFANS_FAIR_SYNC_ACCOUNTS', raising=False)
    store=SimpleNamespace()
    fair.turn(store,'tenant','emma')
    monkeypatch.setenv('ONLYFANS_FAIR_SYNC_ACCOUNTS',' emma , ')
    fair.turn(store,'tenant','emma-other')
    assert calls==[] and not hasattr(store,'_fair_sync_turns')


def test_rotation_is_per_account_and_retained_on_shared_store(monkeypatch):
    calls=[];lanes(monkeypatch,calls)
    monkeypatch.setenv('ONLYFANS_FAIR_SYNC_ACCOUNTS','emma,other')
    store=SimpleNamespace()
    for _ in range(6):fair.turn(store,'tenant','emma')
    fair.turn(store,'tenant','other')
    assert [x[0] for x in calls]==['vault','offer','posts']*2+['vault']
    assert all(x[1:] == ('tenant','emma',1) for x in calls[:6])


def test_deferred_lane_does_not_monopolise_next_turn(monkeypatch):
    calls=[];lanes(monkeypatch,calls,fail=True)
    monkeypatch.setenv('ONLYFANS_FAIR_SYNC_ACCOUNTS','emma')
    store=SimpleNamespace()
    for _ in range(3):
        with pytest.raises(RuntimeError):fair.turn(store,'tenant','emma')
    assert [x[0] for x in calls]==['vault','offer','posts']


def test_feature_flags_still_gate_offer_and_post_reads(monkeypatch):
    calls=[];lanes(monkeypatch,calls)
    monkeypatch.setenv('ONLYFANS_FAIR_SYNC_ACCOUNTS','emma')
    for flag in ('ONLYFANS_OFFER_IMPORT_ENABLED','ONLYFANS_AUTOMATIC_IMPORTS_ENABLED','ONLYFANS_CONTENT_SYNC_ENABLED'):
        monkeypatch.delenv(flag,raising=False)
    store=SimpleNamespace()
    for _ in range(3):fair.turn(store,'tenant','emma')
    assert [x[0] for x in calls]==['vault']
