import importlib.util
from pathlib import Path
import pytest
s=importlib.util.spec_from_file_location('bound',Path(__file__).with_name('bound_legacy_reservations.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

@pytest.mark.parametrize('old,target', [([3,6,9],3),([3,6,9],8),([3,6,9],18),([1,1],2),([10000,999,3],5000)])
def test_allocation_keeps_aggregate_bound_and_no_claim_of_zero_charge(old,target):
 r=m.allocate(old,target)
 assert sum(r)==target and all(1<=a<=b for a,b in zip(r,old))

@pytest.mark.parametrize('usage,byok',[(float('nan'),0),(-1,0),(1,-1),(float('inf'),0)])
def test_invalid_provider_cost_refuses(usage,byok):
 with pytest.raises(ValueError):m.ceiling_units(usage,byok)

def test_byok_and_margin_are_included_conservatively():
 assert m.ceiling_units('2','1')==37000

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified,TENANT
from sqlalchemy.orm import Session
from sqlalchemy import select
from db.models import AccountAiConfig,GrokCall,GrokDailyCost
from db.onlyfans_connection_models import OfProviderConnection,OfConnectionChange
from datetime import datetime,timezone


def seed(database,monkeypatch):
 import onlyfans_runtime.connections as connections
 monkeypatch.setattr(connections,'utcnow',lambda:datetime(2026,11,1,1))
 store,account,_=verified(database,monkeypatch,model={'modelId':'openrouter/deepseek-v4.1-flash','apiKey':'synthetic-model-secret'})
 with store.engine.begin() as c:c.exec_driver_sql(Path('/app/deploy/profile-learning/schema.sql').read_text())
 with Session(store.engine) as s,s.begin():
  cfg=s.get(AccountAiConfig,account);cfg.daily_cost_cap_cents=600
  conn=s.get(OfProviderConnection,account)
  proof={account:{'key_reference':'test-ref','binding_id':conn.binding_id,'connection_revision':conn.revision,
   'provider_date':'2026-11-01T18:00:00+00:00','observed_at':datetime.now(timezone.utc).isoformat(),
   'aggregate_budget_floor_units':130}}
  for units,status in [(30,'unknown'),(50,'unknown'),(100,'done')]:
   s.add(GrokCall(account_id=account,fan_id=1,purpose='gen_info',provider='openrouter',
     model='openrouter/deepseek-v4.1-flash',endpoint='https://openrouter.ai/api/v1/chat/completions',
     prompt_json='{}',status=status,cost_cents=units,called_at=datetime(2026,11,1,12,7)))
  s.add(GrokDailyCost(account_id=account,provider='openrouter',day='2026-11-01',cost_cents=180,call_count=3,is_capped=False))
 return store,account,proof,{account:(2,80,'test-ref')}


def test_dry_run_and_atomic_recovery_keep_costs_auditable_and_cap_unchanged(database,monkeypatch):
 store,account,proof,expected=seed(database,monkeypatch)
 r=m.apply_bounds(store,TENANT,proof,expected=expected)
 assert not r['applied'] and r['released_units']==50
 with Session(store.engine) as s:assert s.scalar(select(GrokDailyCost)).cost_cents==180
 r=m.apply_bounds(store,TENANT,proof,expected=expected,apply=True)
 assert r['released_units']==50
 with Session(store.engine) as s:
  calls=s.scalars(select(GrokCall).order_by(GrokCall.id)).all();ledger=s.scalar(select(GrokDailyCost))
  assert ledger.cost_cents==130==sum(c.cost_cents for c in calls)
  assert calls[-1].cost_cents==100 and calls[-1].status=='done'
  assert all(c.status=='unknown' and c.cost_cents>0 for c in calls[:2])
  assert all(m.json.loads(c.response_text)['legacy_reservation']['original_units'] in {30,50} for c in calls[:2])
  assert s.get(AccountAiConfig,account).daily_cost_cap_cents==600
  assert ledger.call_count==3
 again=m.apply_bounds(store,TENANT,proof,expected=expected,apply=True)
 assert again['released_units']==0 and again['accounts'][0]['already_applied']
 store.close()


@pytest.mark.parametrize('failure',['wrong_tenant','stale_proof','key_changed','snapshot_changed'])
def test_invalid_evidence_never_releases_capacity(database,monkeypatch,failure):
 store,account,proof,expected=seed(database,monkeypatch)
 tenant=TENANT
 if failure=='wrong_tenant':tenant='wrong-tenant'
 if failure=='stale_proof':proof[account]['observed_at']='2020-01-01T00:00:00+00:00'
 if failure=='key_changed':proof[account]['connection_revision']+=1
 if failure=='snapshot_changed':expected[account]=(3,80,'test-ref')
 with pytest.raises(ValueError):m.apply_bounds(store,tenant,proof,expected=expected,apply=True)
 with Session(store.engine) as s:
  assert s.scalar(select(GrokDailyCost)).cost_cents==180
  assert all(c.response_text is None for c in s.scalars(select(GrokCall)))
 store.close()
