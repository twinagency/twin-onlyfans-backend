"""Native composer, retained cost, actual local HTTP, receipt and canonical history."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from test_onlyfans_loopback_transport_pg import peer_server, transport_for
from db.models import Message, GrokCall, GrokDailyCost
from db.onlyfans_delivery_models import OfNativePlan, OfTextIntent
from onlyfans_runtime.native_planning import SyntheticNativeModel


def test_native_cost_evidence_and_one_use_http_receipt_share_one_history(database, monkeypatch):
    _, engine = database
    store, authority, claim, planner = ready(database, monkeypatch)
    model = SyntheticNativeModel("Thanks for asking. How has your day been?",reported_cost_millicents=7)
    plan_id = planner.prepare(TENANT,claim,model)
    intent_id = planner.queue(TENANT,ACCOUNT,plan_id)
    with Session(engine) as s:
        assert list(s.scalars(select(Message).where(Message.direction == "out"))) == []
    def inspect_wire(record):
        with Session(engine) as s:
            plan = s.get(OfNativePlan,(ACCOUNT,plan_id))
            intent = s.get(OfTextIntent,(ACCOUNT,intent_id))
            call = s.get(GrokCall,plan.model_call_id)
            assert plan.status == "queued" and call.status == "done" and call.cost_cents == 7
            assert intent.wire_started_at is not None and intent.status == "dispatching"
            assert list(s.scalars(select(Message).where(Message.direction == "out"))) == []
            assert record["json"]["payload"]["text"] == model.body
    with peer_server(on_request=inspect_wire) as peer:
        transport = transport_for(peer)
        assert authority.dispatch(TENANT,ACCOUNT,intent_id,transport) == "accepted"
        assert authority.dispatch(TENANT,ACCOUNT,intent_id,transport) == "accepted"
        assert len(peer.records) == 1 and len(model.calls) == 1
    with Session(engine) as s:
        outbound = list(s.scalars(select(Message).where(Message.direction == "out")))
        assert len(outbound) == 1 and outbound[0].body == model.body and outbound[0].message_id == 9001
        cost = s.scalar(select(GrokDailyCost))
        assert (cost.cost_cents,cost.call_count) == (7,1)
        assert s.get(OfTextIntent,(ACCOUNT,intent_id)).projected_at is not None
    store.close()
