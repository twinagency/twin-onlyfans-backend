"""Actual rendered writer input: scope and policy provenance, no model calls."""
from copy import deepcopy
import json

import pytest

from moxy_replacement.configuration import ActionBlocked, load, permission_context
from moxy_replacement import examples
from moxy_replacement.writer import messages
from test_moxy_permission_and_retry_context import scene


def snapshot(creator='amelia', text='How was your day?'):
    value = scene()
    value['instructions'] = load(creator).public_context()
    value['event']['payload'] = {}
    value['history'] = [{'id': 10001, 'direction': 'in', 'sender': 'fan', 'text': text}]
    value['pending_fan_messages'] = deepcopy(value['history'])
    return value


def rendered(value):
    return json.dumps(messages(value), ensure_ascii=False)


def test_render_keeps_complete_authority_unchanged():
    value = snapshot()
    before = deepcopy(value)
    rendered(value)
    assert value == before
    cfg = load('amelia')
    assert cfg.resolve('talker_timing', {'talker_label': True, 'currently_spending': False}).value['minimum_reply_gap_seconds'] == 3600


def test_operator_unresolved_notes_never_reach_writer():
    value = snapshot()
    value['instructions']['unresolved'].append('PRIVATE_OPERATOR_SENTINEL_917')
    assert 'PRIVATE_OPERATOR_SENTINEL_917' not in rendered(value)


@pytest.mark.parametrize('creator,phrase', [
    ('amelia', 'The talker label permits replies once an hour'),
    ('amelia', 'The sheet asks staff to follow her active Snapchat'),
    ('amelia', 'pinned marital/anxiety boundaries'),
    ('cristina', 'apply the documented VIP date label'),
    ('cristina', 'The named top fan has all content'),
])
def test_reviewed_staff_and_other_fan_prose_is_removed(creator, phrase):
    assert phrase not in rendered(snapshot(creator))


def test_examples_do_not_create_a_second_business_interpretation():
    value = snapshot(text='Can you send me a photo?')
    prompt = messages(value)
    assert 'Learn their decisions' not in prompt[0]['content']
    refs = examples.references('amelia', value['history'], value['fan_state'])
    assert 'latest_exchange_reads_as' not in refs
    assert all('lesson' not in e and 'situation' not in e for e in refs['examples'])


def test_runtime_voice_set_does_not_use_another_creator_or_infer_situation():
    refs = examples.references('amelia', [{'direction': 'in', 'text': 'My bills are overdue'}])
    assert all(e['source_creator'].casefold() == 'amelia' for e in refs['examples'])
    assert refs == examples.references('amelia', [{'direction':'in','text':'How was your day?'}])
    assert refs['creator_voice_reference_count'] == 2
    assert not examples.references('unknown_creator')['examples']


