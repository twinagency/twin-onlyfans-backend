"""The supervised approval gate for 'fansapi' accounts (Plan C), offline.

Real worker, real database, real TextAuthority and real transport against httpx.MockTransport; the model is the
in-process fixture. Nothing here approves automatically: every approval is an explicit call with a named operator,
and the worker fixture's `operator_on` helper is switched off so the worker's own behaviour is what is observed.
"""
import ast
import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import world, inbound, posts_text, SECRET, PROVIDER, CREATOR, FAN2  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from approval_helpers import queue_decision
from conversation_engine import approval
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal, offer_body
from conversation_engine.journey import JourneyConfig, configure_journey
from conversation_engine.real_worker import RealConversationWorker
from db.models import CatalogItem, OfConversationState, VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfEngineOfferGrant, OfEngineOwner, OfEngineRequest
from fans_api_adapter.events import FansEventIntake
from fans_api_adapter.real_transport import FansApiRealTransport, RealFansConfig, build_client
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority

OP = 'operator-jane'


# ------------------------------------------------------------------ helpers
def stage(world, reply=None, say='hello there', parts=None):
    """The fan writes; the worker stages the draft. Returns (staged result, source message id)."""
    world.operator_on = False
    if parts:
        world.reply(lambda snap: SyntheticProposal(reply or 'one', parts=tuple(parts)))
    else:
        world.say_plain(reply or 'Hey you, glad you wrote')
    result, mid = world.say(say)
    assert result['outcome'] == 'awaiting_approval', result
    return result, mid


def ok(world, staged, actor=OP):
    a = staged['approval']
    return approval.approve(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=a['version'], actor=actor)


def approvals(world):
    return sorted(world.rows(OfEngineApproval), key=lambda r: (r.generation, r.version))


def restaged(view):
    """The staged-result shape for an edited version (its decisions are a new generation)."""
    return {'decisionId': view['parts'][0]['decision_id'], 'approval': view}


def late(world, mid, body, seconds=1):
    """A webhook the engine has accepted but not yet applied to the transcript."""
    raw = json.dumps(inbound(mid, body, seconds=seconds), separators=(',', ':')).encode()
    world.intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, f'evt-late-{mid}')


def fresh_worker(world):
    """A restarted process: new service objects over the same database."""
    client = build_client(RealFansConfig('fixture-token', PROVIDER, 'https://api.fansapi.com', 5), creator_id=CREATOR,
                          epoch=1, transport=world.fans.transport)
    transport = FansApiRealTransport(world.store, TENANT, ACCOUNT, client, allowed_chats=frozenset({str(FAN), str(FAN2)}),
                                     sleep=lambda s: None, enabled=True)
    engine = EngineService(world.store, SyntheticModel({}))
    intake = FansEventIntake(world.store, human_grace_seconds=0, mismatch_settle_seconds=0)
    return RealConversationWorker(engine, intake, transport, sleep=lambda sec: world.no_throttle())


def offer_draft(world, lead='Here you go'):
    world.operator_on = False
    world.backfilled(FAN)
    grant = world.grant()
    world.reply(lambda snap: SyntheticProposal(lead, action='offer', offer_grant_id=grant))
    staged, mid = world.say('show me the set')
    assert staged['outcome'] == 'awaiting_approval', staged
    return staged, grant


def send(world, staged):
    world.no_throttle()
    return world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])


# ------------------------------------------------------------------ staging
def test_turn_stages_a_validated_draft_and_sends_nothing(world):
    staged, mid = stage(world)
    assert world.fans.posts == [] and world.rows(OfTextIntent) == []
    rows = approvals(world)
    assert [(r.status, r.version, r.generation, r.created_by, r.actor) for r in rows] == [('awaiting', 1, 0, 'engine', None)]
    parts = json.loads(rows[0].parts_json)
    assert [(p['part_index'], p['action'], p['body']) for p in parts] == [(0, 'reply', 'Hey you, glad you wrote')]
    assert len(rows[0].draft_hash) == 64 and rows[0].source_message_id == mid
    assert [d.status for d in world.rows(OfEngineDecision)] == ['ready']
    # restaging (duplicate webhook / second turn) returns the same version, never a second one
    again = world.turn(TENANT, ACCOUNT, FAN, mid)
    assert again['outcome'] == 'awaiting_approval' and len(approvals(world)) == 1 and world.fans.posts == []


