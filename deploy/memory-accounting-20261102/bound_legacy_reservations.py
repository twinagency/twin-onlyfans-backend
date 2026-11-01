"""One-time conservative recovery of legacy reservation capacity.

This adjusts an ACCOUNT-LEVEL uncertainty reserve; it does not assign a bill to
individual requests. Original estimates, unresolved statuses and evidence survive.
The whole key's UTC-day spend, including unrelated workloads and BYOK, plus 20%
and $0.10 is retained as a conservative account-wide budget floor. Existing
non-legacy ledger entries are preserved. No request is marked free or successful.
"""
import json,hashlib,math
from datetime import datetime,timedelta,timezone
from decimal import Decimal,ROUND_CEILING
from email.utils import parsedate_to_datetime

DAY='2026-11-01'
BATCH='legacy-key-day-bound-20261101-v1'
EXPECTED={
 'of-24181859':(280,46630,'77c9c41e3dd0'),
 'of-312988353':(405,47645,'77c9c41e3dd0'),
 'of-323358988':(235,52363,'77c9c41e3dd0'),
 'of-63665785':(414,48111,'77c9c41e3dd0'),
 'of-87030857':(245,47256,'77c9c41e3dd0'),
 'of-99421145':(295,55136,'f11748653bbd')}


def ceiling_units(usage,byok):
    values=[Decimal(str(v)) for v in (usage,byok)]
    if any(not v.is_finite() or v<0 for v in values):raise ValueError('invalid provider spend')
    return int(((sum(values)*Decimal('1.20')+Decimal('0.10'))*10000).to_integral_value(rounding=ROUND_CEILING))


