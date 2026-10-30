"""Real PostgreSQL configuration, migration and ledger scheduling acceptance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import json

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,seed
from test_onlyfans_configuration_pg import setup,save,TENANT,ACCOUNT
from db.models import AccountAiConfig,Message
from db.onlyfans_models import OfConfigurationChange
from db.onlyfans_connection_models import OfProviderConnection
from native_pilot.ledger import Ledger,Refused,_dt
from native_pilot.pacing import Scheduler,Authority
from native_pilot.pacing_schema import upgrade
from onlyfans_runtime.configuration import get_configuration,InvalidConfiguration
from onlyfans_runtime.store import Refused as ConfigurationRefused
from onlyfans_runtime.reply_policy import PACING_KEY,FORMAT_KEY
from twin_bridge import store as raw_store


def test_independent_field_saves_preserve_contextual_settings_and_cancel_only_unsent_work(database):
    store=setup(database)
    with Session(database[1]) as s,s.begin():
        row=s.get(AccountAiConfig,ACCOUNT)
        raw=json.loads(row.ai_chatter_config_json);raw["fastt_contextual_sales_mode"]="shadow"
        row.ai_chatter_config_json=json.dumps(raw)
    data=get_configuration(store,TENANT,ACCOUNT)
    assert not data["nativeReplyPacingEnabled"] and not data["nativeReplyFormatEnabled"]
    first=save(store,data,{"nativeReplyPacingEnabled":True})
    assert save(store,data,{"nativeReplyPacingEnabled":True})==first
    with pytest.raises(ConfigurationRefused,match="revision"):
        save(store,data,{"nativeReplyFormatEnabled":True},key="c"*64)
    fresh=get_configuration(store,TENANT,ACCOUNT)
    save(store,fresh,{"nativeReplyFormatEnabled":True},key="d"*64)
    fresh=get_configuration(store,TENANT,ACCOUNT)
    assert fresh["nativeReplyPacingEnabled"] and fresh["nativeReplyFormatEnabled"]
    for field in ("nativeReplyPacingEnabled","nativeReplyFormatEnabled"):
        for bad in (0,1,None,"false",[]):
            with pytest.raises(InvalidConfiguration):save(store,fresh,{field:bad},key="e"*64)
    raw_store.ensure_runtime_schema(database[0])
    with raw_store.transaction(database[0]) as con:
        con.execute("INSERT INTO native_reply_schedules VALUES('s',?,101,'g',1,?,?,?,25,?,0,0,'waiting',NULL,?,?)",
                    (ACCOUNT,*[_dt(datetime.utcnow())]*6))
    save(store,fresh,{"nativeReplyPacingEnabled":False},key="f"*64)
    final=get_configuration(store,TENANT,ACCOUNT)
    assert not final["nativeReplyPacingEnabled"] and final["nativeReplyFormatEnabled"]
    with Session(database[1]) as s:
        saved=json.loads(s.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json)
        assert saved["private"]=="retained" and saved["fastt_contextual_sales_mode"]=="shadow"
        assert saved[FORMAT_KEY] is True and saved[PACING_KEY] is False
        audits=s.query(OfConfigurationChange).all()
        assert len(audits)==3 and all("private" not in r.before_json+r.after_json for r in audits)
    with raw_store.readonly(database[0]) as con:
        assert con.execute("SELECT state FROM native_reply_schedules WHERE schedule_id='s'").fetchone()[0]=="cancelled"
    store.close()


def native_fixture(database):
    url,engine=database;account,fan="of-900001",900002
    store=seed(url,engine,account=account,fan=fan)
    now=datetime.utcnow()
    with Session(engine) as s,s.begin():
        s.add(AccountAiConfig(account_id=account,ai_chatter_config_json=json.dumps({PACING_KEY:True,FORMAT_KEY:False})))
        s.add(OfProviderConnection(account_id=account,binding_id="binding-"+account,owner_id=900001,revision=1,
            sealed_config="synthetic-never-decrypted",status="verified",verified_at=now-timedelta(minutes=1),
            expires_at=now+timedelta(hours=1),updated_at=now-timedelta(minutes=1)))
        s.add(Message(account_id=account,fan_id=fan,message_id=100,direction="in",body="Synthetic question",
            created_at=now+timedelta(seconds=1),ingested_at=now+timedelta(seconds=2)))
    raw_store.ensure_runtime_schema(url)
    upgrade(url);upgrade(url)
    with raw_store.transaction(url) as con:
        con.execute("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,scope_fingerprint,state,"
            "revision,reserved,send_budget_limit,send_budget_used,updated_at) VALUES(?,?,'synthetic','c','f','active',4,true,3,3,?)",
            (account,fan,_dt(now)))
    ledger=Ledger(url)
    pilot=ledger.activate(pilot_id="synthetic-pacing-pg",account_id=account,fan_id=fan,owner_token="synthetic-owner-000001",
        cutoff=now,deadline=now+timedelta(minutes=30),limit=10,now=now)
    clock=now+timedelta(seconds=10)
    snap=ledger.snapshot(pilot_id=pilot.pilot_id,owner_token=pilot.owner_token,now=clock)
    return store,ledger,pilot,clock,snap


def test_actual_pg_ledger_persists_one_target_under_concurrent_workers_and_restart(database):
    store,ledger,pilot,clock,snap=native_fixture(database)
    class Rng:
        calls=0
        def triangular(self,*args):self.calls+=1;return 35
    rng=Rng();scheduler=Scheduler(ledger,clock=lambda:clock,rng=rng)
    policy=scheduler.policy(pilot.account_id,pilot.fan_id)
    with ThreadPoolExecutor(2) as pool:
        rows=list(pool.map(lambda _:scheduler.pending(pilot,snap,policy),range(2)))
    assert rows[0]==rows[1] and rng.calls==1
    restart=Scheduler(Ledger(database[0]),clock=lambda:clock,rng=rng)
    assert restart.pending(pilot,snap,policy)["target_send_at"]==rows[0]["target_send_at"]
    assert rng.calls==1
    authority=Authority(ledger,pilot.account_id,pilot.fan_id,policy,clock=lambda:clock)
    def admit(con,key):authority.check(con);scheduler.claim(con,rows[0],key)
    key=ledger.claim_input(pilot_id=pilot.pilot_id,owner_token=pilot.owner_token,now=clock,snapshot=snap,admission_check=admit)
    with raw_store.transaction(database[0]) as con:
        con.execute("UPDATE of_runtime SET configuration_revision=configuration_revision+1 WHERE account_id=?",(pilot.account_id,))
    with pytest.raises(Refused,match="reply-policy-changed"):
        ledger.begin_attempt(pilot_id=pilot.pilot_id,owner_token=pilot.owner_token,input_key=key,bubble_no=1,
            revision=pilot.revision,now=clock,authority_check=authority.check)
    assert ledger.receipts(pilot_id=pilot.pilot_id)==[]
    upgrade(database[0])
    with raw_store.readonly(database[0]) as con:
        assert con.execute("SELECT count(*) FROM native_reply_schedules").fetchone()[0]==1
    store.close()
