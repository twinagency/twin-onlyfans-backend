"""The engagement origin through the real TextAuthority (prepare, admit, consume, receipt), plus worker glue."""
import pytest
from sqlalchemy.orm import Session

from test_onlyfans_launch_engagement_pg import (rig, schema_template, database, schedule, ACCOUNT, FAN, TENANT)  # noqa: F401
from db.models import LadderQuote, Message, OfConversationState, OfMessageDelivery
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.launch_engagement.models import Action
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority


def revision(engine):
    with Session(engine) as s:
        return s.get(OfConversationState, (ACCOUNT, FAN)).revision


def prepare(authority, engine, item, proposal, **changes):
    values = dict(command_key=proposal["command_key"], expected_revision=revision(engine), body=proposal["body"],
                  origin="engagement", actor=proposal["actor"], catalog_item_id=item,
                  engagement_action_id=proposal["action_id"])
    values.update(changes)
    return authority.prepare(TENANT, ACCOUNT, FAN, **values)


def test_engagement_offer_prepares_atomically_dispatches_and_records_ai_receipt(rig):
    service, engine, item, _ = rig
    action = schedule(rig)
    proposal = service.due(TENANT, ACCOUNT)
    authority = TextAuthority(service.store)
    intent = prepare(authority, engine, item, proposal)
    assert prepare(authority, engine, item, proposal) == intent, "idempotent retry returns the linked intent"
    with Session(engine) as s:
        row, stored = s.get(Action, (ACCOUNT, action)), s.get(OfTextIntent, (ACCOUNT, intent))
        assert row.intent_id == intent and row.status == "queued"
        assert stored.origin == "engagement" and stored.expires_at <= row.expires_at
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == "accepted"
    with Session(engine) as s:
        message = s.get(Message, (ACCOUNT, FAN, 9001))
        assert message.automation_kind == "onlyfans_engagement" and message.price_cents == 1000
        assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001)).origin == "ai"
        assert s.query(LadderQuote).filter_by(account_id=ACCOUNT, fan_id=FAN, kind="fixed").count() == 1


@pytest.mark.parametrize("changes", [{"engagement_action_id": None}, {"origin": "campaign"}, {"body": "Changed text"},
                                     {"catalog_item_id": None}])
def test_engagement_prepare_refuses_mismatched_or_missing_action(rig, changes):
    service, engine, item, _ = rig
    schedule(rig)
    proposal = service.due(TENANT, ACCOUNT)
    with pytest.raises(Refused):
        prepare(TextAuthority(service.store), engine, item, proposal, **changes)
    with Session(engine) as s:
        assert s.query(OfTextIntent).count() == 0, "a refused prepare leaves neither intent nor link"


def test_cancelled_engagement_is_refused_before_the_wire(rig):
    service, engine, item, _ = rig
    action = schedule(rig)
    proposal = service.due(TENANT, ACCOUNT)
    authority = TextAuthority(service.store)
    intent = prepare(authority, engine, item, proposal)
    service.cancel(TENANT, ACCOUNT, action, actor="synthetic-operator", reason="operator_cancelled")
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == "cancelled"
    with Session(engine) as s:
        assert s.query(Message).filter_by(account_id=ACCOUNT, direction="out", message_id=9001).count() == 0
