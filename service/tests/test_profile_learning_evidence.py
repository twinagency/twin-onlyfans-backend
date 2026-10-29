import pytest
from onlyfans_runtime.profile_learning.evidence import digest, merge, summary, validate


def source(body='Me llamo Carlos', direction='in', automation=None):
    return {'text': body, 'direction': direction, 'at': '2026-01-01',
            'digest': digest(body), 'automation': automation}


def proposal(field='name', value='Carlos', quote='Me llamo Carlos', key='1:2'):
    return {'facts': [{'field': field, 'value': value, 'source': key, 'quote': quote}]}


def test_multilingual_explicit_statement():
    assert validate(proposal(), {'1:2': source()})[0]['value'] == 'Carlos'


@pytest.mark.parametrize('response', [None, [], {'facts': 'no'}, {'facts': [], 'summary': 'invented'}])
def test_invalid_shapes(response):
    with pytest.raises(ValueError):
        validate(response, {})


@pytest.mark.parametrize('change', [
    {'value': 'invented'}, {'source': 'other:fan'}, {'quote': 'invented'},
    {'field': 'wealth'}, {'field': 'health'}, {'value': ''}, {'quote': ''},
])
def test_unsupported_or_unquoted_claims(change):
    value = proposal()
    value['facts'][0].update(change)
    assert not validate(value, {'1:2': source()})


def test_no_fan_fact_from_creator_message():
    assert not validate(proposal(), {'1:2': source(direction='out')})


def test_no_automatic_message_biography():
    facts=validate(proposal('hobbies'), {'1:2': source(direction='out', automation='ai')}, creator=True)
    assert facts[0]['attribution']=='automation account claim'


def test_creator_claims_remain_unverified():
    value = validate(proposal('hobbies'), {'1:2': source(direction='out')}, creator=True)
    assert value[0]['attribution'] == 'account claim'
    assert 'Account claim' in summary(value, creator=True)


@pytest.mark.parametrize('age', ['17', '121', 'twenty', '２０'])
def test_invalid_ages(age):
    assert not validate(proposal('age', age, age), {'1:2': source(age)})


def test_updates_keep_old_facts_deduplicate_and_preserve_conflicts():
    old = validate(proposal(), {'1:2': source()})
    newer = validate(proposal(value='Bob', quote='Bob', key='1:3'), {'1:3': source('Bob')})
    assert len(merge(old, old)) == 1
    result=merge(old,newer)
    assert len(result)==1 and result[0]['status']=='uncertain'
    assert result[0]['conflicts'][0]['value']=='Bob'


def test_conversation_activity_does_not_evict_personal_facts():
    old = validate(proposal(), {'1:2': source()})
    topics = [{**old[0], 'field': 'recent_topic', 'source': f'1:{i}', 'at': f'2026-02-{i:03}'}
              for i in range(100)]
    result = merge(old, topics)
    assert any(f['field']=='name' and f['value']=='Carlos' for f in result)
    assert len(result)==2
    assert len(next(f for f in result if f['field']=='recent_topic')['evidence'])==1  # same evidence is not duplicated


def test_silent_fan_summary_is_honest():
    assert 'No supported personal details' in summary([])


@pytest.mark.parametrize('confirmation', ["I've always lived here", 'here', 'I live there'])
def test_referential_location_confirmation_does_not_conflict_with_named_place(confirmation):
    named = validate(proposal('location', 'New Jersey', "I'm in New Jersey"),
                     {'1:2': source("I'm in New Jersey")})
    repeated = validate(proposal('location', confirmation, confirmation, '1:3'),
                        {'1:3': source(confirmation)})
    memories = merge(named, repeated)
    assert len(memories) == 1 and memories[0]['value'] == 'New Jersey'
    assert not memories[0].get('conflicts')


def test_relative_proximity_is_not_an_identifiable_current_location():
    body='Like 10min from where I usually go. Pretty close lol'
    assert not validate(proposal('location', '10min from where I usually go', body),
                        {'1:2': source(body)})


def test_hometown_and_current_residence_are_distinct_memories():
    born=validate(proposal('hometown', 'Hawaii', 'Born and raised in Hawaii'),
                  {'1:2':source('Born and raised in Hawaii')})
    current=validate(proposal('location', 'Palolo valley', 'I live in Palolo valley', '1:3'),
                     {'1:3':source('I live in Palolo valley')})
    merged=merge(born,current)
    assert len(merged)==2 and all(not f.get('conflicts') for f in merged)


def test_short_answer_retains_disambiguating_question_in_reply_context():
    from onlyfans_runtime.profile_learning.evidence import select_memories
    sources={'q':{**source('Do you enjoy the ocean?',direction='out'),'fan':1,'context_only':True},
             '1:2':{**source('Always'),'fan':1}}
    result=proposal('interests','Always','Always')
    result['facts'][0].update(context_sources=['q'],context_quotes=['Do you enjoy the ocean?'])
    memories=select_memories(validate(result,sources),now='2026-01-02')
    assert memories[0]['contextEvidence'][0]['quote']=='Do you enjoy the ocean?'
    assert 'Do you enjoy the ocean?' in summary(validate(result,sources),now='2026-01-02')


def test_repeated_photo_requests_merge_without_erasing_the_request():
    old=validate(proposal('request','Send me a pic','Send me a pic'),{'1:2':source('Send me a pic')})
    new=validate(proposal('request','Please send another picture','Please send another picture','1:3'),
                 {'1:3':source('Please send another picture')})
    result=merge(old,new)
    assert len(result)==1 and len(result[0]['evidence'])==2
    assert result[0]['status']=='current' and not result[0].get('conflicts')


def test_old_requests_cannot_crowd_out_enduring_facts():
    from onlyfans_runtime.profile_learning.evidence import select_memories
    facts=validate(proposal(),{'1:2':source()})
    facts+=validate(proposal('occupation','engineer','I work as an engineer','1:3'),
                    {'1:3':source('I work as an engineer')})
    facts+=validate(proposal('interests','tennis','I play tennis','1:4'),{'1:4':source('I play tennis')})
    for i in range(30):
        body=f'Please send photo in outfit {i}'
        facts+=validate(proposal('request',body,body,f'1:{i+5}'),{f'1:{i+5}':source(body)})
    selected=select_memories(merge([],facts),now='2026-01-08')
    assert {'name','occupation','interests'} <= {f['field'] for f in selected}
    assert 'Carlos' in summary(facts,now='2026-01-08')
    assert len(merge([],facts))==33  # Outstanding requests remain stored.
