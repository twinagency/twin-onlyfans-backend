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
    assert not any(pacing.settings(f.path,L.ACCT).values())
    assert not f.sql("SELECT * FROM native_reply_pacing")


def test_delivery_metric_uses_receipt_not_target(f):
    d,_,_=prepare(f,dice=Dice(60))
    event=pacing.delivery_event(d['authority'],d['schedule'],wire_at=L.NOW+timedelta(seconds=96),
                                receipt_at=L.NOW+timedelta(seconds=100))
    assert event['verified_receipt_to_receipt_s']==98
    assert event['provider_created_to_receipt_s']==99
    assert event['target_miss_s']==38
    assert event['intake_lag_s']==1
    detailed=pacing.delivery_event(d['authority'],d['schedule'],wire_at=L.NOW+timedelta(seconds=96),
        receipt_at=L.NOW+timedelta(seconds=100),eligible_at=L.NOW+timedelta(seconds=62),
        admitted_at=L.NOW+timedelta(seconds=70),draft_ready_at=L.NOW+timedelta(seconds=85),
        throttle_done_at=L.NOW+timedelta(seconds=90))
    assert detailed['components']==dict(intentional_wait_s=52,before_scheduling_s=8,scheduler_queue_s=8,
        generation_and_shaping_s=15,account_throttle_s=5,preflight_s=6,transport_to_receipt_s=4)


def test_policy_bounds():
    dice=random.Random(47)
    for mode in (35,60):
        values=[dice.triangular(25,90,mode) for _ in range(1000)]
        assert min(values)>=25 and max(values)<=90 and len(set(values))>900


def test_confirmed_active_exchange_uses_shorter_mode_and_measured_allowance(f):
    key=f.claim()
    attempt=f.attempt(key)
    f.sent(attempt,900)
    f.sql("UPDATE native_pilot_receipts SET updated_at=?",(_dt(L.NOW+timedelta(seconds=14)),))
    f.ledger.finish_input(**f.call,input_key=key)
    f.source(900,direction='out',created_at=_dt(L.NOW+timedelta(seconds=14)),ingested_at=_dt(L.NOW+timedelta(seconds=14)))
    f.source(101,created_at=_dt(L.NOW+timedelta(seconds=20)),ingested_at=_dt(L.NOW+timedelta(seconds=21)))
    dice=Dice(25)
    d,_,_=prepare(f,seconds=30,dice=dice)
    assert dice.calls==[(25,90,35)]
    assert d['pipeline_allowance_s']==4
    # The lower target also includes observed processing time, not an added 25-second hold.
    assert d['eligible_at']==L.NOW+timedelta(seconds=42)
    # A follow-up receipt must not make an exchange active.
    f.sql("UPDATE native_reply_pacing SET state='invalidated'")
    f.sql("UPDATE of_conversation_state SET revision=revision+1")
    f.sql("UPDATE native_pilot_inputs SET source_hash='followup:synthetic'")
    dice=Dice(50)
    prepare(f,seconds=31,dice=dice)
    assert dice.calls==[(25,90,60)]


def test_waiting_turn_releases_engine_and_claim_slot(f):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch
    from native_pilot.service import Service
    from native_pilot.engine import NativeEngine

    async def run():
        service=Service(SimpleNamespace(account=L.ACCT,fan=L.FAN,all_fans=False))
        service.path,service.ledger,service.pilot=f.path,f.ledger,f.pilot
        service.strict=True
        service.cooldown_left=lambda:0
        called=[]
        async def execute(*args,**kwargs):called.append(args);return None
        service.execute=execute
        with patch('native_pilot.runner.now',return_value=L.NOW+timedelta(seconds=10)),patch.object(pacing.random,'triangular',return_value=25):
            await asyncio.wait_for(service.turn(),timeout=0.5)
        assert not called
        assert not f.sql('SELECT * FROM native_pilot_inputs')
        assert not f.sql('SELECT * FROM native_pilot_receipts')
        with patch('native_pilot.runner.now',return_value=L.NOW+timedelta(seconds=70)):
            await service.turn()
        assert len(called)==1
    asyncio.run(run())


