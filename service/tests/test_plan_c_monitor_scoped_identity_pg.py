"""Current account-bound recovery validates actual persisted identity; no legacy-constant patches."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from db.onlyfans_models import OfRuntime
from db.onlyfans_engine_models import OfFansConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime import monitor_incident_recovery as recovery
from onlyfans_runtime.store import Refused,utcnow
from test_plan_c_text_activation_pg import stopped_text,configured_draft,world,database
from test_plan_c_scope_pg import ACTOR,OTHER,key
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT


def proof_for(w,epoch,version):
    view=pilot_scope.describe(w.store,TENANT,ACCOUNT)
    with Session(w.database[1]) as s:
        binding=s.get(OfRuntime,ACCOUNT).binding_id
    now=utcnow();start=(now-timedelta(minutes=10)).isoformat()+'+00:00';end=(now-timedelta(minutes=1)).isoformat()+'+00:00'
    proof={'version':version,'incident_sha256':'cd'*32,'incident_id':'a'*32,'account_id':ACCOUNT,
      'binding_id':binding,'connection_epoch':epoch,'scope_revision':view['revision'],
      'classification':'sender_not_ready','affected_interval':{'start':start,'end':end},
      'delivery_url_sha256':'d'*64,'config_version':'e'*64,'actor':ACTOR,'evidence_sha256':'f'*64,
      'resolved_at':now.isoformat()+'+00:00'}
    if version==recovery.V3:
        proof.update(classification=recovery.READINESS,evidence_reference='operator:fansapi:reviewed current stopped scope',named_sends=[],cause_caveat=recovery.READINESS_CAVEAT)
        proof['audit_command_key']=recovery.command_key_v3(proof['incident_sha256'],proof['evidence_sha256'],start,end,view['revision'],proof['evidence_reference'],[])
    else:proof['audit_command_key']=recovery.command_key_v2(proof['incident_sha256'],proof['evidence_sha256'],start,end,view['revision'])
    return proof


def prepare(stopped_text,epoch):
    w,*_=stopped_text
    view=pilot_scope.describe(w.store,TENANT,ACCOUNT)
    pilot_scope.set_status(w.store,TENANT,ACCOUNT,'stopped',expected_revision=view['revision'],command_key=key(83001),actor=ACTOR,reason='retain scoped incident fixture')
    if epoch!=1:
        # An isolated persisted connection snapshot, without enabling a runtime or
        # grant; this tests epoch agreement in the audit, not reconnect execution.
        with Session(w.database[1]) as s,s.begin():
            old=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==ACCOUNT,OfFansConnection.status=='active'))
            old.status='retired';s.flush()
            s.add(OfFansConnection(account_id=ACCOUNT,epoch=epoch,binding_id=old.binding_id,
                provider_account_id=old.provider_account_id,provider_creator_id=old.provider_creator_id,status='active',created_at=utcnow()))
    return w


@pytest.mark.parametrize('version',[recovery.V2,recovery.V3])
@pytest.mark.parametrize('epoch',[1,2])
def test_nonlegacy_account_and_current_epoch_audit_without_patched_identity(stopped_text,version,epoch):
    w=prepare(stopped_text,epoch)
    assert ACCOUNT!=recovery.ACCOUNT
    proof=proof_for(w,epoch,version);before=pilot_scope.describe(w.store,TENANT,ACCOUNT)
    result=recovery.record_resolution(w.store,TENANT,ACCOUNT,proof=proof,actor=ACTOR,command_key=proof['audit_command_key'])
    assert result['recorded'] and not result['sendingEnabled'] and not result['holdsReleased']
    assert recovery.record_resolution(w.store,TENANT,ACCOUNT,proof=proof,actor=ACTOR,command_key=proof['audit_command_key'])==result
    assert pilot_scope.describe(w.store,TENANT,ACCOUNT)==before
    with Session(w.database[1]) as s:
        assert not s.get(OfRuntime,ACCOUNT).enabled and not s.get(OfDeliveryPolicy,ACCOUNT).enabled


@pytest.mark.parametrize('field,value',[('account_id','other-account'),('binding_id','other-binding'),('connection_epoch',1),('scope_revision',999),('actor',OTHER)])
def test_scoped_proof_refuses_cross_account_old_epoch_and_stale_revision(stopped_text,field,value):
    w=prepare(stopped_text,2);proof=proof_for(w,2,recovery.V3);proof[field]=value
    with pytest.raises(Refused):recovery.record_resolution(w.store,TENANT,ACCOUNT,proof=proof,actor=ACTOR,command_key=proof['audit_command_key'])
