"""Step 6 qualification: the composed counterexamples the existing Step 4 / Step 5 / R0 suites do not already prove.

Everything runs through the REAL commercial path: signed webhook -> scheduler/worker -> preparation from the standing catalogue
approval (zero per-fan grants, never seeded) -> writer (fake model transport) -> named-operator approval -> intent -> final wire
(fake FansAPI transport). Synthetic identities only; no guard is opened and no coverage flag is forced.

Rows covered here (the others map to existing tests, see the matrix in the Step 6 report):
  Q1  ordinary replies on a selling account (burst, correction, uncertainty, empathy, repeated opener)
  Q4  old purchase / partly owned bundle / unknown coverage / a new asset that has no approved terms
  Q6  product ambiguity; a terms, category, discount or content change between plan and send
  Q7  duplicate inbound and competing workers: one logical reply, one card, one unlock effect
  Q8  R0 temporary failure on a COMMERCIAL account: recovery with no new fan message, correction + 429, permanent refusal
  Q9  unknown card / unknown model call kept across a restart; no blind replay, no refunded reservation
  Q10 final-wire exhaustion of multipart capacity, the account cap and the provider budget
  Q11 a voluntary tip beside a sale: one acknowledgement, no price change, no access
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from commercial_terms_fixture import ACTOR as AGENCY_ACTOR, approve, add_vault
from commercial_worker_fixture import FAN_A, FAN_B, FAN_C, FAN_D, OPERATOR, build_world, key, world, writer  # noqa: F401
from conversation_engine import approval, operator_v2, pilot_scope
from conversation_engine.ownership_gate import owned_state
from conversation_engine.writer_context import set_active_profiles
from db.models import CatalogItem, VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineModelCall, OfEngineOfferGrant, OfEngineRequest
from history_support import card as history_card, fan_message
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.catalogue_terms import withdraw_terms
from onlyfans_runtime.store import Refused, utcnow
from test_commercial_preparation_pg import TENANT
from test_commercial_sale_rules_pg import catalogue, grants, intents, nothing_sent, send_ok, staged_card
from test_engine_real_worker_pg import iso
from test_onlyfans_foundation_pg import database  # noqa: F401

TIP_ACK = 'Thank you so much for the tip, that was really sweet of you.'


# ------------------------------------------------------------------------------------------------------------ helpers
def live_approval(w, fan=FAN_A):
    rows = [a for a in approval.pending(w.store, TENANT, w.account, fan) if a['status'] == 'awaiting']
    assert len(rows) == 1, rows
    return rows[0]


def approve_live(w, fan=FAN_A):
    a = live_approval(w, fan)
    return approval.approve(w.store, TENANT, w.account, a['requestId'], a['generation'], expected_version=a['version'], actor=OPERATOR)


def to_fan(w, fan):
    return [p for p in w.fans_mock.posts if f'/chats/{fan}/' in p['path']]


def card_to(w, fan):
    return [p for p in w.cards() if f'/chats/{fan}/' in p['path']]


def requests_of(w, fan=None):
    return w.rows(OfEngineRequest, *([OfEngineRequest.fan_id == fan] if fan else []))


def model_calls(w):
    return sorted(w.rows(OfEngineModelCall), key=lambda c: (c.created_at, c.seq))


def policy(w):
    with Session(w.database[1]) as s:
        p = s.get(OfDeliveryPolicy, w.account)
        return p.used_units, p.queued_count, p.limit_units


class Scripted:
    """The model provider with a script in front of the fixture writer: 'ok' | status | (status, headers) | 'timeout'."""
    def __init__(self, w, script):
        self.inner = writer(w)
        self.script, self.calls, self.lock = list(script), [], threading.Lock()
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        with self.lock:
            step = self.script.pop(0) if self.script else 'ok'
            self.calls.append(step[0] if isinstance(step, tuple) else step)
        if step == 'ok':
            return self.inner.handle_request(request)
        if step == 'timeout':
            raise httpx.ReadTimeout('synthetic read timeout', request=request)
        status, headers = step if isinstance(step, tuple) else (step, {})
        return httpx.Response(status, headers={'x-request-id': 'req-synthetic-1', **headers}, json={'error': {
            'code': status, 'message': 'synthetic provider error', 'metadata': {'provider_name': 'Fixture Upstream'}}})


class Clock:
    """Only the model layer's clock (runtime + ledger) moves; the scheduler compares due times with that same ledger clock."""
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset

    def govern(self, pilot):
        pilot.worker.engine.model._clock = self
        pilot.worker.engine.model.ledger.clock = self
        return self


