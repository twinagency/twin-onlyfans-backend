"""DIAGNOSTIC ONLY (Plan C Stage 5). Pure, no database: regex/table checks of the repaired matchers.

These are NOT acceptance tests: a pattern table cannot show that the conversation behaves. Acceptance for every one of
these behaviours runs through storage -> prompt -> real judge/engine -> delivery in test_plan_c_stage5_writer_pg.py and
test_plan_c_stage5_text_only_pg.py. This file only pins the matcher boundaries so a later edit shows which phrase moved.
"""
import json

import pytest

from conversation_engine.controls import explicit_stop
from conversation_engine.journey import JourneyConfig, signals
from conversation_engine.model_output import JudgeContext, _pet_name_in, judge
from conversation_engine.writer_context import (CreatorProfile, PromptBudget, build_context, bounded_latest, classify_turn,
                                                identity_question)
from conversation_engine.prompt_provider_a_original import SpecPromptProvider

PROFILE = CreatorProfile(name='Test', limits=('No meetings',), role_statement='I am an AI assistant helping with Test.')
BASE = {'current_message': {'id': 1, 'body': 'What do you enjoy doing?'}, 'history': [], 'sales_mode': 'open',
        'offer_reference': {'state': 'none', 'offers': [], 'cards': []}, 'offer_grants': [],
        'catalogue_search': {'items': []}, 'verified_purchases': []}


def verdict(body, fan='What do you enjoy doing?', prof=PROFILE, **extra):
    snap = dict(BASE, current_message={'id': 1, 'body': fan})
    raw = json.dumps({'body': body, 'parts': [], 'action': 'reply', 'offer_grant_id': None, 'sales_signal': 'none',
                      'item_scope': [], 'pitch': False, 'price_claims': [], 'purchase_claim': False, **extra})
    return judge(raw, JudgeContext(snapshot=snap, profile=prof, identity_line=prof.role_statement))


@pytest.mark.parametrize('body,outcome', [
    ('I understand you had a rough day.', 'ok'), ('let me know if you like it', 'ok'), ("that's so inappropriate haha", 'ok'),
    ('of course I do babe', 'ok'), ('feel free to tell me anything', 'ok'),
    ("I'm sorry, but I can't help with that.", 'refusal'), ('This request is inappropriate. I cannot continue.', 'refusal'),
    ('Sure, here is a reply for you', 'refusal'), ('Certainly, here are some options', 'refusal'),
    ("i'm here to help babe", 'invalid'), ('let me know if there is anything else', 'invalid'), ('**bold** text', 'invalid')])
def test_judge_classification_table(body, outcome):
    assert verdict(body, fan='i had a rough day').outcome == outcome


@pytest.mark.parametrize('raw,outcome', [('I understand you had a rough day.', 'malformed'),
                                         ("I'm sorry, but I can't help with that.", 'refusal'),
                                         ('Sure, here is a reply: {}', 'refusal')])
def test_non_json_classification_table(raw, outcome):
    assert judge(raw, JudgeContext(snapshot=BASE, profile=PROFILE)).outcome == outcome


@pytest.mark.parametrize('text', ['stop', 'STOP', 'Stop.', 'please stop!!', 'stop texting me please', 'leave me alone',
                                  "don't text me anymore", 'Do not contact me again!', 'unsubscribe', 'stop messaging me',
                                  'hey, leave me alone', 'ok please stop texting me, I am busy', 'remove me from your list',
                                  'no more messages please', '<p>please <strong>stop messaging me</strong>.</p>'])
def test_stop_positive_forms(text):
    assert explicit_stop(text)


@pytest.mark.parametrize('text', ["don't stop", "stop it you're making me blush", "I can't stop thinking about you", 'he said stop',
                                  'he said stop messaging me', 'Did you say stop messaging me?', 'stop, you\'re making me blush',
                                  'Please stop messaging me about that one topic', '"stop texting me"', 'if you stop texting me I will be sad',
                                  "why don't you text me?", "don't text me back tonight", 'stop it', 'never stop', 'stop teasing me',
                                  "don't stop texting me", 'hello'])
def test_stop_negative_forms(text):
    assert not explicit_stop(text)