def test_nothing_below_the_worker_can_send_an_unapproved_draft(world):
    staged, mid = stage(world)
    decision = staged['decisionId']
    with pytest.raises(Refused, match='approval_required'):
        world.engine.queue(TENANT, ACCOUNT, decision)                    # EngineService.queue refuses
    with Session(world.database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    with pytest.raises(Refused, match='engine approval: approval_required'):    # and so does TextAuthority itself
        TextAuthority(world.store).prepare(TENANT, ACCOUNT, FAN, command_key='engine:' + decision, expected_revision=revision,
            body='Hey you, glad you wrote', origin='automated', actor='offline-engine', engine_decision_id=decision)
    result = send(world, staged)
    assert result['outcome'] == 'not_authorized:approval_required'
    assert world.fans.posts == [] and world.rows(OfTextIntent) == []
    assert [r.status for r in approvals(world)] == ['awaiting']          # a refused send does not cancel the draft


def test_every_live_part_needs_the_approval_and_text_authority_admission(world):
    staged, mid = stage(world, 'one', parts=('two', 'three'))
    seen = []
    world.fans.hooks.append(lambda n: seen.append([r.status for r in approvals(world)]))
    ok(world, staged)
    result = send(world, staged)
    assert result['outcome'] == 'accepted' and posts_text(world) == ['one', 'two', 'three']
    assert seen == [['approved']] * 3                                    # approved during every part, not before or after
    assert [r.status for r in approvals(world)] == ['consumed']          # used up only after the last part settled
    intents = world.rows(OfTextIntent)
    assert len(intents) == 3 and {i.status for i in intents} == {'accepted'}
    assert {d.status for d in world.rows(OfEngineDecision)} == {'completed'}
    assert send(world, staged)['outcome'] == 'accepted' and len(world.fans.posts) == 3     # repeating sends nothing


def test_withdrawing_the_approval_after_queueing_stops_the_wire(world):
    staged, mid = stage(world)
    ok(world, staged)
    intent = world.engine.queue(TENANT, ACCOUNT, staged['decisionId'])
    assert approval.invalidate_for_change(world.store, TENANT, ACCOUNT, FAN, 'operator_withdrew') == 1
    world.no_throttle()
    outcome = world.worker.authority.dispatch(TENANT, ACCOUNT, intent, world.transport)    # admission re-checks the approval
    assert outcome in {'cancelled', 'refused'} and world.fans.posts == []
    assert world.rows(OfTextIntent)[0].status == 'cancelled'
    assert world.rows(OfEngineDecision)[0].status == 'cancelled'


# ------------------------------------------------------------------ approve / duplicate / actor
def test_duplicate_approval_click_is_idempotent_and_one_send(world):
    staged, mid = stage(world)
    first = ok(world, staged, 'operator-jane')
    second = ok(world, staged, 'operator-bob')
    assert first['duplicate'] is False and second['duplicate'] is True and second['actor'] == 'operator-jane'
    assert len(approvals(world)) == 1
    assert send(world, staged)['outcome'] == 'accepted' and send(world, staged)['outcome'] == 'accepted'
    assert len(world.fans.posts) == 1 and len(world.rows(OfTextIntent)) == 1


@pytest.mark.parametrize('actor', ['engine', 'offline-engine', 'auto-approver', 'system', '', ' '])
def test_automated_or_blank_actors_cannot_approve(world, actor):
    staged, mid = stage(world)
    with pytest.raises(Refused, match='named human'):
        ok(world, staged, actor)
    assert [r.status for r in approvals(world)] == ['awaiting']


def test_approval_needs_the_current_version(world):
    staged, mid = stage(world)
    a = staged['approval']
    with pytest.raises(Refused, match='version changed'):
        approval.approve(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=2, actor=OP)


# ------------------------------------------------------------------ edit / reject
def test_edit_creates_a_new_version_and_invalidates_the_approved_one(world):
    staged, mid = stage(world, 'one', parts=('two',))
    ok(world, staged)
    a = staged['approval']
    v2 = approval.edit(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP, parts=['uno', 'dos'])
    assert (v2['version'], v2['status'], v2['generation']) == (2, 'awaiting', 1) and [p['body'] for p in v2['parts']] == ['uno', 'dos']
    rows = approvals(world)
    assert [(r.version, r.status, r.invalidation_reason) for r in rows] == [(1, 'invalidated', 'edited'), (2, 'awaiting', None)]
    assert json.loads(rows[0].parts_json)[0]['body'] == 'one'            # the approved version keeps its exact text
    # the old draft is dead: its decisions are cancelled and the old approval cannot be used again
    assert send(world, staged)['outcome'].startswith('not_sent:') and world.fans.posts == []
    with pytest.raises(Refused, match='approval is invalidated'):
        approval.approve(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP)
    edited = restaged(v2)
    assert send(world, edited)['outcome'] == 'not_authorized:approval_required' and world.fans.posts == []
    assert approval.current(world.store, TENANT, ACCOUNT, a['requestId'])['version'] == 2
    approval.approve(world.store, TENANT, ACCOUNT, a['requestId'], 1, expected_version=2, actor=OP)
    assert send(world, edited)['outcome'] == 'accepted' and posts_text(world) == ['uno', 'dos']
    assert [r.status for r in approvals(world)] == ['invalidated', 'consumed']


def test_edit_runs_the_same_validation_as_model_output(world):
    staged, mid = stage(world)
    a = staged['approval']
    def edit(parts):
        return approval.edit(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP, parts=parts)
    for bad, match in [(['It is $5 for you'], 'price'), (['thanks for buying it'], 'purchase'),
                       (['nice ' + chr(0x2014) + ' really'], 'dashes'), (['a', 'b'], 'number of parts'), ([''], 'invalid'),
                       (['x' * 5000], 'invalid')]:
        with pytest.raises(Refused, match=match):
            edit(bad)
    assert [(r.version, r.status) for r in approvals(world)] == [(1, 'awaiting')]
    assert world.rows(OfEngineDecision)[0].body == 'Hey you, glad you wrote'


def test_edit_of_an_offer_keeps_the_card_and_rejects_a_smuggled_price(world):
    staged, grant = offer_draft(world)
    a = staged['approval']
    with pytest.raises(Refused, match='price'):
        approval.edit(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP, parts=['only $1 today'])
    v2 = approval.edit(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP, parts=['A new lead-in'])
    body = v2['parts'][0]['body']
    assert body.startswith('A new lead-in') and 'USD 10.00' in body and v2['parts'][0]['price_cents'] == 1000
    ok_ = approval.approve(world.store, TENANT, ACCOUNT, a['requestId'], v2['generation'], expected_version=2, actor=OP)
    assert ok_['status'] == 'approved'
    result = send(world, restaged(v2))
    assert result['outcome'] == 'accepted'
    post = world.fans.posts[0]['body']
    assert post['text'] == body and post['price'] == 10.0 and post['mediaFiles'] == ['11', '12', '13']


def test_reject_cancels_the_draft_and_it_is_never_regenerated_by_a_duplicate(world):
    staged, mid = stage(world)
    a = staged['approval']
    approval.reject(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP, reason='off tone')
    row = approvals(world)[0]
    assert (row.status, row.actor) == ('rejected', OP) and row.invalidation_reason.startswith('rejected')
    assert world.rows(OfEngineDecision)[0].status == 'cancelled' and world.rows(OfEngineRequest)[0].status == 'cancelled'
    with pytest.raises(Refused, match='approval is rejected'):
        approval.approve(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP)
    assert send(world, staged)['outcome'].startswith('not_sent:')
    assert world.turn(TENANT, ACCOUNT, FAN, mid)['outcome'] == 'rejected'
    assert world.fans.posts == [] and len(approvals(world)) == 1


# ------------------------------------------------------------------ expiry / restart
def test_expired_approval_cannot_send_and_an_expired_draft_cannot_be_approved(world):
    staged, mid = stage(world)
    ok(world, staged)
    with Session(world.database[1]) as s, s.begin():
        s.scalar(select(OfEngineApproval)).expires_at = utcnow() - timedelta(seconds=1)
    assert send(world, staged)['outcome'] == 'not_authorized:approval_expired'
    row = approvals(world)[0]
    assert (row.status, row.invalidation_reason) == ('expired', 'ttl') and world.fans.posts == []
    # a draft that waited too long
    staged2, mid2 = stage(world, 'second draft', say='are you there')
    with Session(world.database[1]) as s, s.begin():
        s.scalar(select(OfEngineApproval).where(OfEngineApproval.status == 'awaiting')).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(Refused, match='expired'):
        ok(world, staged2)
    assert sorted(r.status for r in approvals(world)) == ['expired', 'expired'] and world.fans.posts == []
    assert approval.expire_stale(world.store, TENANT, ACCOUNT) == 0


def test_restart_between_approve_and_dispatch_never_duplicates(world):
    staged, mid = stage(world)
    ok(world, staged)
    # crash after the intent was queued but before dispatch; a new process finishes the same action
    intent = world.engine.queue(TENANT, ACCOUNT, staged['decisionId'])
    restarted = fresh_worker(world)
    world.no_throttle()
    assert restarted.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] == 'accepted'
    assert len(world.fans.posts) == 1 and [i.intent_id for i in world.rows(OfTextIntent)] == [intent]
    # another restart, another click: still one send
    again = fresh_worker(world)
    world.no_throttle()
    assert again.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] == 'accepted'
    assert len(world.fans.posts) == 1 and len(world.rows(OfTextIntent)) == 1
    # crash after approve, before anything was queued
    staged2, mid2 = stage(world, 'second', say='and another thing')
    ok(world, staged2)
    third = fresh_worker(world)
    world.no_throttle()
    assert third.send_approved(TENANT, ACCOUNT, staged2['decisionId'])['outcome'] == 'accepted'
    assert len(world.fans.posts) == 2 and posts_text(world)[-1] == 'second'


