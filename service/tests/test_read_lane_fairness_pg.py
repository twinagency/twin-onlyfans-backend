"""R4: deterministic multi-lane simulation of one account's shared provider-read budget.

Each lane attempts at its production cadence on a fake clock; the real
provider_read_budget, ledger scan, and vault job code decide who acquires. The
recorded log answers which hypotheses reproduce (see docs R4 report)."""
from collections import Counter
from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401
from test_onlyfans_connections_pg import TENANT
from test_onlyfans_vault_pg import queue, media, page
from db.onlyfans_vault_models import OfVaultSyncJob
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from onlyfans_runtime import provider_read_budget as budget, vault_management
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow


class Sim:
    def __init__(self, store, account, monkeypatch):
        self.store, self.account = store, account
        self.t = utcnow()
        self.log = []                   # (second, lane, outcome)
        monkeypatch.setattr(budget, 'utcnow', lambda: self.t)
        monkeypatch.setattr(vault_management, 'utcnow', lambda: self.t)
        self.pages = 0

    def read(self, kind):
        try:
            claim = budget.acquire(self.store, TENANT, self.account, connection_revision=1, kind=kind)
        except budget.ReadDeferred as d:
            self.log.append((self.t, kind, 'deferred:' + str(d)))
            return False
        budget.release(self.store, TENANT, claim)
        self.log.append((self.t, kind, 'ok'))
        return True

    def vault(self):
        before = self.pages
        vault_management.process_vault_jobs(self.store, TENANT)
        self.log.append((self.t, 'vault', 'ok' if self.pages > before else 'deferred'))

    def run(self, seconds, *, recent_per_second=0, history_every=15, vault_every=15):
        for sec in range(seconds):
            self.t += timedelta(seconds=1)
            for _ in range(recent_per_second):
                self.read('recent')
            if sec % history_every == 0:
                self.read('history')
            if sec % vault_every == 0:
                self.vault()

    def count(self, lane):
        return sum(1 for _, k, o in self.log if k == lane and o == 'ok')


@pytest.fixture
def sim(native, monkeypatch):
    store, account, _ = native
    from db.onlyfans_pilot_models import ensure_tables
    ensure_tables(store.engine)
    queue(store, account)

    def read_page(self, *, offset, limit):
        sim_.pages += 1
        return page([media(offset + i + 1) for i in range(24)], more=True)
    monkeypatch.setattr(NativeProviderConnector, 'vault_page', read_page)
    sim_ = Sim(store, account, monkeypatch)
    return sim_


def test_idle_recent_lets_vault_and_history_progress(sim):
    """Control: with only the recent head poll, background lanes progress (no starvation)."""
    sim.run(120, recent_per_second=0)
    assert sim.count('vault') >= 6 and sim.count('history') >= 6


def test_saturating_recent_backlog_does_not_starve_background_lanes(sim):
    """Onboarding backlog: recent polls ~1/s with up to 4 pages each (4 reads/s) > refill (1/s).
    Hypothesis H1 (token drain). Baseline: vault/history acquire ~0 times in 5 minutes."""
    sim.run(300, recent_per_second=4)
    recent = sim.count('recent')
    assert sim.count('vault') >= 5, Counter((k, o.split(':')[0]) for _, k, o in sim.log)
    assert sim.count('history') >= 5
    # incoming messages keep at least 85% of the refill (310 tokens incl. the initial bucket)
    assert recent >= 0.85 * 300, recent


def test_vault_progress_with_replies_waiting_under_saturation(sim):
    from test_fastt_read_priority_pg import set_board
    set_board(sim.store, sim.account, 'waiting')
    # keep the board fresh on the simulated clock
    sim.run(300, recent_per_second=4)
    assert sim.count('vault') >= 5