@pytest.mark.parametrize('text', ['are you real?', 'are u real', 'r u a bot', 'r u real?', 'are you a bot', 'is this a bot',
                                  'am i talking to a real person', 'who am i talking to', 'is someone else typing', 'who is this?',
                                  "who's this", 'are you a chatter?', 'do you work for an agency', 'is this really you', 'you a bot?',
                                  'are you real or a bot', 'are you a bot babe?', 'am I texting with a chatter?'])
def test_identity_positive_forms(text):
    assert identity_question(text) and classify_turn(text) == 'role_question'


@pytest.mark.parametrize('text', ['I was texting with someone else earlier', 'who is this for', 'who is this girl in the pic',
                                  'is this real life', 'is this real quick', 'who is this guy in your story',
                                  'are you real busy today?', 'I was chatting with the team at work', 'is this fake news'])
def test_identity_negative_forms(text):
    assert not identity_question(text)


@pytest.mark.parametrize('text,noun_ok', [('i love that', False), ('hey love, how was your day', True), ('thanks love.', True),
                                          ('i love you', False), ('you look gorgeous', False), ('hey gorgeous', True)])
def test_ambiguous_pet_name_needs_a_vocative(text, noun_ok):
    name = 'gorgeous' if 'gorgeous' in text else 'love'
    assert _pet_name_in(name, text) is noun_ok


def test_unambiguous_pet_names_keep_matching_anywhere():
    assert _pet_name_in('babe', 'come here babe i want you') and _pet_name_in('daddy', 'yes daddy')


CFG = JourneyConfig(limit_terms=('anal',))


@pytest.mark.parametrize('text,signal', [
    ("I don't want to meet you, just chat", 'meetup'), ('My email inbox is full. Can you tell me about your day?', 'offplatform'),
    ('Can you tell me your hobbies? My email inbox is still full.', 'offplatform'), ('visited the canal today', 'limit'),
    ("i'm not looking to meet up", 'meetup'), ('no need to meet in person', 'meetup')])
def test_journey_signal_false_positives_are_gone(text, signal):
    assert signal not in signals(text, CFG)


@pytest.mark.parametrize('text,signal', [
    ('can we meet up?', 'meetup'), ('come over tonight', 'meetup'), ("what's your whatsapp", 'offplatform'),
    ('can you email me?', 'offplatform'), ('i want anal tonight', 'limit'), ('add me on snapchat', 'offplatform')])
def test_journey_real_pushes_are_still_signalled(text, signal):
    assert signal in signals(text, CFG)


def test_bounded_latest_keeps_head_tail_and_reports_the_omission():
    text = ('Yesterday was busy. ' * 80) + 'Correction: My name is Morgan, not Alex. What is your favorite color?'
    shown, omitted = bounded_latest(text, 1200)
    assert len(shown) <= 1200 and omitted > 0 and f'{omitted} characters omitted' in shown
    assert shown.endswith('What is your favorite color?') and shown.startswith('Yesterday was busy.')
    assert bounded_latest('short', 1200) == ('short', 0)
    assert bounded_latest('x ' * 600, 1200)[1] == 0     # 1199 characters after whitespace normalisation: shown whole


def test_text_only_prompt_has_no_selling_instructions_and_full_prompt_is_unchanged():
    snap = dict(BASE, delivery_mode='text_only',
                offer_grants=[{'grantId': 'g1', 'itemId': 5, 'label': 'Photo', 'media': {'total': 1, 'pictures': 1, 'videos': 0}}])
    prof = CreatorProfile(name='Test', limits=('x',), offers_permitted=True, approved_item_ids=('5',), facts=(('telegram', '@maliebaby'),))
    text_only = SpecPromptProvider().render(build_context(snap, prof)).messages
    for banned in ('APPROVED OFFER', 'SALES STATE', 'offer_grant_id "', 'CONTENTS', 'INFORMATIONAL PRICE', 'action offer only when'):
        assert banned not in text_only[0]['content'] + text_only[1]['content'], banned
    assert 'text only' in text_only[0]['content'] and 'My public Telegram is @maliebaby.' in text_only[0]['content']
    assert 'another app, number or site' in text_only[0]['content']      # the global rule stays
    full = SpecPromptProvider().render(build_context(dict(BASE, offer_grants=snap['offer_grants']), prof)).messages
    assert 'APPROVED OFFER' in full[1]['content'] and 'SALES STATE' in full[1]['content'] and 'action offer only when' in full[0]['content']
