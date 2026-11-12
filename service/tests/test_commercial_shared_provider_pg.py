"""One shared ceiling above ten independent creator counters, at concurrent reservation."""
import json
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from conversation_engine.preparation import PreparationRequestGuard, view
from db.onlyfans_engine_models import OfEnginePilotScope
from onlyfans_runtime.store import Refused
from test_commercial_preparation_pg import setup,TENANT
from test_onlyfans_foundation_pg import database

def test_ten_concurrent_accounts_share_ceiling_without_duplicate_budget_ledger(database,tmp_path,monkeypatch):
    fleet=[setup(database,i,requests=100,credits=200) for i in range(1,11)]
    path=tmp_path/'team.json'
    config={'version':'provider-team.v1','team':'offline-team','accounts':[a for _,a,_ in fleet],
            'creditLimit':11,'externalReserved':1}
    path.write_text(json.dumps(config));monkeypatch.setenv('TWIN_PROVIDER_TEAM_LIMITS_FILE',str(path))
    def reserve(index):
        store,account,_=fleet[index%10]
        try:
            PreparationRequestGuard(store,TENANT,account,1)('GET','https://api.fansapi.com/api/accounts',True)
            return True
        except Refused as exc:
            assert str(exc)=='shared provider credit budget exhausted'
            return False
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes=list(pool.map(reserve,range(30)))
        assert sum(outcomes)==5
        with Session(database[1]) as session:
            assert session.scalar(select(func.sum(OfEnginePilotScope.provider_credits_reserved)))==10
            assert session.scalar(select(func.sum(OfEnginePilotScope.provider_requests_used)))==5
        # Restart cannot refund consumed team or account reservations.
        assert reserve(0) is False
        assert all(view(s,TENANT,a)['providerCreditsReserved']<=200 for s,a,_ in fleet)
        # A separate provider team remains usable despite this team's exhaustion.
        other,account,_=setup(database,11,requests=100,credits=200);fleet.append((other,account,{}))
        path2=tmp_path/'other.json'
        path2.write_text(json.dumps({**config,'team':'other-team','accounts':[account],'creditLimit':3}))
        monkeypatch.setenv('TWIN_PROVIDER_TEAM_LIMITS_FILE',str(path2))
        PreparationRequestGuard(other,TENANT,account,1)('GET','https://api.fansapi.com/api/accounts',True)
        assert view(other,TENANT,account)['providerCreditsReserved']==2
    finally:
        for store,_,_ in fleet:store.close()
