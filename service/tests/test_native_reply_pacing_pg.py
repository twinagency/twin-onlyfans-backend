"""Run the timing races and authority fences on actual PostgreSQL storage."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed
from db.models import AccountAiConfig
from db.onlyfans_connection_models import OfProviderConnection
from native_pilot.ledger import Ledger, _dt
from native_pilot import pacing, pacing_schema
from twin_bridge import store
import test_native_pilot_ledger as L
from test_native_reply_pacing import (
    test_clean_stop_before_send_keeps_schedule_waiting_and_fan_is_answered_after_restart,
    test_done_schedule_whose_unsent_input_was_dropped_is_recovered,
    test_sent_reply_still_closes_schedule,
    test_completed_unsent_input_does_not_leave_a_spent_schedule_holding_the_next_message,
    test_sent_input_schedule_is_still_closed_and_unfinished_input_keeps_it,
    test_one_target_survives_restart_and_changed_caption,
    test_burst_cannot_reset_target_or_debounce,
    test_two_workers_create_only_one_target,
    test_changed_authority_refuses_before_spending,
    test_wire_recheck_catches_disable_after_attempt,
    test_stale_input_preserves_target_and_new_message,
    test_regeneration_budget_does_not_claim_new_input,
    test_delivery_metric_uses_receipt_not_target,
    test_confirmed_active_exchange_uses_shorter_mode_and_measured_allowance,
)


class Fixture(L.Fixture):
    def __init__(self,url):
        self.path=url
        self.ledger=Ledger(url)
        self.args=dict(pilot_id='synthetic-test',account_id=L.ACCT,fan_id=L.FAN,owner_token=L.OWNER,
                       cutoff=L.NOW,deadline=L.NOW+timedelta(minutes=30),limit=10,now=L.NOW)
        self.call=dict(pilot_id='synthetic-test',owner_token=L.OWNER,now=L.NOW+timedelta(seconds=10))

    def sql(self,sql,args=()):
        with store.transaction(self.path) as con:
            return con.execute(sql.replace('enabled=0','enabled=false'),args).fetchall()

    def source(self,mid=100,**kw):
        defaults=dict(sender_name='Synthetic Fan',is_tip=False,is_unsent=False,is_promise=False)
        defaults.update(kw)
        return super().source(mid,**defaults)


@pytest.fixture
def f(database):
    url,engine=database
    seed(url,engine,account=L.ACCT,fan=L.FAN)
    store.ensure_runtime_schema(url)
    with Session(engine) as s,s.begin():
        s.add(AccountAiConfig(account_id=L.ACCT,ai_chatter_config_json=json.dumps({pacing.PACING:True})))
        s.add(OfProviderConnection(account_id=L.ACCT,binding_id='binding-'+L.ACCT,owner_id=int(L.ACCT),
            revision=1,sealed_config='synthetic-unused',expires_at=L.NOW+timedelta(days=1),status='verified',
            verified_at=L.NOW,updated_at=L.NOW,automate_new_conversations=False,automation_starts_at=L.NOW))
        s.execute(text('UPDATE of_runtime SET activated_at=:now,configuration_revision=1'),{'now':L.NOW})
        s.execute(text('UPDATE of_conversation_state SET revision=1'))
    f=Fixture(url)
    f.sql("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,scope_fingerprint,"
          "state,revision,reserved,send_budget_limit,send_budget_used,updated_at) "
          "VALUES(?,?,'synthetic','synthetic','synthetic','active',4,true,3,3,?)",(L.ACCT,L.FAN,_dt(L.NOW)))
    f.activate()
    pacing_schema.upgrade(url)
    f.source()
    yield f
    store.dispose_pools()


def test_pg_schema_rollback_retains_pending_target_and_receipt(f):
    from sqlalchemy import create_engine
    from test_native_reply_pacing import prepare
    decision,_,_=prepare(f)
    key=f.claim();attempt=f.attempt(key);f.sent(attempt,987)
    before=f.sql('SELECT schedule_key,target_at,state FROM native_reply_pacing')
    receipts=f.sql('SELECT attempt_id,state,provider_message_id FROM native_pilot_receipts')
    assert receipts and before
    pacing_schema.downgrade(f.path)
    pacing_schema.upgrade(f.path)
    assert f.sql('SELECT schedule_key,target_at,state FROM native_reply_pacing')==before
    assert f.sql('SELECT attempt_id,state,provider_message_id FROM native_pilot_receipts')==receipts
    engine=create_engine(f.path)
    with Session(engine) as session,session.begin():pacing.invalidate_settings(session,L.ACCT)
    assert f.sql('SELECT state FROM native_reply_pacing')[0][0]=='invalidated'
    assert f.sql('SELECT attempt_id,state,provider_message_id FROM native_pilot_receipts')==receipts
    engine.dispose()