def test_requeued_creator_context_failure_keeps_original_schedule(f):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    f.sql("UPDATE native_pilot_inputs SET state='requeued:0' WHERE input_key=?",(key,))
    pacing.finish(f.ledger,d,now=now,error='creator-facts-not-current')
    assert f.sql("SELECT state FROM native_reply_pacing")[0][0]=='waiting'
    retry,_,_=prepare(f,seconds=72,ledger=Ledger(f.path))
    assert retry['schedule']['target_at']==d['schedule']['target_at']
    assert retry['schedule']['admissions']==1


def test_recover_legacy_done_schedule_only_for_proven_unsent_retry(f):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    f.sql("UPDATE native_pilot_inputs SET state='requeued:0' WHERE input_key=?",(key,))
    f.sql("UPDATE native_reply_pacing SET state='done'")
    retry,_,_=prepare(f,seconds=72,ledger=Ledger(f.path))
    assert retry['schedule']['schedule_key']==d['schedule']['schedule_key']
    assert retry['schedule']['target_at']==d['schedule']['target_at']
    assert retry['schedule']['state']=='waiting'
    assert retry['schedule']['admissions']==1
    assert f.sql('SELECT count(*) FROM native_reply_pacing')[0][0]==1


@pytest.mark.parametrize('state',['dispatching','unknown','sent'])
def test_recovery_refuses_standing_delivery(f,state):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    f.sql("UPDATE native_pilot_inputs SET state='requeued:0' WHERE input_key=?",(key,))
    f.sql("UPDATE native_reply_pacing SET state='done'")
    f.sql("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) VALUES(99,?,?,1,?,?,?)",(f.pilot.pilot_id,key,state,_dt(now),_dt(now)))
    with store.transaction(f.path) as con:
        row=con.execute('SELECT * FROM native_reply_pacing').fetchone()
        with pytest.raises(Refused,match='reply-schedule-terminal'):
            pacing._reuse_schedule(con,row,f.pilot.pilot_id,now)


# Regression: 31 Oct 2026 production. A restart (clean stop) ended a turn that had not sent anything. pacing.finish
# closed the schedule as 'done' (the stop reason is not in its retryable list and the input was still 'generating'),
# then release_service deleted the unsent input so it would be answered again after the restart. The closed schedule
# plus the missing input left every later prepare() raising reply-schedule-terminal: the fan was never answered
# (Amelia: 4 fans owed 4+ hours).
def test_clean_stop_before_send_keeps_schedule_waiting_and_fan_is_answered_after_restart(f):
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    pacing.finish(f.ledger,d,now=now,error='service-stopping',sent=False)
    f.sql("DELETE FROM native_pilot_inputs WHERE input_key=?",(key,))   # what Ledger.release_service does to an unsent claim
    assert f.sql("SELECT state FROM native_reply_pacing")[0][0]=='waiting'
    retry,_,_=prepare(f,seconds=72,ledger=Ledger(f.path))
    assert retry['schedule']['schedule_key']==d['schedule']['schedule_key']
    assert retry['schedule']['target_at']==d['schedule']['target_at']


def test_done_schedule_whose_unsent_input_was_dropped_is_recovered(f):
    # The rows already stuck in production have exactly this shape: state done, input_key set, input row gone, no receipt.
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    f.sql("UPDATE native_reply_pacing SET state='done'")
    f.sql("DELETE FROM native_pilot_inputs WHERE input_key=?",(key,))
    retry,_,_=prepare(f,seconds=72,ledger=Ledger(f.path))
    assert retry['schedule']['schedule_key']==d['schedule']['schedule_key']
    assert retry['schedule']['target_at']==d['schedule']['target_at']
    assert retry['schedule']['state']=='waiting'
    assert f.sql('SELECT count(*) FROM native_reply_pacing')[0][0]==1


