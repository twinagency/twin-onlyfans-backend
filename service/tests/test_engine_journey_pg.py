"""Plan C journey subset: what stayed, what was deferred, and the refusal / opt-out / identity / history rules.

Reuses the real-path world of test_engine_real_worker_pg (real FansEventIntake, EngineService, TextAuthority on PostgreSQL,
provider = httpx.MockTransport, model = in-process fixture). No network, no real model.
"""
from datetime import timedelta
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_engine_real_worker_pg import SECRET, inbound, world  # noqa: F401  (the world fixture)
from conversation_engine import journey
from conversation_engine.engine import SyntheticProposal
from conversation_engine.journey import (JourneyConfig, JourneyService, active, backfill_complete, configure_journey,
                                         journey_config, journey_context, signals)
from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOfferGrant, OfEngineOwner, OfEngineRequest
from onlyfans_runtime.store import utcnow

IDENT = 'This chat is written by an AI assistant that works for her team.'


def ingest(world, mid, text, seconds=0):
    """Canonical inbound message only: no planning, no sending."""
    raw = json.dumps(inbound(mid, text, FAN, seconds), separators=(',', ':')).encode()
    signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    world.intake.accept(raw, signature, SECRET, 'evt-' + hashlib.sha256(raw).hexdigest()[:24])
    world.store.drain(TENANT, ACCOUNT)
    return mid


def age_everything(world, hours):
    with Session(world.database[1]) as s, s.begin():
        for message in s.scalars(select(Message).where(Message.account_id == ACCOUNT)):
            message.created_at = message.created_at - timedelta(hours=hours)


def decisions(world):
    return {d.decision_id: d.status for d in world.rows(OfEngineDecision)}


@pytest.fixture(autouse=True)
def clean_journey_state():
    yield
    configure_journey(JourneyConfig())
    set_active_profiles(None)


# ---- the public names the real worker imports are still there; the deferred defaults are gone ---------------------
def test_public_surface_kept_and_deferred_business_defaults_removed():
    from conversation_engine import real_worker  # noqa: F401  (imports JourneyService, backfill_complete, journey_config)
    assert callable(JourneyService.before_plan) and callable(JourneyService.after_evidence) and callable(backfill_complete)
    config = journey_config()
    for gone in ('welcome_item_id', 'welcome_ttl_hours', 'whale_hold_cents', 'whale_flag_cents'):
        assert not hasattr(config, gone), gone
    assert not hasattr(journey, 'WELCOME_PREFIX') and not hasattr(JourneyService, '_ensure_welcome')
    assert config.auto_backfill is False and config.temporary_pause_reopen_hours is None
    assert journey.JourneyService.after_evidence(None, None, None, None) == {'action': 'none'}


# ---- refusal: stop the pitch NOW and invalidate unsent sales drafts -------------------------------------------------
def test_refusal_invalidates_an_unsent_ready_offer_draft(world):
    world.backfilled(FAN)
    grant = world.grant()
    world.reply(lambda snap: SyntheticProposal('look at this', action='offer', offer_grant_id=grant))
    first = ingest(world, 201, 'show me the set')
    draft = world.engine.plan(TENANT, ACCOUNT, FAN, first)
    assert decisions(world)[draft] == 'ready'                                # a sales draft that was never sent
    second = ingest(world, 202, 'hmm that is too expensive for me', seconds=1)
    verdict = world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, second)
    assert verdict['action'] == 'plan' and verdict['paused'] == 'refusal'
    assert decisions(world)[draft] == 'cancelled'                            # invalidated, can never be sent
    assert world.owner()[1] == 'paused' and world.owner()[2] == second
    assert world.fans.posts == []


