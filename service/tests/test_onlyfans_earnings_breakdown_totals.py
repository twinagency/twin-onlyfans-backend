import asyncio
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from onlyfans_read.earnings import read_earnings

def test_breakdowns_do_not_replace_cleared_totals():
    now=datetime.now(timezone.utc)
    plain=now.replace(tzinfo=None)
    scan=SimpleNamespace(account_id="account",binding_id="binding",owner_id=1,connection_revision=1,
        last_complete_at=plain,last_full_at=plain,status="idle")
    class Session:
        def __init__(self):self.scalar_calls=0;self.list_calls=0;self.rows_calls=0
        async def connection(self,**kw):return None
        async def scalar(self,q):
            self.scalar_calls+=1
            return now if self.scalar_calls==1 else None
        async def scalars(self,q):
            self.list_calls+=1
            return [SimpleNamespace(account_id="account",id="binding")] if self.list_calls==1 else []
        async def execute(self,q):
            if str(q).startswith("SET "):return None
            values=[
                [(scan,"binding","binding",1,1)],
                [("USD","cleared",2,3000,2400,2),("USD","pending",1,600,480,1)],
                [("account","tip",1,1000,800,1),("account","ppv_message",2,2600,2080,2)],
            ][self.rows_calls]
            self.rows_calls+=1
            return SimpleNamespace(all=lambda:values)
    result=asyncio.run(read_earnings(Session(),tenant_id="tenant",start=(now-timedelta(days=30)).isoformat().replace("+00:00","Z"),end=now.isoformat().replace("+00:00","Z"),include_pending=True,include_history=True))
    assert result["status"]=="current"
    assert result["recorded"]["netCents"]==2880
    assert result["clearedGrossCents"]==3000,result
    assert result["clearedNetCents"]==2400,result
    assert result["statistics"]["byAccount"][0]["netCents"]==2880
if __name__=="__main__":
    test_breakdowns_do_not_replace_cleared_totals()
    print("PASS multiple revenue kinds preserve cleared gross/net totals")