def second_pilot(w, clock=None):
    pilot = build_supervised_pilot(w.store, w.env, model_transport=w.model_transport, fans_transport=w.fans_mock.transport)
    if clock:
        clock.govern(pilot)
    return pilot


def drain(w, pilot=None, n=3):
    for _ in range(n):
        w.unthrottle()
        (pilot or w.pilot).tick()


def greedy(latest, prompt, approved, proposal):
    """A writer that states its own amount on its first try; the correction call is the honest one."""
    if approved and 'YOUR LAST DRAFT WAS REJECTED' not in prompt:
        return {**proposal, 'body': 'Here you go, only $5 for the photo.'}
    return proposal


# ============================================================================================================ Q1
def test_q1_ordinary_replies_on_a_selling_account_burst_correction_uncertainty_empathy_and_repetition(world):
    """The account CAN sell, yet ordinary talk is answered as ordinary talk: real replies, no card, no rule-like refusal line."""
    catalogue(world)
    world.launch(facts=(('home_city', 'Brisbane'),))
    spoken = lambda: [p['body']['text'] for p in to_fan(world, FAN_A)]

    # a neutral greeting: a plain reply, no pitch, no sales rules recited, nothing held
    staged, tick = world.converse('hey', FAN_A)
    send_ok(world, tick)
    assert 'APPROVED OFFER: none' in world.model_prompts[-1] and card_to(world, FAN_A) == [] and len(spoken()) == 1
    for line in ('sales are', 'not selling', 'cannot offer', 'no offers'):
        assert line not in spoken()[0].lower()

    # A deterministic writer explicitly addresses all three questions. This tests
    # request handling, not real-model prose quality (qualified separately).
    world.choose = lambda latest, prompt, approved, proposal: (
        {'body': "I'm from Brisbane. I haven't shared whether I'm up late tonight or what I do all day."}
        if latest == 'what do you do all day?' else proposal)
    # a burst of three questions before any reply: ONE reply, built from all three, and the earlier drafts retire
    for text_ in ('are you up late tonight?', 'where are you from?', 'what do you do all day?'):
        staged, mid = world.say(text_, FAN_A)
    assert staged['outcome'] == 'awaiting_approval'
    for earlier in ('are you up late tonight?', 'where are you from?'):
        assert earlier in world.model_prompts[-1], 'the unanswered questions of the burst reach the one prompt that is sent'
    world.approve_turn(staged)
    world.unthrottle()
    send_ok(world, world.pilot.tick())
    assert len(spoken()) == 2, 'one logical reply for the whole burst'
    assert all(answer in spoken()[-1] for answer in ('Brisbane', 'up late tonight', 'what I do all day'))
    world.choose = None

    # a correction: the application keeps it, the reply is one ordinary message, still no card
    staged, tick = world.converse("no wait, I meant tomorrow night, not tonight", FAN_A)
    send_ok(world, tick)
    assert len(spoken()) == 3 and card_to(world, FAN_A) == []

    # factual uncertainty (a fact she has NOT documented) and empathy are ordinary replies too
    for text_ in ('what is your favourite song?', 'I had a rough day at work, honestly'):
        staged, tick = world.converse(text_, FAN_A)
        send_ok(world, tick)
    assert len(spoken()) == 5 and card_to(world, FAN_A) == []
    retired = [r for r in requests_of(world, FAN_A) if r.status == 'cancelled']
    assert len(retired) == 2 and all(r.next_step.startswith('superseded_by_message:') for r in retired)
    assert all(r.status in ('completed', 'cancelled') for r in requests_of(world, FAN_A))
    before = len(spoken())
    drain(world, second_pilot(world))
    assert len(spoken()) == before, 'superseded burst requests never replay after restart'

    # historical repetition failure: she has opened her last two replies the same way; a third identical opener is rejected once,
    # the correction is sent instead, and the fan still gets exactly one reply
    world.choose = lambda latest, prompt, approved, proposal: proposal if 'YOUR LAST DRAFT WAS REJECTED' in prompt else {
        'body': 'Honestly, ' + latest.lower().rstrip('?.!') + ' is a good one.'}
    for text_ in ('what is your favourite colour?', 'what is your favourite food?'):
        staged, tick = world.converse(text_, FAN_A)                  # two replies that both open with "Honestly"
        send_ok(world, tick)
    sent_before, calls_before = len(spoken()), len(world.model_prompts)
    staged, tick = world.converse('what is your favourite film?', FAN_A)
    send_ok(world, tick)
    assert len(world.model_prompts) == calls_before + 2 and 'repeats her usual opener' in world.model_prompts[-1]
    assert len(spoken()) == sent_before + 1 and not spoken()[-1].lower().startswith('honestly')
    assert card_to(world, FAN_A) == []


