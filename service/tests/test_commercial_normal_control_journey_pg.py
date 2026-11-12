"""Normal authenticated controls and autonomous delivery; no SQL enable or seeded fan grants."""
import json
from datetime import timedelta
from contextlib import contextmanager
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from db.models import Account,Fan,OfCreatorBinding,OfConversationState
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineOfferGrant,OfEngineAccessEvidence
from onlyfans_runtime.store import Store,Event,utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from onlyfans_runtime.worker import ChatPage,MessagePage,read_step
from onlyfans_commands.configuration_api import router as configs
from onlyfans_commands.vault_api import router as vault
from onlyfans_commands.content_api import router as content
from onlyfans_commands.content_api_v2 import router as content_v2
from onlyfans_commands.engine_approval_api import router as operator
from onlyfans_commands.api import router as controls
from onlyfans_commands.api import conversation_id_for
from conversation_engine import preparation,pilot_scope,commercial_lifecycle
from conversation_engine.model_config import ModelConfig
from conversation_engine.writer_context import CreatorProfile,profile_data,set_active_profiles
from commercial_worker_fixture import World,CommercialFans,writer,OPERATOR,SECRET,key,FAN_A
from history_support import Provider,chat_page,fan_message,spec,factory
from test_commercial_preparation_pg import TENANT,config,page
from test_real_transport_pg import _item
from test_onlyfans_foundation_pg import database

