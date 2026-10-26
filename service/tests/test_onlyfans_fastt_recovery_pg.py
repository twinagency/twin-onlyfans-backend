"""Expired FASTT inputs recover only with current controls and two consistent ledgers."""
from datetime import timedelta
import pytest
from test_fastt_controls_pg import database, template
from test_onlyfans_fastt_takeover_pg import prepared, takeover, ACCOUNT, FAN, OWNER
from native_pilot.ledger import Ledger, Refused
from twin_bridge import store as bridge

NEW="synthetic-replacement-owner-0002"


@pytest.mark.parametrize("case",["prewire","not_sent","native_unknown","bridge_sent","missing_bridge","orphan","edited","takeover"])
def test_expired_input_recovery_respects_retained_proof(database,case):
    store,authority,ledger,pilot,args=prepared(database,service=True)
    try:
        now=args["now"]
        ledger.count_model_call(pilot_id=pilot.pilot_id,owner_token=OWNER,now=now)
        if case not in ("prewire","edited","takeover"):
            attempt=ledger.begin_attempt(**args)
            ledger.record_not_sent(pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=attempt.id,now=now)
            with bridge.transaction(database[0]) as c:
                if case=="native_unknown":
                    c.execute("UPDATE native_pilot_receipts SET state='delivery_unknown' WHERE attempt_id=?",(attempt.id,))
                elif case=="bridge_sent":
                    c.execute("UPDATE twin_bridge_send_attempts SET state='sent' WHERE id=?",(attempt.id,))
                elif case=="missing_bridge":
                    c.execute("DELETE FROM twin_bridge_send_attempts WHERE id=?",(attempt.id,))
                elif case=="orphan":
                    c.execute("DELETE FROM native_pilot_receipts WHERE attempt_id=?",(attempt.id,))
        elif case=="edited":
            with bridge.transaction(database[0]) as c:
                c.execute("UPDATE messages SET body='edited synthetic input' WHERE account_id=? AND fan_id=?",(ACCOUNT,FAN))
        elif case=="takeover":
            takeover(store)
        with bridge.readonly(database[0]) as c:
            before=tuple(c.execute("SELECT used_units,limit_units FROM of_delivery_policy WHERE account_id=?",(ACCOUNT,)).fetchone())
            budget=tuple(c.execute("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations "
                "WHERE account_id=? AND fan_id=?",(ACCOUNT,FAN)).fetchone())
        new=Ledger(database[0],allowance=True)
        adopted=new.activate_service(pilot_id=pilot.pilot_id,account_id=ACCOUNT,fan_id=FAN,
            owner_token=NEW,now=now+timedelta(seconds=30),ttl_s=20)
        with bridge.readonly(database[0]) as c:
            state=c.execute("SELECT state FROM native_pilot_inputs WHERE input_key=?",(args["input_key"],)).fetchone()[0]
            assert state==("requeued:1" if case in ("prewire","not_sent") else "stopped")
            assert c.execute("SELECT model_calls FROM native_pilots WHERE pilot_id=?",(pilot.pilot_id,)).fetchone()[0]==1
            assert tuple(c.execute("SELECT used_units,limit_units FROM of_delivery_policy WHERE account_id=?",(ACCOUNT,)).fetchone())==before
            assert tuple(c.execute("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations "
                "WHERE account_id=? AND fan_id=?",(ACCOUNT,FAN)).fetchone())==budget
        assert adopted.revision>pilot.revision
        with pytest.raises(Refused,match="pilot-owner-mismatch"):
            ledger.count_model_call(pilot_id=pilot.pilot_id,owner_token=OWNER,now=now)
        if case in ("prewire","not_sent"):
            call=dict(pilot_id=pilot.pilot_id,owner_token=NEW,now=now+timedelta(seconds=31))
            assert new.claim_input(**call,snapshot=new.snapshot(**call))==args["input_key"]
    finally:
        store.close()
