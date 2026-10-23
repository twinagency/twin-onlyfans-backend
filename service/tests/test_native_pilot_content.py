"""Run the real native catalogue path with synthetic data and blocked sockets."""
import asyncio, json, logging, os, socket, sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def exercise(tmp):
    from db.engine import engine, get_session
    from db.models import Base, Account, Fan, Employee, Message, CatalogItem, ContentOffer
    from sqlalchemy import select
    from native_pilot.engine import NativeEngine
    from native_pilot.content import ContentPlan
    from native_pilot.ledger import Refused
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    account, fan = '900001', 900002
    spec={'account_id':account,'fan_id':fan,'items':[
        {'catalog_id':1,'media_id':1001,'kind':'photo','price_cents':0},
        {'catalog_id':2,'media_id':1002,'kind':'video','price_cents':300}]}
    manifest=Path(tmp)/'manifest.json';manifest.write_text(json.dumps(spec))
    plan=ContentPlan(manifest,account,fan)
    async with get_session() as s:
        s.add(Account(id=account,nickname='Fixture'))
        s.add(Employee(id=1,display_name='Automation',is_active=True))
        s.add_all([Fan(account_id=account,fan_id=fan),Fan(account_id=account,fan_id=fan+1)])
        s.add(Message(account_id=account,fan_id=fan,message_id=100,direction='in',body='Can I see a free photo preview?',created_at=datetime.utcnow()-timedelta(seconds=30)))
        s.add(CatalogItem(id=1,account_id=account,label='Photo preview',kind='photo',description_for_ai='One photo. No other content details are verified.',media_ids='[1001]',price_cents=0,is_free_teaser=True,enabled=True))
        s.add(CatalogItem(id=2,account_id=account,label='Video',kind='video',description_for_ai='One video, 21 seconds. No other content details are verified.',media_ids='[1002]',duration_sec=21,price_cents=300,is_free_teaser=False,enabled=False))
    phase=1;sent=[];calls=[]
    async def chat(**kw):
        calls.append(kw)
        assert kw['account_id']==account and kw['fan_id']==fan
        if kw.get('response_format'):return SimpleNamespace(content='{}',parsed={})
        return SimpleNamespace(content=('Here is a photo preview.\n>>OFFER 1' if phase==1 else 'This video is $3 to unlock.\n>>OFFER 2'),parsed=None)
    def send(fid,text,kw):
        assert fid==fan
        term=plan.terms(kw)
        sent.append((fid,text,kw))
        result={'id':10000+len(sent),'text':text,'createdAt':datetime.utcnow().isoformat(),'fromUser':{'id':int(account)}}
        if term:result.update(media=[{'id':term['media_id'],'type':term['kind']}],mediaCount=1,price=term['price_cents']/100,lockedText=False)
        return result
    try:
        with NativeEngine(account,fan,chat=chat,send=send,content=plan) as native:
            a=await native.run(1)
            assert not a.get('errors'),a
            assert any(k.get('media_files')==[1001] and k.get('price')==0 for _,_,k in sent),('free-media-not-sent',a,sent)
            async with get_session() as s:
                s.add(Message(account_id=account,fan_id=fan,message_id=200,direction='in',body='Send the three dollar video please',created_at=datetime.utcnow()+timedelta(seconds=1)))
                f=await s.get(Fan,(account,fan));f.automation_paused_until=datetime.utcnow()-timedelta(seconds=1)
                item=await s.get(CatalogItem,2);item.enabled=True
            phase=2
            b=await native.run(2)
            assert not b.get('errors'),b
            assert any(k.get('media_files')==[1002] and k.get('price')==3 for _,_,k in sent),('paid-media-not-sent',b,sent)
            async with get_session() as s:
                offers=(await s.execute(select(ContentOffer))).scalars().all()
                assert len(offers)==2,[(x.mode,x.status) for x in offers]
                assert {(x.mode,x.status) for x in offers}=={('free','delivered'),('ppv','open')}
        for bad in [{'media_files':[1003],'price':3},{'media_files':[1002],'price':4},{'media_files':[1002],'price':True},{'media_files':[1002],'price':3,'previews':[1001]},{'media_files':[1002],'price':3,'locked_text':True}]:
            try:plan.terms(bad)
            except Refused:pass
            else:raise AssertionError('invalid terms accepted')
        valid={'id':500,'price':3,'media':[{'id':1002,'type':'video'}]}
        assert plan.evidence(valid,plan.paid)['price_cents']==300
        for change in [{'price':4},{'media':[{'id':1003,'type':'video'}]},{'media':[{'id':1002,'type':'photo'}]},{'lockedText':True},{'isUnsent':True}]:
            try:plan.evidence({**valid,**change},plan.paid)
            except Refused:pass
            else:raise AssertionError('contradictory receipt accepted')
        print(json.dumps({'native_free_and_paid':True,'provider_calls':len(sent),'model_calls':len(calls),'negative_terms_and_receipts':10}))
    finally:await engine.dispose()

def main():
    import dotenv
    dotenv.load_dotenv=lambda *a,**kw:False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='native-content-fixture-') as tmp:
        with patch.dict(os.environ,{'DATABASE_URL':'sqlite+aiosqlite:///'+(Path(tmp)/'fixture.db').as_posix(),'PYTHON_DOTENV_DISABLED':'1'},clear=True):
            async def run():
                def deny(*a,**kw):raise AssertionError('network forbidden')
                with patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny),patch.object(socket,'create_connection',deny):
                    await exercise(tmp)
            asyncio.run(run())
if __name__=='__main__':main()
