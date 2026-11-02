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


def test_receipts_from_another_tenant_do_not_complete_this_tenants_memory(ready):
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from db.onlyfans_models import OfRuntime
    from onlyfans_runtime.history_progress import recent_progress
    store,recovery,engine,cutoff=ready
    claim=recovery.claim(T,A)
    recovery.page(T,claim,[event(20,cutoff)],has_more=False)
    with engine.begin() as db:
        for tenant in ['other-tenant', T]:
            db.execute(text('''INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id)
                SELECT :tenant,account_id,binding_id,101 FROM of_runtime WHERE account_id=:account
                ON CONFLICT DO NOTHING'''), {'tenant':tenant,'account':A})
        db.execute(text('''INSERT INTO of_profile_receipts(tenant_id,account_id,binding_id,fan_id,
            source_fan,message_id,body_md5,source_at,direction,unsent,automation,next_chunk,total_chunks)
            SELECT 'other-tenant',m.account_id,r.binding_id,m.fan_id,m.fan_id,m.message_id,
            md5(m.body),m.created_at,m.direction,m.is_unsent,coalesce(m.automation_kind,''),1,1
            FROM messages m JOIN of_runtime r ON r.account_id=m.account_id
            WHERE m.account_id=:account AND m.message_id=20'''), {'account':A})
    assert capture(store,T,A)['receipts']=={}
    with Session(engine) as db:
        result=recent_progress(db,db.get(OfRuntime,A))
        assert result['memoryProcessing']['messagesAwaitingProcessing']==1
    with engine.begin() as db:
        db.execute(text('''INSERT INTO of_profile_receipts SELECT :tenant,account_id,binding_id,
            fan_id,source_fan,message_id,body_md5,source_at,direction,unsent,automation,
            next_chunk,total_chunks,processed_at FROM of_profile_receipts
            WHERE tenant_id='other-tenant' AND account_id=:account'''), {'tenant':T,'account':A})
    assert len(capture(store,T,A)['receipts'])==1
    with Session(engine) as db:
        assert recent_progress(db,db.get(OfRuntime,A))['memoryProcessing']['messagesAwaitingProcessing']==0
