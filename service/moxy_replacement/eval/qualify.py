"""Bounded paid qualification on real inventory, with no fan events or sends.

Run explicitly inside the authorised worker environment. Uses the selected
account's credential and existing daily cap; an independent 50-cent cap and
10-call limit further bound this evaluation. Stops on unknown usage or failure.
Outputs are private review evidence, never automatically published.
"""
import argparse,asyncio,json,os
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from db.models import AccountAiConfig
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime.connections import _open
from onlyfans_runtime.provider_model import NativeProviderModel,MAX_OUTPUT_TOKENS
from onlyfans_runtime.store import utcnow
from ..runtime import snapshot,validate_decision
from .. import writer
from ..parts import plan

CASES=[
 ('affection','Sup baby, I love you',{'reply','invite','offer'}),
 ('direct_buy','Have you got an outfit photo I can buy?',{'offer'}),
 ('ordinary_chat','Just talk for now, no offers please.',{'reply','none'}),
 ('repeat_no_restart','Finished training. What photos have you got?',{'reply','invite','offer'}),
 ('delivery_complaint','I paid for the last photo but it will not open. Can you check?',{'reply','handover'}),
]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--account',required=True);parser.add_argument('--fan',required=True,type=int)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    if args.account!='of-312988353' or args.fan!=318022380:
        raise SystemExit('This qualification is authorised only for Amelia/Gervonta.')
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=(out/'lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    journal=out/'budget.json'
    entries=json.loads(journal.read_text()) if journal.exists() else []
    def save():journal.write_text(json.dumps(entries,indent=2))
    if any(e.get('actual') is None for e in entries):raise SystemExit('Unknown prior usage: stop.')
    engine=create_engine(os.environ['DATABASE_URL'])
    with Session(engine) as s:
        s.execute(text('SET TRANSACTION READ ONLY'))
        s.execute(text("SET LOCAL statement_timeout='8s'"))
        row=s.get(OfProviderConnection,args.account)
        if not row or row.status!='verified' or row.expires_at<=utcnow():raise SystemExit('Account credential is not current.')
        clear=_open(row,'twin-internal');credential=clear['model'];expires=row.expires_at.replace(tzinfo=timezone.utc)
        cfg=s.get(AccountAiConfig,args.account)
        config={c.name:getattr(cfg,c.name) for c in cfg.__table__.columns}
        from automations._common import resolve_model_from_config
        chosen=resolve_model_from_config(cfg,'ai_chatter',account_id=args.account,require_explicit=True)
        event=SimpleNamespace(kind='turn',payload_json='{}')
        base=snapshot(s,args.account,args.fan,event)
        from db.onlyfans_moxy_models import MoxyAccount
        settings=json.loads(s.get(MoxyAccount,args.account).settings_json)
        s.rollback()
    # The known false historical flags are excluded ONLY from synthetic scenarios.
    # No fan facts, controls, purchases or holds are changed in the real database.
    base['fan_state'].update(ordinary_chat_only=False,sales_refused=False,boundary_hold=False)
    base['invitation']=None
    results=[]
    for repetition in range(2):
        for name,body,allowed in CASES:
            snap=deepcopy(base)
            snap['history']=[{'id':1,'direction':'in','text':body,'at':utcnow().isoformat(),
                'sender':'fan','attachment_description':None,'media_count':0,'price_cents':0}]
            if name=='repeat_no_restart':
                snap['history'].insert(0,{'id':0,'direction':'out','text':'haha easy there, we just met. How did training go?',
                    'at':utcnow().isoformat(),'sender':'ai','attachment_description':None,'media_count':0,'price_cents':0})
            snap['event']={'kind':'turn','payload':{'qualification_only':True}}
            prompts=writer.messages(snap)
            model=NativeProviderModel(account_id=args.account,model_id=credential['modelId'],
                api_key=credential['apiKey'],expires_at=expires)
            model.configure_qualification(account_id=args.account,chosen_model=chosen,config=config,messages=prompts)
            reserve=model._cost(len(json.dumps(prompts).encode())+4096,MAX_OUTPUT_TOKENS)
            if len(entries)>=10 or sum(e['actual'] for e in entries)+reserve>5000:
                raise SystemExit('Qualification budget exhausted.')
            entry={'case':name,'repetition':repetition,'reserved':reserve,'actual':None}
            entries.append(entry);save()
            with Session(engine) as s,s.begin():
                call,units,day=model.reserve(s,account_id=args.account,fan_id=args.fan,model=chosen,messages=prompts,now=utcnow())
                entry['call_id']=call.id
            save()
            raw=None;error=None
            try:raw=asyncio.run(asyncio.wait_for(model.generate(prompts),timeout=45))
            except Exception as exc:error=type(exc).__name__
            with Session(engine) as s,s.begin():
                actual=model.settle(s,account_id=args.account,call_id=entry['call_id'],reserved_millicents=units,
                    actual_millicents=model.reported_cost_millicents if model.completed else None,now=utcnow())
            entry['actual']=actual;save()
            if actual is None:raise SystemExit('Qualification usage unknown; no retry, no fan event created.')
            parsed=None
            if raw:
                try:
                    parsed=writer.parse(raw)
                    validate_decision(settings,snap,parsed)
                except Exception as exc:error=str(exc)
            passed=not error and parsed['action'] in allowed
            if name=='affection' and parsed:
                passed=passed and not any(o['active'] for o in parsed.get('observations',[])) and 'boundary_reset' not in parsed['flags']
            if name=='ordinary_chat' and parsed:
                passed=passed and any(o['kind']=='ordinary_chat_only' and o['active'] for o in parsed.get('observations',[]))
            if name=='repeat_no_restart' and parsed:
                passed=passed and not any(p['text'].lower().startswith('haha easy there') for p in plan(parsed))
            record={'case':name,'repetition':repetition,'passed':passed,'error':error,'decision':parsed,
                'actual_millicents':actual,'real_inventory_products':len(base['products'])}
            results.append(record)
            (out/'results.json').write_text(json.dumps(results,indent=2))
            print(json.dumps({k:record[k] for k in ('case','repetition','passed','error','actual_millicents')}),flush=True)
    if not all(r['passed'] for r in results):raise SystemExit('Qualification failed: review private evidence.')


if __name__=='__main__':main()