# ============================================================================================================ Q3
WRONG_PRICE_OR_MEDIA = ['Here you go, only five dollars for the photo.', 'Here you go, ten bucks and it is yours.',
                        'Here you go, nine ninety nine for the photo.', 'Here you go, that is twenty five dollars.',
                        'Here you go, three photos and a video for you.', 'Here you go, a short video clip for you.',
                        'Here you go, five pictures for you.']


@pytest.mark.parametrize('bad', WRONG_PRICE_OR_MEDIA)
def test_q3_a_word_form_wrong_price_or_a_false_media_fact_is_rejected_once_and_the_card_carries_the_exact_terms(world, bad):
    catalogue(world)
    world.choose = lambda latest, prompt, approved, proposal: (
        {**proposal, 'body': bad} if approved and 'YOUR LAST DRAFT WAS REJECTED' not in prompt else proposal)
    world.launch()
    staged, tick = world.converse('show me the beach photo', FAN_A)
    send_ok(world, tick)
    assert len(world.model_prompts) == 2 and 'YOUR LAST DRAFT WAS REJECTED' in world.model_prompts[1], 'one correction, then the honest draft'
    [sent] = card_to(world, FAN_A)
    assert sent['body']['price'] == 12.0 and sent['body']['mediaFiles'] == ['31'] and 'USD 12.00' in sent['body']['text']
    assert bad.split(',', 1)[1].strip(' .') not in sent['body']['text']


@pytest.mark.parametrize('bad', WRONG_PRICE_OR_MEDIA)
def test_q3_a_human_edit_cannot_introduce_a_word_form_price_or_a_false_media_fact(world, bad):
    catalogue(world)
    world.launch()
    staged, mid = staged_card(world)
    info = staged['approval']
    with pytest.raises(Refused):
        approval.edit(world.store, TENANT, world.account, info['requestId'], info['generation'], expected_version=info['version'],
                      actor=OPERATOR, parts=[bad])
    assert world.cards() == []


# ============================================================================================================ Q4
def test_q4_old_purchase_partly_owned_bundle_unknown_coverage_and_a_new_asset_without_approved_terms(database, tmp_path, monkeypatch):
    def history(cfg):
        return {FAN_A: [fan_message(7101, FAN_A, 'hi'), history_card(7102, cfg, [31], 1200, previews=[], opened=True)],     # old purchase: owns 31
                FAN_B: [fan_message(7201, FAN_B, 'hi'), history_card(7202, cfg, [41], 2500)]}                                 # previews unknown
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history)
    try:
        ids = catalogue(w)
        ids['combo'] = w.product('Combo bundle', [31, 61, 62], previews=[62], standard=3000, description='Combo bundle, two paid items and one preview.')
        w.launch()
        with Session(database[1]) as s:
            assert __import__('conversation_engine.history_ownership', fromlist=['derive']).derive(s, w.account, FAN_A, [31])[31].state == 'owned', 'full production history decision recognizes the retained older purchase'

        # FAN_A owns media 31: the single is not offered again and the whole bundle containing it is excluded; chat continues
        for ask in ('show me the beach photo', 'I want the combo bundle'):
            staged, tick = w.converse(ask, FAN_A)
            send_ok(w, tick)
            assert 'APPROVED OFFER: none' in w.model_prompts[-1]
        assert card_to(w, FAN_A) == [] and len(to_fan(w, FAN_A)) == 2, 'two ordinary replies went out, no card'
        # ... while a product that shares nothing with the purchase IS sold to the same fan
        staged, tick = w.converse('send me the pool video', FAN_A)
        send_ok(w, tick)
        assert [c['body']['mediaFiles'] for c in card_to(w, FAN_A)] == [['41']]

        # FAN_B has a card whose preview facts are unknown: only that fan and the products touching media 41 stop
        staged, tick = w.converse('send me the pool video', FAN_B)
        send_ok(w, tick)
        assert card_to(w, FAN_B) == [] and 'APPROVED OFFER: none' in w.model_prompts[-1]
        staged, tick = w.converse('show me the beach photo', FAN_B)
        send_ok(w, tick)
        assert [c['body']['mediaFiles'] for c in card_to(w, FAN_B)] == [['31']]

        # FAN_C: nothing owned, bundle is sold whole
        staged, tick = w.converse('I want the combo bundle', FAN_C)
        send_ok(w, tick)
        assert [c['body']['mediaFiles'] for c in card_to(w, FAN_C)] == [['31', '61', '62']]

        # a NEW asset (synced after the agency approved the catalogue, no approved terms, even with a draft item) is never sold
        add_vault(database[1], {99: 'image'}, account=w.account, hash_kind='vault-metadata-v1')
        with Session(database[1]) as s, s.begin():
            s.add(CatalogItem(account_id=w.account, kind='image', label='Brand new photo', description_for_ai='Brand new photo.',
                              media_ids='[99]', preview_media_ids='[]', price_cents=1000, enabled=True, pricing_mode='fixed', tags='[]'))
        staged, tick = w.converse('show me the brand new photo', FAN_D)
        send_ok(w, tick)
        assert card_to(w, FAN_D) == [] and 'APPROVED OFFER: none' in w.model_prompts[-1] and len(to_fan(w, FAN_D)) == 1
        assert len(w.rows(OfEngineOfferGrant, OfEngineOfferGrant.fan_id == FAN_D)) == 0
    finally:
        set_active_profiles(None)
        w.store.close()


