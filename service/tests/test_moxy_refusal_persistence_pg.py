"""A failed sales draft must not erase an explicit fan sales refusal."""
from db.onlyfans_moxy_models import MoxyEvent
from sqlalchemy.orm import Session
from test_moxy_integrated_pg import setup, decision, run, TENANT, ACCOUNT
from test_onlyfans_foundation_pg import database, event


def test_refusal_survives_rejected_offer_and_later_generic_message(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    try:
        refused='I am really not interested in buying anything'
        store.record(TENANT, ACCOUNT, event(mid=2, body=refused))
        store.drain(TENANT, ACCOUNT)
        first,_=run(service,engine,decision('offer','You will love this one','first',
            sequence='fixture_level', flags=['refusal'], observations=[{
                'kind':'sales_refused','active':True,'message_id':2,
                'quote':refused,'product_id':None}]),key='refusal-first',payload={'source_message_id':2})
        with Session(engine) as session:
            first_event=session.get(MoxyEvent,(ACCOUNT,first))
            assert first_event.status == 'held', first_event.reason
        store.record(TENANT, ACCOUNT, event(mid=3,body="ok what's new"))
        store.drain(TENANT, ACCOUNT)
        second,_=run(service,engine,decision('offer','Look what I have for you','first',
            sequence='fixture_level'),key='refusal-followup',payload={'source_message_id':3})
        with Session(engine) as session:
            second_event=session.get(MoxyEvent,(ACCOUNT,second))
            assert second_event.status == 'held', (second_event.status,second_event.reason)
    finally:
        store.close()
