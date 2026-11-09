"""The approved pilot cap applies across UTC days, including verified prior costs."""
from datetime import datetime, timedelta, timezone
import pytest
from test_plan_c_model_billing_pg import database, world, configured_draft, supervised, billing_pilot, run
from test_plan_c_supervised_pg import stage
from test_onlyfans_text_delivery_pg import ACCOUNT


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_prior_pilot_spend_still_blocks_another_fan_after_midnight(supervised, monkeypatch):
    metadata={"data":{"id":"gen-offline-one","model":"m/primary","total_cost":1000001,
                      "finish_reason":"error"}}
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,metadata=metadata)
    ledger=pilot.worker.engine.model.ledger
    result=run(pilot,args)
    assert result["actualMicrousd"] > ledger.config.creator_cap_microusd
    tomorrow=day+timedelta(days=1)
    monkeypatch.setattr(ledger,"clock",lambda:datetime.combine(tomorrow,datetime.min.time()))
    assert ledger.committed(day=tomorrow,account=ACCOUNT)==0
    stage(pilot,18501,"hello from the second fan",fan=102)
    assert len(posts)==1 and world.fans.posts==[]