# ------------------------------------------------------------------ new fan input
def test_new_input_after_generation_blocks_approval(world):
    staged, mid = stage(world)
    late(world, mid + 1, 'wait, one more thing')                          # accepted by the webhook, not yet applied
    with pytest.raises(Refused, match='stale'):
        ok(world, staged)
    assert [(r.status, r.invalidation_reason) for r in approvals(world)] == [('invalidated', 'new_inbound_pending')]
    assert world.fans.posts == []


def test_new_turn_withdraws_older_drafts_and_stages_the_new_one(world):
    staged, mid = stage(world)
    world.say_plain('answer to the second message')
    second, mid2 = world.say('are you still there', seconds=2)
    assert second['outcome'] == 'awaiting_approval'
    by_status = {r.source_message_id: r.status for r in approvals(world)}
    assert by_status == {mid: 'invalidated', mid2: 'awaiting'}
    with pytest.raises(Refused, match='approval is invalidated'):
        ok(world, staged)
    assert {d.body: d.status for d in world.rows(OfEngineDecision)} == {'Hey you, glad you wrote': 'cancelled',
                                                                        'answer to the second message': 'ready'}
    ok(world, second)
    assert send(world, second)['outcome'] == 'accepted' and posts_text(world) == ['answer to the second message']


def test_new_input_after_approval_blocks_the_send(world):
    staged, mid = stage(world)
    ok(world, staged)
    late(world, mid + 1, 'actually never mind that question')
    result = send(world, staged)
    assert result['outcome'].startswith('superseded:') and world.fans.posts == []
    assert [r.status for r in approvals(world)] == ['invalidated'] and world.rows(OfEngineDecision)[0].status == 'cancelled'


