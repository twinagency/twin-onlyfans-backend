import json,time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup,run,decision,model,status
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.models import Fan,OfConversationState,AccountAiConfig,VaultItem,CatalogItem
from db.onlyfans_moxy_models import MoxyEvent
from moxy_replacement.configuration import load
from moxy_replacement.controls import view
from moxy_replacement.runtime import MoxyService
from onlyfans_runtime.store import Refused,utcnow


def test_configuration_only_third_creator_runs_real_writer_and_delivery_plan(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    third=deepcopy(load('amelia').creator)
    third['creator_key']='synthetic_third';third['rules']=[];third['unresolved']=[]
    settings['creator_document']=third
    service.configure(TENANT,ACCOUNT,expected_revision=1,mode='on',settings=settings,actor='fixture-operator')
    identity,m=run(service,engine,decision())
    assert status(engine,identity)=='ready'
    assert 'synthetic_third' in str(m.calls[0])
    service.queue(TENANT,ACCOUNT,identity)
    store.close()


def test_existing_configuration_auth_controls_mode_and_blocks_wrong_tenant(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with pytest.raises(Refused):view(store,'wrong-tenant',ACCOUNT)
    from onlyfans_commands.configuration_api import router,HEADER,SCHEMA
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TOKEN','fixture-moxy-config-token')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TENANT',TENANT)
    app=FastAPI();app.state.control_store=store;app.include_router(router)
    client=TestClient(app);url='/onlyfans/configuration/v1/accounts/'+ACCOUNT+'/moxy'
    assert client.get(url).status_code==401
    headers={'Authorization':'Bearer fixture-moxy-config-token',HEADER:SCHEMA}
    result=client.get(url,headers=headers)
    assert result.status_code==200 and result.json()['moxy']['engine']=='moxy'
    result=client.put(url,headers=headers,json={'expectedRevision':1,'mode':'off','settings':settings,'actor':'op-session:'+'a'*64})
    assert result.status_code==200
    assert client.get(url,headers=headers).json()['moxy']['mode']=='off'
    store.close()


def test_bounded_parallel_fan_preparation_measures_fixture_latency(database,monkeypatch,tmp_path):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    from test_onlyfans_foundation_pg import seed
    from test_onlyfans_text_delivery_pg import current
    from onlyfans_runtime.text_delivery import TextAuthority
    other='acc-b'
    second_store=seed(database[0],engine,account=other,tenant=TENANT,fan=301)
    current(engine,account=other,fan=301,mode='automated')
    TextAuthority(store).configure(TENANT,other,expected_revision=0,enabled=True,limit_units=20,
        throttle_seconds=1,actor='fixture-operator',reason='Fixture load only',media_enabled=True)
    second=deepcopy(settings);second['creator_key']='cristina';second['catalogue_bindings']={}
    second['operational'].update({k:'fixture-authority' for k in load('cristina').creator['required_operational_fields']})
    with Session(engine) as s,s.begin():
        original=s.get(AccountAiConfig,ACCOUNT)
        s.add(AccountAiConfig(**{c.name:(other if c.name=='account_id' else getattr(original,c.name)) for c in original.__table__.columns}))
        for v in s.scalars(select(VaultItem).where(VaultItem.account_id==ACCOUNT)):
            s.add(VaultItem(**{c.name:(other if c.name=='account_id' else getattr(v,c.name)) for c in v.__table__.columns}))
        for key,old_id in settings['catalogue_bindings'].items():
            old=s.get(CatalogItem,old_id)
            new=CatalogItem(**{c.name:(other if c.name=='account_id' else getattr(old,c.name)) for c in old.__table__.columns if c.name!='id'})
            s.add(new);s.flush();second['catalogue_bindings'][key]=new.id
    service.configure(TENANT,other,expected_revision=0,mode='on',settings=second,actor='fixture-operator')
    identities=[]
    with Session(engine) as s,s.begin():
        for account,fans in [(ACCOUNT,range(201,209)),(other,range(302,309))]:
            for fan in fans:
                s.add(Fan(account_id=account,fan_id=fan));s.flush()
                s.add(OfConversationState(account_id=account,fan_id=fan,mode='automated',revision=0,input_revision=0,takeover_state='none'))
    for account,fans in [(ACCOUNT,range(201,209)),(other,range(301,309))]:
        for fan in fans:identities.append((account,service.add_event(TENANT,account,fan,'scheduled','fixture-load-'+str(fan),{})))
    latencies=[]
    def prepare(target):
        account,identity=target
        started=time.monotonic()
        MoxyService(store).prepare(TENANT,account,identity,model(decision(),delay_seconds=.01))
        latencies.append(time.monotonic()-started)
        with Session(engine) as s:return s.get(MoxyEvent,(account,identity)).status
    start=time.monotonic()
    with ThreadPoolExecutor(4) as pool:assert list(pool.map(prepare,identities))==['ready']*16
    elapsed=time.monotonic()-start
    report={'fixture_only':True,'creators':2,'fans':16,'workers':4,'phase':'model preparation; no live send',
        'model_delay_seconds':.01,'elapsed_seconds':elapsed,'max_latency_seconds':max(latencies),
        'p95_latency_seconds':sorted(latencies)[-1],'production_capacity_claim':False}
    print('MOXY_LOAD='+json.dumps(report))
    assert elapsed<180
    second_store.close();store.close()
