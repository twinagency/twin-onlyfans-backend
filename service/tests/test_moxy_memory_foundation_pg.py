"""Memory readiness is evidence, not an implicit switch to missing facts.

Real disposable PostgreSQL and the actual shared snapshot; all inputs synthetic.
No model or provider calls are made by this suite.
"""
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_profile_learning_pg import pg_store, run
from test_profile_learning_queue import add_message, fan_due
from db.models import Fan, FanProfile, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.launch_history_memory.memory import snapshot
from onlyfans_runtime.launch_history_memory.models import MemoryCorrection
from onlyfans_runtime.profile_learning.storage import profile_view, reply_memory


@pytest.fixture(autouse=True)
def learning_enabled(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    monkeypatch.delenv('ONLYFANS_PROFILE_ACCOUNTS', raising=False)


def view(store):
    with Session(store.engine) as s:
        return reply_memory(s, 'of-1', 1)


def job_state(store, *, complete=False, checked=True, pending=False, error=None,
              scope='all_stored_history'):
    fan_due(store)
    now = datetime.utcnow()
    with store.engine.begin() as c:
        c.execute(text("""UPDATE of_profile_learning SET document_json=:doc,
            checked_at=:checked, queued_at=:queued, error_code=:error
            WHERE account_id='of-1' AND fan_id=1"""), {
            'doc': json.dumps({'history_complete': complete, 'memory_count': 0,
                               'processing_scope': scope}),
            'checked': now if checked else None,
            'queued': now - timedelta(minutes=1) if pending else now + timedelta(hours=2),
            'error': error})


def test_enabled_without_job_reports_unprocessed_not_empty_complete(pg_store):
    result = view(pg_store)
    assert result['memories'] == []
    assert result['learningAvailability'] == {
        'workerSelected': True, 'state': 'not_processed',
        'processingScope': None, 'scopedHistoryProcessed': False,
        'storedHistoryProcessed': False,
    }


@pytest.mark.parametrize(('complete', 'checked', 'pending', 'error', 'state'), [
    (False, False, False, None, 'not_processed'),
    (False, True, False, None, 'processing_history'),
    (True, True, True, None, 'processing_updates'),
    (True, True, False, None, 'ready'),
    (True, True, False, 'profile-budget-exhausted', 'failed'),
])
def test_empty_memory_does_not_collapse_distinct_coverage_states(
        pg_store, complete, checked, pending, error, state):
    job_state(pg_store, complete=complete, checked=checked, pending=pending, error=error)
    result = view(pg_store)
    assert result['memories'] == []
    available = result['learningAvailability']
    assert available['state'] == state
    assert available['scopedHistoryProcessed'] is (state == 'ready')
    assert available['storedHistoryProcessed'] is (state == 'ready')


def test_recent_scope_completion_is_not_complete_stored_history(pg_store):
    job_state(pg_store, complete=True, scope='recent_window_and_activated_older')
    available = view(pg_store)['learningAvailability']
    assert available['scopedHistoryProcessed'] is True
    assert available['storedHistoryProcessed'] is False


def test_failed_learning_does_not_advertise_complete_coverage_in_inbox(pg_store):
    job_state(pg_store, complete=True, error='profile-source-changed')
    with Session(pg_store.engine) as s:
        result = profile_view(s, 'tenant', 'of-1', 'binding', 1)
    assert result['state'] == 'failed'
    assert result['scopedHistoryProcessed'] is False
    assert result['storedHistoryProcessed'] is False


def test_cohort_mismatch_is_visible_without_discarding_proven_existing_memory(pg_store, monkeypatch):
    add_message(pg_store)
    fan_due(pg_store)
    run(pg_store)
    before = view(pg_store)
    assert before['memories']
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS', '["another-account"]')
    result = view(pg_store)
    assert result['learningAvailability']['workerSelected'] is False
    assert result['memories'] == before['memories']


@pytest.mark.parametrize('change', ['revoke', 'rebind'])
def test_invalid_current_binding_cannot_expose_previous_memory(pg_store, change):
    add_message(pg_store)
    fan_due(pg_store)
    run(pg_store)
    with Session(pg_store.engine) as s, s.begin():
        if change == 'revoke':
            s.get(OfCreatorBinding, 'binding').revoked_at = datetime.utcnow()
        else:
            s.get(OfCreatorBinding, 'binding').revoked_at = datetime.utcnow()
            s.flush()
            s.add(OfCreatorBinding(id='replacement', account_id='of-1', tenant_id='tenant',
                                   creator_id='replacement-creator'))
            s.flush()
            s.get(OfRuntime, 'of-1').binding_id = 'replacement'
    result = view(pg_store)
    assert result['memories'] == []
    assert result['learningAvailability']['state'] == (
        'binding_unavailable' if change == 'revoke' else 'not_processed')


@pytest.mark.parametrize('change', ['edit', 'unsend'])
def test_unavailable_worker_never_makes_invalid_source_usable(pg_store, monkeypatch, change):
    from db.models import Message
    add_message(pg_store)
    fan_due(pg_store)
    run(pg_store)
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS', '[]')
    with Session(pg_store.engine) as s, s.begin():
        source = s.get(Message, ('of-1', 1, 1))
        if change == 'edit':
            source.body = 'This is different source text'
        else:
            source.is_unsent = True
    assert view(pg_store)['memories'] == []


def test_incomplete_snapshot_keeps_operator_corrections_without_legacy_resurrection(pg_store):
    with Session(pg_store.engine) as s, s.begin():
        s.get(Fan, ('of-1', 1)).real_name = 'Unverified old name'
        s.add(FanProfile(account_id='of-1', fan_id=1, short_bio='STALE_UNDATED_PROFILE'))
        s.add_all([
            MemoryCorrection(account_id='of-1', fan_id=1, command_key='clear-name',
                field='real_name', operation='clear', value=None, before_json='{}',
                request_hash='a' * 64, revision=1, actor='synthetic-operator', reason='Correction',
                created_at=datetime.utcnow()),
            MemoryCorrection(account_id='of-1', fan_id=1, command_key='set-city',
                field='home_city', operation='set', value='Perth', before_json='{}',
                request_hash='b' * 64, revision=2, actor='synthetic-operator', reason='Correction',
                created_at=datetime.utcnow()),
        ])
    with Session(pg_store.engine) as s:
        result = snapshot(s, 'of-1', 1, query='what do you remember?')
        assert reply_memory(s, 'of-1', 1)['learningAvailability']['state'] == 'not_processed'
    assert 'learningAvailability' not in result['learnedProfile']
    assert result['facts'] == {'home_city': {'value': 'Perth', 'source': 'operator', 'revision': 2}}
    assert result['overrides']['real_name']['operation'] == 'clear'
    assert 'STALE_UNDATED_PROFILE' not in json.dumps(result)
    assert 'Unverified old name' not in json.dumps(result)


def test_disabled_learning_preserves_existing_api_opt_out(pg_store, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '0')
    assert view(pg_store) is None


def test_operator_clear_excludes_previously_learned_name_when_worker_paused(pg_store, monkeypatch):
    add_message(pg_store, body='My name is Carlos')
    fan_due(pg_store)

    async def name(*args, **kwargs):
        return {'facts': [{'field': 'name', 'value': 'Carlos',
                          'quote': 'My name is Carlos', 'source': '1:1'}]}, None

    run(pg_store, name)
    assert view(pg_store)['memories']
    with Session(pg_store.engine) as s, s.begin():
        s.add(MemoryCorrection(account_id='of-1', fan_id=1, command_key='clear-learned-name',
            field='real_name', operation='clear', value=None, before_json='{}',
            request_hash='c' * 64, revision=1, actor='synthetic-operator', reason='Correction',
            created_at=datetime.utcnow()))
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS', '[]')
    with Session(pg_store.engine) as s:
        result = snapshot(s, 'of-1', 1, query='name')
    assert result['learnedProfile']['memories'] == []
    assert view(pg_store)['learningAvailability']['workerSelected'] is False
    assert result['overrides']['real_name']['operation'] == 'clear'
    assert 'Carlos' not in json.dumps(result)


def test_invalid_worker_scope_is_diagnostic_without_stopping_reply_memory(pg_store, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS', '{invalid-json')
    result = view(pg_store)
    assert result['learningAvailability']['workerSelected'] is None
    assert result['learningAvailability']['workerScopeError'] == 'invalid-profile-account-scope'
    with Session(pg_store.engine) as s:
        assert snapshot(s, 'of-1', 1)['learnedProfile']['memories'] == []


@pytest.mark.parametrize('scope', ['unrecognized-scope', None, ['all_stored_history']])
def test_unknown_processing_scope_does_not_claim_complete_history(pg_store, scope):
    job_state(pg_store, complete=True, scope=scope)
    result = view(pg_store)['learningAvailability']
    assert result['state'] == 'invalid_coverage'
    assert result['scopedHistoryProcessed'] is False
    assert result['storedHistoryProcessed'] is False


def test_operational_coverage_changes_do_not_change_authority_but_evidence_does(pg_store, monkeypatch):
    from db.models import Message
    fan_due(pg_store)
    with Session(pg_store.engine) as s:
        before = snapshot(s, 'of-1', 1, query='hobbies')
    job_state(pg_store, complete=True)
    monkeypatch.setenv('ONLYFANS_PROFILE_ACCOUNTS', '[]')
    with Session(pg_store.engine) as s:
        operational_only = snapshot(s, 'of-1', 1, query='hobbies')
        profile = profile_view(s, 'tenant', 'of-1', 'binding', 1)
    assert operational_only == before
    assert profile['learningAvailability']['state'] == 'ready'
    assert profile['learningAvailability']['workerSelected'] is False

    monkeypatch.delenv('ONLYFANS_PROFILE_ACCOUNTS')
    add_message(pg_store)
    fan_due(pg_store)
    run(pg_store)
    with Session(pg_store.engine) as s:
        learned = snapshot(s, 'of-1', 1, query='hobbies')
    assert learned['learnedProfile']['memories']
    assert learned['fingerprint'] != before['fingerprint']
    with Session(pg_store.engine) as s, s.begin():
        s.get(Message, ('of-1', 1, 1)).body = 'That source has been edited'
    with Session(pg_store.engine) as s:
        edited = snapshot(s, 'of-1', 1, query='hobbies')
    assert edited['learnedProfile']['memories'] == []
    assert edited['fingerprint'] != learned['fingerprint']