def test_refusal_cancels_a_queued_never_dispatched_offer_and_its_queue_accounting(world):
    world.backfilled(FAN)
    grant = world.grant()
    world.reply(lambda snap: SyntheticProposal('look at this', action='offer', offer_grant_id=grant))
    draft = world.engine.plan(TENANT, ACCOUNT, FAN, ingest(world, 211, 'show me the set'))
    intent = world.engine.queue(TENANT, ACCOUNT, draft)
    with Session(world.database[1]) as s:
        queued = s.get(OfDeliveryPolicy, ACCOUNT).queued_count
    assert queued >= 1 and decisions(world)[draft] == 'queued'
    world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, ingest(world, 212, "i can't afford that", seconds=1))
    with Session(world.database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'cancelled'
        assert s.get(OfDeliveryPolicy, ACCOUNT).queued_count == queued - 1
    assert decisions(world)[draft] == 'cancelled' and world.fans.posts == []


def test_refusal_never_touches_a_plain_reply_draft(world):
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    world.reply(lambda snap: SyntheticProposal('just a chat reply'))
    draft = world.engine.plan(TENANT, ACCOUNT, FAN, ingest(world, 221, 'hello'))
    world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, ingest(world, 222, 'not now thanks', seconds=1))
    assert decisions(world)[draft] == 'ready'                                # only SALES drafts are invalidated


# ---- temporary "not now" is not a permanent ban; an explicit opt-out is distinct -------------------------------------
def test_signals_distinguish_temporary_not_now_from_explicit_sales_optout():
    assert signals('not now, maybe later') == {'refusal'}
    assert signals("it's too expensive for me") == {'refusal'}
    assert signals('please stop trying to sell me things') >= {'refusal', 'sales_optout'}
    assert signals('stop offering me stuff, just chat') >= {'sales_optout'}
    assert 'sales_optout' not in signals('no thanks')
    assert signals('i love your last pic') == frozenset() and signals('how much is it') == frozenset()  # a price question is not a refusal


def test_pause_ends_by_the_fans_clear_request_never_by_time_with_the_default_config(world):
    world.backfilled(FAN)
    grant = world.grant()
    world.say_plain('no problem at all')
    r, mid = world.say('not now, maybe later')
    assert r['outcome'] == 'accepted' and world.owner()[1] == 'paused'
    age_everything(world, 24 * 30)
    world.reply(lambda snap: SyntheticProposal('how about it now', action='offer', offer_grant_id=grant))
    blocked, _ = world.say('hey, what are you up to', seconds=1)
    assert blocked['outcome'] == 'refused' and world.owner()[1] == 'paused'          # time alone changed nothing
    world.reply(lambda snap: SyntheticProposal('sure, here it is', action='offer', offer_grant_id=grant, sales_signal='reopen',
                                               item_scope=(str(world.item),)))
    back, _ = world.say('actually yes i want the set, how do i buy it', seconds=2)    # a clear renewed request
    assert back['outcome'] == 'accepted' and world.owner()[1] == 'reopened_scoped'


def test_temporary_pause_may_become_eligible_for_a_fresh_offer_on_a_later_inbound_message_when_configured(world):
    configure_journey(JourneyConfig(temporary_pause_reopen_hours=12))
    world.backfilled(FAN)
    world.say_plain('no problem at all')
    world.say("it's too expensive for me")
    assert world.owner()[1] == 'paused'
    soon = ingest(world, 301, 'hello again', seconds=1)
    assert world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, soon)['reopened'] is False   # under 12 hours: still paused
    assert world.owner()[1] == 'paused'
    age_everything(world, 13)
    later = ingest(world, 302, 'hey, you there?', seconds=2)
    verdict = world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, later)
    assert verdict['reopened'] is True and world.owner()[1] == 'open'
    assert world.fans.posts and all('price' not in p['body'] for p in world.fans.posts)    # nothing was sent unprompted
    count = len(world.fans.posts)
    assert world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, later)['reopened'] is False  # nothing left to reopen
    assert len(world.fans.posts) == count


def test_explicit_sales_optout_is_never_reopened_by_elapsed_time(world):
    configure_journey(JourneyConfig(temporary_pause_reopen_hours=12))
    world.say_plain('understood, just chatting then')
    world.say('please stop trying to sell me stuff')
    assert world.owner()[1] == 'paused'
    age_everything(world, 24 * 30)
    verdict = world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, ingest(world, 311, 'hey you', seconds=1))
    assert verdict['reopened'] is False and world.owner()[1] == 'paused'


def test_a_pause_with_an_unrecognised_reason_is_not_reopened_by_time_either(world):
    configure_journey(JourneyConfig(temporary_pause_reopen_hours=12))
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    with Session(world.database[1]) as s, s.begin():                      # paused by the model's own decline signal
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        mid = ingest(world, 320, 'let me think about my life choices')
        owner.sales_mode, owner.sales_source_message_id = 'paused', mid
    age_everything(world, 24)
    assert world.worker.journey.before_plan(TENANT, ACCOUNT, FAN, ingest(world, 321, 'hi again', seconds=1))['reopened'] is False
    assert world.owner()[1] == 'paused'


