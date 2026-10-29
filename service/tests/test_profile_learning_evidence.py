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
    assert not validate(proposal('hobbies'), {'1:2': source(direction='out', automation='ai')}, creator=True)


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
    assert {x['value'] for x in merge(old, newer)} == {'Carlos', 'Bob'}


def test_conversation_activity_does_not_evict_personal_facts():
    old = validate(proposal(), {'1:2': source()})
    topics = [{**old[0], 'field': 'recent_topic', 'source': f'1:{i}', 'at': f'2026-02-{i:03}'}
              for i in range(100)]
    result = merge(old, topics)
    assert old[0] in result
    assert len(result) == 7


def test_silent_fan_summary_is_honest():
    assert 'No supported personal details' in summary([])
