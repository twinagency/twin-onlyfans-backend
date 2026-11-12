"""Step 5.1 through the real signed-webhook intake, supervised scheduler/worker and delivery path (fake model, fake provider).

Ordinary outside activity is context, attributed to the outside, and never our grant, proof, creator fact or writing example.
It never weakens: explicit takeover, fan stop/no-contact, unresolved delivery, freshness, duplicate protection, authentication,
unknown/malformed/conflicting events. One held fan or creator never stops another.

External-tool limit (recorded, not solved): a creator-account tool outside this system does not take our locks. Once OUR wire has
started, an outside reply can land at the same moment and nothing local can prevent it; we only detect it afterwards.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import (OfEngineAccessEvidence, OfEngineApproval, OfEngineDecision, OfEngineOfferGrant,
                                       OfEnginePilotWebhookDigest, OfEngineRequest)
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_runtime.store import Refused
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401
from coexistence_fixture import FAN_A, FAN_B, OPERATOR, build_world, key

OUTSIDE = 'outside_activity_recorded_coexistence'


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


def staff(w, fan, text='hey, it is me, quick note', queued=False):
    w.mid += 1
    w.deliver(w.sent(w.mid, fan, text, queued=queued))
    return w.mid


def evidence_of(w, mid):
    return next(e for e in w.evidence('human_sent') if e.message_id == mid)


def request_status(w, fan=FAN_A):
    return {r.source_message_id: (r.status, r.next_step) for r in w.rows(OfEngineRequest, OfEngineRequest.fan_id == fan)}


# --------------------------------------------------------------------------------------------- default stays a takeover
def test_without_the_setting_an_outside_reply_still_requests_takeover(make):
    w = make()
    w.launch(coexist=False)
    mid = staff(w, FAN_A)
    ev = evidence_of(w, mid)
    assert (ev.status, ev.reason) == ('applied', 'takeover_requested')
    assert w.state(FAN_A)[0] == 'human_controlled'
    assert w.posts() == []


# --------------------------------------------------------------------------------------------- ordinary reply = context
def test_with_the_setting_an_outside_reply_is_context_attributed_to_the_outside_and_grants_nothing(make):
    w = make()
    w.launch(coexist=True)
    mid = staff(w, FAN_A, 'I sent you my favourite song, hope you like it')
    ev = evidence_of(w, mid)
    assert (ev.status, ev.reason) == ('applied', OUTSIDE) and ev.intent_id is None and ev.grant_id is None
    assert w.state(FAN_A) == ('automated', 'none')
    assert w.block(FAN_A) is None and w.block(FAN_B) is None
    w.tick()                                                           # the recorded message reaches canonical history
    assert w.message(mid) == ('out', 'I sent you my favourite song, hope you like it')
    # outside-engine: no engine intent, no offer grant, no access evidence, no purchase fact came from it
    assert w.rows(OfTextIntent) == [] and w.rows(OfEngineOfferGrant) == [] and w.rows(OfEngineAccessEvidence) == []
    # and the writer is told honestly who it was
    w.say('thanks, that is lovely')
    prompt = w.model_prompts[-1]
    assert 'sent outside this system' in prompt and 'I sent you my favourite song' in prompt
    assert 'HER: I sent you my favourite song' not in prompt           # never presented as her own engine-written line


def test_the_outside_text_is_never_offered_back_as_an_approved_writing_example(make):
    w = make()
    w.launch(coexist=True)
    staff(w, FAN_A, 'tonight I am thinking about you, no pictures though')
    w.tick()
    w.say('what are you doing tonight')
    prompt = w.model_prompts[-1]
    assert 'APPROVED EXAMPLES OF HER VOICE' not in prompt


def test_an_outside_paid_card_is_recorded_with_the_provider_price_and_is_never_called_free_or_ours(make):
    w = make()
    w.launch(coexist=True)
    w.mid += 1
    card = w.sent(w.mid, FAN_A, 'something for you')
    card['payload'].update(price=7, media=[{'id': 777}])
    w.deliver(card)
    w.tick()
    with Session(w.database[1]) as s:
        from db.models import Message
        row = s.get(Message, (w.account, FAN_A, w.mid))
        assert (row.direction, row.price_cents, row.media_count) == ('out', 700, 1)
    w.say('what is this?')
    prompt = w.model_prompts[-1]
    assert 'sent outside this system; price, contents and access unknown' in prompt and 'free media' not in prompt
    assert w.rows(OfTextIntent) == [] and w.rows(OfEngineOfferGrant) == [], 'a staff card is never our card, grant or proof'


def test_an_outside_send_that_cannot_be_recorded_honestly_is_held_for_that_fan_only(make):
    w = make()
    w.launch(coexist=True)
    w.mid += 1
    card = w.sent(w.mid, FAN_A, 'broken price')
    card['payload']['price'] = 'seven dollars'
    w.deliver(card)
    ev = evidence_of(w, w.mid)
    assert (ev.status, ev.reason) == ('held', 'outside_send_unrecordable')
    assert w.block(FAN_A) == 'pilot_evidence_pending' and w.block(FAN_B) is None


# --------------------------------------------------------------------------------------------- before our wire
def test_an_outside_reply_before_our_wire_cancels_the_stale_draft_and_replans_from_current_context(make):
    w = make()
    w.launch(coexist=True)
    staged, mid = w.say('hello there')
    assert staged['outcome'] == 'awaiting_approval' and w.posts() == []
    first = w.approvals()[-1]
    staff_mid = staff(w, FAN_A, 'Hey it is me, I will answer this one, give me a minute')
    old = [a for a in w.approvals() if (a.generation, a.version) == (first.generation, first.version)][0]
    assert old.status == 'invalidated' and old.invalidation_reason == 'outside_activity'
    assert w.posts() == []
    w.tick(2)                                                          # the scheduler replans: new generation, new draft
    approvals = w.approvals()
    assert approvals[-1].generation == first.generation + 1 and approvals[-1].status == 'awaiting'
    decisions = w.rows(OfEngineDecision, OfEngineDecision.fan_id == FAN_A)
    assert {d.generation: d.status for d in decisions} == {first.generation: 'cancelled', first.generation + 1: 'ready'}
    assert 'Hey it is me, I will answer this one' in w.model_prompts[-1] and 'sent outside this system' in w.model_prompts[-1]
    w.approve_pending()
    w.tick()
    assert len(w.posts()) == 1                                         # exactly one send: the replanned one
    assert w.message(staff_mid) is not None


def test_an_already_approved_but_unsent_draft_is_never_sent_after_an_outside_reply(make):
    w = make()
    w.launch(coexist=True)
    staged, mid = w.say('hello there')
    w.approve(staged)                                                  # approved, not yet on the wire
    staff(w, FAN_A, 'taking this one myself, hold on')
    w.tick(1)
    assert w.posts() == [], 'the stale approved draft must not reach the wire'
    intents = w.rows(OfTextIntent)
    assert all(i.status in ('cancelled',) for i in intents)
    w.tick(1)
    assert w.approvals()[-1].status == 'awaiting' and w.approvals()[-1].generation == 1


def test_autonomous_mode_replans_the_same_way_and_sends_exactly_one_reply(make):
    w = make()
    w.launch(coexist=True, authorization_mode='autonomous')
    staged, mid = w.say('hello there')
    assert w.posts() == [] and w.approvals()[-1].status == 'awaiting'
    staff(w, FAN_A, 'I am answering this one myself, one second')
    w.tick(4)
    assert len(w.posts()) == 1, w.posts()                               # the replanned draft only; the stale one never went out
    assert 'I am answering this one myself' in w.model_prompts[-1]
    assert {d.generation: d.status for d in w.rows(OfEngineDecision)} == {0: 'cancelled', 1: 'completed'}


def test_a_correction_before_our_wire_triggers_the_same_replanning(make):
    w = make()
    w.launch(coexist=True)
    staged, mid = w.say('what is your name')
    staff(w, FAN_A, 'sorry, that was my own typo, ignore the last text')
    w.tick(2)
    assert w.approvals()[-1].generation == 1 and w.posts() == []


# --------------------------------------------------------------------------------------------- after our send
def test_an_outside_reply_after_our_send_is_context_only_and_creates_no_duplicate(make):
    w = make()
    w.launch(coexist=True)
    w.converse('hello there')
    assert len(w.posts()) == 1
    before = (len(w.rows(OfEngineDecision)), len(w.rows(OfTextIntent)))
    mid = staff(w, FAN_A, 'and this is the creator adding something later')
    w.tick(3)
    assert len(w.posts()) == 1 and (len(w.rows(OfEngineDecision)), len(w.rows(OfTextIntent))) == before
    assert (evidence_of(w, mid).status, evidence_of(w, mid).reason) == ('applied', OUTSIDE)
    assert w.state(FAN_A) == ('automated', 'none') and w.message(mid) is not None
    assert request_status(w)[w.mid - 1][0] == 'completed'


# --------------------------------------------------------------------------------------------- controls win
def test_explicit_takeover_wins_over_coexistence(make):
    w = make()
    w.launch(coexist=True)
    with Session(w.database[1]) as s:
        from db.models import OfConversationState
        revision = s.get(OfConversationState, (w.account, FAN_A)).revision
    w.store.control(TENANT, w.account, FAN_A, mode='human_controlled', expected_revision=revision, command_key='takeover-explicit',
                    actor=OPERATOR, reason='operator takes the chat', action='request_takeover')
    mid = staff(w, FAN_A)
    assert (evidence_of(w, mid).status, evidence_of(w, mid).reason) == ('applied', 'already_human')
    assert w.state(FAN_A)[0] == 'human_controlled'
    result, _ = w.say('are you there?')
    w.tick(2)
    assert w.posts() == [] and w.approvals() == []                    # nothing drafted for a human-controlled chat


def test_fan_stop_wins_over_coexistence_and_an_outside_reply_does_not_reopen_it(make):
    w = make()
    w.launch(coexist=True)
    w.say('please stop texting me')
    assert w.state(FAN_A)[0] == 'no_contact'
    mid = staff(w, FAN_A, 'sorry about that, it will stop')
    w.tick(3)
    assert w.state(FAN_A)[0] == 'no_contact' and w.posts() == []
    assert w.approvals() == []
    assert (evidence_of(w, mid).status) == 'applied'                  # recorded as context; the stop is untouched


def test_a_control_change_does_not_revive_an_old_backlog(make):
    w = make()
    w.launch(coexist=True)
    w.say('hello there')                                              # staged, never approved
    with Session(w.database[1]) as s:
        from db.models import OfConversationState
        revision = s.get(OfConversationState, (w.account, FAN_A)).revision
    w.store.control(TENANT, w.account, FAN_A, mode='human_controlled', expected_revision=revision, command_key='tk-1',
                    actor=OPERATOR, reason='takeover', action='request_takeover')
    with Session(w.database[1]) as s:
        revision = s.get(OfConversationState, (w.account, FAN_A)).revision
    w.store.control(TENANT, w.account, FAN_A, mode='automated', expected_revision=revision, command_key='tk-2',
                    actor=OPERATOR, reason='hand back', action='release_takeover')
    w.tick(4)
    assert w.posts() == []
    assert not [a for a in w.approvals() if a.status in ('awaiting', 'approved')], 'handback must not resurrect the old draft'


# --------------------------------------------------------------------------------------------- queue / broadcast
def test_queue_send_to_an_engine_owned_fan_holds_that_fan_when_coexistence_is_off(make):
    w = make()
    w.launch(coexist=False)
    mid = staff(w, FAN_A, 'weekend blast!', queued=True)
    ev = evidence_of(w, mid)
    assert (ev.status, ev.reason) == ('held', 'queued_send_to_engine_owned_fan')
    assert w.block(FAN_A) == 'pilot_evidence_pending' and w.block(FAN_B) is None
    w.deliver(w.queue())
    assert w.block(FAN_A) == w.block(FAN_B) == 'pilot_evidence_pending'


def test_known_queue_and_broadcast_events_are_context_when_coexistence_is_on(make):
    w = make()
    w.launch(coexist=True)
    mid = staff(w, FAN_A, 'weekend blast!', queued=True)
    ev = evidence_of(w, mid)
    assert ev.status == 'applied' and ev.reason == 'queued_send_outside_context_recorded' and ev.intent_id is None
    w.deliver(w.queue('chat_queue.updated'))
    w.deliver(w.queue('chat_queue.finished'))
    assert w.block(FAN_A) is None and w.block(FAN_B) is None
    w.tick()
    assert w.message(mid) == ('out', 'weekend blast!')                # in context, honestly outside
    with Session(w.database[1]) as s:                                 # retained evidence, not discarded
        assert s.scalar(select(OfEnginePilotWebhookDigest)) is not None
    staged, _ = w.say('hello there')
    assert staged['outcome'] == 'awaiting_approval'
    w.say('hey are you there', FAN_B)
    assert len([a for a in w.approvals(FAN_A) if a.status == 'awaiting']) == 1
    assert len([a for a in w.approvals(FAN_B) if a.status == 'awaiting']) == 1


def test_a_queued_send_to_a_fan_outside_the_audience_stays_ignored_either_way(make):
    w = make()
    w.launch(coexist=True)
    w.mid += 1
    result = w.deliver(w.sent(w.mid, 999001, 'blast to someone else', queued=True))
    assert result['outcome'] == 'outside_pilot_scope'                 # never enters the engine path, never holds anyone
    assert w.block(FAN_A) is None and w.block(FAN_B) is None and w.evidence('human_sent', 999001) == []


# --------------------------------------------------------------------------------------------- unknown / malformed / conflicting / auth stay held
def test_malformed_unknown_conflicting_and_auth_events_stay_held_with_coexistence_on(make):
    w = make()
    w.launch(coexist=True)
    w.deliver({'event': 'chat_queue.updated', 'account_id': w.provider, 'payload': {'queue_id': 1, 'total': -1}}, event_key='bad-queue')
    assert w.block(FAN_A) == w.block(FAN_B) == 'pilot_evidence_pending'


def test_a_conflicting_queue_variant_holds_even_with_coexistence_on(make):
    w = make()
    w.launch(coexist=True)
    a, b = w.queue(), w.queue()
    b['payload']['isCanceled'] = True
    w.deliver(a, event_key='q-conflict')
    assert w.block(FAN_A) is None
    w.deliver(b, event_key='q-conflict')
    assert w.block(FAN_A) == w.block(FAN_B) == 'pilot_evidence_pending'


def test_an_undocumented_event_type_stays_held_with_coexistence_on(make):
    w = make()
    w.launch(coexist=True)
    w.deliver({'event': 'posts.new', 'account_id': w.provider, 'payload': {'id': 77}}, event_key='posts-new')
    assert w.block(FAN_A) == w.block(FAN_B) == 'pilot_evidence_pending'


def test_authentication_events_still_stop_sending_with_coexistence_on(make):
    w = make()
    w.launch(coexist=True)
    with pytest.raises(Refused, match='enabled fansapi'):               # the worker stops itself once the provider says authentication failed
        w.deliver({'event': 'accounts.authentication_failed', 'account_id': w.provider, 'payload': {'onlyfans_id': w.creator}},
                  event_key='auth-1')
    assert w.block(FAN_A) is not None and w.block(FAN_B) is not None
    w.say('hello there')
    assert w.posts() == []


def test_a_malformed_outside_send_event_is_held_not_ignored(make):
    w = make()
    w.launch(coexist=True)
    w.deliver({'event': 'messages.sent', 'account_id': w.provider, 'payload': {'id': 123, 'text': 'x', 'createdAt': '2026-01-01T00:00:00Z'}},
              event_key='no-recipient')
    assert w.block(FAN_A) == w.block(FAN_B) == 'pilot_evidence_pending'      # the recipient is unknown: nothing may proceed unreviewed


# --------------------------------------------------------------------------------------------- isolation
def test_a_hold_on_one_fan_does_not_stop_another_fan_or_another_creator(make):
    a = make(1)
    b = make(2, fans=(411, 412))
    a.launch(coexist=True)
    b.launch(coexist=True)
    # a real hold on fan A: an outside reply we cannot yet classify (within the human grace window)
    a.pilot.worker.intake.grace = timedelta(hours=1)
    mid = staff(a, FAN_A)
    assert evidence_of(a, mid).status == 'pending' and a.block(FAN_A) == 'pilot_evidence_pending'
    assert a.block(FAN_B) is None
    a.say('hello?', FAN_A)
    assert a.approvals(FAN_A) == []                                    # the held fan is not planned for
    ok, _ = a.say('hey are you there', FAN_B)
    assert ok['outcome'] == 'awaiting_approval'
    # an account-wide hold on creator A (unknown event) leaves creator B untouched
    a.deliver({'event': 'posts.new', 'account_id': a.provider, 'payload': {'id': 1}}, event_key='a-unknown')
    assert a.block(FAN_B) == 'pilot_evidence_pending'
    other, _ = b.say('hi there', 411)
    assert other['outcome'] == 'awaiting_approval'
    assert b.block(411) is None and b.block(412) is None