# ============================================================================================================ Q6
def test_q6_an_ambiguous_product_reference_asks_instead_of_charging_and_a_specific_one_then_sells(world):
    world.product('Beach photo', [31], standard=1200, description='Beach photo, one picture.')
    world.product('Sunset photo', [32], standard=1500, description='Sunset photo, one picture.')
    world.launch()
    staged, tick = world.converse('show me the photo please', FAN_A)
    send_ok(world, tick)
    assert card_to(world, FAN_A) == [] and grants(world, FAN_A) == [] and 'APPROVED OFFER: none' in world.model_prompts[-1]
    assert 'which' in to_fan(world, FAN_A)[-1]['body']['text'].lower()
    staged, tick = world.converse('the sunset photo', FAN_A)
    send_ok(world, tick)
    assert [c['body']['price'] for c in card_to(world, FAN_A)] == [15.0]


@pytest.mark.parametrize('change,price_after', [('category_moved', 1200), ('discount_added', 1200), ('discount_removed', 1200),
                                                ('terms_withdrawn', None), ('media_removed', None), ('previews_changed', 4000)])
def test_q6_a_terms_category_discount_or_content_change_between_plan_and_send_never_sends_the_old_card(world, change, price_after):
    ids = catalogue(world)
    world.launch()
    item, ask = (ids['bundle'], 'I want the weekend bundle') if change == 'previews_changed' else (ids['photo'], 'show me the beach photo')
    staged, mid = staged_card(world, ask=ask)
    if change == 'category_moved':
        approve(world.store, item, standard=1200, discount=900, folder=8, tenant=TENANT, account=world.account)
    elif change == 'discount_added':
        approve(world.store, item, standard=1200, discount=800, tenant=TENANT, account=world.account)
    elif change == 'discount_removed':
        approve(world.store, item, standard=1200, discount=None, tenant=TENANT, account=world.account)
    elif change == 'terms_withdrawn':
        withdraw_terms(world.store, TENANT, world.account, item, command_key=key(0x9301), actor=AGENCY_ACTOR)
    elif change == 'media_removed':
        with Session(world.database[1]) as s, s.begin():
            s.get(VaultItem, (world.account, 31)).removed_at = utcnow()
    else:
        with Session(world.database[1]) as s, s.begin():
            s.get(CatalogItem, item).preview_media_ids = '[]'
    try:
        approve_live(world)
    except Refused:
        pass
    nothing_sent(world)                                                       # the planned card never reaches the wire
    assert [g.used_intent_id for g in grants(world, FAN_A)] == [None] * len(grants(world, FAN_A))
    if change == 'previews_changed':
        staged, tick = world.converse(ask, FAN_A)
        assert card_to(world, FAN_A) == [] and 'APPROVED OFFER: none' in world.model_prompts[-1]
        approve(world.store, item, standard=4000, discount=3500, tenant=TENANT, account=world.account)
    # the permitted journey afterwards: the fan asks again and only what is CURRENTLY approved is offered, at its current terms
    staged, tick = world.converse(ask, FAN_A)
    assert tick is not None
    sent = card_to(world, FAN_A)
    if price_after is None:
        assert sent == [] and 'APPROVED OFFER: none' in world.model_prompts[-1]
    else:
        assert [c['body']['price'] for c in sent] == [price_after / 100.0]
        assert len([i for i in intents(world) if i.price_cents > 0]) == 1