def test_new_input_between_parts_stops_the_remaining_parts(world):
    staged, mid = stage(world, 'one', parts=('two', 'three'))
    ok(world, staged)
    world.fans.hooks.append(lambda n: late(world, 900, 'hold on a second') if n == 1 else None)
    result = send(world, staged)
    assert posts_text(world) == ['one'] and result['outcome'].startswith('superseded:')
    assert [r.status for r in approvals(world)] == ['invalidated']
    assert {d.part_index: d.status for d in world.rows(OfEngineDecision)} == {0: 'completed', 1: 'cancelled', 2: 'cancelled'}


# ------------------------------------------------------------------ ownership
def _control(world, mode, action, key, actor='op-session:' + 'a' * 64):
    with Session(world.database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    return world.store.control(TENANT, ACCOUNT, FAN, mode=mode, expected_revision=revision, command_key=key, actor=actor,
                               reason='operator test', action=action)


def test_takeover_before_dispatch_withdraws_the_approval(world):
    staged, mid = stage(world)
    ok(world, staged)
    _control(world, 'human_controlled', 'request_takeover', 'take-1')
    assert [(r.status, r.invalidation_reason) for r in approvals(world)] == [('invalidated', 'control_changed')]
    result = send(world, staged)
    assert result['outcome'] == 'superseded:owner_changed' and world.fans.posts == []


def test_handback_restores_planning_but_never_revives_the_stale_draft(world):
    staged, mid = stage(world)
    old = staged['approval']
    ok(world, staged)
    _control(world, 'human_controlled', 'request_takeover', 'take-2')
    _control(world, 'automated', 'release_takeover', 'give-back-2')       # an explicit human handback
    assert world.rows(OfEngineDecision)[0].status == 'ready'              # the stale draft is still there, unapproved
    assert [r.status for r in approvals(world)] == ['invalidated']
    with pytest.raises(Refused, match='approval is invalidated'):
        approval.approve(world.store, TENANT, ACCOUNT, old['requestId'], old['generation'], expected_version=1, actor=OP)
    assert send(world, staged)['outcome'] != 'accepted' and world.fans.posts == []
    # planning is eligible again: a fresh draft is staged and needs its own approval
    world.say_plain('a fresh answer after handback')
    fresh = world.turn(TENANT, ACCOUNT, FAN, mid)
    assert fresh['outcome'] == 'awaiting_approval' and fresh['approval']['generation'] == 1 and fresh['approval']['version'] == 1
    assert world.fans.posts == []
    with pytest.raises(Refused):
        ok(world, staged)
    ok(world, fresh)
    assert send(world, fresh)['outcome'] == 'accepted' and posts_text(world) == ['a fresh answer after handback']


def test_handback_with_no_new_turn_leaves_the_conversation_silent(world):
    staged, mid = stage(world)
    _control(world, 'human_controlled', 'request_takeover', 'take-3')
    _control(world, 'automated', 'release_takeover', 'give-back-3')
    assert world.fans.posts == [] and [r.status for r in approvals(world)] == ['invalidated']
    assert world.rows(OfTextIntent) == []


# ------------------------------------------------------------------ kill switch, delivery policy, opt-out
def test_kill_switch_and_disabled_delivery_stop_a_send_that_was_approved(world, monkeypatch):
    staged, mid = stage(world)
    ok(world, staged)
    monkeypatch.delenv('TWIN_ENGINE_FANSAPI_REAL')
    for call in (lambda: approval.authorize_send(world.store, TENANT, ACCOUNT, staged['decisionId']),
                 lambda: world.engine.queue(TENANT, ACCOUNT, staged['decisionId'])):
        with pytest.raises(Refused, match='kill_switch'):
            call()
    with pytest.raises(Refused, match='not enabled'):
        send(world, staged)
    assert world.fans.posts == [] and world.rows(OfTextIntent) == []
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    with Session(world.database[1]) as s, s.begin():
        s.get(OfDeliveryPolicy, ACCOUNT).enabled = False
    with pytest.raises(Refused, match='delivery_disabled'):
        approval.authorize_send(world.store, TENANT, ACCOUNT, staged['decisionId'])
    with Session(world.database[1]) as s, s.begin():
        s.get(OfDeliveryPolicy, ACCOUNT).enabled = True
    assert approval.authorize_send(world.store, TENANT, ACCOUNT, staged['decisionId'])['version'] == 1   # nothing was consumed


def test_opt_out_after_approval_stops_the_send(world):
    staged, mid = stage(world)
    ok(world, staged)
    late(world, mid + 1, 'please stop contacting me', seconds=2)       # applied to the transcript, no turn run
    world.store.drain(TENANT, ACCOUNT)
    with pytest.raises(Refused, match='opt_out'):
        approval.authorize_send(world.store, TENANT, ACCOUNT, staged['decisionId'])
    with pytest.raises(Refused, match='opt_out'):
        world.engine.queue(TENANT, ACCOUNT, staged['decisionId'])
    result = send(world, staged)
    assert result['outcome'].startswith('superseded:') and world.fans.posts == []   # the worker's newer-input check fires first
    assert [r.status for r in approvals(world)] == ['invalidated']


def test_opt_out_through_the_worker_puts_the_conversation_in_no_contact(world):
    staged, mid = stage(world)
    ok(world, staged)
    world.mid += 1
    refused = world.send(inbound(world.mid, 'please stop contacting me', seconds=2))
    assert refused['outcome'] == 'refused' and 'no contact' in refused['detail']
    assert send(world, staged)['outcome'].startswith(('superseded:', 'not_sent:')) and world.fans.posts == []
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
    assert all(r.status == 'invalidated' for r in approvals(world))


# ------------------------------------------------------------------ catalogue, media, price, card vs reply
def test_changed_catalogue_price_or_media_after_approval_blocks_the_card(world):
    staged, grant = offer_draft(world)
    ok(world, staged)
    with Session(world.database[1]) as s, s.begin():
        s.get(CatalogItem, world.item).price_cents = 2000
    result = send(world, staged)
    assert result['outcome'].startswith(('superseded:', 'not_queued:', 'not_authorized:')) and world.fans.posts == []
    assert [r.status for r in approvals(world)] == ['invalidated'] and world.rows(OfEngineDecision)[0].status == 'cancelled'


def test_removed_media_after_approval_blocks_the_card(world):
    staged, grant = offer_draft(world)
    ok(world, staged)
    with Session(world.database[1]) as s, s.begin():
        s.scalars(select(VaultItem).where(VaultItem.media_id == 12)).one().removed_at = utcnow()
    assert send(world, staged)['outcome'].startswith('superseded:') and world.fans.posts == []


def test_an_approved_reply_cannot_send_a_card_or_a_later_draft(world):
    staged, mid = stage(world)
    ok(world, staged)
    # a card draft for the next message is planned by the engine; the reply's approval does not cover it
    world.backfilled(FAN)
    grant = world.grant()
    late(world, mid + 1, 'show me the set please', seconds=2)
    world.store.drain(TENANT, ACCOUNT)
    world.reply(lambda snap: SyntheticProposal('Here you go', action='offer', offer_grant_id=grant))
    card = world.engine.plan(TENANT, ACCOUNT, FAN, mid + 1)
    for call in (lambda: approval.authorize_send(world.store, TENANT, ACCOUNT, card),
                 lambda: world.engine.queue(TENANT, ACCOUNT, card)):
        with pytest.raises(Refused, match='approval_missing'):
            call()
    with Session(world.database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
        card_row = s.get(OfEngineDecision, (ACCOUNT, card))
        body, item = card_row.body, card_row.catalog_item_id
        terms = json.loads(card_row.terms_json)
    with pytest.raises(Refused, match='engine approval: approval_missing'):     # nor can TextAuthority be asked directly
        TextAuthority(world.store).prepare(TENANT, ACCOUNT, FAN, command_key='engine:' + card, expected_revision=revision,
            body=body, origin='automated', actor='offline-engine', engine_decision_id=card, catalog_item_id=item,
            quoted_price_cents=terms['price_cents'])
    assert world.fans.posts == [] and world.rows(OfTextIntent) == []
    # the card gets its own approval row, which is awaiting a decision of its own
    info = approval.create_draft_approval(world.store, TENANT, ACCOUNT, card)
    assert info['status'] == 'awaiting' and info['parts'][0]['action'] == 'offer' and info['parts'][0]['price_cents'] == 1000


def test_decision_text_is_immutable_so_approved_text_cannot_be_swapped(world):
    staged, mid = stage(world)
    ok(world, staged)
    with Session(world.database[1]) as s:
        with pytest.raises(Exception, match='immutable'):
            s.execute(text("UPDATE of_engine_decision SET body='something the operator never saw'")); s.commit()
        s.rollback()
    assert approval.authorize_send(world.store, TENANT, ACCOUNT, staged['decisionId'])['version'] == 1


def test_a_later_generation_needs_its_own_approval(world):
    staged, mid = stage(world)
    ok(world, staged)
    world.say_plain('different later model output')
    with Session(world.database[1]) as s, s.begin():          # make the first draft supersedable: operator withdrew it
        pass
    approval.invalidate_for_change(world.store, TENANT, ACCOUNT, FAN, 'operator_withdrew')
    fresh = world.turn(TENANT, ACCOUNT, FAN, mid)
    assert fresh['outcome'] == 'awaiting_approval' and fresh['approval']['generation'] == 1
    with pytest.raises(Refused):
        approval.authorize_send(world.store, TENANT, ACCOUNT, fresh['decisionId'])
    assert world.fans.posts == []


# ------------------------------------------------------------------ evidence is protected
def test_approval_rows_are_immutable_and_never_deleted(world):
    staged, mid = stage(world)
    with Session(world.database[1]) as s:
        with pytest.raises(Exception, match='immutable'):
            s.execute(text("UPDATE of_engine_approval SET parts_json='[]'")); s.commit()
        s.rollback()
        with pytest.raises(Exception, match='retained'):
            s.execute(text('DELETE FROM of_engine_approval')); s.commit()
        s.rollback()
        with pytest.raises(Exception, match='starts awaiting'):
            row = approvals(world)[0]
            s.execute(text("""INSERT INTO of_engine_approval(account_id,request_id,generation,version,fan_id,binding_id,source_message_id,
                owner_epoch,control_revision,input_revision,status,parts_json,draft_hash,created_by,actor,decided_at,expires_at,created_at,updated_at)
                SELECT account_id,request_id,generation,2,fan_id,binding_id,source_message_id,owner_epoch,control_revision,input_revision,
                'approved',parts_json,draft_hash,'x','x',now(),now(),now(),now() FROM of_engine_approval""")); s.commit()
        s.rollback()
    ok(world, staged)
    a = staged['approval']
    with Session(world.database[1]) as s:
        with pytest.raises(Exception, match='immutable'):
            s.execute(text("UPDATE of_engine_approval SET actor='someone-else'")); s.commit()
        s.rollback()
    approval.reject(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'], expected_version=1, actor=OP)   # revoke
    with Session(world.database[1]) as s:
        with pytest.raises(Exception, match='immutable'):
            s.execute(text("UPDATE of_engine_approval SET status='approved'")); s.commit()    # terminal rows never come back


def test_pending_and_describe_show_the_exact_payload(world):
    staged, mid = stage(world, 'one', parts=('two',))
    live = approval.pending(world.store, TENANT, ACCOUNT)
    assert [(v['status'], [p['body'] for p in v['parts']]) for v in live] == [('awaiting', ['one', 'two'])]
    a = staged['approval']
    assert approval.describe(world.store, TENANT, ACCOUNT, a['requestId'], a['generation'])['draftHash'] == live[0]['draftHash']
    assert approval.pending(world.store, TENANT, ACCOUNT, fan_id=FAN2) == []


# ------------------------------------------------------------------ defaults and unrelated accounts
def test_no_automatic_approval_or_handback_exists_in_the_production_path():
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob('*.py'):
        rel = path.relative_to(root)
        if rel.parts[0] in {'tests', 'tools'} or rel.as_posix() in {'conversation_engine/approval.py', 'conversation_engine/operator_v2.py'}:   # operator_v2 only runs on a named human's HTTP command
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
                if name in {'approve', 'edit'} and rel.parts[0] in {'conversation_engine', 'fans_api_adapter', 'onlyfans_runtime'}:
                    offenders.append(f'{rel}:{node.lineno} calls {name}')
    assert offenders == []
    engine_sources = ' '.join(p.read_text(encoding='utf-8') for p in (root / 'conversation_engine').glob('*.py') if p.name != 'operator_v2.py')
    assert 'release_takeover' not in engine_sources and 'resume_automation' not in engine_sources
    # the only status writer that approves is approval.approve
    source = (root / 'conversation_engine/approval.py').read_text(encoding='utf-8')
    assert source.count("'approved', actor") == 1


def test_synthetic_accounts_need_no_approval_and_leave_no_rows(database):
    store, authority = configured(database, mode='automated')
    engine = EngineService(store, SyntheticModel({'hello': SyntheticProposal('hi there')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    from test_onlyfans_foundation_pg import event
    store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='hello'))
    store.drain(TENANT, ACCOUNT)
    intent = engine.queue(TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, FAN, 1))
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == 'accepted'
    with Session(database[1]) as s:
        assert s.scalar(select(OfEngineApproval)) is None
    with pytest.raises(Refused, match='fansapi'):
        approval.create_draft_approval(store, TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, FAN, 1))
    store.close()


# ------------------------------------------------------------------ inactive draft-only operation
def test_draft_only_has_no_transport_and_approval_cannot_escape_disabled_policy(world, monkeypatch):
    from conversation_engine.draft_only import DraftOnlyTransport
    with Session(world.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        revision = policy.revision
    world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False,
        limit_units=200, throttle_seconds=1, media_enabled=True, actor=OP, reason='Draft-only qualification')
    monkeypatch.delenv('TWIN_ENGINE_FANSAPI_REAL', raising=False)
    world.engine = EngineService(world.store, SyntheticModel({}), draft_only=True)
    world.worker = RealConversationWorker(world.engine, world.intake, DraftOnlyTransport(world.store, TENANT, ACCOUNT, allowed_chats=frozenset({str(FAN)})))
    staged, _ = stage(world)
    assert ok(world, staged)['status'] == 'approved'
    assert send(world, staged)['outcome'] == 'draft_only'
    assert world.fans.posts == [] and world.rows(OfTextIntent) == []
    # Another process cannot use the approval while the shared policy is off,
    # even if its own real-transport flag is enabled.
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    other = EngineService(world.store, SyntheticModel({}))
    with pytest.raises(Refused):
        other.queue(TENANT, ACCOUNT, staged['decisionId'])
    restarted = RealConversationWorker(
        EngineService(world.store, SyntheticModel({}), draft_only=True),
        world.intake, DraftOnlyTransport(world.store, TENANT, ACCOUNT, allowed_chats=frozenset({str(FAN)})))
    assert restarted.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] == 'draft_only'
    assert world.fans.posts == []
    # Enabling delivery changes the policy-bound approval digest. The prior
    # approval cannot become send authority after a later mode transition.
    with Session(world.database[1]) as s:
        revision = s.get(OfDeliveryPolicy, ACCOUNT).revision
    world.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=True,
        limit_units=200, throttle_seconds=1, media_enabled=True, actor=OP, reason='Later review')
    active = RealConversationWorker(other, world.intake, world.transport)
    result = active.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert result['outcome'].startswith('superseded:approval_mismatch')
    assert world.fans.posts == [] and world.rows(OfTextIntent) == []


