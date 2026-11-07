"""Representative approved identity replies after an unknown simulated Fans API send."""
import httpx
import pytest
from sqlalchemy.orm import Session

from conversation_engine import approval
from conversation_engine.engine import SyntheticProposal
from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineIndependenceProof
from test_engine_real_worker_pg import world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_planc_defects_pg import intents, posts_to, timeout_for

IDENTITY = 'I am an AI assistant for this creator.'


def profile(world, line=IDENTITY):
    profiles = StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Fixture creator', limits=('no meetups',),
        role_statement=line, offers_permitted=True, approved_item_ids=(str(world.item),))})
    set_active_profiles(profiles)
    return profiles


def unknown_first(world, body='Hi.'):
    profile(world)
    saved = world.fans.transport.handler
    timeout_for(world, FAN)
    world.say_plain(body)
    result, _ = world.say('hello')
    assert result['outcome'] == 'unknown'
    [anchor] = intents(world, FAN)
    world.fans.transport.handler = saved
    world.operator_on = False
    return anchor


def approve(world, result):
    staged = result['approval']
    return approval.approve(world.store, TENANT, ACCOUNT, staged['requestId'], staged['generation'],
        expected_version=staged['version'], actor='op-session:'+'a'*64)


def test_exact_approved_identity_reply_proceeds_without_clearing_earlier_unknown(world):
    anchor = unknown_first(world)
    world.say_plain(IDENTITY)
    staged, _ = world.say('are you an ai?', seconds=5)
    assert staged['outcome'] == 'awaiting_approval' and len(posts_to(world, FAN)) == 1
    with Session(world.database[1]) as s:
        proof = s.get(OfEngineIndependenceProof, (ACCOUNT, staged['decisionId']))
        assert proof is not None and proof.kind == 'identity.v1' and proof.anchor_intent_id == anchor.intent_id
    approve(world, staged)
    world.no_throttle()
    sent = world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert sent['outcome'] == 'accepted' and len(posts_to(world, FAN)) == 2
    assert len({p['key'] for p in posts_to(world, FAN)}) == 2
    with Session(world.database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, anchor.intent_id)).status == 'unknown'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == anchor.attempt_id
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id is None
    world.say_plain('nothing has been confirmed')
    dependent, _ = world.say('did you send that?', seconds=10)
    assert dependent['outcome'] == 'not_ready' and len(posts_to(world, FAN)) == 2


def test_identity_proof_uses_real_model_pipeline_with_a_simulated_client(world):
    anchor = unknown_first(world)
    from conversation_engine.model_client import FixtureChatClient
    from conversation_engine.model_runtime import ModelRuntime
    from test_engine_model_pg import config, ok
    client = FixtureChatClient([ok(IDENTITY)])
    world.engine.model = ModelRuntime(world.database[1], client, config(), profile(world))
    staged, _ = world.say('are you an ai?', seconds=5)
    assert staged['outcome'] == 'awaiting_approval' and len(client.requests) == 1
    approve(world, staged)
    world.no_throttle()
    assert world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] == 'accepted'
    with Session(world.database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, anchor.intent_id)).status == 'unknown'
    assert len(posts_to(world, FAN)) == 2


def test_two_workers_cannot_duplicate_the_independent_approved_reply(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    anchor = unknown_first(world)
    world.say_plain(IDENTITY)
    staged, _ = world.say('are you an ai?', seconds=5)
    approve(world, staged)
    world.no_throttle()
    barrier = Barrier(2)
    def send():
        barrier.wait(timeout=10)
        return world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda unused: send(), range(2)))
    assert len(results) == 2
    assert len(posts_to(world, FAN)) == 2  # the original unknown plus exactly one independent send
    assert len({p['key'] for p in posts_to(world, FAN)}) == 2
    assert next(row for row in intents(world, FAN) if row.intent_id == anchor.intent_id).status == 'unknown'


