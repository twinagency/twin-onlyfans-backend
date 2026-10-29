"""Bounded authorised-history quality check. No fabricated input or provider sends."""
import asyncio,json,os
from sqlalchemy import select,exists,or_
from sqlalchemy.orm import Session
from db.models import Message,OfCreatorBinding
from db.onlyfans_models import OfIngress,OfRuntime
from onlyfans_runtime.store import Store,Refused
from onlyfans_runtime.profile_learning.worker import fingerprint
from onlyfans_runtime.profile_learning.model import extract
from onlyfans_runtime.profile_learning.evidence import validate

async def main():
    store=Store(os.environ['DATABASE_URL']);tenant=os.environ['ONLYFANS_WORKER_TENANT']
    with Session(store.engine) as s:
        bindings=s.scalars(select(OfCreatorBinding).join(OfRuntime,OfRuntime.binding_id==OfCreatorBinding.id).where(
            OfCreatorBinding.tenant_id==tenant,OfCreatorBinding.channel=='onlyfans',OfCreatorBinding.revoked_at.is_(None))).all()
        pairs=[(b.account_id,b.id) for b in bindings]
    for account,binding in pairs:
        with Session(store.engine) as s,s.begin():
            store._runtime(s,tenant,account)
            proof=exists(select(OfIngress.event_key).where(OfIngress.account_id==Message.account_id,
                OfIngress.binding_id==binding,OfIngress.fan_id==Message.fan_id,OfIngress.message_id==Message.message_id,OfIngress.status=='applied'))
            sample=s.scalar(select(Message).where(Message.account_id==account,Message.direction=='in',
                Message.is_unsent.is_(False),proof,or_(*(Message.body.ilike('%'+x+'%') for x in
                    ['my name','I work','I live','my birthday','wedding','my job']))).order_by(Message.created_at.desc()).limit(1))
            if sample is None:
                print(json.dumps({'account':account,'state':'no-bounded-factual-sample'}),flush=True);continue
            fan=sample.fan_id
            rows=s.scalars(select(Message).where(Message.account_id==account,Message.fan_id==fan,
                Message.is_unsent.is_(False),proof,Message.created_at<=sample.created_at).order_by(
                Message.created_at.desc(),Message.message_id.desc()).limit(8)).all()
            sources={}
            for m in reversed(rows):
                if len(m.body)>4000:continue
                key=f'{fan}:{m.message_id}'
                sources[key]={'id':key,'fan':fan,'message':m.message_id,'text':m.body,'at':m.created_at.isoformat(),
                    'direction':m.direction,'automation':m.automation_kind,'digest':fingerprint(m),
                    'context_only':m.direction!='in'}
        try:
            result,call=await extract(store,tenant,account,binding,fan,sources)
            facts=validate(result,sources)
            print(json.dumps({'account':account,'fan':fan,'state':'analysed','modelCall':call,
                'sourceMessages':len(sources),'acceptedFacts':len(facts),'proposedFacts':len(result.get('facts',[])),
                'sample':[{'field':f['field'],'value':f['value'],'quote':f['quote'],'status':f.get('status','current'),
                    'source':f['source'],'at':f['at']} for f in facts[:5]]},ensure_ascii=False),flush=True)
        except Exception as exc:
            public=str(exc) if isinstance(exc,Refused) else 'analysis-deferred'
            print(json.dumps({'account':account,'state':public,'credentialOwner':'existing account model configuration'}),flush=True)
    store.close()
    from db.engine import engine
    await engine.dispose()

if __name__=='__main__':asyncio.run(main())