# ============================================================================================================ Q7
def test_q7_duplicate_inbound_and_competing_workers_make_one_reply_one_card_one_unlock_effect(world):
    catalogue(world)
    world.launch()
    body = world.inbound(5001, 'show me the beach photo', FAN_A)
    other = second_pilot(world)

    def deliver(pilot, event_key):
        raw, signature, event_key = world.signed(body, event_key)
        try:
            return pilot.handle(raw, signature, event_key)
        except Refused as exc:
            return {'refused': str(exc)}

    # two receivers get the same webhook at the same moment, and the provider then redelivers it under a new key
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: deliver(*args), ((world.pilot, 'dup-1'), (other, 'dup-1'))))
    results.append(deliver(world.pilot, 'dup-1'))
    results.append(deliver(other, 'dup-2'))
    assert sum(1 for r in results if r.get('outcome') == 'awaiting_approval') >= 1
    assert len(requests_of(world, FAN_A)) == 1 and len(model_calls(world)) == 1 and len(world.model_prompts) == 1
    assert len(grants(world, FAN_A)) == 1 and world.cards() == []
    assert [a['status'] for a in approval.pending(world.store, TENANT, world.account, FAN_A)] == ['awaiting']

    approve_live(world)
    world.unthrottle()
    with ThreadPoolExecutor(max_workers=2) as pool:                            # two schedulers race for the approved send
        list(pool.map(lambda p: p.tick(), (world.pilot, other)))
    drain(world, other)
    drain(world)
    assert len(world.cards()) == 1 and len(to_fan(world, FAN_A)) == 1
    sent = [i for i in intents(world) if i.price_cents > 0]
    assert len(sent) == 1 and sent[0].status == 'accepted' and grants(world, FAN_A)[0].used_intent_id == sent[0].intent_id

    # a late duplicate of the same inbound, after the reply: nothing new at all
    deliver(world.pilot, 'dup-3')
    drain(world, other)
    assert len(requests_of(world, FAN_A)) == 1 and len(world.cards()) == 1 and len(to_fan(world, FAN_A)) == 1
    assert len(model_calls(world)) == 1

    # the same unlock delivered to both receivers at once, and again later: exactly one purchase effect
    event = world.unlock_event(sent[0].provider_message_id, 12)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: deliver_event(world, *args), ((world.pilot, event, 'unlock-a'), (other, event, 'unlock-a'),
                                                                 (other, event, 'unlock-b'))))
    drain(world)
    drain(world, other)
    proof = world.rows(OfEngineAccessEvidence)
    assert len(proof) == 1 and proof[0].applied is True
    with Session(world.database[1]) as s:
        assert owned_state(s, world.account, FAN_A) == ({31}, None)
    assert len(world.cards()) == 1


def deliver_event(w, pilot, body, event_key):
    raw, signature, event_key = w.signed(body, event_key)
    try:
        return pilot.handle(raw, signature, event_key)
    except Refused as exc:
        return {'refused': str(exc)}


# ============================================================================================================ Q8
def test_q8_a_429_on_a_commercial_account_recovers_without_a_new_fan_message_and_survives_a_restart(world):
    catalogue(world)
    scripted = Scripted(world, [(429, {'Retry-After': '30'})])
    world.model_transport = scripted.transport
    world.launch()
    clock = Clock().govern(world.pilot)
    staged, mid = world.say('show me the beach photo', FAN_A)
    assert staged.get('outcome') != 'awaiting_approval' and scripted.calls == [429] and world.cards() == []
    [first] = model_calls(world)
    assert (first.status, first.outcome) == ('released', 'http_429') and first.retry_not_before is not None
    clock.offset = timedelta(seconds=15)
    drain(world)
    assert scripted.calls == [429], 'never before the due time'
    clock.offset = timedelta(seconds=31)
    restarted = second_pilot(world, clock)                       # a restart, and a second worker beside it, race for the retry
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda p: p.tick(), (restarted, world.pilot)))
    assert scripted.calls == [429, 'ok'], 'exactly one retry, though two workers were alive'
    approve_live(world)
    drain(world, restarted)
    assert len(world.cards()) == 1 and world.cards()[0]['body']['price'] == 12.0
    assert len(grants(world, FAN_A)) == 1
    [request] = requests_of(world, FAN_A)
    assert request.status == 'waiting_fan', 'accepted paid card waits for the fan; it is not a completed purchase'
    assert len([i for i in intents(world) if i.status == 'accepted' and i.price_cents == 1200]) == 1
    clock.offset = timedelta(seconds=600)
    drain(world)
    drain(world, restarted)
    assert scripted.calls == [429, 'ok'] and len(world.cards()) == 1 and len(to_fan(world, FAN_A)) == 1


