"""Fake-clock timing with real ledger transactions; no model/provider/network."""
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_native_pilot_ledger as L
from native_pilot import pacing, pacing_schema
from native_pilot.ledger import Ledger, Refused, _dt
from twin_bridge import store


class Dice:
    def __init__(self, value=60):
        self.value, self.calls = value, []

    def triangular(self, low, high, mode):
        self.calls.append((low, high, mode))
        return self.value


@pytest.fixture
def f(tmp_path):
    f = L.Fixture(tmp_path, 'pacing')
    f.activate()
    f.sql("CREATE TABLE account_ai_config(account_id TEXT PRIMARY KEY,ai_chatter_config_json TEXT)")
    f.sql("INSERT INTO account_ai_config VALUES(?,?)", (L.ACCT,json.dumps({pacing.PACING:True,'unrelated':'keep'})))
    f.sql("CREATE TABLE of_runtime(account_id TEXT,enabled INTEGER,binding_id TEXT,activated_at TEXT,configuration_revision INTEGER)")
    f.sql("INSERT INTO of_runtime VALUES(?,1,'binding',?,1)",(L.ACCT,_dt(L.NOW)))
    f.sql("CREATE TABLE of_provider_connection(account_id TEXT,binding_id TEXT,revision INTEGER,status TEXT,expires_at TEXT,automation_starts_at TEXT)")
    f.sql("INSERT INTO of_provider_connection VALUES(?,'binding',1,'verified',?,?)",(L.ACCT,_dt(L.NOW+timedelta(days=1)),_dt(L.NOW)))
    f.sql("CREATE TABLE of_conversation_state(account_id TEXT,fan_id INTEGER,mode TEXT,takeover_state TEXT,revision INTEGER)")
    f.sql("INSERT INTO of_conversation_state VALUES(?,?,'automated','none',1)",(L.ACCT,L.FAN))
    f.sql("CREATE TABLE of_configuration_change(account_id TEXT,revision INTEGER,after_json TEXT)")
    pacing_schema.upgrade(f.path)
    f.source()
    return f


def prepare(f, seconds=10, dice=None, ledger=None):
    led = ledger or f.ledger
    now = L.NOW+timedelta(seconds=seconds)
    snap = led.snapshot(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,now=now)
    return pacing.prepare(led,f.pilot,snap,now=now,rng=dice or Dice()),snap,now


def test_one_target_survives_restart_and_changed_caption(f):
    dice = Dice(52)
    d,snap,_ = prepare(f,dice=dice)
    f.sql("UPDATE messages SET body='updated caption' WHERE message_id=100")
    again,_,_ = prepare(f,ledger=Ledger(f.path),dice=dice)
    assert len(dice.calls)==1
    assert d['schedule']['schedule_key']==again['schedule']['schedule_key']
    assert again['schedule']['target_at']==_dt(L.NOW+timedelta(seconds=54))
    assert not f.sql("SELECT * FROM native_pilot_inputs")
    assert not f.sql("SELECT * FROM native_pilot_receipts")


def test_burst_cannot_reset_target_or_debounce(f):
    dice=Dice(25)
    first,_,_=prepare(f,dice=dice)
    for mid,t in [(101,8),(102,16),(103,21),(104,24)]:
        f.source(mid,created_at=_dt(L.NOW+timedelta(seconds=t)),ingested_at=_dt(L.NOW+timedelta(seconds=t)))
    due,snap,_=prepare(f,seconds=25,dice=dice)
    assert snap.member_ids==(100,101,102,103,104)
    assert due['schedule']['target_at']==first['schedule']['target_at']
    assert due['eligible_at']==L.NOW+timedelta(seconds=27)
    assert len(dice.calls)==1
    overdue,_,now=prepare(f,seconds=100,dice=dice)
    assert overdue['eligible_at']<now


def test_two_workers_create_only_one_target(f):
    dice=Dice(35)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:prepare(f,dice=dice,ledger=Ledger(f.path))[0],range(2)))
    assert results[0]['schedule']['schedule_key']==results[1]['schedule']['schedule_key']
    assert len(dice.calls)==1
    assert f.sql("SELECT count(*) FROM native_reply_pacing")[0][0]==1


