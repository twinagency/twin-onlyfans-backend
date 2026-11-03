"""Bounded explicit test-fan archive refresh using the existing history writer."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Fan,OfConversationState
from onlyfans_runtime.store import Refused,utcnow
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery,OfHistoryCursor,ArchiveClaim
from onlyfans_runtime.worker import _provider_claim,_release_provider

def refresh_test_history(store,tenant,account,fan,reader,*,connection_revision,max_pages=4):
    if reader.account_id!=account or type(fan) is not int or fan<=0 or not 1<=max_pages<=4:
        raise Refused('invalid_test_history_scope')
    recovery=HistoryRecovery(store)
    with Session(store.engine) as s:
        runtime=recovery._runtime(s,tenant,account,connection_revision)
        control=s.get(OfConversationState,(account,fan))
        if s.get(Fan,(account,fan)) is None or not control or control.mode!='paused':
            raise Refused('test_fan_must_remain_paused')
    recovery.request_fan(tenant,account,fan,connection_revision=connection_revision)
    outcomes=[]
    for _ in range(max_pages):
        budget=claim=error=None
        try:
            budget=_provider_claim(store,tenant,account,reader,connection_revision,'history')
            with Session(store.engine) as s,s.begin():
                runtime=recovery._runtime(s,tenant,account,connection_revision)
                control=s.get(OfConversationState,(account,fan))
                if not control or control.mode!='paused':raise Refused('test_fan_control_changed')
                row=s.scalar(select(OfHistoryCursor).where(
                    OfHistoryCursor.account_id==account,OfHistoryCursor.fan_id==fan,
                    HistoryRecovery._eligible(OfHistoryCursor,utcnow())).with_for_update(skip_locked=True))
                if row is None:
                    previous=s.get(OfHistoryCursor,(account,fan))
                    return {'complete':bool(previous and previous.status=='complete' and previous.start_before_id is None),
                            'outcomes':outcomes,'reason':'complete_or_leased'}
                recovery._binding(row,runtime)
                if row.start_before_id is not None:raise Refused('full_test_history_boundary_missing')
                if recovery._live_work(s,account,fan):
                    return {'complete':False,'outcomes':outcomes,'reason':'live_input_pending'}
                recovery._lease(row,utcnow())
                claim=ArchiveClaim(account,runtime.binding_id,fan,row.generation,row.lease_token,
                    row.before_id,connection_revision,page_limit=row.page_limit,scope='lifetime')
            page=reader.messages(fan_id=fan,before_id=claim.before_id,limit=claim.page_limit)
            result=recovery.page(tenant,claim,page.events,has_more=page.has_more,
                observations=page.observations,provider_read_claim=budget)
            outcomes.append({'result':result,'messages':len(page.events),'has_more':page.has_more})
            if result=='complete':return {'complete':True,'outcomes':outcomes}
            if result!='continue':return {'complete':False,'outcomes':outcomes,'reason':result}
        except Exception as exc:
            error=exc
            if claim:
                try:recovery.fail(tenant,claim,code='test_history_refresh_failed')
                except Exception:pass
            return {'complete':False,'outcomes':outcomes,'reason':type(exc).__name__}
        finally:
            _release_provider(store,tenant,budget,error)
    return {'complete':False,'outcomes':outcomes,'reason':'bounded_test_read_limit'}
