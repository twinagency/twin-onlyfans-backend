"""A Maile-only learner must leave other stopped account queues untouched."""
from datetime import datetime,timedelta
import threading
from sqlalchemy import text
from test_profile_learning_queue import store, fan_due, state
from onlyfans_runtime.profile_learning import worker,cohort
from onlyfans_runtime.store import Refused
import pytest


def test_disabled_cohort_has_no_claim_or_silent_completion(store,monkeypatch):
    fan_due(store)
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS','[]')
    assert worker.claim_job(store,'tenant',threading.Event()) is None
    assert worker.finalize_silent(store,'tenant',threading.Event())==0
    assert state(store)['checked_at'] is None


def test_another_account_cohort_cannot_touch_existing_due_work(store,monkeypatch):
    fan_due(store)
    due=datetime.utcnow()+timedelta(days=1)
    with store.engine.begin() as c:c.execute(text('UPDATE of_profile_learning SET next_at=:due,maintenance_at=NULL'),{'due':due})
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS','["another-account"]')
    worker.daily_maintenance(store,'tenant')
    assert datetime.fromisoformat(str(state(store)['next_at']))==due
    assert worker.claim_job(store,'tenant',threading.Event()) is None
    assert worker.finalize_silent(store,'tenant',threading.Event())==0


def test_selected_account_can_claim_existing_work(store,monkeypatch):
    fan_due(store)
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS','["of-1"]')
    assert worker.claim_job(store,'tenant',threading.Event())['account_id']=='of-1'


def test_invalid_cohort_fails_closed(monkeypatch):
    for value in ['{}','[1]','bad']:
        monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS',value)
        with pytest.raises(Refused,match='invalid-profile-account-scope'):cohort.includes('of-1')
