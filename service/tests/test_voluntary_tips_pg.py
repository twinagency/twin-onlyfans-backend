"""Step 5.2: a validated voluntary tip is accounted once and acknowledged once, through the existing intake, evidence, request,
decision and delivery owners. No tip request, no promised reward or discount, no media or access, no fabricated fan message,
no separate sender. Conflicting or insufficient money evidence stays visible and holds only that fan.
"""
import pytest
from sqlalchemy.orm import Session

from conversation_engine import model_output
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import (OfEngineAccessEvidence, OfEngineDecision, OfEngineOfferGrant, OfEnginePilotWebhookDigest,
                                       OfEngineRequest)
from db.models import Message
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401
from coexistence_fixture import FAN_A, FAN_B, TIP_ACK, build_world


@pytest.fixture
def make(database, tmp_path, monkeypatch):
    worlds = []

    def build(i=1, **kw):
        w = build_world(database, tmp_path, monkeypatch, i=i, **kw)
        worlds.append(w)
        return w
    yield build
    for w in worlds:
        w.store.close()


def tips(w, fan=FAN_A):
    return [e for e in w.evidence('tip', fan)]


def acknowledgements(w):
    return [p for p in w.posts() if 'tip' in p['text'].lower()]


# ------------------------------------------------------------------------------------------------ accounted once
def test_a_validated_tip_is_recorded_once_with_no_message_request_or_reply_of_its_own(make):
    w = make()
    w.launch()
    requests_before, messages_before = len(w.rows(OfEngineRequest)), len(w.rows(Message))
    result = w.deliver(w.tip(FAN_A, 'tip-100', gross=5, net=4))
    assert result['outcome'] == 'evidence'
    (row,) = tips(w)
    assert (row.status, row.amount_cents, row.provider_transaction_id, row.fan_id) == ('pending', 500, 'tip-100', FAN_A)
    w.tick(3)
    assert w.posts() == [], 'a tip does not trigger a reply by itself: no fabricated fan message, no tip-sending worker'
    assert len(w.rows(OfEngineRequest)) == requests_before and len(w.rows(Message)) == messages_before
    assert w.block(FAN_A) is None, 'an established tip is not a hold'


def test_the_tip_is_acknowledged_once_through_the_next_real_reply_and_grants_nothing(make):
    w = make()
    w.launch()
    w.deliver(w.tip(FAN_A, 'tip-101', gross=5, net=4))
    staged, mid = w.say('hey you')
    assert staged['outcome'] == 'awaiting_approval'
    assert 'VERIFIED TIP' in w.model_prompts[-1]
    w.approve(staged)
    w.tick()
    assert [p['text'] for p in w.posts()] == [TIP_ACK]
    (post,) = w.posts()
    assert 'price' not in post and 'mediaFiles' not in post, 'no card, media or access came with the thanks'
    (row,) = tips(w)
    assert row.status == 'applied' and row.intent_id is not None and row.peer_id is not None
    assert w.rows(OfEngineOfferGrant) == [] and w.rows(OfEngineAccessEvidence) == []
    intent = w.rows(OfTextIntent)[0]
    assert intent.price_cents == 0 and not intent.catalog_item_id
    # ordinary chat continues and the tip is never thanked again
    staged2, _ = w.say('what are you up to')
    assert 'VERIFIED TIP' not in w.model_prompts[-1]
    w.approve(staged2)
    w.tick()
    assert len(w.posts()) == 2 and len(acknowledgements(w)) == 1


def test_a_tip_is_only_thanked_in_a_draft_that_is_actually_sent_never_double_counted_when_a_draft_is_replanned(make):
    w = make()
    w.launch(coexist=True)
    w.deliver(w.tip(FAN_A, 'tip-102'))
    staged, _ = w.say('hey you')
    # an outside reply makes that first draft stale; the replanned draft carries the one acknowledgement
    w.mid += 1
    w.deliver(w.sent(w.mid, FAN_A, 'quick note from me'))
    w.tick(2)
    w.approve_pending()
    w.tick()
    assert len(acknowledgements(w)) == 1
    (row,) = tips(w)
    assert row.status == 'applied'


# ------------------------------------------------------------------------------------------------ duplicate / key change / replay
def test_duplicate_changed_key_and_replay_never_duplicate_accounting_or_thanks(make):
    w = make()
    w.launch()
    body = w.tip(FAN_A, 'tip-200', gross=10, net=8)
    w.deliver(body, event_key='tip-key-1')
    w.deliver(body, event_key='tip-key-1')                                   # exact redelivery
    w.deliver(body, event_key='tip-key-2')                                   # changed event key, same provider tip identity
    assert len(tips(w)) == 1
    projections = w.projections()
    assert projections['tip-key-1'][0] == 'projected' and projections['tip-key-2'] == ('projected', 'tip_duplicate')
    staged, _ = w.say('hey')
    w.approve(staged)
    w.tick()
    assert len(acknowledgements(w)) == 1
    w.deliver(body, event_key='tip-key-3')                                   # replay AFTER the thanks
    staged, _ = w.say('and again')
    assert 'VERIFIED TIP' not in w.model_prompts[-1]
    w.approve(staged)
    w.tick()
    assert len(tips(w)) == 1 and len(acknowledgements(w)) == 1