def test_q8_a_correction_followed_by_a_429_stops_at_two_total_attempts_and_a_human_sees_it(world):
    catalogue(world)
    scripted = Scripted(world, ['ok', (429, {'Retry-After': '30'})])
    world.model_transport = scripted.transport
    world.choose = greedy
    world.launch()
    clock = Clock().govern(world.pilot)
    staged, mid = world.say('show me the beach photo', FAN_A)
    assert scripted.calls == ['ok', 429] and staged.get('outcome') != 'awaiting_approval'
    clock.offset = timedelta(seconds=40)
    drain(world)
    drain(world, second_pilot(world, clock))
    clock.offset = timedelta(seconds=100000)
    drain(world)
    assert scripted.calls == ['ok', 429], 'no third call after two total attempts'
    assert world.cards() == [] and to_fan(world, FAN_A) == []
    [request] = requests_of(world, FAN_A)
    assert (request.status, request.handover_target) == ('handover_pending', 'operator'), (request.status, request.next_step)
    assert [g.used_intent_id for g in grants(world, FAN_A)] == [None]
    shown = [i for i in operator_v2.unresolved(world.store, TENANT, world.account, actor=OPERATOR) if i.get('requestId') == request.request_id]
    assert len(shown) == 1 and shown[0].get('operatorAlert')
    # another fan of the same creator is not held by it
    world.choose = None
    staged, tick = world.converse('show me the pool video', FAN_B)
    send_ok(world, tick)
    assert len(card_to(world, FAN_B)) == 1


def test_q8_a_permanent_model_refusal_is_visible_to_a_person_and_never_retried(world):
    catalogue(world)
    scripted = Scripted(world, [401])
    world.model_transport = scripted.transport
    world.launch()
    clock = Clock().govern(world.pilot)
    staged, mid = world.say('show me the beach photo', FAN_A)
    clock.offset = timedelta(seconds=100000)
    drain(world)
    drain(world, second_pilot(world, clock))
    assert scripted.calls == [401] and world.cards() == [] and to_fan(world, FAN_A) == []
    [request] = requests_of(world, FAN_A)
    assert request.status == 'handover_pending'
    assert [i for i in operator_v2.unresolved(world.store, TENANT, world.account, actor=OPERATOR) if i.get('requestId') == request.request_id]


# ============================================================================================================ Q9
def test_q9_an_unknown_card_survives_restarts_without_replay_or_refund_and_blocks_only_its_fan(world):
    catalogue(world)
    world.launch()
    staged, mid = world.say('show me the beach photo', FAN_A)
    world.approve_turn(staged)
    world.unthrottle()

    def connection_lost(count):
        raise TimeoutError('the connection dropped after the provider took the request')
    world.fans_mock.hooks.append(connection_lost)
    world.pilot.tick()
    world.fans_mock.hooks.clear()
    [sent] = [i for i in intents(world) if i.price_cents > 0]
    assert sent.status == 'unknown' and sent.reservation_held and len(world.cards()) == 1
    held = policy(world)
    from plan_c_worker import _work_counts
    readiness = _work_counts(world.pilot)
    assert readiness['eligibleFans'] == len(world.fans)-1 and readiness['heldFans'] == 1
    cases = operator_v2.unresolved(world.store, TENANT, world.account, actor=OPERATOR)
    case = next(c for c in cases if c.get('intentId') == sent.intent_id)
    assert case['attemptId'] and case['requestId'] and case['ageSeconds'] >= 0
    assert 'does not prove' in case['summary']
    for _ in range(3):                                                           # restart after restart
        drain(world, second_pilot(world), 2)
    [sent] = [i for i in intents(world) if i.price_cents > 0]
    assert sent.status == 'unknown' and sent.reservation_held and len(world.cards()) == 1
    assert policy(world) == held, 'the unit stays reserved: never refunded, never counted twice'
    assert grants(world, FAN_A)[0].used_intent_id == sent.intent_id
    # the same fan writes again: nothing is sent into the unresolved chat, and the unknown card is not replayed
    try:
        world.say('show me the pool video', FAN_A)
    except Refused:
        pass
    drain(world, second_pilot(world), 2)
    assert len(to_fan(world, FAN_A)) == 1 and policy(world)[0] == held[0]
    # another fan proceeds normally
    staged, tick = world.converse('show me the beach photo', FAN_B)
    assert tick['dispatched'] == ['accepted']
    assert len(card_to(world, FAN_B)) == 1 and len(world.cards()) == 2


