from datetime import timezone
import hashlib,json
from sqlalchemy.orm import Session
from test_onlyfans_text_delivery_pg import configured,prepare
from test_onlyfans_foundation_pg import database
from db.models import Message
from db.onlyfans_models import OfIngress,OfRuntime
from onlyfans_runtime.store import Event,utcnow
from onlyfans_runtime.history_repair import inspect,repair
from onlyfans_runtime.text_delivery import SyntheticTextTransport

def test_verified_repair_unblocks_exactly_one_manual_dispatch_and_is_idempotent(database):
    _,engine=database;store,authority=configured(database)
    event=Event(101,501,utcnow().replace(tzinfo=timezone.utc).isoformat(),'out','Hello & welcome')
    payload=event.payload();key=hashlib.sha256(payload.encode()).hexdigest()
    with Session(engine) as s,s.begin():
        runtime=s.get(OfRuntime,'acc-a')
        s.add(Message(account_id='acc-a',fan_id=101,message_id=501,direction='out',body='<p>Hello &amp; welcome</p>',price_cents=0,media_ids='[]',created_at=utcnow()))
        s.add(OfIngress(account_id='acc-a',binding_id=runtime.binding_id,event_key=key,fan_id=101,message_id=501,
            payload=payload,source='import',status='quarantined',reason='conflicting_message_identity',received_at=utcnow()))
    snapshot=inspect(store,'tenant-a','acc-a',101)
    args=dict(command_key='a'*64,actor='op-session:'+('b'*64),reason='Verify representation-only history',expected_fingerprint=snapshot['fingerprint'])
    first=repair(store,'tenant-a','acc-a',101,**args)
    assert first['repaired']==1 and first['retained']==0
    assert repair(store,'tenant-a','acc-a',101,**args)==first
    with Session(engine) as s:
        row=s.get(OfIngress,('acc-a',key))
        assert row.payload==payload and row.event_key==key and row.status=='applied'
        assert s.get(Message,('acc-a',101,501)).body=='<p>Hello &amp; welcome</p>'
        assert s.get(OfRuntime,'acc-a').pending_events==0
    intent=prepare(authority);wire=SyntheticTextTransport('acc-a')
    assert authority.dispatch('tenant-a','acc-a',intent,wire)=='accepted'
    authority.dispatch('tenant-a','acc-a',intent,wire)
    assert len(wire.calls)==1
    store.close()
