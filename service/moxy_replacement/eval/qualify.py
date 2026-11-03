"""Bounded paid qualification on real inventory, with no fan events or sends.

Run explicitly inside the authorised worker environment. Uses the selected
account's credential and existing daily cap; an independent 50-cent cap and
20-call total limit further bound this evaluation. Stops on unknown usage or failure.
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
 ('accepted_invitation','Yes please, send that photo.',{'offer'}),
 ('purchase_continuation','That was great. Have you got another different outfit photo?',{'offer','invite'}),
 ('innuendo_follow','I meant a different kind of rough \U0001fae2',{'reply','invite','offer'}),
 ('unavailable_pivot','Going with what?\nI came here to see your pussy not talk about all this random stuff I tried to hint at something naughty and you\u2019re just being boring',{'invite','offer'}),
]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--account',required=True);parser.add_argument('--fan',required=True,type=int)
    parser.add_argument('--out',required=True);parser.add_argument('--only',choices=[c[0] for c in CASES])
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
        # Snapshot takes shared consistency locks; rollback all preparation.
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
    for repetition in range(1):
        for name,body,allowed in CASES:
            if args.only and name!=args.only:continue
            snap=deepcopy(base)
            snap['history']=[{'id':1,'direction':'in','text':body,'at':utcnow().isoformat(),
                'sender':'fan','attachment_description':None,'media_count':0,'price_cents':0}]
            if name=='innuendo_follow':
                snap['history'][:0]=[
                    {'id':-2,'direction':'in','text':'I like it rough','at':utcnow().isoformat(),'sender':'fan','attachment_description':None,'media_count':0,'price_cents':0},
                    {'id':-1,'direction':'out','text':"rough is cute until you've gotta set up the tent in the dark \U0001f602 but noted, I like a challenge",
                     'at':utcnow().isoformat(),'sender':'ai','attachment_description':None,'media_count':0,'price_cents':0}]
            if name=='repeat_no_restart':
                snap['history'].insert(0,{'id':0,'direction':'out','text':'haha easy there, we just met. How did training go?',
                    'at':utcnow().isoformat(),'sender':'ai','attachment_description':None,'media_count':0,'price_cents':0})
            if name in {'accepted_invitation','purchase_continuation'}:
                from ..runtime import configuration
                pa=configuration(settings)[1]
                eligible=[]
                for key,p in snap['products'].items():
                    if p['category']!='covered_booty':continue
                    try:pa.terms(key,snap['fan_state'])
                    except Exception:continue
                    eligible.append(key)
                if len(eligible)<2:raise SystemExit('Insufficient distinct eligible photos for continuation qualification.')
                key=eligible[0]
                if name=='accepted_invitation':
                    snap['invitation']={'product_id':key,'fingerprint':pa.fingerprint,'sequence':None}
                    snap['history'].insert(0,{'id':0,'direction':'out','text':'Want this covered outfit photograph?',
                        'at':utcnow().isoformat(),'sender':'ai','attachment_description':None,'media_count':0,'price_cents':0})
                else:
                    # This is a labelled synthetic purchase result in memory, never a financial write.
                    snap['fan_state']['purchased_products']=sorted(set(snap['fan_state']['purchased_products'])|{key})
                    snap['fan_state']['owned_assets']=sorted(set(snap['fan_state']['owned_assets'])|set(snap['inventory'][key]['media_ids']))
                    snap['history'].insert(0,{'id':0,'direction':'out','text':'The selected covered outfit photograph.',
                        'at':utcnow().isoformat(),'sender':'ai','attachment_description':None,'media_count':1,
                        'price_cents':snap['products'][key]['price_cents']})
            snap['event']={'kind':'turn','payload':{'qualification_only':True}}
            prompts=writer.messages(snap)
            model=NativeProviderModel(account_id=args.account,model_id=credential['modelId'],
                api_key=credential['apiKey'],expires_at=expires)
            model.configure_qualification(account_id=args.account,chosen_model=chosen,config=config,messages=prompts)
            reserve=model._cost(len(json.dumps(prompts).encode())+4096,MAX_OUTPUT_TOKENS)
            if len(entries)>=20 or sum(e['actual'] for e in entries)+reserve>5000:
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
            if name=='innuendo_follow' and parsed:
                passed=passed and not any('tent' in p['text'].lower() for p in plan(parsed))
            if name=='unavailable_pivot' and parsed:
                passed=passed and parsed.get('product_id') is not None
            if name=='repeat_no_restart' and parsed:
                passed=passed and not any(p['text'].lower().startswith('haha easy there') for p in plan(parsed))
            record={'case':name,'repetition':repetition,'passed':passed,'error':error,'decision':parsed,'raw_output':raw,
                'actual_millicents':actual,'real_inventory_products':len(base['products'])}
            results.append(record)
            (out/'results.json').write_text(json.dumps(results,indent=2))
            print(json.dumps({k:record[k] for k in ('case','repetition','passed','error','actual_millicents')}),flush=True)
    if not all(r['passed'] for r in results):raise SystemExit('Qualification failed: review private evidence.')


if __name__=='__main__':main()
