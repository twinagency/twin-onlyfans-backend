"""Batched control reads must retain the existing fail-closed repairs."""
from sqlalchemy.orm import Session
from db.models import OfConversationState, SkipList
from test_fastt_controls_pg import template, database, configured, set_row, ACCOUNT, FAN, TENANT


def test_controls_window_selects_missing_or_drifting_rows_without_writing(database):
    _, engine = database
    store, _ = configured(database, mode='automated')
    try:
        assert store.runtime_stop_candidates(ACCOUNT,[FAN])==set()
        assert store.runtime_stop_candidates(ACCOUNT,[FAN+1])=={FAN+1}
        set_row(engine,'manual_restrict')
        assert store.runtime_stop_candidates(ACCOUNT,[FAN])=={FAN}
        # The read itself cannot change control or grant permission.
        with Session(engine) as s:
            assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='automated'
        assert store.reconcile_runtime_stop(TENANT,ACCOUNT,FAN)['mode']=='paused'
        assert store.runtime_stop_candidates(ACCOUNT,[FAN])==set()
        with Session(engine) as s,s.begin():
            s.query(SkipList).filter_by(account_id=ACCOUNT,fan_id=FAN).delete()
        assert store.runtime_stop_candidates(ACCOUNT,[FAN])=={FAN}
        assert store.reconcile_runtime_stop(TENANT,ACCOUNT,FAN)['stopped']
        assert store.runtime_stop_candidates(ACCOUNT,[FAN])==set()
        with Session(engine) as s,s.begin():
            s.delete(s.get(OfConversationState,(ACCOUNT,FAN)))
        assert store.runtime_stop_candidates(ACCOUNT,[FAN])=={FAN}
    finally:store.close()
