"""Profile polling must not silently discard an otherwise current Moxy reply."""
import pytest
from sqlalchemy.orm import Session
from db.models import Fan
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from test_moxy_integrated_pg import setup,decision,model,run,status


def change_fan(engine,field):
    with Session(engine) as s,s.begin():
        fan=s.get(Fan,(ACCOUNT,101))
        if field=='profile_last_synced_at':fan.profile_last_synced_at=utcnow()
        else:fan.lifetime_spend_cents=(fan.lifetime_spend_cents or 0)+300


@pytest.mark.parametrize('field',['profile_last_synced_at','lifetime_spend_cents'])
def test_profile_poll_during_generation_preserves_only_unchanged_evidence(database,monkeypatch,field):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','profile-refresh-generation',{})
    m=model(decision(),on_generate=lambda:change_fan(engine,field))
    service.prepare(TENANT,ACCOUNT,identity,m)
    assert status(engine,identity)==('ready' if field=='profile_last_synced_at' else 'stale')
    if field=='profile_last_synced_at':
        intent=service.queue(TENANT,ACCOUNT,identity)
        wire=SyntheticTextTransport(ACCOUNT)
        assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
        assert len(wire.calls)==1 and status(engine,identity)=='done'
    store.close()


@pytest.mark.parametrize('field',['profile_last_synced_at','lifetime_spend_cents'])
def test_profile_poll_before_dispatch_preserves_only_unchanged_evidence(database,monkeypatch,field):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,m=run(service,engine,decision())
    intent=service.queue(TENANT,ACCOUNT,identity)
    change_fan(engine,field)
    wire=SyntheticTextTransport(ACCOUNT)
    outcome=authority.dispatch(TENANT,ACCOUNT,intent,wire)
    if field=='profile_last_synced_at':
        assert outcome=='accepted' and len(wire.calls)==1
        assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
        assert len(wire.calls)==1
    else:assert outcome in {'cancelled','refused'} and wire.calls==[]
    store.close()
