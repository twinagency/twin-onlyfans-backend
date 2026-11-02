"""A provider-rejected campaign decision becomes an owned, visible case; nothing is re-sent."""
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup, model, decision
from test_moxy_campaigns_pg import campaign_event
from test_onlyfans_campaigns_pg import create
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.campaigns import CampaignService
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, ProviderThrottled


def test_campaign_provider_rejection_is_owned_visible_and_not_resent(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    campaigns = CampaignService(store); created = create(campaigns); campaigns.queue_due(TENANT, ACCOUNT)
    identity = campaign_event(engine)
    generator = model(decision(text='How has your training been?'))
    service.prepare(TENANT, ACCOUNT, identity, generator)
    intent = service.queue(TENANT, ACCOUNT, identity)
    def throttled(*args, **kwargs): raise ProviderThrottled(retry_after=30)
    monkeypatch.setattr(SyntheticTextTransport, 'send', throttled)
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == 'not_accepted'
    fresh = model(decision())
    assert service.step(TENANT, ACCOUNT, fresh) == 0
    with Session(engine) as s:
        event = s.get(MoxyEvent, (ACCOUNT, identity))
        assert event.status == 'held' and event.reason == 'campaign_provider_recovery_required'
        cases = s.scalars(select(OfOperatorCase).where(OfOperatorCase.category == 'campaign_recovery')).all()
        assert len(cases) == 1 and cases[0].status == 'open' and cases[0].scope == 'offer'
        assert cases[0].owner == 'fixture-operator' and identity in cases[0].evidence_json
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'not_accepted'   # original rejection retained
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1     # nothing re-sent
        case_id = cases[0].case_id
    # Later ticks neither duplicate the case nor reopen it after an operator resolves it.
    service.step(TENANT, ACCOUNT, fresh)
    with Session(engine) as s, s.begin():
        s.get(OfOperatorCase, (ACCOUNT, case_id)).status = 'resolved'
    service.step(TENANT, ACCOUNT, fresh)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfOperatorCase).where(OfOperatorCase.category == 'campaign_recovery')) == 1
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    assert fresh.calls == []
    store.close()