def test_draft_only_cannot_run_with_delivery_enabled(world):
    from conversation_engine.draft_only import DraftOnlyTransport
    draft = EngineService(world.store, SyntheticModel({}), draft_only=True)
    worker = RealConversationWorker(draft, world.intake, DraftOnlyTransport(world.store, TENANT, ACCOUNT, allowed_chats=frozenset({str(FAN)})))
    with pytest.raises(Refused, match='shared delivery policy'):
        worker.turn(TENANT, ACCOUNT, FAN, 1)
    with pytest.raises(Refused, match='draft-only worker scope'):
        RealConversationWorker(world.engine, world.intake, DraftOnlyTransport(world.store, TENANT, ACCOUNT, allowed_chats=frozenset({str(FAN)})))


def test_non_allowlisted_fan_never_reaches_planning(world):
    world.operator_on = False
    world.transport.allowed_chats = frozenset({str(FAN)})
    world.say_plain('fixture reply')
    result, _ = world.say('outside pilot', fan=FAN2)
    assert result['outcome'] == 'outside_pilot_scope'
    assert world.snaps == [] and world.fans.posts == []
    with Session(world.database[1]) as s:
        assert s.get(OfEngineOwner, (ACCOUNT, FAN2)) is None


def test_edit_requires_creator_profile(world, monkeypatch):
    from conversation_engine.writer_context import set_active_profiles
    set_active_profiles(None)
    monkeypatch.delenv('ENGINE_MODEL_PROFILES_FILE', raising=False)
    staged, _ = stage(world)
    row = staged['approval']
    with pytest.raises(Refused, match='creator profile'):
        approval.edit(world.store, TENANT, ACCOUNT, row['requestId'], row['generation'],
                      expected_version=row['version'], actor=OP, parts=['Safe edited reply'])
    assert posts_text(world) == []


