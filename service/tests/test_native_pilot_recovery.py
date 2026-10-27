"""Expired-owner recovery never converts uncertain sends into replay permission."""
from datetime import timedelta
import pytest
from test_native_pilot_ledger import Fixture, NOW, ACCT, FAN, OWNER
from native_pilot.ledger import Ledger, Refused

NEW = "synthetic-recovery-owner-0002"
PID = "svc-recovery"


def prepared(tmp_path):
    f = Fixture(tmp_path, "recovery")
    led = Ledger(f.path)
    pilot = led.activate_service(pilot_id=PID, account_id=ACCT, fan_id=FAN,
        owner_token=OWNER, now=NOW, ttl_s=20)
    f.source()
    call = dict(pilot_id=PID, owner_token=OWNER)
    now = NOW + timedelta(seconds=10)
    key = led.claim_input(**call, snapshot=led.snapshot(**call, now=now), now=now)
    led.reserve_model_call(**call, now=now)
    return f, led, pilot, call, key, now


def adopt(f):
    led = Ledger(f.path)
    pilot = led.activate_service(pilot_id=PID, account_id=ACCT, fan_id=FAN,
        owner_token=NEW, now=NOW + timedelta(seconds=30), ttl_s=20)
    return led, pilot


@pytest.mark.parametrize("receipt", [False, True])
def test_recovery_keeps_counts_and_receipts_and_fences_old_owner(tmp_path, receipt):
    f, led, pilot, call, key, now = prepared(tmp_path)
    if receipt:
        attempt = led.begin_attempt(**call, input_key=key, bubble_no=1, revision=pilot.revision, now=now)
        led.record_not_sent(**call, attempt_id=attempt.id, now=now)
    before = f.sql("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0]
    new, adopted = adopt(f)
    args = dict(pilot_id=PID, owner_token=NEW, now=NOW + timedelta(seconds=31))
    assert adopted.revision > pilot.revision
    assert new.claim_input(**args, snapshot=new.snapshot(**args)) == key
    assert f.sql("SELECT model_calls FROM native_pilots")[0][0] == 1
    assert f.sql("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0] == before
    assert len(new.receipts(pilot_id=PID)) == int(receipt)
    with pytest.raises(Refused, match="pilot-owner-mismatch"):
        led.claim_input(**call, snapshot=led.snapshot(**call, now=now), now=now)


@pytest.mark.parametrize("corruption", ["native_sent", "native_unknown", "bridge_sent", "bridge_missing", "orphan", "source"])
def test_missing_or_conflicting_proof_cannot_recover(tmp_path, corruption):
    f, led, pilot, call, key, now = prepared(tmp_path)
    if corruption == "source":
        f.sql("UPDATE messages SET body='edited inbound'")
    else:
        attempt = led.begin_attempt(**call, input_key=key, bubble_no=1, revision=pilot.revision, now=now)
        led.record_not_sent(**call, attempt_id=attempt.id, now=now)
        if corruption == "bridge_missing":
            f.sql("DELETE FROM twin_bridge_send_attempts WHERE id=?", (attempt.id,))
        elif corruption == "orphan":
            f.sql("DELETE FROM native_pilot_receipts WHERE attempt_id=?", (attempt.id,))
        elif corruption.startswith("native_"):
            f.sql("UPDATE native_pilot_receipts SET state=? WHERE attempt_id=?",
                ("sent" if corruption == "native_sent" else "delivery_unknown", attempt.id))
        else:
            f.sql("UPDATE twin_bridge_send_attempts SET state='sent' WHERE id=?", (attempt.id,))
    adopt(f)
    assert f.sql("SELECT state FROM native_pilot_inputs WHERE input_key=?", (key,))[0][0] == "stopped"


def test_live_owner_cannot_be_adopted(tmp_path):
    f, led, pilot, call, key, now = prepared(tmp_path)
    with pytest.raises(Refused, match="owner-live"):
        led.activate_service(pilot_id=PID, account_id=ACCT, fan_id=FAN,
            owner_token=NEW, now=now, ttl_s=20)
    assert f.sql("SELECT state FROM native_pilot_inputs")[0][0] == "generating"