def allocate(old,target):
    if not old or any(type(v) is not int or v<1 for v in old):raise ValueError('invalid old reservations')
    if not len(old)<=target<=sum(old):raise ValueError('invalid aggregate target')
    weights=[v-1 for v in old];total=sum(weights);remaining=target-len(old)
    if not total:return old[:]
    out=[1+remaining*w//total for w in weights]
    order=sorted(range(len(old)),key=lambda i: (-(remaining*weights[i]%total),i))
    for i in order[:target-sum(out)]:out[i]+=1
    assert sum(out)==target and all(1<=a<=b for a,b in zip(out,old))
    return out


def gather_proofs(store,tenant):
    import httpx
    from sqlalchemy.orm import Session
    from db.onlyfans_connection_models import OfProviderConnection
    from onlyfans_runtime.connections import _open
    proofs={};keys={}
    with Session(store.engine) as s:
        for account,(_,_,expected_ref) in EXPECTED.items():
            row=s.get(OfProviderConnection,account)
            if row is None:raise ValueError('connection missing')
            cred=_open(row,tenant).get('model')
            if not cred or cred.get('modelId')!='openrouter/deepseek-v4.1-flash':raise ValueError('saved model changed')
            key=cred['apiKey'];ref=hashlib.sha256(key.encode()).hexdigest()[:12]
            if ref!=expected_ref:raise ValueError('historical key identity changed')
            keys[ref]=key
            proofs[account]={'key_reference':ref,'connection_revision':row.revision,'binding_id':row.binding_id}
    for ref,key in keys.items():
        with httpx.Client(timeout=15,trust_env=False,follow_redirects=False) as client:
            r=client.get('https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+key})
        if r.status_code!=200:raise ValueError('provider evidence unavailable')
        provider_day=parsedate_to_datetime(r.headers['date']).astimezone(timezone.utc)
        if provider_day.strftime('%Y-%m-%d')!=DAY or abs((datetime.now(timezone.utc)-provider_day).total_seconds())>120:
            raise ValueError('provider evidence UTC day/freshness mismatch')
        d=r.json()['data'];units=ceiling_units(d['usage_daily'],d['byok_usage_daily'])
        proof={'provider':'openrouter','endpoint':'/api/v1/key','key_reference':ref,
            'provider_date':provider_day.isoformat(),'observed_at':datetime.now(timezone.utc).isoformat(),
            'usage_daily_usd':str(d['usage_daily']),'byok_usage_daily_usd':str(d['byok_usage_daily']),
            'aggregate_budget_floor_units':units,'margin':'20 percent plus USD 0.10',
            'scope':'whole API key, all workloads; repeated conservatively for each creator sharing key',
            'per_call_charge':'unknown'}
        for account in proofs:
            if proofs[account]['key_reference']==ref:proofs[account].update(proof)
    return proofs


def apply_bounds(store,tenant,proofs,*,apply=False,expected=EXPECTED,day=DAY,batch=BATCH):
    from sqlalchemy import select,text,func
    from sqlalchemy.orm import Session
    from db.models import AccountAiConfig,GrokCall,GrokDailyCost,OfCreatorBinding
    from db.onlyfans_connection_models import OfProviderConnection,OfConnectionChange
    start=datetime.strptime(day,'%Y-%m-%d');end=start+timedelta(days=1)
    results=[]
    with Session(store.engine) as s,s.begin():
        for account,(expected_count,expected_units,key_ref) in sorted(expected.items()):
            proof=proofs[account]
            if proof['key_reference']!=key_ref or not proof['provider_date'].startswith(day):raise ValueError('wrong proof scope/day')
            if abs((datetime.now(timezone.utc)-datetime.fromisoformat(proof['observed_at'])).total_seconds())>300:raise ValueError('stale proof')
            binding=s.scalar(select(OfCreatorBinding).where(OfCreatorBinding.account_id==account,
                OfCreatorBinding.tenant_id==tenant,OfCreatorBinding.revoked_at.is_(None)))
            if binding is None or binding.id!=proof['binding_id']:raise ValueError('tenant/binding changed')
            connection=s.get(OfProviderConnection,account)
            if connection is None or connection.revision!=proof['connection_revision']:raise ValueError('connection revision changed')
            if s.scalar(select(func.count()).select_from(OfConnectionChange).where(OfConnectionChange.account_id==account,
                    OfConnectionChange.created_at>=datetime(2026,11,1,12,3))):raise ValueError('credential history changed')
            cfg=s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id==account).with_for_update())
            if cfg is None or cfg.daily_cost_cap_cents!=600:raise ValueError('saved daily cap changed')
            ledger=s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id==account,
                GrokDailyCost.provider=='openrouter',GrokDailyCost.day==day).with_for_update())
            if ledger is None:raise ValueError('ledger missing')
            calls=s.scalars(select(GrokCall).where(GrokCall.account_id==account,GrokCall.provider=='openrouter',
                GrokCall.purpose=='gen_info',GrokCall.status=='unknown',GrokCall.called_at>=start,
                GrokCall.called_at<end,GrokCall.response_text.is_(None)).order_by(GrokCall.id).with_for_update()).all()
            if not calls:
                previous=s.scalars(select(GrokCall.response_text).where(GrokCall.account_id==account,
                    GrokCall.called_at>=start,GrokCall.called_at<end,GrokCall.response_text.like('%'+batch+'%'))).all()
                if len(previous)!=expected_count:raise ValueError('legacy snapshot missing or changed')
                results.append({'account':account,'already_applied':True});continue
            before=sum(c.cost_cents for c in calls)
            if len(calls)!=expected_count or before!=expected_units:raise ValueError('legacy reservation snapshot changed')
            if any(c.model!='openrouter/deepseek-v4.1-flash' or c.tokens_in is not None or c.tokens_out is not None
                   or c.response_json is not None or c.called_at>=datetime(2026,11,1,15) for c in calls):raise ValueError('not an untracked legacy call')
            other=ledger.cost_cents-before
            if other<0:raise ValueError('ledger inconsistency')
            target=max(len(calls),min(before,max(0,proof['aggregate_budget_floor_units']-other)))
            values=allocate([c.cost_cents for c in calls],target)
            after=other+target
            if after>=cfg.daily_cost_cap_cents*100:raise ValueError('provider bound leaves account exhausted')
            result={'account':account,'legacy_calls':len(calls),'old_hold_units':before,'new_hold_units':target,
                'released_units':before-target,'other_ledger_units_preserved':other,'new_ledger_units':after,
                'saved_daily_cap_cents':cfg.daily_cost_cap_cents,'proof':proof}
            results.append(result)
            if not apply:continue
            for call,units in zip(calls,values):
                audit={'schema':'profile-call-audit.v1','billing':'provider_key_day_bound','generation_id':None,
                    'batch_id':batch,'legacy_reservation':{'original_units':call.cost_cents,'current_units':units,
                    'meaning':'distributed account-level uncertainty reserve, not an individual billed cost'},
                    'provider_evidence':proof,'resolution':'capacity recovered under conservative provider-evidenced aggregate bound; individual charge remains unknown'}
                call.response_text=json.dumps(audit,ensure_ascii=True);call.cost_cents=units
            ledger.cost_cents=after;ledger.updated_at=datetime.utcnow()
            s.execute(text('''UPDATE of_profile_accounts SET hold_until=NULL,hold_code=NULL
                WHERE tenant_id=:t AND account_id=:a AND binding_id=:b AND hold_code='profile-budget-exhausted' '''),
                {'t':tenant,'a':account,'b':binding.id})
            s.execute(text('''UPDATE of_profile_learning SET next_at=CURRENT_TIMESTAMP,error_code=NULL,attempts=0
                WHERE tenant_id=:t AND account_id=:a AND binding_id=:b AND error_code='profile-budget-exhausted'
                AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)'''),{'t':tenant,'a':account,'b':binding.id})
    return {'applied':apply,'batch_id':batch,'day':day,'accounts':results,
            'released_units':sum(r.get('released_units',0) for r in results)}


if __name__=='__main__':
    import os,argparse
    from onlyfans_runtime.store import Store
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    store=Store(os.environ['DATABASE_URL']);tenant=os.environ['ONLYFANS_WORKER_TENANT']
    try:print(json.dumps(apply_bounds(store,tenant,gather_proofs(store,tenant),apply=args.apply),ensure_ascii=True))
    finally:store.close()
