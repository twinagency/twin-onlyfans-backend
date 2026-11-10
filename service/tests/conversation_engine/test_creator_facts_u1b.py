"""U1b: unsupported or contradicted creator facts (job, pets, siblings, education, nationality, exclusivity, current local
time) are rejected by the shared judge; supported facts, fan facts, corrections, quotes, hypotheticals and ordinary talk are not.

Pure judge-level tests (no database, no network). Each decision about ambiguous wording is written down in a test below.
Facts come ONLY from the approved profile; an absent fact is never a negative and never permission to invent one.
"""
import json

import pytest

from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile

BASE = {'age': '27', 'job': 'barista', 'home_city': 'Perth', 'limits': 'no meetups'}
PROFILE = CreatorProfile.from_facts('Riley', BASE, voice='warm')
RICH = CreatorProfile.from_facts('Riley', {**BASE, 'pets': 'a dog named Max', 'family': 'two older brothers',
                                           'education': 'nursing at UQ', 'nationality': 'Brazilian'}, voice='warm')


def snap(message='tell me about you', history=()):
    return {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'delivery_mode': 'text_only',
            'current_message': {'id': 9, 'body': message},
            'history': [{'direction': 'in', 'body': b} for b in history],
            'memory': {'state': 'current', 'memory': {'facts': {}}}, 'offer_reference': {'state': 'none', 'offers': []},
            'catalogue_search': {'items': []}, 'offer_grants': [], 'price_information': [], 'verified_purchases': []}


def verdict(body, message='tell me about you', profile=PROFILE, history=()):
    return judge(json.dumps({'body': body}), JudgeContext(snapshot=snap(message, history), profile=profile))


def rejected(body, message='tell me about you', profile=PROFILE, history=()):
    v = verdict(body, message, profile, history)
    assert v.outcome == 'invalid' and v.proposal is not None, (body, v.outcome, v.reasons)
    return v.reasons


def accepted(body, message='tell me about you', profile=PROFILE, history=()):
    v = verdict(body, message, profile, history)
    assert v.outcome == 'ok', (body, v.outcome, v.reasons)


# ---- the preserved counterexamples (probe_creator_facts.log in evidence/u5-u1) ------------------------------------------------
@pytest.mark.parametrize('body', ['I work as a nurse at the hospital', 'I work as a nurse', "I'm a nurse", 'my job is nursing',
                                  'I used to work as a nurse', 'i work as a waitress', "I am a teacher"])
def test_job_that_contradicts_the_documented_job_is_rejected(body):
    assert any('job' in r for r in rejected(body, 'what do you do?'))


@pytest.mark.parametrize('body', ['I work as a barista', "I'm a barista", 'i work as a barista at a little cafe', 'my job is barista'])
def test_documented_job_is_answerable(body):
    accepted(body, 'what do you do?')


def test_job_is_unsupported_when_no_job_is_documented():
    bare = CreatorProfile.from_facts('Riley', {'age': '27', 'limits': 'no meetups'})
    assert any('job' in r for r in rejected('I work as a barista', 'what do you do?', bare))
    accepted('long shift, my feet hurt', 'how was your day', bare)


@pytest.mark.parametrize('body,why', [
    ('I have a dog named Max', 'pet'), ('my cat is so cute', 'pet'), ('I have two cats', 'pet'), ("I've got a puppy", 'pet'),
    ('I grew up with three brothers', 'famil'), ('I have two sisters', 'famil'), ('my brother just called me', 'famil'),
    ("I'm an only child", 'famil'),
    ('I studied nursing at UQ', 'education'), ("I'm a uni student", 'education'), ('I have a degree in law', 'education'),
    ("I'm Brazilian", 'nationality'), ("I'm half Italian", 'nationality'), ('i am french', 'nationality'),
    ("it's 3am here", 'local time'), ("its 3:15 pm for me right now", 'local time'), ("it's almost midnight here", 'local time'),
])
def test_invented_personal_facts_are_rejected_when_not_documented(body, why):
    assert any(why in r for r in rejected(body)), body


@pytest.mark.parametrize('body', ["I'm only texting you", 'i only talk to you babe, nobody else', 'I only text you, no one else',
                                  'I only chat with you', "I'm not talking to anyone else but you"])
def test_exclusivity_claims_are_rejected(body):
    assert any('exclusiv' in r for r in rejected(body, 'hey')), body


# ---- documented facts stay answerable; wrong details of a documented topic are contradictions ---------------------------------
@pytest.mark.parametrize('body', ['I have a dog named Max', 'my dog Max is asleep next to me', 'I grew up with two brothers',
                                  'I have two older brothers', 'I studied nursing at UQ', "I'm Brazilian", 'I have a dog'])
def test_documented_personal_facts_are_answerable(body):
    accepted(body, profile=RICH)


@pytest.mark.parametrize('body', ['I have a dog named Rex', 'I have two cats', 'I grew up with three brothers', 'I have two sisters',
                                  'I studied law at Harvard', "I'm Italian", 'I work as a nurse'])
def test_a_documented_topic_does_not_license_a_different_detail(body):
    rejected(body, profile=RICH)


