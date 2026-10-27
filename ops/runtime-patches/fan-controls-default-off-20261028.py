
import os,json,hashlib
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import Session
from db.schema import upgrade,HEAD
from onlyfans_runtime.store import Store
from onlyfans_runtime.configuration import fingerprint,update_configuration
from onlyfans_runtime.connections import change_connection
from db.models import AccountAiConfig,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy

url=os.environ["DATABASE_URL"]
engine=create_engine(url)
def state_digest():
    with engine.connect() as c:
        rows=[tuple(r) for r in c.execute(text("SELECT account_id,fan_id,mode,revision,takeover_state FROM of_conversation_state ORDER BY account_id,fan_id"))]
        return {"count":len(rows),"sha256":hashlib.sha256(json.dumps(rows).encode()).hexdigest()}
before=state_digest()
with engine.begin() as c:
    upgrade(c)
store=Store(url)
actor="op-session:"+hashlib.sha256(b"jesse-request-universal-fan-controls-20261028").hexdigest()
changes=[]
with Session(engine) as s:
    accounts=[(r.account_id,s.get(OfCreatorBinding,r.binding_id).tenant_id) for r in s.scalars(select(OfRuntime))]
for account,tenant in accounts:
    with Session(engine) as s:
        runtime=s.get(OfRuntime,account);cfg=s.get(AccountAiConfig,account)
        assert cfg is not None
        raw=json.loads(cfg.ai_chatter_config_json or "{}")
        scope=raw.get("fastt_scope")
        revision=runtime.configuration_revision;fp=fingerprint(cfg)
    if scope != {"mode":"all","fans":[]}:
        rev,_=update_configuration(store,tenant,account,
            patch={"fasttScope":{"mode":"all","fans":[]}},
            expected_revision=revision,expected_fingerprint=fp,
            command_key=hashlib.sha256(("all-fans-default-off:"+account).encode()).hexdigest(),actor=actor)
        changes.append({"account":account,"scope":"all","configurationRevision":rev})
    with Session(engine) as s:
        connection=s.get(OfProviderConnection,account);policy=s.get(OfDeliveryPolicy,account)
        automatic=bool(connection and connection.automate_new_conversations)
        revision=connection.revision if connection else None
        if automatic:
            assert policy is not None and policy.enabled
            payload={"limitUnits":policy.limit_units,"throttleSeconds":policy.throttle_seconds,
                     "mediaEnabled":policy.media_enabled,"automateNewConversations":False}
    if automatic:
        rev=change_connection(store,tenant,account,action="activate",payload=payload,
             expected_revision=revision,command_key=hashlib.sha256(("new-fans-off:"+account).encode()).hexdigest(),actor=actor)
        changes.append({"account":account,"automateNewConversations":False,"connectionRevision":rev})
after=state_digest()
assert before==after,"Existing fan control choices changed unexpectedly"
with engine.connect() as c:
    default=c.scalar(text("SELECT column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='of_conversation_state' AND column_name='mode'"))
    assert "'paused'" in default
    assert c.scalar(text("SELECT count(*) FROM of_provider_connection WHERE automate_new_conversations"))==0
print(json.dumps({"schema":HEAD,"changes":changes,"existingFanStatesPreserved":before,"default":default}))
store.close()
engine.dispose()