def test_changed_identity_wording_invalidates_an_approved_independent_draft(world):
    anchor = unknown_first(world)
    world.say_plain(IDENTITY)
    staged, _ = world.say('are you an ai?', seconds=5)
    assert staged['outcome'] == 'awaiting_approval'
    approve(world, staged)
    profile(world, line='This account uses a different approved assistant statement.')
    world.no_throttle()
    result = world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert result['outcome'] != 'accepted' and len(posts_to(world, FAN)) == 1
    assert intents(world, FAN)[0].status == 'unknown'


def test_new_dependent_input_invalidates_an_approved_independent_draft(world):
    anchor = unknown_first(world)
    world.say_plain(IDENTITY)
    staged, _ = world.say('are you an ai?', seconds=5)
    approve(world, staged)
    world.say_plain('I will check that')
    followup, _ = world.say('actually, did you send the earlier message?', seconds=10)
    assert followup['outcome'] == 'not_ready'
    world.no_throttle()
    assert world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] != 'accepted'
    assert len(posts_to(world, FAN)) == 1 and intents(world, FAN)[0].status == 'unknown'


def test_unknown_dependent_or_unapproved_identity_never_proceeds(world):
    anchor = unknown_first(world, body='I am sending the set now')
    world.say_plain(IDENTITY)
    blocked, _ = world.say('are you an ai?', seconds=5)
    assert blocked['outcome'] == 'not_ready' and len(posts_to(world, FAN)) == 1
    assert intents(world, FAN)[0].status == 'unknown'


def test_identity_needs_current_approved_wording_and_no_additional_part(world):
    anchor = unknown_first(world)
    profile(world, line='')
    world.say_plain(IDENTITY)
    blocked, _ = world.say('are you an ai?', seconds=5)
    assert blocked['outcome'] == 'not_ready' and len(posts_to(world, FAN)) == 1
    profile(world)
    world.reply(lambda snap: SyntheticProposal(IDENTITY, parts=('anything else?',)))
    staged, _ = world.say('are you a bot?', seconds=10)
    assert staged['outcome'] == 'awaiting_approval'
    with Session(world.database[1]) as s:
        assert s.get(OfEngineIndependenceProof, (ACCOUNT, staged['decisionId'])) is None
    approve(world, staged)
    world.no_throttle()
    result = world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert result['outcome'] != 'accepted' and len(posts_to(world, FAN)) == 1
    assert intents(world, FAN)[0].status == 'unknown'


def test_second_unknown_keeps_both_attempts_and_blocks_further_replies(world):
    anchor = unknown_first(world)
    timeout_for(world, FAN)
    world.say_plain(IDENTITY)
    staged, _ = world.say('are you an ai?', seconds=5)
    assert staged['outcome'] == 'awaiting_approval'
    approve(world, staged)
    world.no_throttle()
    assert world.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])['outcome'] == 'unknown'
    assert sorted(row.status for row in intents(world, FAN)) == ['unknown', 'unknown']
    world.say_plain(IDENTITY)
    again, _ = world.say('are you a bot?', seconds=10)
    assert again['outcome'] == 'not_ready' and len(posts_to(world, FAN)) == 2
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == anchor.attempt_id
    # Later positive provider evidence for the first send promotes the second
    # unknown into the fan's unresolved pointer without an account-wide slot.
    from dataclasses import replace
    from test_onlyfans_text_delivery_pg import accepted
    with Session(world.database[1]) as s:
        row = s.get(OfTextIntent, (ACCOUNT, anchor.intent_id))
        grant = world.authority._grant(row)
    proof = replace(accepted(grant, message_id=7999), evidence_ref='fansapi:fixture-positive-history')
    assert world.authority.record_accepted(TENANT, ACCOUNT, anchor.intent_id, grant.token, proof) == 'accepted'
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == anchor.attempt_id
    assert world.authority.materialize(TENANT, ACCOUNT, anchor.intent_id) is True
    with Session(world.database[1]) as s:
        rows = list(s.query(OfTextIntent).filter_by(account_id=ACCOUNT, fan_id=FAN).all())
        remaining = next(row for row in rows if row.intent_id != anchor.intent_id)
        assert remaining.status == 'unknown'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == remaining.attempt_id
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id is None