def test_normal_authenticated_controls_reach_one_autonomous_sale_and_continued_chat(database,tmp_path,monkeypatch):
    account='normal-route-account'
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY','1')
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL','1')
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    with Session(database[1]) as session,session.begin():
        session.add(Account(id=account,nickname='Offline fixture'))
        session.flush()
        session.add(OfCreatorBinding(id='normal-binding',tenant_id=TENANT,creator_id='normal-creator',
            creator_name='Offline fixture',account_id=account,handle='fixture',channel='onlyfans'))
        session.add(Fan(account_id=account,fan_id=FAN_A))
        session.flush()
        session.add(OfConversationState(account_id=account,fan_id=FAN_A,mode='paused',revision=0,takeover_state='none',requires_attention=False))
    store=Store(database[0])
    try:
        store.register(TENANT,account,activated_at=utcnow()-timedelta(hours=1),enabled=False)
        TextAuthority(store).configure(TENANT,account,expected_revision=0,enabled=False,media_enabled=False,
            limit_units=0,throttle_seconds=1,transport_kind='fansapi',actor=OPERATOR,reason='Initial disabled connector')
        app=FastAPI();app.state.control_store=store
        for router in (configs,vault,content,content_v2,operator,controls):app.include_router(router)
        @contextmanager
        def control():
            with monkeypatch.context() as environment:
                for name,value in {'ONLYFANS_PROCESS_ROLE':'api','ONLYFANS_CONFIGURATION_TOKEN':'config-fixture',
                    'ONLYFANS_CONFIGURATION_TENANT':TENANT,'ONLYFANS_CONTENT_TOKEN':'content-fixture',
                    'ONLYFANS_CONTENT_TENANT':TENANT,'ONLYFANS_CONTROL_TOKEN':'control-fixture','ONLYFANS_CONTROL_TENANT':TENANT}.items():
                    environment.setenv(name,value)
                with TestClient(app) as client:yield client
        cfgroot=f'/onlyfans/configuration/v1/accounts/{account}'
        vaultroot=f'/onlyfans/content/v1/accounts/{account}/vault'
        contentroot=f'/onlyfans/content/v1/accounts/{account}/items'
        op_root=f'/onlyfans/control/v2/accounts/{account}/pilot-scope'
        hcfg={'Authorization':'Bearer config-fixture','X-OnlyFans-Configuration-Version':'of-configuration.v3'}
        hcontent={'Authorization':'Bearer content-fixture','X-OnlyFans-Content-Version':'of-content.v1'}
        hop={'Authorization':'Bearer control-fixture','X-OnlyFans-Engine-Operator-Version':'of-engine-operator.v2'}
        cfg=config(8,requests=1000,credits=2000)
        with control() as c:
            request={'action':'configure','expectedRevision':0,'commandKey':key(1),'actor':OPERATOR,'reason':'Approved offline preparation','config':cfg}
            assert c.post(cfgroot+'/preparation',json=request).status_code==401
            result=c.post(cfgroot+'/preparation',headers=hcfg,json=request);assert result.status_code==200,result.text
            history={'action':'configure','expectedRevision':0,'commandKey':key(2),'actor':OPERATOR,'reason':'Approved history coverage','spec':spec()}
            result=c.post(cfgroot+'/history-import',headers=hcfg,json=history);assert result.status_code==200,result.text
        from fans_api_adapter.history_import import enlist_fan,fan_coverage
        enlist_fan(store,account,FAN_A)
        wire=Provider(cfg);wire.vault=[lambda:page([_item(31)])];wire.chats[FAN_A]=[chat_page([fan_message(701,FAN_A,'Earlier hello')])]
        for _ in range(12):
            preparation.prepare_one(store,TENANT,account,client_factory=factory(wire))
            if fan_coverage(store,TENANT,account,FAN_A)['state']=='complete':break
        assert fan_coverage(store,TENANT,account,FAN_A)['state']=='complete'
        with control() as c:
            def post(path,body,headers=hcontent):
                result=c.post(path,headers=headers,json=body);assert result.status_code==200,result.text;return result.json()
            made=post(vaultroot+'/folders',{'action':'create','name':'Beach','mediaIds':None,'expectedFingerprint':None,'commandKey':key(3),'actor':OPERATOR})
            folder=c.get(vaultroot+'/folders/'+made['targetId'],headers=hcontent).json()['folder']
            post(vaultroot+'/folders/'+folder['folderId'],{'action':'add','name':None,'mediaIds':['31'],
                'expectedFingerprint':folder['fingerprint'],'commandKey':key(4),'actor':OPERATOR})
            asset=c.get(vaultroot+'/assets/31',headers=hcontent).json()['asset']
            post(vaultroot+'/assets/31',{'patch':{'description':'One beach photograph.'},'expectedFingerprint':asset['fingerprint'],'commandKey':key(5),'actor':OPERATOR})
            price=c.get(vaultroot+'/category-price/'+folder['folderId'],headers=hcontent).json()['categoryPrice']
            post(vaultroot+'/category-price/'+folder['folderId'],{'priceCents':1200,'expectedFingerprint':price['fingerprint'],'commandKey':key(6),'actor':OPERATOR})
            review=c.get(vaultroot+'/category-review/'+folder['folderId']+'?mediaIds=31',headers=hcontent).json()
            drafted=post(vaultroot+'/category-drafts',{'commandKey':key(7),'actor':OPERATOR,'category':{
                'folderId':int(folder['folderId']),'priceCents':1200,'categoryPriceRevision':1,
                'classificationConfirmed':True,'rightsConfirmed':True,'reviewedState':review['reviewedState'],
                'products':[{'label':'Beach photo','description':'One beach photograph.','mediaIds':['31'],'previewMediaIds':[]}]}})
            item=str(drafted['items'][0]['itemId'])
            v2root=f'/onlyfans/content/v2/accounts/{account}/items'
            v2headers={**hcontent,'X-OnlyFans-Content-Version':'of-content.v2'}
            saved=c.get(v2root+'/'+item,headers=v2headers).json()['item']
            post(v2root+'/'+item,{'patch':{'enabled':True},'expectedRevision':saved['revision'],
                'expectedFingerprint':saved['fingerprint'],'commandKey':key(8),'actor':OPERATOR,'acknowledgeOverride':True},headers=v2headers)
        w=World();w.store=store;w.database=database;w.account=account;w.fans=(FAN_A,);w.cfg=cfg
        w.provider=cfg['provider_account'];w.creator=cfg['creator_id']
        secret=tmp_path/'secret';secret.write_bytes(SECRET);secret.chmod(0o600)
        creds=tmp_path/'fans';creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT='+w.provider+'\n');creds.chmod(0o600)
        profile=profile_data(CreatorProfile(name='Offline fixture',facts=(('home_city','Brisbane'),),limits=('no meetups',),role_statement='the creator herself',offers_permitted=True,approved_item_ids=()))
        profiles=tmp_path/'profiles.json';profiles.write_text(json.dumps({account:profile}))
        w.env={'TWIN_PLAN_C_SUPERVISED':'1','TWIN_PLAN_C_DRAFT_ONLY':'0','TWIN_PLAN_C_TENANT':TENANT,'TWIN_PLAN_C_ACCOUNT':account,
            'TWIN_PLAN_C_WEBHOOK_SECRET_FILE':str(secret),'ONLYFANS_PROCESS_ROLE':'worker','TWIN_ENGINE_FANSAPI_REAL':'1',
            'TWIN_ENGINE_FANSAPI_ENV_FILE':str(creds),'ENGINE_MODEL_ENABLED':'1','ENGINE_MODEL_PRIMARY':'m/primary',
            'ENGINE_MODEL_UPSTREAM':'fixture-provider','ENGINE_MODEL_UPSTREAM_REPORTED_NAME':'Fixture Provider',
            'ENGINE_MODEL_ALLOWED':'m/primary','ENGINE_MODEL_ACCOUNTS':account,'ENGINE_MODEL_KEY_ENV':'FIXTURE_KEY',
            'FIXTURE_KEY':'fixture-only','ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK':'1000000','ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK':'2000000',
            'ENGINE_MODEL_DAILY_CAP_MICROUSD':'1000000','ENGINE_MODEL_CREATOR_CAP_MICROUSD':'1000000','ENGINE_MODEL_PROFILES_FILE':str(profiles)}
        snapshot=commercial_lifecycle.catalogue_snapshot(store,TENANT,account,evidence_ref='vault:reviewed-normal-route',valid_until=utcnow()+timedelta(days=1))
        body={'fans':[FAN_A],'creator_profile':profile,'delivery_mode':'full','catalogue':snapshot,
            'operator':{'actor':OPERATOR,'name':'Offline operator','permissions':['configure','activate','approve','reject','edit','stop','takeover','handback','reconcile','correct','read']},
            'model':pilot_scope.model_spec(ModelConfig.from_env(w.env)),
            'limits':{'send_units':200,'daily_send_units':100,'throttle_seconds':1,'provider_requests':4000,'provider_credit_budget':8000,'provider_credits_per_request':2}}
        with control() as c:
            result=c.post(cfgroot+'/commercial',headers=hcfg,json={'action':'promote','config':body,'expectedRevision':1,'commandKey':key(9),'actor':OPERATOR,'reason':'Reviewed product and audience'})
            assert result.status_code==200,result.text
        # Existing bounded read owner establishes actual recent recovery; no flags are forced.
        revision=pilot_scope.describe(store,TENANT,account)['revision']
        client=factory(wire)(cfg,1,preparation.PreparationRequestGuard(store,TENANT,account,revision))
        assert client.identity().state=='verified'
        class Reader:
            account_id=account
            def chats(self,**kw):return ChatPage([FAN_A],None)
            def messages(self,*,fan_id,before_id,limit):
                result=client.history(str(fan_id),limit=limit)
                assert result.state=='observed'
                return MessagePage([Event(fan_id,int(m['id']),m['createdAt'],'in',m['text']) for m in result.data],False)
        store.begin_scan(TENANT,account)
        for _ in range(4):read_step(store,TENANT,account,Reader())
        with control() as c:
            resumed=c.post(f'/onlyfans/control/v1/accounts/{account}/conversations/{conversation_id_for(account,FAN_A)}',
                headers={'Authorization':'Bearer control-fixture','X-OnlyFans-Control-Version':'of-controls.v1'},
                json={'action':'resume_automation','expectedRevision':0,'commandKey':key(12),'actor':OPERATOR})
            assert resumed.status_code==200,resumed.text
            configuration=pilot_scope.describe(store,TENANT,account)['config'];configuration['authorization_mode']='autonomous'
            result=c.post(op_root+'/configure',headers=hop,json={'config':configuration,'expectedRevision':revision,'commandKey':key(10),'actor':OPERATOR,'reason':'Approved autonomous mode'})
            assert result.status_code==200,result.text
            revision=result.json()['revision']
            activation={'status':'active','expectedRevision':revision,'commandKey':key(11),'actor':OPERATOR,'reason':'Reviewed normal-route activation'}
            assert c.post(op_root+'/status',headers=hop,json={**activation,'expectedRevision':revision-1}).status_code==409
            result=c.post(op_root+'/status',headers=hop,json=activation);assert result.status_code==200,result.text
        w.fans_mock=CommercialFans(w.provider,w.creator);w.model_transport=writer(w)
        from model_transport.pilot_supervised import build_supervised_pilot
        w.pilot=build_supervised_pilot(store,w.env,model_transport=w.model_transport,fans_transport=w.fans_mock.transport)
        with Session(database[1]) as session:
            assert session.scalar(select(func.count()).select_from(OfEngineOfferGrant))==0
        w.say('where are you from?',FAN_A);w.unthrottle();w.pilot.tick()
        assert w.fans_mock.posts[-1]['body']['text']=='I am from Brisbane.'
        w.say('show me a photo',FAN_A);w.unthrottle();w.pilot.tick()
        assert not w.cards()
        assert 'Which particular' in w.fans_mock.posts[-1]['body']['text']
        w.say('show me the beach photo',FAN_A);w.unthrottle();w.pilot.tick()
        assert len(w.cards())==1 and w.cards()[0]['body']['mediaFiles']==['31'] and w.cards()[0]['body']['price']==12
        with Session(database[1]) as session:
            intent=session.scalar(select(OfTextIntent).where(OfTextIntent.account_id==account,OfTextIntent.price_cents==1200))
            message_id=intent.provider_message_id
        w.deliver(w.unlock_event(message_id,12));w.unthrottle();w.pilot.tick()
        with Session(database[1]) as session:
            assert session.scalar(select(func.count()).select_from(OfEngineAccessEvidence).where(OfEngineAccessEvidence.applied.is_(True)))==1
        w.say('Thanks, how is your day?',FAN_A);w.unthrottle();w.pilot.tick()
        assert len(w.fans_mock.posts)==4 and len(w.cards())==1
    finally:
        set_active_profiles(None);store.close()