def test_two_different_tips_are_two_facts_and_each_is_acknowledged_once(make):
    w = make()
    w.launch()
    w.deliver(w.tip(FAN_A, 'tip-300', gross=5, net=4))
    w.deliver(w.tip(FAN_A, 'tip-301', gross=7, net=6))
    assert len(tips(w)) == 2
    staged, _ = w.say('hello')
    w.approve(staged)
    w.tick()
    assert len(acknowledgements(w)) == 1 and {t.status for t in tips(w)} == {'applied'}, 'one thanks covers the tips shown to the writer'


# ------------------------------------------------------------------------------------------------ conflicting / insufficient
def test_a_conflicting_amount_for_the_same_tip_is_held_visibly_and_holds_only_that_fan(make):
    w = make()
    w.launch()
    w.deliver(w.tip(FAN_A, 'tip-400', gross=5, net=4), event_key='ka')
    result = w.deliver(w.tip(FAN_A, 'tip-400', gross=50, net=40), event_key='kb')
    (row,) = tips(w)
    assert row.amount_cents == 500                                           # the first evidence is never overwritten
    assert w.projections()['kb'] == ('held', 'conflicting_duplicate')
    assert w.block(FAN_A) == 'pilot_evidence_pending' and w.block(FAN_B) is None
    held, _ = w.say('hello', FAN_A)
    assert w.approvals(FAN_A) == []
    other, _ = w.say('hi there', FAN_B)
    assert other['outcome'] == 'awaiting_approval' and 'VERIFIED TIP' not in w.model_prompts[-1]
    w.tick(2)
    assert acknowledgements(w) == []


@pytest.mark.parametrize('change', [
    {'amountGross': None}, {'amountGross': 0}, {'amountGross': -3}, {'amountGross': '5'}, {'amountGross': True},
    {'amountGross': 5, 'amountNet': 9}, {'subType': 'subscription'}, {'subType': None}, {'user_id': '99999'},
    {'currency': 'EUR'}, {'id': ''}, {'id': None}, {'createdAt': 'yesterday'}, {'amountGross': 1e12}])
def test_insufficient_or_inconsistent_money_evidence_creates_no_tip_and_stays_held_for_that_fan(make, change):
    w = make()
    w.launch()
    w.deliver(w.tip(FAN_A, 'tip-500', **change), event_key='bad-tip')
    assert tips(w) == [], 'no accounting from evidence that does not establish a tip'
    with Session(w.database[1]) as s:
        held = [d for d in s.query(OfEnginePilotWebhookDigest).all() if d.event_key == 'bad-tip']
    assert held and all(d.safety_hold for d in held), 'visible and held, never silently dropped'
    assert w.block(FAN_A) == 'pilot_evidence_pending' and w.block(FAN_B) is None
    staged, _ = w.say('hey', FAN_B)
    assert staged['outcome'] == 'awaiting_approval'
    w.tick(2)
    assert acknowledgements(w) == []


def test_a_tip_from_a_fan_outside_the_audience_or_for_another_creator_is_never_accounted(make):
    w = make()
    w.launch()
    w.deliver(w.tip(999777, 'tip-600'), event_key='outside-fan')
    assert tips(w, 999777) == [] and w.block(FAN_A) is None
    other = w.tip(FAN_A, 'tip-601')
    other['account_id'] = 'acct_someone_else'
    w.deliver(other, event_key='other-account')
    assert tips(w) == []


# ------------------------------------------------------------------------------------------------ the writer may not ask or promise
@pytest.mark.parametrize('text', [
    'tip me and I will send more', 'send me a tip babe', 'would you like to leave a tip?', 'spoil me with a tip tonight',
    'a little tip would make my day', 'drop a tip and I will unlock something', 'tips keep me going, hint hint',
    'I would love a tip from you'])
def test_the_writer_never_proactively_asks_for_a_tip(text):
    problems = model_output.tip_problems([text], {})
    assert problems and any('tip' in p for p in problems)


@pytest.mark.parametrize('text', [
    'thank you for the tip, here is a discount on the next one', 'thanks for the tip, I will send you something special',
    'thanks for tipping, you get a free photo for that', 'thank you for the tip, I owe you one', 'that tip earned you a video'])
def test_a_thank_you_may_not_promise_a_reward_discount_or_access(text):
    problems = model_output.tip_problems([text], {'tip_facts': [{'amount_cents': 500}]})
    assert problems


def test_a_plain_thank_you_for_an_established_tip_is_valid_and_a_claim_without_a_tip_is_not():
    facts = {'tip_facts': [{'amount_cents': 500}]}
    assert model_output.tip_problems([TIP_ACK], facts) == []
    assert model_output.tip_problems(['you are so sweet, tell me about your day'], {}) == []
    assert model_output.tip_problems([TIP_ACK], {})                          # no established tip: the claim is invented


def test_a_model_that_asks_for_a_tip_is_never_sent(make):
    w = make(choose=None)
    w.choose = lambda latest, prompt, proposal: {'body': 'send me a tip babe'}
    w.launch()
    staged, _ = w.say('hey')
    w.tick(2)
    assert not any('tip' in p['text'].lower() for p in w.posts())
    assert all('tip' not in d.body.lower() for d in w.rows(OfEngineDecision)), 'the asking draft was rejected, not staged'