# ---- fan facts are not creator facts; direct corrections; questions; quotes; hypotheticals ----------------------------------
@pytest.mark.parametrize('body,message', [
    ('you work as a nurse? that is so cool', 'I work as a nurse'),
    ('a dog named Max, you must love him. do you have a cat too?', 'I have a dog named Max'),
    ("you're Brazilian, I love that", "I'm Brazilian"),
    ('oh you studied nursing, tell me more', 'I studied nursing at uni'),
    ('three brothers, wow, was it loud?', 'I grew up with three brothers'),
    ("it's 3am for you?? go to sleep", "it's 3am here lol"),
    ("it's 3am, you should be asleep", "its 3am and I can't sleep"),
])
def test_fan_facts_reflected_back_are_not_creator_claims(body, message):
    accepted(body, message)


@pytest.mark.parametrize('body,message', [
    ('I never said I was a nurse, I work as a barista', 'you told me you are a nurse'),
    ('no, I am not a nurse, I work as a barista', 'wait are you a nurse?'),
    ("I don't have a dog, no pets for me", 'do you have a dog?'),
    ("I'm not Brazilian, no", 'you are Brazilian right?'),
    ("no I don't have any brothers", 'do you have brothers?'),
])
def test_direct_corrections_and_denials_are_not_claims(body, message):
    accepted(body, message)


@pytest.mark.parametrize('body', [
    'if I had a dog I would call him Max', 'imagine I was a nurse for you', 'you said "I work as a nurse" haha, nope',
    "in another life I'd be a nurse", 'do I look like I have a dog?', 'am I a nurse? you tell me', 'maybe I will get a cat one day',
    'I want to have a dog someday', 'would you like me to be a teacher for you?',
])
def test_quoted_hypothetical_and_question_wording_is_not_a_claim(body):
    accepted(body)


# ---- no blanket bans: ordinary talk, numbers, empathy and the word "only" -----------------------------------------------------
@pytest.mark.parametrize('body', [
    'I only just woke up', 'I only had coffee today', 'only you could make me smile at this hour', 'I only text you back when my shift ends',
    'I just woke up, give me ten minutes', 'long shift, my feet hurt', "I'm a little tired but I'm happy you messaged",
    "I'm a bit shy at first", 'I work late tonight', 'I work from home sometimes', "I'm at work right now", 'it takes 3 hours to get there',
    "it's 3 minutes from here", 'I am free at 3pm tomorrow', "it was 3am when I finally got to bed", "it's 5pm somewhere, cheers",
    "that sounds rough, I'm sorry you had a hard day", 'I studied hard for that exam and I am so glad it is over',
    "I'm studying for a test tonight, so my night is boring", 'my phone is on 3 percent', 'I have a long day tomorrow',
    'I have two minutes before my break', "I'm just texting you to say hi", 'I just talk too much when I am nervous', "I'm in a good mood now", 'I work out every morning', 'I have a thing for confident guys',
])
def test_ordinary_conversation_is_not_banned(body):
    accepted(body, 'how was your day')


# ---- existing age / city / identity rejections stay ---------------------------------------------------------------------------
@pytest.mark.parametrize('body', ["I'm 24", 'I live in Paris', 'my real name is Jessica', "I'm a real person"])
def test_age_city_name_and_real_person_claims_stay_rejected(body):
    rejected(body)


# ---- decisions on ambiguous wording (conservative; each is a test so a change is deliberate) --------------------------------
def test_decision_home_city_does_not_document_a_nationality():
    """Perth is documented; "Australian" is not. A nationality needs its own documented fact (or the country named in one)."""
    assert any('nationality' in r for r in rejected("I'm Australian"))
    accepted("I'm Australian", profile=CreatorProfile.from_facts('Riley', {**BASE, 'nationality': 'Australian'}))
    accepted("I'm Australian", profile=CreatorProfile.from_facts('Riley', {**BASE, 'background': 'born in Australia'}))


def test_decision_workplace_only_and_vague_work_talk_is_not_judged():
    """Only an explicit occupation ("I work as ...", "my job is ...", "I'm a nurse") is a claim. "I work at the hospital" names a
    workplace, not an occupation, and nothing documents workplaces: left to the writer prompt, recorded as a residual."""
    accepted('I work at the cafe on weekends', 'what do you do?')


def test_decision_second_unsupported_fact_in_the_same_text_is_still_found():
    reasons = rejected('I work as a barista and I have a dog named Max, I also studied law', 'tell me about you')
    assert any('pet' in r for r in reasons) and any('education' in r for r in reasons) and not any('job' in r for r in reasons)


def test_decision_operator_edits_use_the_same_checks():
    from conversation_engine.model_output import edit_problems
    ctx = JudgeContext(snapshot=snap(), profile=PROFILE)
    assert any('pet' in r for r in edit_problems(['I have a dog named Max'], ctx))
    assert edit_problems(['I work as a barista'], ctx) == []


def test_reasons_do_not_invent_a_limitation():
    for body in ['I work as a nurse', "it's 3am here", "I'm only texting you"]:
        assert all('cannot' not in r.lower() and 'limit' not in r.lower() for r in rejected(body))
