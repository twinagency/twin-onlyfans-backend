"""The incident-specific audit preserves every application hold and send flag."""
import pytest
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from db.models import OfConversationState
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime import monitor_incident_recovery as recovery
from onlyfans_runtime.store import Refused,utcnow
from test_plan_c_text_activation_pg import stopped_text,configured_draft,world,database
from test_plan_c_scope_pg import ACTOR,OTHER,key
from test_onlyfans_text_delivery_pg import ACCOUNT,FAN,TENANT
from test_engine_real_worker_pg import PROVIDER,CREATOR

@pytest.fixture
def incident(stopped_text,monkeypatch):
 w,*_=stopped_text
 stopped=pilot_scope.set_status(w.store,TENANT,ACCOUNT,'stopped',expected_revision=1,command_key=key(33000),actor=ACTOR,reason='explicit incident fixture stop')
 with Session(w.database[1]) as s:binding=s.get(OfRuntime,ACCOUNT).binding_id
 monkeypatch.setattr(recovery,'PROVIDER',PROVIDER);monkeypatch.setattr(recovery,'CREATOR',CREATOR)
 monkeypatch.setattr(recovery,'ACCOUNT',ACCOUNT);monkeypatch.setattr(recovery,'BINDING',binding)
 monkeypatch.setattr(recovery,'FANS',frozenset(pilot_scope.describe(w.store,TENANT,ACCOUNT)['config']['fans']))
 proof={'version':'plan-c-monitor-resolution.v1','incident_sha256':recovery.INCIDENT,'account_id':ACCOUNT,
  'binding_id':binding,'connection_epoch':1,'scope_revision':stopped['revision'],'classification':'credit_monitor_unavailable',
  'actor':ACTOR,'audit_command_key':key(33001),'evidence_sha256':'e'*64,'resolved_at':utcnow().isoformat()+'+00:00'}
 return w,proof

def record(w,proof,actor=ACTOR):
 return recovery.record_resolution(w.store,TENANT,ACCOUNT,proof=proof,actor=actor,command_key=proof['audit_command_key'])

def test_incident_audit_and_replay_preserve_unrelated_hold(incident):
 w,proof=incident
 with Session(w.database[1]) as s,s.begin():
  s.get(OfConversationState,(ACCOUNT,FAN)).mode='no_contact'
 before=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 first=record(w,proof);assert record(w,proof)==first
 assert first['sendingEnabled'] is False and first['holdsReleased'] is False
 assert pilot_scope.describe(w.store,TENANT,ACCOUNT)==before
 with Session(w.database[1]) as s:
  assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
  assert not s.get(OfRuntime,ACCOUNT).enabled and not s.get(OfDeliveryPolicy,ACCOUNT).enabled

@pytest.mark.parametrize('field,value',[('incident_sha256','a'*64),('binding_id','wrong'),('connection_epoch',2),
 ('scope_revision',99),('actor',OTHER),('resolved_at','2000-01-01T00:00:00+00:00')])
def test_stale_wrong_resolution_is_refused(incident,field,value):
 w,proof=incident;proof={**proof,field:value}
 with pytest.raises(Refused):record(w,proof)
 with Session(w.database[1]) as s:assert not s.get(OfRuntime,ACCOUNT).enabled