# ---- identity: only from approved wording, otherwise a human -----------------------------------------------------------
def test_profile_identity_wording_is_the_approved_line_for_the_journey(world):
    set_active_profiles(StaticCreatorProfiles({ACCOUNT: CreatorProfile(name='Riley', limits=('no meetups',), role_statement=IDENT)}))
    assert journey.identity_line_for(ACCOUNT) == IDENT
    assert journey.identity_line_for(ACCOUNT, JourneyConfig(identity_line='  Explicit line.  ')) == 'Explicit line.'
    world.say_plain(IDENT)
    result, mid = world.say('are you a real person?')
    assert result['outcome'] == 'accepted' and world.owner()[0] == 'active'
    assert world.snaps[-1]['journey']['approved_identity_line'] == IDENT
    world.say_plain('yes totally human, promise')
    wrong, _ = world.say('wait, is this a bot?', seconds=5)               # a new identity question, off-wording answer
    assert wrong['outcome'] == 'refused' and 'approved wording' in wrong['detail']


def test_no_profile_and_no_line_means_identity_goes_to_a_human(world):
    set_active_profiles(StaticCreatorProfiles({}))
    assert journey.identity_line_for(ACCOUNT) is None
    world.say_plain('x')
    result, _ = world.say('is this a bot?')
    assert result['outcome'] == 'handed_to_human' and result['reason'] == 'identity' and world.fans.posts == []


# ---- history coverage: a paid offer waits until the fan's history is known -----------------------------------------------
def test_history_check_needs_a_finished_backfill_and_no_unscoped_messages(world):
    world.engine.select_simulated(TENANT, ACCOUNT, FAN)
    mid = ingest(world, 401, 'show me the set')
    with Session(world.database[1]) as s:
        runtime = world.store._runtime(s, TENANT, ACCOUNT)
        owner, message = s.get(OfEngineOwner, (ACCOUNT, FAN)), s.get(Message, (ACCOUNT, FAN, mid))
        assert active(s, ACCOUNT) and backfill_complete(s, ACCOUNT, FAN) is False
        ctx = journey_context(s, runtime, owner, None, message)
    assert ctx['offers_allowed'] is False and ctx['backfill_complete'] is False
    world.backfilled(FAN)
    with Session(world.database[1]) as s:
        ctx = journey_context(s, runtime := world.store._runtime(s, TENANT, ACCOUNT), s.get(OfEngineOwner, (ACCOUNT, FAN)), None,
                              s.get(Message, (ACCOUNT, FAN, mid)))
    assert ctx['offers_allowed'] is True and ctx['history_scope_incomplete'] is False
    with Session(world.database[1]) as s, s.begin():                     # a message no ingress/receipt of this binding vouches for
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=1, direction='in', body='older chat', created_at=utcnow() - timedelta(days=9)))
    with Session(world.database[1]) as s:
        ctx = journey_context(s, world.store._runtime(s, TENANT, ACCOUNT), s.get(OfEngineOwner, (ACCOUNT, FAN)), None,
                              s.get(Message, (ACCOUNT, FAN, mid)))
    assert ctx['backfill_complete'] is True and ctx['history_scope_incomplete'] is True and ctx['offers_allowed'] is False


# ---- kept routing rules, restated as pilot rules (the full flows live in test_engine_real_worker_pg) -----------------------
def test_compliment_signal_needs_no_ask_and_never_fires_for_a_request():
    assert 'compliment' in signals('omg thank you, that was so hot')
    assert 'compliment' not in signals('thank you, can you send more?')
    assert 'compliment' not in signals('thanks, how much is the next one')
    assert 'compliment' not in signals('hello')


def test_no_outreach_path_exists_in_the_journey_service():
    # Timed or proactive outreach is deferred: the service has no method that plans, queues or sends on a timer.
    names = {n for n in dir(JourneyService) if not n.startswith('__')}
    assert not {n for n in names if any(w in n for w in ('welcome', 'outreach', 'followup', 'schedule', 'send', 'dispatch', 'queue'))}
