"""V5 repair B: a creator-fact claim is grounded in the relevant profile field keeping WHO the fact describes and whether it is
affirmative or negative. Pure judge-level tests. Another person's occupation never supports hers, a denied pet never supports
ownership, an unrelated field or the fan's own claim never becomes her fact, ambiguity is not positive support."""
import json

import pytest

from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile

BASE = {'age': '27', 'home_city': 'Perth', 'limits': 'no meetups'}
Q3 = CreatorProfile.from_facts('Riley', {**BASE, 'job': 'teacher', 'family': 'my sister is a nurse', 'pets': 'no dogs'}, voice='warm')


def profile(**facts):
    return CreatorProfile.from_facts('Riley', {**BASE, **facts}, voice='warm')


def verdict(body, prof, message='tell me about you', history=()):
    snap = {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'delivery_mode': 'text_only',
            'current_message': {'id': 9, 'body': message}, 'history': [{'direction': 'in', 'body': b} for b in history],
            'memory': {'state': 'current', 'memory': {'facts': {}}}, 'offer_reference': {'state': 'none', 'offers': []},
            'catalogue_search': {'items': []}, 'offer_grants': [], 'price_information': [], 'verified_purchases': []}
    return judge(json.dumps({'body': body}), JudgeContext(snapshot=snap, profile=prof))


def rejected(body, prof, why, **kw):
    v = verdict(body, prof, **kw)
    assert v.outcome == 'invalid' and any(why in r for r in v.reasons), (body, v.outcome, v.reasons)


def accepted(body, prof, **kw):
    v = verdict(body, prof, **kw)
    assert v.outcome == 'ok', (body, v.outcome, v.reasons)


# ---- the Q3 synthetic profile ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('body', ['I work as a nurse.', "I'm a nurse", 'I am a nurse', 'my job is nursing', 'I work as a nurse at the hospital',
                                  'I used to work as a nurse'])
def test_another_persons_occupation_does_not_support_her_job(body):
    rejected(body, Q3, 'job', message='What do you do?')


@pytest.mark.parametrize('body', ['I have a dog.', 'I have two dogs', 'my dog is asleep next to me', "I've got a puppy", 'I have a dog named Max'])
def test_a_denied_pet_does_not_support_ownership(body):
    rejected(body, Q3, 'pet', message='Do you have a dog?')


@pytest.mark.parametrize('body,message', [('I work as a teacher.', 'What do you do?'), ("I'm a teacher", 'what do you do'),
                                          ('Good to hear from you.', 'hello'), ('I have a sister', 'tell me about your family'),
                                          ("I don't have a dog", 'do you have a dog?'), ('no dogs for me, sadly', 'do you have a dog?'),
                                          ('I have no dogs', 'any pets?'), ('my sister is a nurse', 'what about your family?'),
                                          ("I don't work as a nurse, I teach", 'are you a nurse?'),
                                          ('my sister is a nurse and I work as a teacher', 'tell me about you')])
def test_truthful_statements_and_denials_pass(body, message):
    accepted(body, Q3, message=message)


# ---- neighbours: other keys, other people, negation, unrelated fields, the fan's claim ---------------------------------------------
def test_job_field_containing_both_creator_and_other_person_keeps_only_hers():
    p = profile(job='barista; my sister is a nurse')
    accepted('I work as a barista', p)
    rejected('I work as a nurse', p, 'job')


@pytest.mark.parametrize('key', ['job', 'occupation', 'work', 'profession', 'career'])
def test_any_documented_job_key_supports_the_job(key):
    accepted("I'm a barista", profile(**{key: 'barista'}))
    rejected("I'm a nurse", profile(**{key: 'barista'}), 'job')


def test_negated_job_is_not_support():
    rejected("I'm a nurse", profile(job='not a nurse, just a barista'), 'job')
    rejected("I'm a nurse", profile(job='no nursing, barista'), 'job')


def test_pet_items_are_not_pooled_and_negation_covers_its_list():
    p = profile(pets='a cat named Tom, no dogs or hamsters')
    accepted('I have a cat named Tom', p)
    accepted('my cat Tom is asleep', p)
    rejected('I have a dog', p, 'pet')
    rejected('I have a hamster', p, 'pet')
    rejected('I have two cats', p, 'pet')
    rejected('I have a cat named Max', p, 'pet')


def test_ambiguous_negation_scope_is_not_positive_support():
    p = profile(pets='no dogs, a cat named Tom')   # unclear whether the cat is also denied
    rejected('I have a cat named Tom', p, 'pet')


def test_pet_belonging_to_someone_else_is_not_hers():
    p = profile(pets="my sister has a dog; a cat named Tom")
    rejected('I have a dog', p, 'pet')
    accepted('I have a cat named Tom', p)
    accepted('my sister has a dog', profile(pets='no dogs', family='one sister, one brother'))             # about her sister, not an ownership claim
    accepted("my brother's dog is so loud", profile(pets='no dogs', family='one sister, one brother'))


def test_none_means_no_pets():
    rejected('I have a cat', profile(pets='none'), 'pet')
    rejected('I have a cat', profile(pets='no pets'), 'pet')


def test_family_counts_are_not_borrowed_from_other_relatives():
    p = profile(family='two older brothers; my sister is a nurse')
    accepted('I have two older brothers', p)
    accepted('I have a sister', p)
    rejected('I have two sisters', p, 'famil')
    rejected('I have three brothers', p, 'famil')
    rejected('I have a brother', profile(family='no brothers or sisters'), 'famil')


def test_education_and_nationality_follow_subject_and_negation():
    rejected('I studied law', profile(education='my brother studied law'), 'education')
    rejected("I'm a uni student", profile(education='no university'), 'education')
    accepted('I studied nursing at UQ', profile(education='nursing at UQ'))
    rejected("I'm Brazilian", profile(background='my mother is Brazilian'), 'nationality')
    rejected("I'm Brazilian", profile(nationality='not Brazilian'), 'nationality')
    accepted("I'm Brazilian", profile(nationality='Brazilian'))
    accepted("I'm Australian", profile(background='born in Australia'))


def test_an_unrelated_field_never_becomes_her_fact():
    p = profile(job='teacher', kinks='nurse roleplay', likes='walking my neighbour dog', tattoos='a small rose')
    rejected("I'm a nurse", p, 'job')
    rejected('I have a dog', p, 'pet')
    accepted("I'm a teacher", p)


def test_the_fans_own_claim_never_becomes_her_fact():
    rejected('I have a dog too!', Q3, 'pet', message='I have a dog named Rex', history=['I have a dog named Rex'])
    rejected("I'm a nurse too", Q3, 'job', message="I'm a nurse", history=["I'm a nurse"])
    accepted('you have a dog named Rex? lucky', Q3, message='I have a dog named Rex')


def test_documented_paraphrases_still_pass():
    p = profile(job='Cover story: barista at a little cafe', pets='a dog named Max', family='two older brothers')
    for body in ('I work as a barista', "I'm a barista", 'my job is barista', 'I have a dog', 'my dog Max is asleep', 'I grew up with two brothers'):
        accepted(body, p)