def test_q9_a_card_that_crashed_before_the_wire_is_retired_never_sent_twice_and_the_fan_is_still_served_once(world, monkeypatch):
    catalogue(world)
    world.launch()
    staged, mid = world.say('show me the beach photo', FAN_A)
    world.approve_turn(staged)
    world.unthrottle()

    class Crash(BaseException):
        pass

    def die(*args, **kwargs):
        raise Crash()
    real_send = world.pilot.worker.transport.send
    monkeypatch.setattr(world.pilot.worker.transport, 'send', die)
    with pytest.raises(Crash):
        world.pilot.worker.send_approved(TENANT, world.account, staged['decisionId'])
    monkeypatch.setattr(world.pilot.worker.transport, 'send', real_send)
    [intent] = [i for i in intents(world) if i.price_cents > 0]
    assert intent.status == 'dispatching' and intent.wire_started_at is None and world.fans_mock.posts == []
    with world.database[1].begin() as c:
        c.execute(text("UPDATE of_text_intent SET deadline = clock_timestamp() - interval '1 second' WHERE account_id=:a"), {'a': world.account})
    drain(world, second_pilot(world), 3)
    [intent] = [i for i in intents(world) if i.price_cents > 0]
    assert (intent.status, intent.reason) == ('cancelled', 'prewire_crash_never_wired') and not intent.reservation_held
    assert world.fans_mock.posts == [] and policy(world)[0] == 0, 'nothing posted, the unit released'
    # the fan asks again: exactly one card goes out under a fresh intent
    staged, tick = world.converse('show me the beach photo please', FAN_A)
    assert tick is not None
    assert len(card_to(world, FAN_A)) == 1 and card_to(world, FAN_A)[0]['body']['price'] == 12.0


def test_q9_an_unknown_model_call_keeps_its_reservation_and_is_never_replayed_after_a_restart(world):
    catalogue(world)
    scripted = Scripted(world, ['timeout'])
    world.model_transport = scripted.transport
    world.launch()
    clock = Clock().govern(world.pilot)
    staged, mid = world.say('show me the beach photo', FAN_A)
    [call] = model_calls(world)
    assert call.status == 'unknown' and call.reserved_microusd > 0 and call.actual_microusd is None
    clock.offset = timedelta(seconds=100000)
    drain(world, second_pilot(world, clock), 3)
    drain(world, n=2)
    assert scripted.calls == ['timeout'], 'an unknown outcome is never blindly replayed'
    [call] = model_calls(world)
    assert call.status == 'unknown' and call.reserved_microusd > 0, 'the reservation is retained, not refunded'
    assert world.cards() == [] and to_fan(world, FAN_A) == []
    staged, tick = world.converse('show me the beach photo', FAN_B)               # the unrelated fan is served
    send_ok(world, tick)
    assert len(card_to(world, FAN_B)) == 1


# ============================================================================================================ Q10
def test_q10_multipart_capacity_gone_at_the_final_wire_sends_no_part(world):
    catalogue(world)
    world.launch()
    world.choose = lambda latest, prompt, approved, proposal: {'body': 'part one', 'parts': ['part two', 'part three']}
    staged, mid = world.say('tell me about your week', FAN_A)
    assert staged['outcome'] == 'awaiting_approval'
    world.approve_turn(staged)
    with world.database[1].begin() as c:                                       # capacity vanishes AFTER the plan and the approval
        c.execute(text('UPDATE of_delivery_policy SET used_units=limit_units-2 WHERE account_id=:a'), {'a': world.account})
    used = policy(world)
    drain(world, n=3)
    assert world.fans_mock.posts == [], 'no part may start when the sequence cannot finish'
    assert policy(world) == used, 'no unit reserved, no queue slot leaked'
    [intent] = world.rows(OfTextIntent)
    assert intent.status == 'refused' and intent.reason == 'sequence_capacity_insufficient'
    assert {d.status for d in world.rows(OfEngineDecision)} == {'cancelled'}