def test_edit_respects_documented_creator_limits(world):
    from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles
    set_active_profiles(StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Fixture creator', limits=('no meetups',),
        limit_terms=('secretword',), offers_permitted=True, approved_item_ids=(str(world.item),))}))
    staged, _ = stage(world)
    row = staged['approval']
    with pytest.raises(Refused, match='creator or pilot rules'):
        approval.edit(world.store, TENANT, ACCOUNT, row['requestId'], row['generation'],
                      expected_version=row['version'], actor=OP, parts=['Here is secretword'])
    assert posts_text(world) == []


def test_creator_profile_change_invalidates_an_approved_draft(world):
    from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles
    staged, _ = stage(world)
    ok(world, staged)
    set_active_profiles(StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Changed creator', limits=('no meetups',))}))
    result = world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert result['outcome'] != 'accepted' and posts_text(world) == []


def test_creator_profile_change_during_generation_cannot_become_a_draft(world):
    from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles
    world.operator_on = False
    def changed_profile(_snapshot):
        set_active_profiles(StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Corrected creator', limits=('no meetups',))}))
        return SyntheticProposal('A fixture reply')
    world.reply(changed_profile)
    result, _ = world.say('Hello from fixture fan')
    assert result['outcome'] == 'refused'
    assert 'creator profile changed' in result['detail']
    assert posts_text(world) == [] and approvals(world) == []
