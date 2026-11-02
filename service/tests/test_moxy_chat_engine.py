"""Pure (no database) checks of prompt scope, creator-rule timing and foundation loading."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import shutil
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from moxy_replacement import runtime, writer
from moxy_replacement.configuration import (load, load_foundation, CreatorConfiguration,
    ActionBlocked, InvalidConfiguration)
from onlyfans_runtime.store import utcnow
from test_moxy_replacement_configuration import product, state


def snap(creator='amelia', pilot=None, **extra):
    return {'fan_id': 101, 'pilot': pilot, 'instructions': load(creator).public_context(),
            'event': {'kind': 'turn', 'payload': {}}, 'history': [], **extra}


def system(s):
    return writer.messages(s)[0]['content']


# Task 1: pilot restrictions apply only when a pilot scope exists.
def test_pilot_present_prompt_keeps_pilot_restrictions():
    text = system(snap(pilot={'fan_ids': [101], 'product_ids': ['first'], 'supervisor': 'op'}))
    assert 'pilot scope is present' in text
    assert 'require handover to its supervisor' in text
    assert 'No unattended campaigns' in text


def test_pilot_absent_prompt_has_no_pilot_restrictions_and_follows_configuration():
    text = system(snap())
    assert 'No unattended campaigns' not in text
    assert 'handover to its supervisor' not in text
    assert 'pilot scope' not in text
    assert 'explicitly authorises them' in text           # services only through a rule
    assert 'approved remedies' in text                    # remedies, not blanket handover
    assert 'No message-count quota' in text               # shared rules unchanged


# Eval-found writer ambiguities (2026-11-03 smoke run): acceptance action, sequence naming, complaint promises.
def test_prompt_pins_acceptance_action_sequence_name_and_complaint_promises():
    text = system(snap())
    assert 'accepts the retained invitation, the action is offer with that invitation' in text
    assert 'belongs to a sequence listed in the snapshot, set sequence to that sequence' in text
    assert 'unless it is listed in approved_remedies' in text
    pilot = system(snap(pilot={'fan_ids': [101], 'product_ids': ['first'], 'supervisor': 'op'}))
    assert 'set sequence to that sequence' in pilot       # rule is scope-independent


def test_smoke_budget_accepts_counted_entry_but_stops_on_new_unknown(tmp_path):
    from moxy_replacement.eval.smoke import Budget
    import json as j
    path = tmp_path / 'j.json'
    path.write_text(j.dumps({'cap_usd': 1.0, 'cap_calls': 20, 'calls': [
        {'label': 'c10', 'status': 'unknown_cost_counted_at_reserve', 'cost_usd': 0.05, 'note': 'approved'}]}))
    b = Budget(path)
    i = b.reserve('next')                                  # approved entry does not stop the run
    with pytest.raises(SystemExit):
        b.settle(i, None, None)                            # new unknown cost stops
    with pytest.raises(SystemExit):
        Budget(path).reserve('after')                      # and nothing further runs


# Task 2 + 3 helpers
def settings_for(creator_document=None, creator='amelia'):
    result = {'creator_key': creator, 'products': [product()], 'sequences': {},
              'catalogue_bindings': {}, 'operational': {}}
    if creator_document is not None:
        result['creator_document'] = creator_document
    return result


def talker_snap(minutes_ago):
    at = (utcnow() - timedelta(minutes=minutes_ago)).isoformat()
    facts = {'talker_label': True, 'currently_spending': False}
    return {'fan_id': 101, 'fan_state': facts, 'event': {'kind': 'turn', 'payload': {}}, 'campaign': None,
            'history': [{'id': 1, 'direction': 'out', 'text': 'hi', 'at': at}], 'inventory': {},
            'obligations': [], 'approved_remedies': {}, 'operational': {}, 'pilot': None}


def reply():
    return {'action': 'reply', 'text': 'Hey', 'product_id': None, 'sequence': None, 'flags': [],
            'reason': 'fixture', 'permission': None, 'question': None, 'resolve_obligation': None}


def with_gap(seconds):
    doc = deepcopy(load('amelia').creator)
    for rule in doc['rules']:
        if rule['key'] == 'talker_timing':
            rule['value'] = {'minimum_reply_gap_seconds': seconds}
    doc['creator_key'] = 'gap_fixture'
    return doc


def test_talker_gap_default_amelia_value_is_read_from_sheet():
    s = settings_for()
    with pytest.raises(ActionBlocked, match='creator_reply_gap_setting'):
        runtime.validate_decision(s, talker_snap(30), reply())
    runtime.validate_decision(s, talker_snap(90), reply())


def test_talker_gap_uses_non_default_creator_value():
    s = settings_for(with_gap(120))                    # two minutes, not an hour
    runtime.validate_decision(s, talker_snap(5), reply())
    with pytest.raises(ActionBlocked, match='creator_reply_gap_setting'):
        runtime.validate_decision(settings_for(with_gap(600)), talker_snap(5), reply())
    runtime.validate_decision(settings_for(with_gap(600)), talker_snap(11), reply())


def test_talker_label_without_creator_rule_is_unresolved_not_guessed():
    doc = deepcopy(load('amelia').creator)
    doc['creator_key'], doc['rules'], doc['unresolved'] = 'no_gap_fixture', [], []
    with pytest.raises(ActionBlocked, match='talker_timing_unresolved'):
        runtime.validate_decision(settings_for(doc), talker_snap(5), reply())


def test_unlabelled_fan_is_not_gated_by_talker_rule():
    s = talker_snap(1); s['fan_state'] = {}
    runtime.validate_decision(settings_for(), s, reply())


# Task 3: foundation is loaded on its own.
def test_third_creator_works_without_amelia_file(tmp_path, monkeypatch):
    src = Path(runtime.__file__).with_name('sources')
    only = tmp_path / 'sources'; only.mkdir()
    shutil.copy(src / 'foundation.v1.json', only / 'foundation.v1.json')
    third = deepcopy(load('amelia').creator)
    third['creator_key'], third['rules'], third['unresolved'] = 'synthetic_third', [], []
    assert load_foundation(only) == load_foundation()
    with pytest.raises(FileNotFoundError):
        load('amelia', only)
    def refuse(key, directory=None):
        raise AssertionError('creator file must not be loaded for a creator_document: ' + key)
    monkeypatch.setattr(runtime, 'load', refuse)
    monkeypatch.setattr(runtime, 'load_foundation', lambda: load_foundation(only))
    cfg, pa = runtime.configuration(settings_for(third, creator='synthetic_third'))
    assert cfg.public_context()['creator_key'] == 'synthetic_third'
    assert cfg.resolve('privacy', {}).authority == 'foundation'
