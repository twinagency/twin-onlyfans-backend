"""Scoped, auditable reporting and provider-evidenced reconciliation for profile calls.

Run with the worker's saved credentials. No allowance override or inferred refund.
Legacy calls without generation IDs remain reserved and explicitly unreconciled.
"""
import json
from datetime import datetime,timedelta
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import GrokCall,GrokDailyCost,OfCreatorBinding
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime.connections import _open
from native_pilot.budget import DailyBudget
from native_pilot.scope import AccountScope


def metadata(call):
    try:
        value=json.loads(call.response_text or '{}')
        return value if type(value) is dict and value.get('schema')=='profile-call-audit.v1' else {}
    except (ValueError,TypeError):return {}


def report(store,tenant,day):
    start=datetime.strptime(day,'%Y-%m-%d');end=start+timedelta(days=1)
    rows=[]
    with Session(store.engine) as s:
        accounts=s.scalars(select(OfCreatorBinding.account_id).where(OfCreatorBinding.tenant_id==tenant,
            OfCreatorBinding.revoked_at.is_(None),OfCreatorBinding.channel=='onlyfans')).all()
        for account in sorted(set(accounts)):
            calls=s.scalars(select(GrokCall).where(GrokCall.account_id==account,GrokCall.purpose=='gen_info',
                GrokCall.called_at>=start,GrokCall.called_at<end)).all()
            buckets={};missing=0;failures=0
            for c in calls:
                m=metadata(c)
                bucket=('unreconciled_reservation' if c.status=='unknown' else
                    'active_reservation' if c.status=='pending' else
                    'provider_reported' if m.get('billing') in {'provider_reported_cost','provider_reconciled'} else
                    'confirmed_no_dispatch' if m.get('billing')=='not_dispatched_zero_cost' else 'settled_token_estimate')
                b=buckets.setdefault(bucket,{'calls':0,'ledger_units':0});b['calls']+=1;b['ledger_units']+=c.cost_cents or 0
                missing+=c.status=='unknown' and not m.get('generation_id')
                failures+=bool(c.error_text)
            for b in buckets.values():b['usd']=str(Decimal(b['ledger_units'])/10000)
            rows.append({'account':account,'day':day,'buckets':buckets,'unknown_without_generation_id':missing,
                'calls_with_failure_details':failures})
    return rows


def reconcile_known(store,tenant,*,limit=25):
    """At most one metadata request per identified unresolved call per invocation.

    A returned generation id must exactly match the persisted response id. The
    original UTC day's ledger and reservation are locked and adjusted once.
    """
    import httpx
    results=[]
    with Session(store.engine) as s:
        accounts=set(s.scalars(select(OfCreatorBinding.account_id).where(OfCreatorBinding.tenant_id==tenant,
            OfCreatorBinding.revoked_at.is_(None),OfCreatorBinding.channel=='onlyfans')).all())
        calls=s.scalars(select(GrokCall).where(GrokCall.account_id.in_(accounts),GrokCall.purpose=='gen_info',
            GrokCall.provider=='openrouter',GrokCall.status=='unknown').order_by(GrokCall.id).limit(5000)).all()
        identified=[(c.id,c.account_id,c.model,metadata(c)) for c in calls if metadata(c).get('generation_id') and metadata(c).get('reconciliation_attempts',0)<5][:limit]
    for call_id,account,model,audit in identified:
        if audit.get('reconciliation_attempts',0)>=5:continue
        audit['reconciliation_attempts']=audit.get('reconciliation_attempts',0)+1
        audit['reconciliation_attempted_at']=datetime.utcnow().isoformat()
        # Persist the bounded attempt before network I/O; a crash cannot reset retries.
        with Session(store.engine) as s,s.begin():
            call=s.scalar(select(GrokCall).where(GrokCall.id==call_id).with_for_update())
            if call.status!='unknown':continue
            call.response_text=json.dumps(audit,ensure_ascii=True)
        with Session(store.engine) as s:
            row=s.get(OfProviderConnection,account)
            if row is None:continue
            credential=_open(row,tenant).get('model')
            if type(credential) is not dict or credential.get('modelId')!=model:continue
            key=credential.get('apiKey')
            if not key:continue
        try:
            with httpx.Client(timeout=10,follow_redirects=False,trust_env=False) as client:
                response=client.get('https://openrouter.ai/api/v1/generation',
                    params={'id':audit['generation_id']},headers={'Authorization':'Bearer '+key})
            if response.status_code!=200:
                results.append({'call_id':call_id,'result':'provider_metadata_unavailable','status':response.status_code});continue
            data=response.json().get('data',{})
            if data.get('id')!=audit['generation_id'] or data.get('total_cost') is None:continue
            budget=DailyBudget(AccountScope.parse(account),model,endpoint='https://openrouter.ai/api/v1/chat/completions')
            units=budget.actual_units(cost_usd=data['total_cost'],tokens_in=None,tokens_out=None)
            if units is None:continue
            with Session(store.engine) as s,s.begin():
                call=s.scalar(select(GrokCall).where(GrokCall.id==call_id,GrokCall.account_id==account).with_for_update())
                if call.status!='unknown' or metadata(call).get('generation_id')!=audit['generation_id']:continue
                day=call.called_at.strftime('%Y-%m-%d')
                ledger=s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id==account,
                    GrokDailyCost.provider=='openrouter',GrokDailyCost.day==day).with_for_update())
                if ledger is None or ledger.cost_cents<(call.cost_cents or 0):continue
                before=call.cost_cents
                ledger.cost_cents+=units-before;ledger.updated_at=datetime.utcnow()
                call.status='done';call.cost_cents=units
                audit.update(billing='provider_reconciled',reconciliation={'generation_id':data['id'],
                    'provider_total_cost_usd':str(data['total_cost']),'previous_reservation_units':before,
                    'at':datetime.utcnow().isoformat()})
                call.response_text=json.dumps(audit,ensure_ascii=True)
                results.append({'call_id':call_id,'result':'settled','before_units':before,'actual_units':units})
        except (httpx.HTTPError,ValueError):
            results.append({'call_id':call_id,'result':'provider_lookup_failed'})
    return results


if __name__=='__main__':
    import argparse,os
    from onlyfans_runtime.store import Store
    parser=argparse.ArgumentParser();parser.add_argument('--day',default=datetime.utcnow().strftime('%Y-%m-%d'))
    parser.add_argument('--reconcile',action='store_true');args=parser.parse_args()
    store=Store(os.environ['DATABASE_URL']);tenant=os.environ['ONLYFANS_WORKER_TENANT']
    try:
        reconciled=reconcile_known(store,tenant) if args.reconcile else []
        print(json.dumps({'reconciled':reconciled,'accounts':report(store,tenant,args.day)},ensure_ascii=True))
    finally:store.close()
