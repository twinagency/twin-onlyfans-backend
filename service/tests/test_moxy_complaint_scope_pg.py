"""Complaint scope, synthetic only: conversational troubleshooting versus remedy authority."""
import json
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfTextIntent
from moxy_replacement.pilot import route_specialist
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from test_moxy_integrated_pg import decision, model
from test_moxy_pilot_conversation_permission_pg import settings
from test_moxy_pilot_pg import pilot
from test_moxy_reply_recovery_pg import add, row
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


@pytest.mark.parametrize('text', [
    'That sounds frustrating. Let me look into it for you.',
    'I can check what happened with the card.',
    'I can check whether a refund is possible.',
])
def test_routine_complaint_reply_is_not_automatically_handed_over(text):
    proposed = decision(text=text, flags=['complaint'])
    assert route_specialist(settings(), proposed) == proposed


@pytest.mark.parametrize('text', [
    "I've refunded you.",
    'I will refund you.',
    'I can reimburse you.',
    "I'll give you a refund.",
    'I processed your refund.',
    'Your refund has been approved.',
    "I'll send you a replacement photo.",
    'I will replace the photo for free.',
    'I will replace the photo.',
])
@pytest.mark.parametrize('flags', [[], ['complaint']])
def test_pilot_remedy_claim_or_promise_still_requires_human(text, flags):
    proposed = decision(text=text, flags=flags)
    routed = route_specialist(settings(), proposed)
    # Invalid draft wording cannot itself transfer the whole conversation.
    # The validator refuses the commitment; any actual remedy remains human-owned.
    assert routed == proposed
    from moxy_replacement.grounding import commitment_problem
    assert commitment_problem({},routed,[text]) == 'unsupported_remedy_commitment'


def test_complaint_does_not_authorize_a_new_sale_or_specialist_service():
    from test_moxy_foundation_authority_regressions import authority_scene
    from moxy_replacement.runtime import validate_decision
    from moxy_replacement.configuration import ActionBlocked
    cfg,snap,_=authority_scene('There is a problem with the card.',no_push=True)
    proposed = decision(action='offer', text='Here is a photo.', product_id='first', flags=['complaint'])
    assert route_specialist(settings(), proposed) == proposed
    with pytest.raises(ActionBlocked,match='no_push'):
        validate_decision(cfg,snap,proposed,planning=True)


def test_an_unrelated_personal_replacement_is_not_a_media_remedy():
    proposed = decision(text='I can replace my gym shoes.')
    assert route_specialist(settings(), proposed) == proposed


def test_a_creator_changing_her_profile_photo_is_not_a_customer_remedy():
    proposed = decision(text='I can replace my profile photo.')
    assert route_specialist(settings(), proposed) == proposed


@pytest.mark.parametrize('action', ['invite', 'offer', 'resend', 'alternative', 'continue'])
def test_unapproved_refund_in_any_visible_part_requires_human_even_without_a_flag(action):
    proposed = decision(action=action, text='Here is the photo.', product_id='p', messages=[
        {'text': 'Here is the photo.', 'media': action != 'invite'},
        {'text': "I'll give you a refund.", 'media': False},
    ])
    routed = route_specialist(settings(), proposed)
    assert routed == proposed
    from moxy_replacement.grounding import commitment_problem
    assert commitment_problem({},routed,[p['text'] for p in proposed['messages']]) == 'unsupported_remedy_commitment'


def test_actual_resend_action_may_announce_its_own_card():
    proposed = decision(action='resend', text="I'll resend your photo now.", product_id='p')
    assert route_specialist(settings(), proposed) == proposed


def test_resend_announcement_does_not_hide_a_separate_refund_promise():
    proposed = decision(action='resend', text="I'll resend it and I'll refund you.", product_id='p')
    assert route_specialist(settings(), proposed) == proposed
    from moxy_replacement.grounding import commitment_problem
    assert commitment_problem({},proposed,[proposed['text']]) == 'unsupported_remedy_commitment'


def test_pilot_offer_with_unapproved_refund_gets_one_correction_without_takeover(database, monkeypatch):
    engine, store, service, authority, _, _ = pilot(database, monkeypatch)
    try:
        identity = add(service, key='refund-in-offer')
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            action='offer', text='Here is the photo.', product_id='first', sequence='fixture_level',
            messages=[{'text': 'Here is the photo.', 'media': True},
                      {'text': "I'll give you a refund.", 'media': False}])))
        saved = row(engine, identity)
        assert saved.status=='held' and saved.reason=='unsupported_remedy_commitment'
        assert json.loads(saved.decision_json)['action'] == 'offer'
        with Session(engine) as session:
            assert session.query(OfTextIntent).count() == 0
            assert session.query(OfOperatorCase).count() == 0
        from test_moxy_reply_recovery_pg import correction,send
        retry=correction(engine)
        service.prepare(TENANT,ACCOUNT,retry,model(decision('offer','Here is the photo.','first',sequence='fixture_level')))
        assert row(engine,retry).status=='ready',row(engine,retry).reason
        result,wire,_=send(service,authority,engine,retry)
        assert result=='accepted' and wire.calls[0].price_cents==7500
    finally:
        store.close()


def test_pilot_resend_announcement_delivers_the_requested_original_card(database, monkeypatch):
    from test_moxy_resend_repair_pg import prior_card, incoming
    engine, store, service, authority, _, _ = pilot(database, monkeypatch)
    try:
        prior_card(engine, service, authority)
        incoming(store, 'Send it again.')
        identity = add(service, key='requested-resend', mid=2)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            action='resend', text="I'll resend your photo now.", product_id='first', sequence='fixture_level',
            request_kind='resend', request_quote='Send it again.')))
        assert row(engine, identity).status == 'ready', row(engine, identity).reason
        from test_moxy_reply_recovery_pg import send
        outcome, wire, _ = send(service, authority, engine, identity, mid=9002)
        assert outcome == 'accepted' and wire.calls[0].media_ids == (7101,)
        assert wire.calls[0].price_cents == 7500
    finally:
        store.close()


def test_recorded_complaint_can_be_acknowledged_without_unauthorized_remedy(database, monkeypatch):
    engine, store, service, authority, _, _ = pilot(database, monkeypatch)
    try:
        store.record(TENANT, ACCOUNT, event(mid=2, body="I paid for the photo but it didn't open."))
        store.drain(TENANT, ACCOUNT)
        identity = add(service, key='complaint', mid=2)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            text='That sounds frustrating. Let me look into it for you.', flags=['complaint'])))
        assert row(engine, identity).status == 'ready', row(engine, identity).reason
        with Session(engine) as session:
            cases = session.scalars(select(OfOperatorCase)).all()
            assert all(case.scope != 'all' for case in cases)
        intent = service.queue(TENANT, ACCOUNT, identity)
        transport = SyntheticTextTransport(ACCOUNT)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        assert len(transport.calls) == 1 and not transport.calls[0].media_ids
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    finally:
        store.close()
