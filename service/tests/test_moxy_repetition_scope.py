"""Conversation repetition checks use the existing single wording validation."""
import json

from moxy_replacement.runtime import wording_problem
from moxy_replacement.writer import parse
from test_moxy_foundation_authority_regressions import authority_scene
from test_moxy_integrated_pg import decision


def planned(text, parts=None):
    values = {'messages': [{'text': part, 'media': False} for part in parts]} if parts else {}
    return parse(json.dumps(decision(text=text, **values)))


def test_burst_of_fan_messages_does_not_hide_an_actual_recent_creator_line():
    _, snap, _ = authority_scene('How was your day?')
    old = 'That sounds like a lovely change of pace.'
    snap['history'] = [{'id': 10, 'direction': 'out', 'text': old}] + [
        {'id': 100+i, 'direction': 'in', 'text': 'another message'} for i in range(25)]
    assert wording_problem(snap, planned(old)) in {'repeated_recent_line', 'repeated_recent_phrase'}


def test_sibling_message_parts_do_not_repeat_the_same_substantial_line():
    _, snap, _ = authority_scene('How was your day?')
    text = 'That sounds like a lovely change of pace.'
    assert wording_problem(snap, planned(text, [text, text])) == 'repeated_recent_phrase'


def test_repeated_ha_opening_is_detected_across_recent_creator_turns():
    _, snap, _ = authority_scene('How was your day?')
    snap['history'] = [
        {'id': 1, 'direction': 'out', 'text': 'ha, that sounds fun.'},
        {'id': 2, 'direction': 'out', 'text': 'ha, tell me more.'},
        {'id': 3, 'direction': 'out', 'text': 'ha, you are cheeky.'},
    ] + snap['history']
    assert wording_problem(snap, planned('ha, what happened next?')) == 'repeated_recent_phrase'


def test_grounded_price_can_repeat_when_fan_asks_for_it():
    _, snap, _ = authority_scene('Please repeat the price.')
    text = 'The price is $75.'
    assert wording_problem(snap, planned(text, [text, text])) is None


def test_one_casual_ha_after_other_openings_is_not_forced_to_change():
    _, snap, _ = authority_scene('How was your day?')
    snap['history'] = [{'id': 1, 'direction': 'out', 'text': 'hey, lovely to hear from you.'},
                       {'id': 2, 'direction': 'out', 'text': 'ha, that sounds fun.'}] + snap['history']
    assert wording_problem(snap, planned('ha, what happened next?')) is None
