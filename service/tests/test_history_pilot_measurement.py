"""Reused sources, edits, reservations and earlier settlements stay distinct."""
import copy
import pytest
from tools.history_pilot_measurement import SCHEMA, compare, capture
from test_onlyfans_foundation_pg import database
from test_recent_history_onboarding_pg import ready, event, T, A


def baseline():
    return {'schemaVersion':SCHEMA,'accountId':'a','bindingId':'b','cutoffAt':'2026-10-03',
        'observedAt':'2026-11-02T00:00:00+00:00','sources':{'existing':'v1','edited':'old'},
        'receipts':{'existing':{'digest':'v1','committedAt':'2026-10-31 00:00:00'}},
        'modelCalls':{'earlier':{'bucket':'unresolvedReservationUnits','units':30,'latencyMs':0}},
        'recentHttpAttempts':100}


def test_rerun_does_not_claim_existing_sources_or_old_settlements_as_new_work():
    before=baseline();after=copy.deepcopy(before);after['observedAt']='2026-11-02T00:02:00+00:00'
    after['sources'].update(edited='new',fresh='f1')
    after['receipts']['fresh']={'digest':'f1','committedAt':'2026-11-02 00:01:00'}
    after['modelCalls']['earlier']={'bucket':'providerReportedUnits','units':5,'latencyMs':10}
    after['modelCalls']['new']={'bucket':'providerReportedUnits','units':2,'latencyMs':100}
    after['modelCalls']['pending']={'bucket':'activeReservationUnits','units':10,'latencyMs':0}
    after['recentHttpAttempts']=104
    result=compare(before,after)
    assert result['existingWindowSourcesAtStart']==2
    assert result['unchangedExistingWindowSourcesReused']==1
    assert result['newWindowSourcesSaved']==1
    assert result['existingWindowSourcesEdited']==1
    assert result['reusedCompletedIncomingReceipts']==1
    assert result['newlyCompletedIncomingReceipts']==1
    assert result['newModelCallAmounts']['providerReportedUnits']==2
    assert result['newModelCallAmounts']['activeReservationUnits']==10
    assert result['preExistingModelCallSettlementChanges']['providerReportedUnits']==5
    assert result['preExistingModelCallSettlementChanges']['unresolvedReservationUnits']==-30
    assert result['newRecentProviderHttpAttempts']==4
    assert result['observedElapsedSeconds']==120
    assert result['cumulativeNewModelCallLatencyMs']==100
    assert result['fullHistoryTimeOrCostSavingsDemonstrated'] is False


def test_changed_binding_or_cutoff_and_reversed_snapshots_are_refused():
    before=baseline()
    for key,value in [('bindingId','different'),('cutoffAt','different'),('observedAt','2026-11-01T00:00:00+00:00')]:
        after=copy.deepcopy(before);after[key]=value
        with pytest.raises(ValueError):compare(before,after)


def test_private_capture_reads_current_binding_and_reports_new_sources(ready):
    store,recovery,engine,cutoff=ready
    before=capture(store,T,A)
    claim=recovery.claim(T,A)
    recovery.page(T,claim,[event(20,cutoff,body='Private synthetic text')],has_more=False)
    after=capture(store,T,A)
    result=compare(before,after)
    assert result['newWindowSourcesSaved']==1
    assert result['newModelCalls']==0
    assert 'Private synthetic text' not in str(after)
    assert all(len(k)==64 for k in after['sources'])