def test_runtime_reference_path_does_not_call_old_intent_selector(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('The runtime must not run the old situation classifier')
    monkeypatch.setattr(examples, 'select', forbidden)
    assert examples.references('amelia')['creator_voice_reference_count'] == 2


def test_changed_example_text_is_not_trusted_without_matching_provenance(monkeypatch):
    data = deepcopy(examples._bank())
    item = next(e for e in data['examples'] if e['record'] == 103601)
    item['turns'][-1]['text'] = 'Unverified altered example.'
    monkeypatch.setattr(examples, '_bank', lambda: data)
    refs = examples.references('amelia')
    assert refs['creator_voice_reference_count'] == 1
    assert 'Unverified altered example' not in json.dumps(refs)


def test_genuine_stricter_override_is_not_silently_replaced_by_summary():
    value = snapshot()
    rule = next(r for r in value['instructions']['rules'] if r['key'] == 'relationships')
    rule['instruction'] += ' Approved additional boundary: do not use jealousy as a theme.'
    assert 'do not use jealousy as a theme' in rendered(value)


def test_unknown_rule_is_preserved_and_coverage_gap_identified():
    value = snapshot()
    value['instructions']['rules'].append({
        'key': 'unrecognized_future_boundary', 'value': False, 'requires': {},
        'source': {'document': '04_Authenticity_Protocol.pdf', 'pages': [5]},
        'instruction': 'Do not promise this newly named activity.'})
    content = rendered(value)
    assert 'Do not promise this newly named activity' in content
    projected = json.loads(messages(value)[1]['content'])
    assert 'unrecognized_future_boundary' in projected['creator_rules']['unreviewed_keys']


@pytest.mark.parametrize('key', ['talker_timing','fan_exceptions','fan_labels','vip_label'])
def test_unreviewed_operator_constraints_are_neither_leaked_nor_silently_ignored(key):
    value = snapshot('cristina' if key in {'fan_labels','vip_label'} else 'amelia')
    rule = next(r for r in value['instructions']['rules'] if r['key'] == key)
    rule['instruction'] += ' Additional verified-identity restriction must be mapped before use.'
    with pytest.raises(ActionBlocked, match='writer_rule_review_required:' + key):
        messages(value)


@pytest.mark.parametrize('creator', ['amelia', 'cristina'])
def test_every_default_rule_has_reviewed_writer_disposition(creator):
    value = snapshot(creator)
    projected = json.loads(messages(value)[1]['content'])
    rules = projected['creator_rules']
    assert rules['unreviewed_keys'] == []
    assert set(rules['dispositions']) == {r['key'] for r in value['instructions']['rules']}
    assert rules['dispositions']['product:nude_booty'] == 'catalogue_authority'


def test_media_restrictions_and_conversation_permission_remain_distinct():
    value = snapshot()
    value['fan_state'].update(spender=True, vip=True)
    projected = json.loads(messages(value)[1]['content'])
    assert projected['permission_status']['sexting']['allowed'] is True
    media = next(r for r in projected['creator_rules']['rules'] if r['key'] == 'prohibited_categories')
    assert media['scope'] == 'media'
    assert 'kitty' in media['value']
    value['fan_state'].update(spender=False, vip=False)
    assert permission_context(value['instructions'], value['fan_state'], 'sexting')['allowed'] is False


def test_prompt_only_custom_discussion_and_contact_restrictions_survive():
    value = snapshot()
    value['fan_state'].update(vip=False)
    projected = json.loads(messages(value)[1]['content'])
    rules = {r['key']: r for r in projected['creator_rules']['rules']}
    assert 'Do not discuss customs with non-VIPs' in rules['customs']['instruction']
    assert rules['customs']['conditions_met'] is False
    assert 'private off-platform contact' in rules['social_access']['instruction']
    assert 'text-only' in rules['ratings']['instruction']


def test_conditional_dictionary_service_does_not_use_boolean_permission_check():
    value = snapshot('cristina')
    value['fan_state'].update(assigned_operator_authorised=True)
    projected = json.loads(messages(value)[1]['content'])
    rules = {r['key']: r for r in projected['creator_rules']['rules']}
    assert rules['customs']['conditions_met'] is True
    assert 'mapped' in rules['social_access']['instruction']
    assert 'PG13' in rules['ratings']['instruction']


def test_captured_pilot_permission_form_does_not_change_default_creator_authority():
    # This is the exact recorded pilot rule form, not approval to activate it
    # outside the existing selected pilot. No persisted configuration is edited.
    value = snapshot()
    rule = next(r for r in value['instructions']['rules'] if r['key'] == 'sexting')
    rule.update(value=True, requires={}, instruction=(
        'Operator-approved Amelia pilot setting: explicit replies are permitted without VIP or spender status. '
        'The configured pilot fan scope still applies.'))
    value['fan_state'].update(spender=False, vip=False)
    projected = json.loads(messages(value)[1]['content'])
    assert projected['permission_status']['sexting']['allowed'] is True
    assert 'sexting' not in projected['creator_rules']['unreviewed_keys']
    assert permission_context(load('amelia').public_context(), value['fan_state'], 'sexting')['allowed'] is False
