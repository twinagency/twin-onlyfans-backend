"""Real FASTT fan turn and resolver/pricing/delivery, synthetic model/provider.

The authority's persistence/wire races are tested separately. This integration
proves that off/shadow keep the normal reply and live has only one winning card.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


async def exercise():
    from sqlalchemy import select
    from db.engine import engine,get_session
    from db.models import Base,Account,AccountAiConfig,Fan,Message,VaultItem,ContentOffer,VaultSend
    from db.onlyfans_contextual_models import OfContextualOffer  # register production origin table
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine
    from onlyfans_runtime.configuration import SALES_FLAGS
    from onlyfans_runtime.contextual_mode import KEY
    from automations import ai_chatter,content_prompts

    async with engine.begin() as c:await c.run_sync(Base.metadata.create_all)
    results=[]
    for n,(current,fixed,explicit) in enumerate((("off",None,False),("shadow",None,False),("live",None,False),("live",700,False),("live",None,True))):
        account,fan=str(900001+n),900010+n
        now=datetime.utcnow()
        async with get_session() as s:
            s.add(Account(id=account,nickname="Synthetic creator"))
            s.add(AccountAiConfig(account_id=account,ai_chatter_config_json=json.dumps({
                **{k:True for k in SALES_FLAGS},KEY:current,"language":"en",
                **({"fastt_pack_price_cents":fixed} if fixed else {})})))
            s.add(Fan(account_id=account,fan_id=fan))
            s.add(Message(account_id=account,fan_id=fan,message_id=5001,direction="in",
                          body="show me something tonight, what do u have for me" if explicit else "I love your gym workout photos",created_at=now-timedelta(seconds=60)))
            for mid in range(7001,7008):
                s.add(VaultItem(account_id=account,media_id=mid,kind="photo",description="gym workout mirror photo, creator alone",
                    search_text="gym workout mirror photo, creator alone",tags="[]",created_at=now-timedelta(days=1)))
            if current == "live":
                s.add(VaultSend(account_id=account,fan_id=fan,media_id=7001,message_id=4000,
                                price_cents=500,was_purchased=False))
            await s.commit()
        calls,sent,events=[],[],[]
        async def model(**kw):
            purpose=kw["purpose"];calls.append(purpose)
            if purpose=="ai_chatter":return SimpleNamespace(content="How did your workout go?",parsed=None)
            if purpose=="content_resolver_contract":
                data={"is_ask":True,"same_item_requested":False,"subject":None,"company":False,
                      "quote":"show me something tonight, what do u have for me","terms":[]}
            elif purpose=="content_resolver_hook":
                data={"hook":True,"source":"situation","action":"send_ppv","what":"fan is at the gym",
                    "subject":"me at the gym","media_kind":"photo","terms":["gym","workout","mirror"],
                    "bridge":"love a good workout","reason":"fan expresses current interest in gym photos",
                    "evidence":[{"id":5001,"quote":"I love your gym workout photos"}],"supporting_message_ids":[5001]}
            elif purpose=="content_resolver_match":data={"media_ids":list(range(7001,7008))}
            elif purpose=="content_resolver_verify":data={"verdicts":{str(i):True for i in range(7001,7008)}}
            elif purpose=="content_resolver_action":data={"phrase":"me at the gym"}
            elif purpose=="gather_close_answer":data={"line":"love a good workout"}
            else:raise AssertionError("unexpected model purpose "+purpose)
            return SimpleNamespace(content=json.dumps(data),parsed=data)
        class Authority:
            source_message_id=5001
            armed=None
            emit=staticmethod(events.append)
            def check(self,required):
                assert required==current
                return {"mode":current,"revision":1,"binding":"fixture"}
            @contextmanager
            def arm(self,d,p,r,ids):
                assert ids==(5001,) and d.plan.price_cents>0
                self.armed=d
                try:yield
                finally:self.armed=None
        def send(fid,text,kwargs):
            assert fid==fan
            sent.append((text,kwargs))
            return {"id":8000+len(sent),"text":text,"createdAt":datetime.utcnow().isoformat(),
                    "fromUser":{"id":int(account)},"price":kwargs.get("price",0),
                    "media":[{"id":i,"type":"photo"} for i in kwargs.get("media_files",[])],
                    "previews":kwargs.get("previews",[])}
        with NativeEngine(account,fan,chat=model,send=send,content=VaultPlan(account,fan),contextual=Authority()) as native:
            result=await native.run(n+1)
        assert sent,(current,result,events)
        if current in ("off","shadow"):
            assert all(not kwargs for _,kwargs in sent),(current,sent)
            assert sent[0][0]=="How did your workout go?",sent
        else:
            cards=[(text,kw) for text,kw in sent if kw.get("media_files")]
            assert len(cards)==1 and cards[0][1]["price"]>0,(result,sent,events,calls)
            assert 7001 not in cards[0][1]["media_files"],cards
            assert all("already in the card above" not in text for text,_ in sent)
            if not explicit:assert len(sent)==1 and all("How did your workout" not in text for text,_ in sent)
            if fixed:assert round(cards[0][1]["price"]*100)==fixed
        assert result["replies_sent"]==1,(current,result)
        assert ("content_resolver_hook" in calls)==(current!="off" and not explicit),(current,calls,events)
        async with get_session() as s:
            offers=(await s.execute(select(ContentOffer).where(ContentOffer.account_id==account))).scalars().all()
            assert len(offers)==(1 if current=="live" else 0),(current,offers)
        results.append({"mode":current,"fixed_price_cents":fixed,"sends":len(sent),"paid_cards":len(offers),"model_calls":calls})
    await engine.dispose()
    print(json.dumps({"contextual_native_turn":results}))


if __name__=="__main__":
    with tempfile.TemporaryDirectory(prefix="contextual-native-") as tmp:
        os.environ["TWIN_FASTT_CONFIG_ISOLATED"]="1"
        os.environ["DATABASE_URL"]="sqlite+aiosqlite:///"+str(Path(tmp)/"test.db")
        os.environ.pop("ONLYFANS_PROCESS_ROLE",None)
        def deny(*args,**kwargs):raise AssertionError("network forbidden in fixture")
        # Windows creates an internal socket pair while initializing its loop.
        # Initialize that before denying every provider/network connection.
        with asyncio.Runner() as runner:
            runner.get_loop()
            with patch.object(socket.socket,"connect",deny),patch.object(socket,"create_connection",deny):
                runner.run(exercise())