@pytest.mark.parametrize('sql',[
    "UPDATE of_conversation_state SET mode='human_controlled',revision=2",
    "UPDATE of_conversation_state SET revision=2",
    "UPDATE of_runtime SET enabled=0",
    "UPDATE of_provider_connection SET status='paused'",
    "UPDATE of_provider_connection SET revision=2",
    "UPDATE of_runtime SET configuration_revision=2",
])
def test_changed_authority_refuses_before_spending(f,sql):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    before=f.sql("SELECT send_budget_used FROM twin_bridge_conversations")
    f.sql(sql)
    with pytest.raises(Refused):
        f.ledger.begin_attempt(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,input_key=key,bubble_no=1,
                              revision=f.pilot.revision,now=now,reply_authority=d['authority'])
    assert not f.sql("SELECT * FROM native_pilot_receipts")
    assert before==f.sql("SELECT send_budget_used FROM twin_bridge_conversations")


def test_wire_recheck_catches_disable_after_attempt(f):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    a=f.ledger.begin_attempt(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,input_key=key,bubble_no=1,
                            revision=f.pilot.revision,now=now,reply_authority=d['authority'])
    f.sql("UPDATE account_ai_config SET ai_chatter_config_json='{}'")
    with pytest.raises(Refused,match='reply-configuration-changed'):
        f.ledger.validate_dispatch(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,attempt_id=a.id,now=now,reply_authority=d['authority'])
    # The gate does not pretend an already-admitted attempt never existed.
    assert f.sql("SELECT state FROM native_pilot_receipts")[0][0]=='dispatching'


def test_stale_input_preserves_target_and_new_message(f):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    f.source(101,created_at=_dt(now),ingested_at=_dt(now))
    with pytest.raises(Refused,match='source-changed'):
        f.ledger.begin_attempt(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,input_key=key,bubble_no=1,
                              revision=f.pilot.revision,now=now,reply_authority=d['authority'])
    f.ledger.finish_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,input_key=key,now=now)
    pacing.finish(f.ledger,d,now=now,error='source-changed')
    retry,new,_=prepare(f,seconds=72)
    assert retry['schedule']['target_at']==d['schedule']['target_at']
    assert new.message_id==101
    assert not f.ledger.input_claimed(pilot_id=f.pilot.pilot_id,message_id=101)


def test_regeneration_budget_does_not_claim_new_input(f):
    d,snap,now=prepare(f,seconds=70)
    f.sql("UPDATE native_reply_pacing SET admissions=3")
    with pytest.raises(Refused,match='regeneration-budget-held'):
        f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    assert not f.sql("SELECT * FROM native_pilot_inputs")


def test_disable_invalidates_waiting_without_erasing_evidence(f):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    d,_,_=prepare(f)
    eng=create_engine('sqlite:///'+str(f.path))
    with Session(eng) as s,s.begin():pacing.invalidate_settings(s,L.ACCT)
    assert f.sql("SELECT state FROM native_reply_pacing")==[('invalidated',)]
    pacing_schema.downgrade(f.path)
    pacing_schema.upgrade(f.path)
    assert f.sql("SELECT schedule_key FROM native_reply_pacing")==[(d['schedule']['schedule_key'],)]
    eng.dispose()


def test_flag_strict_default_off_and_diagnostic(f,caplog):
    for value in [None,1,0,'true','false',[],{}]:
        f.sql("UPDATE account_ai_config SET ai_chatter_config_json=?",(json.dumps({pacing.PACING:value}),))
        assert pacing.settings(f.path,L.ACCT)[pacing.PACING] is False
    assert 'invalid-native-reply-switch' in caplog.text
    f.sql("UPDATE account_ai_config SET ai_chatter_config_json='{}'")
    assert pacing.settings(f.path,L.ACCT)=={pacing.PACING:False}
    assert not f.sql("SELECT * FROM native_reply_pacing")


def test_delivery_metric_uses_receipt_not_target(f):
    d,_,_=prepare(f,dice=Dice(60))
    event=pacing.delivery_event(d['authority'],d['schedule'],wire_at=L.NOW+timedelta(seconds=96),
                                receipt_at=L.NOW+timedelta(seconds=100))
    assert event['verified_receipt_to_receipt_s']==98
    assert event['provider_created_to_receipt_s']==99
    assert event['target_miss_s']==38
    assert event['intake_lag_s']==1


def test_policy_bounds():
    dice=random.Random(47)
    for mode in (35,60):
        values=[dice.triangular(25,90,mode) for _ in range(1000)]
        assert min(values)>=25 and max(values)<=90 and len(set(values))>900
