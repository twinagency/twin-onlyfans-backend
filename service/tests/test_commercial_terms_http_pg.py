"""Authenticated terms commands use the existing immutable terms owner."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_commercial_catalogue_terms_pg import cat, drafts, prod, AGENCY
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT
from onlyfans_commands.configuration_api import router, HEADER, SCHEMA
from onlyfans_runtime.content_management import get_content, update_content
from db.onlyfans_catalogue_terms_models import OfCatalogueTerms


def test_terms_http_prices_revision_content_replay_and_withdrawal(cat, monkeypatch):
    store,account=cat
    _,[item]=drafts(cat,'Scenic',(11,),1200,[prod('Beach photo',(11,))])
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TOKEN','isolated-configuration')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TENANT',TENANT)
    app=FastAPI();app.state.control_store=store;app.include_router(router)
    headers={'Authorization':'Bearer isolated-configuration',HEADER:SCHEMA}
    path=f'/onlyfans/configuration/v1/accounts/{account}/catalogue-terms/{item}'
    with TestClient(app) as client:
        assert client.get(path).status_code==401
        shown=client.get(path,headers=headers);assert shown.status_code==200
        before=shown.json()['terms']
        command={'action':'approve','expectedRevision':before['revision'],'expectedFingerprint':before['contentFingerprint'],
                 'standardCents':1200,'discountCents':900,'commandKey':'a'*64,'actor':AGENCY}
        saved=client.post(path,headers=headers,json=command);assert saved.status_code==200,saved.text
        after=saved.json()['terms'];assert after['revision']==before['revision']+1 and after['discountCents']==900
        assert client.post(path,headers=headers,json=command).json()['terms']==after
        assert client.post(path,headers=headers,json={**command,'commandKey':'b'*64}).status_code==409
        assert client.post(path,headers=headers,json={**command,'actor':'invalid'}).status_code==403
        assert client.get(path.replace(account,'unknown-account'),headers=headers).status_code==404
        current=get_content(store,TENANT,account,str(item))
        update_content(store,TENANT,account,str(item),patch={'label':'Revised beach photo'},
            expected_revision=current['revision'],expected_fingerprint=current['fingerprint'],command_key='c'*64,actor=AGENCY)
        assert client.post(path,headers=headers,json={**command,'expectedRevision':after['revision'],'commandKey':'d'*64}).status_code==409
        withdraw={'action':'withdraw','expectedRevision':after['revision'],'commandKey':'e'*64,'actor':AGENCY}
        stopped=client.post(path,headers=headers,json=withdraw)
        assert stopped.status_code==200 and stopped.json()['terms']['status']=='withdrawn'
        assert client.post(path,headers=headers,json=withdraw).json()==stopped.json()
        with Session(store.engine) as session:
            rows=list(session.scalars(select(OfCatalogueTerms).where(OfCatalogueTerms.account_id==account)))
            assert len(rows)==2 and {r.status for r in rows}=={'superseded','withdrawn'}