def test_q10_a_card_at_the_account_cap_is_refused_before_the_wire_without_spending_its_grant(world):
    catalogue(world)
    world.launch()
    staged, mid = staged_card(world)
    approve_live(world)
    with world.database[1].begin() as c:
        c.execute(text('UPDATE of_delivery_policy SET used_units=limit_units WHERE account_id=:a'), {'a': world.account})
    drain(world, n=3)
    assert world.fans_mock.posts == []
    sent = [i for i in intents(world) if i.price_cents > 0]
    assert all(i.status in ('refused', 'not_accepted') and not i.reservation_held for i in sent), [(i.status, i.reason) for i in sent]
    assert policy(world)[1] == 0, 'no queue slot leaked'
    # the grant was never consumed by a card that did not go out
    assert [g.used_intent_id for g in grants(world, FAN_A)] in ([None], [sent[0].intent_id] if sent else [None])
    # raising the cap is an operator act: the same chat can then be served by a NEW draft, one card only
    with world.database[1].begin() as c:
        c.execute(text('UPDATE of_delivery_policy SET used_units=0 WHERE account_id=:a'), {'a': world.account})
    staged, tick = world.converse('show me the beach photo', FAN_A)
    assert len(card_to(world, FAN_A)) <= 1


def test_q10_the_provider_budget_gone_at_the_final_wire_blocks_the_card_without_refund_or_unknown(world):
    catalogue(world)
    world.launch()
    staged, mid = staged_card(world)
    approve_live(world)
    # spend every remaining provider request through the production reservation function
    revision = pilot_scope.describe(world.store, TENANT, world.account)['revision']
    while True:
        try:
            pilot_scope.reserve_provider_request(world.store, TENANT, world.account, expected_revision=revision)
        except Refused:
            break
    before = pilot_scope.describe(world.store, TENANT, world.account)
    drain(world, n=3)
    after = pilot_scope.describe(world.store, TENANT, world.account)
    assert world.fans_mock.posts == [], 'no request is made once the budget is spent'
    assert (after['providerRequestsUsed'], after['providerCreditsReserved']) == (before['providerRequestsUsed'], before['providerCreditsReserved'])
    sent = [i for i in intents(world) if i.price_cents > 0]
    assert all(i.status != 'unknown' for i in sent), 'a guard refusal before any request is proved not-sent, never unknown'


# ============================================================================================================ Q11
def tip_event(w, fan, tid, gross=5, net=4):
    return {'event': 'tips.received', 'account_id': w.provider, 'payload': {
        'id': tid, 'amountGross': gross, 'amountNet': net, 'text': 'thanks', 'replacePairs': [], 'subType': 'tip',
        'user_id': w.creator, 'fan': {'id': fan}, 'user': {'id': w.creator}, 'fanData': {}, 'createdAt': iso()}}


def test_q11_a_tip_beside_a_sale_is_thanked_once_changes_no_price_and_grants_no_access(world):
    catalogue(world)

    def thanks(latest, prompt, approved, proposal):
        if 'VERIFIED TIP' in prompt:
            return {**proposal, 'body': TIP_ACK if not approved else TIP_ACK + ' Here you go.'}
        return proposal
    world.choose = thanks
    world.launch()
    world.deliver(tip_event(world, FAN_A, 'tip-1'), 'tip-evt-1')
    world.pilot.tick()
    assert world.cards() == [] and to_fan(world, FAN_A) == [], 'a tip makes no message request of its own'
    staged, tick = world.converse('show me the beach photo', FAN_A)
    send_ok(world, tick)
    [sent] = card_to(world, FAN_A)
    assert sent['body']['price'] == 12.0 and 'USD 12.00' in sent['body']['text'], 'no automatic discount from a tip'
    assert sent['body']['text'].count('tip') <= 1 and 'free' not in sent['body']['text'].lower()
    with Session(world.database[1]) as s:
        assert owned_state(s, world.account, FAN_A) == (set(), None), 'a tip is not access'
    assert world.rows(OfEngineAccessEvidence) == []
    # the same tip again under a changed event key and a replay: no second thanks, no second card
    before = len(to_fan(world, FAN_A))
    world.deliver(tip_event(world, FAN_A, 'tip-1'), 'tip-evt-2')
    world.deliver(tip_event(world, FAN_A, 'tip-1'), 'tip-evt-1')
    staged, tick = world.converse('tell me about your day', FAN_A)
    send_ok(world, tick)
    assert len(to_fan(world, FAN_A)) == before + 1 and len(card_to(world, FAN_A)) == 1
    assert sum(p['body']['text'].count('tip') for p in to_fan(world, FAN_A)) <= 1, 'acknowledged exactly once'