def test_sent_reply_still_closes_schedule(f):
    d,snap,now=prepare(f,seconds=70)
    f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    pacing.finish(f.ledger,d,now=now,error=None,sent=True)
    assert f.sql("SELECT state FROM native_reply_pacing")[0][0]=='done'


def test_completed_unsent_input_does_not_leave_a_spent_schedule_holding_the_next_message(f):
    # An identity hand-off completes the input without a send. The schedule used to stay waiting with its admissions
    # spent, so every later message from that fan was held by reply-regeneration-budget-held.
    d,snap,now=prepare(f,seconds=70)
    key=f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    f.ledger.finish_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,input_key=key,now=now)
    pacing.finish(f.ledger,d,now=now,sent=False)      # what the identity hand-off in service.turn now does
    f.sql("UPDATE native_reply_pacing SET admissions=3")
    f.source(101,created_at=_dt(now),ingested_at=_dt(now))
    retry,new,_=prepare(f,seconds=80,ledger=Ledger(f.path))
    assert retry['schedule']['schedule_key']!=d['schedule']['schedule_key']
    f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=new,now=now+timedelta(seconds=10),
                         reply_authority=retry['authority'])


def test_sent_input_schedule_is_still_closed_and_unfinished_input_keeps_it(f):
    d,snap,now=prepare(f,seconds=70)
    f.ledger.claim_input(pilot_id=f.pilot.pilot_id,owner_token=L.OWNER,snapshot=snap,now=now,reply_authority=d['authority'])
    # claimed, still generating, no receipt: not settled, the schedule is reused (target kept)
    retry,_,_=prepare(f,seconds=72,ledger=Ledger(f.path))
    assert retry['schedule']['schedule_key']==d['schedule']['schedule_key']


def test_extended_pacing_is_explicit_and_preserves_the_original_anchor(f):
    from onlyfans_runtime.reply_settings import DELAY_MAX_KEY
    f.sql("UPDATE account_ai_config SET ai_chatter_config_json=?",(json.dumps({pacing.PACING:True,DELAY_MAX_KEY:180}),))
    dice=Dice(180)
    snap=f.ledger.snapshot(**f.call)
    d=pacing.prepare(f.ledger,f.pilot,snap,now=L.NOW+timedelta(seconds=5),rng=dice)
    assert dice.calls[0][1]==180
    assert d['schedule']['delay_seconds']==180
    first=d['schedule']['first_received_at']
    again=pacing.prepare(f.ledger,f.pilot,snap,now=L.NOW+timedelta(seconds=30),rng=Dice(25))
    assert again['schedule']['first_received_at']==first
    assert again['schedule']['target_at']==d['schedule']['target_at']


@pytest.mark.parametrize('value',[True,25,179,181,'180',None])
def test_delay_configuration_requires_a_supported_literal_integer(value):
    from onlyfans_runtime.configuration import validate_patch,InvalidConfiguration
    with pytest.raises(InvalidConfiguration):validate_patch({'nativeReplyDelayMaxSeconds':value})


def test_expanding_the_legacy_delay_constraint_preserves_every_schedule_field(f):
    # A historical 25..90 table retains its schedule verbatim during widening.
    with store.transaction(f.path) as con:
        con.execute('DROP INDEX native_reply_pacing_pending')
        con.execute('DROP TABLE native_reply_pacing')
        con.execute(pacing_schema.DDL.replace('<=180','<=90'))
        con.execute(pacing_schema.INDEX)
    d,_,_=prepare(f,dice=Dice(65))
    before=f.sql('SELECT * FROM native_reply_pacing')
    pacing_schema.upgrade(f.path);pacing_schema.upgrade(f.path)
    assert f.sql('SELECT * FROM native_reply_pacing')==before
    assert d['schedule']['delay_seconds']==65
