"""Deterministic synthetic admission/receipt tests. No provider or model network."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from unittest.mock import patch

import pytest

import test_native_pilot_ledger as L
from native_pilot.ledger import Ledger, Refused, _dt
from native_pilot.pacing import Scheduler, Authority, Policy, stamp
from onlyfans_runtime.reply_policy import PACING_KEY, enabled


class RandomSource:
    def __init__(self, value=60):
        self.value, self.calls = value, []

    def triangular(self, low, high, mode):
        self.calls.append((low, high, mode))
        return self.value


@pytest.fixture
def env(tmp_path):
    f = L.Fixture(tmp_path, "pacing")
    f.activate()
    f.source()
    f.sql("CREATE TABLE account_ai_config(account_id TEXT PRIMARY KEY,ai_chatter_config_json TEXT)")
    f.sql("INSERT INTO account_ai_config VALUES(?,?)", (L.ACCT, json.dumps({PACING_KEY:True})))
    f.sql("CREATE TABLE of_runtime(account_id TEXT PRIMARY KEY,binding_id TEXT,enabled INTEGER,activated_at TEXT,configuration_revision INTEGER)")
    f.sql("INSERT INTO of_runtime VALUES(?,'binding',1,?,1)", (L.ACCT,_dt(L.NOW)))
    f.sql("CREATE TABLE of_conversation_state(account_id TEXT,fan_id BIGINT,mode TEXT,takeover_state TEXT,revision INTEGER)")
    f.sql("INSERT INTO of_conversation_state VALUES(?,?,'automated','none',1)", (L.ACCT,L.FAN))
    f.sql("CREATE TABLE of_provider_connection(account_id TEXT,binding_id TEXT,status TEXT,last_error TEXT,verified_at TEXT,expires_at TEXT,revision INTEGER,updated_at TEXT)")
    f.sql("INSERT INTO of_provider_connection VALUES(?,'binding','verified',NULL,?,?,1,?)",
          (L.ACCT,_dt(L.NOW),_dt(L.NOW+timedelta(hours=1)),_dt(L.NOW)))
    f.sql("CREATE TABLE of_creator_bindings(id TEXT,account_id TEXT,channel TEXT,revoked_at TEXT)")
    f.sql("INSERT INTO of_creator_bindings VALUES('binding',?,'onlyfans',NULL)", (L.ACCT,))
    f.sql("CREATE TABLE of_control_audit(account_id TEXT,fan_id BIGINT,created_at TEXT)")
    clock = [L.NOW+timedelta(seconds=10)]
    rng = RandomSource()
    scheduler = Scheduler(f.ledger,clock=lambda:clock[0],rng=rng)
    return f,clock,rng,scheduler


def snapshot(f, clock):
    return f.ledger.snapshot(**{**f.call,"now":clock[0]})


def pending(env):
    f,clock,rng,scheduler=env
    policy=scheduler.policy(L.ACCT,L.FAN)
    return scheduler.pending(f.pilot,snapshot(f,clock),policy),policy


def claim(env, row, policy):
    f,clock,rng,scheduler=env
    authority=Authority(f.ledger,L.ACCT,L.FAN,policy,clock=lambda:clock[0])
    def admitted(con,key):
        authority.check(con)
        scheduler.claim(con,row,key)
    return f.ledger.claim_input(**{**f.call,"now":clock[0]},snapshot=snapshot(f,clock),admission_check=admitted)


def test_absent_off_and_corrupt_are_disabled_without_scheduling_writes(env, caplog):
    f,clock,rng,scheduler=env
    for raw in ({},{PACING_KEY:False},{PACING_KEY:"true"},{PACING_KEY:1}):
        f.sql("UPDATE account_ai_config SET ai_chatter_config_json=?",(json.dumps(raw),))
        assert scheduler.policy(L.ACCT,L.FAN)==Policy()
    assert "invalid" in caplog.text
    assert f.sql("SELECT count(*) FROM native_reply_schedules")==[(0,)]
    assert rng.calls==[]
    assert not enabled({},PACING_KEY)


def test_target_drawn_once_and_survives_new_worker_bubbles_and_caption_change(env):
    f,clock,rng,scheduler=env
    row,policy=pending(env)
    f.source(101,body="second bubble",created_at=_dt(L.NOW+timedelta(seconds=8)),ingested_at=_dt(L.NOW+timedelta(seconds=9)))
    again=scheduler.pending(f.pilot,snapshot(f,clock),policy)
    restarted=Scheduler(Ledger(f.path),clock=lambda:clock[0],rng=RandomSource(90))
    f.sql("UPDATE messages SET body='caption correction' WHERE message_id=101")
    after_restart=restarted.pending(f.pilot,snapshot(f,clock),policy)
    assert row["schedule_id"]==again["schedule_id"]==after_restart["schedule_id"]
    assert row["target_send_at"]==again["target_send_at"]==after_restart["target_send_at"]
    assert row["first_received_at"]==_dt(L.NOW+timedelta(seconds=2))
    assert rng.calls==[(25,90,60)] and len(f.sql("SELECT * FROM native_reply_schedules"))==1
    assert f.sql("SELECT count(*) FROM native_pilot_inputs")==[(0,)]


@pytest.mark.parametrize("delay",[25,35,60,90])
def test_bounds_and_debounce_cap_do_not_restart_original_target(env,delay):
    f,clock,rng,scheduler=env;rng.value=delay
    row,policy=pending(env)
    for seconds in (15,20,24,28,35):
        clock[0]=L.NOW+timedelta(seconds=seconds+1)
        f.source(100+seconds,body="more",created_at=_dt(L.NOW+timedelta(seconds=seconds)),ingested_at=_dt(L.NOW+timedelta(seconds=seconds)))
        current=scheduler.pending(f.pilot,snapshot(f,clock),policy)
        assert current["target_send_at"]==row["target_send_at"]
        assert scheduler.eligible_at(current,7)<=max(stamp(row["target_send_at"]),L.NOW+timedelta(seconds=22))
    assert len(rng.calls)==1


def test_two_workers_create_one_durable_target(env):
    f,clock,rng,scheduler=env
    snap=snapshot(f,clock);policy=scheduler.policy(L.ACCT,L.FAN)
    with ThreadPoolExecutor(2) as pool:
        rows=list(pool.map(lambda _:scheduler.pending(f.pilot,snap,policy),range(2)))
    assert rows[0]["schedule_id"]==rows[1]["schedule_id"]
    assert len(rng.calls)==1


def test_only_confirmed_conversational_receipt_establishes_active_exchange(env):
    f,clock,rng,scheduler=env
    key=f.claim();attempt=f.attempt(key);f.sent(attempt,200)
    f.ledger.finish_input(**f.call,input_key=key)
    f.source(200,direction="out",created_at=_dt(L.NOW+timedelta(seconds=11)),ingested_at=_dt(L.NOW+timedelta(seconds=11)))
    f.source(201,created_at=_dt(L.NOW+timedelta(seconds=12)),ingested_at=_dt(L.NOW+timedelta(seconds=13)))
    clock[0]=L.NOW+timedelta(seconds=15)
    row,_=pending(env)
    assert rng.calls==[(25,90,35)]
    assert row["pipeline_allowance_s"]==0  # fake model/transport elapsed zero
    nextkey=claim(env,row,scheduler.policy(L.ACCT,L.FAN))
    f.ledger.finish_input(**{**f.call,"now":clock[0]},input_key=nextkey)
    scheduler.settle(row)
    f.sql("UPDATE native_pilot_inputs SET source_hash='followup:synthetic'")
    f.source(202,created_at=_dt(L.NOW+timedelta(seconds=14)),ingested_at=_dt(L.NOW+timedelta(seconds=14)))
    pending(env)
    assert rng.calls[-1]==(25,90,60)


@pytest.mark.parametrize("change",[
    "UPDATE of_conversation_state SET mode='human',revision=revision+1",
    "UPDATE of_conversation_state SET takeover_state='requested',revision=revision+1",
    "UPDATE of_provider_connection SET status='paused',revision=revision+1",
    "UPDATE of_provider_connection SET revision=revision+1",
    "UPDATE of_runtime SET activated_at='2026-10-16 12:00:01.000000'",
    "UPDATE of_runtime SET configuration_revision=configuration_revision+1",
    "UPDATE account_ai_config SET ai_chatter_config_json='{}'",
])
def test_pending_authority_invalidated_by_controls_connection_or_disable(env,change):
    f,clock,rng,scheduler=env
    row,policy=pending(env)
    authority=Authority(f.ledger,L.ACCT,L.FAN,policy,clock=lambda:clock[0])
    f.sql(change)
    with pytest.raises(Refused):authority.check()
    with pytest.raises(Refused):claim(env,row,policy)
    assert f.sql("SELECT count(*) FROM native_pilot_inputs")==[(0,)]
    assert f.sql("SELECT count(*) FROM native_pilot_receipts")==[(0,)]


def test_stale_source_refuses_claim_and_keeps_target(env):
    f,clock,rng,scheduler=env
    row,policy=pending(env);old=snapshot(f,clock)
    f.source(101,created_at=_dt(L.NOW+timedelta(seconds=8)),ingested_at=_dt(L.NOW+timedelta(seconds=9)))
    with pytest.raises(Refused,match="source-changed"):
        f.ledger.claim_input(**f.call,snapshot=old)
    assert scheduler.pending(f.pilot,snapshot(f,clock),policy)["target_send_at"]==row["target_send_at"]


def test_stale_regeneration_is_durably_bounded_and_never_redraws(env):
    f,clock,rng,scheduler=env
    row,policy=pending(env)
    for index in range(2):
        key=claim(env,row,policy)
        f.ledger.finish_input(**{**f.call,"now":clock[0]},input_key=key)
        scheduler.settle(row,retry=True)
        if index==0:
            f.source(101,created_at=_dt(L.NOW+timedelta(seconds=8)),ingested_at=_dt(L.NOW+timedelta(seconds=9)))
            assert scheduler.pending(f.pilot,snapshot(f,clock),policy)["target_send_at"]==row["target_send_at"]
    with pytest.raises(Refused,match="regeneration-held"):pending(env)
    assert len(rng.calls)==1


def test_unknown_delivery_stays_held_after_restart_and_disable(env):
    f,clock,rng,scheduler=env
    row,policy=pending(env);key=claim(env,row,policy)
    attempt=f.attempt(key)
    f.ledger.record_unknown(**f.call,attempt_id=attempt.id)
    scheduler.settle(row,retry=True)
    assert f.sql("SELECT state FROM native_reply_schedules")==[("unknown",)]
    assert f.ledger.receipts(pilot_id=f.pilot.pilot_id)[0]["state"]=="delivery_unknown"
    f.sql("UPDATE account_ai_config SET ai_chatter_config_json='{}'")
    assert scheduler.policy(L.ACCT,L.FAN)==Policy()
    assert not f.ledger.requeue_input(**f.call,input_key=key,max_requeues=1)


def test_adopted_pre_wire_claim_keeps_original_target_without_new_random_draw(env):
    f,clock,rng,scheduler=env
    row,policy=pending(env);key=claim(env,row,policy)
    # Stand in for the existing ledger's proven-unsent lease adoption. This
    # scheduler never changes a live claim's state to authorize itself.
    f.sql("UPDATE native_pilot_inputs SET state='requeued:1' WHERE input_key=?",(key,))
    again=scheduler.pending(f.pilot,snapshot(f,clock),policy)
    assert again["state"]=="waiting" and again["target_send_at"]==row["target_send_at"]
    assert again["admissions"]==1 and len(rng.calls)==1


def test_waiting_fan_releases_serial_engine_and_overdue_fan_runs_without_extra_wait(env):
    from native_pilot.service import Service
    from native_pilot import runner
    f,clock,rng,scheduler=env
    args=argparse.Namespace(account=L.ACCT,fan=L.FAN,all_fans=False)
    service=Service(args);service.strict=True
    service.path,service.ledger,service.pilot=f.path,f.ledger,f.pilot
    called=[]
    async def execute(*args,**kwargs):called.append(kwargs);return None
    service.execute=execute
    with patch.object(runner,"now",lambda:clock[0]):
        asyncio.run(service.turn())
        assert called==[]
        assert f.sql("SELECT count(*) FROM native_pilot_inputs")==[(0,)]
        assert f.sql("SELECT count(*) FROM native_pilot_receipts")==[(0,)]
        clock[0]=L.NOW+timedelta(seconds=100)
        asyncio.run(service.turn())
    assert len(called)==1 and len(rng.calls)==0  # Service uses its own RNG, persisted once
    assert called[0]["reply_authority"].policy.pacing


def test_receipt_evidence_uses_confirmed_response_time_not_the_schedule(env):
    f,clock,rng,scheduler=env
    row,policy=pending(env);key=claim(env,row,policy)
    attempt=f.attempt(key)
    f.ledger.record_sent(**{**f.call,"now":L.NOW+timedelta(seconds=110)},attempt_id=attempt.id,
        provider_message_id="200",validated_response={"id":200,"text":"synthetic"},evidence={"verified":True})
    receipts=f.ledger.receipts(pilot_id=f.pilot.pilot_id,input_key=key)
    evidence=scheduler.evidence(row,receipts)
    assert evidence["verified_receipt_latency_s"]==108
    assert evidence["provider_created_latency_s"]==109
    assert evidence["target_error_s"]==48 and evidence["outside_window"]


def test_additive_migration_can_run_twice_and_keeps_populated_evidence(env):
    from native_pilot.pacing_schema import upgrade
    f,clock,rng,scheduler=env;row,_=pending(env)
    upgrade(f.path);upgrade(f.path)
    assert f.sql("SELECT schedule_id FROM native_reply_schedules")==[(row["schedule_id"],)]
