"""Candidate regressions promoted from the archived Stage 2 expected failures."""
from copy import deepcopy
import json
import pytest
from moxy_replacement.configuration import ActionBlocked, load, permission_context
from moxy_replacement.writer import messages
from test_moxy_permission_and_retry_context import scene


def sample():
    cfg = load('amelia')
    snap = scene()
    snap['event']['payload'] = {}
    snap['instructions'] = cfg.public_context()
    snap['history'] = [{'id': 10001, 'direction': 'in', 'sender': 'fan', 'text': 'How was your day?'}]
    snap['pending_fan_messages'] = deepcopy(snap['history'])
    return cfg, snap


def test_current_prompt_render_does_not_mutate_full_snapshot():
    cfg, snap = sample()
    before = deepcopy(snap)
    messages(snap)
    assert snap == before
    assert cfg.resolve('talker_timing', {'talker_label': True, 'currently_spending': False}).value['minimum_reply_gap_seconds'] > 0


def test_permission_resolution_remains_creator_scoped():
    cfg, snap = sample()
    assert permission_context(snap['instructions'], {'spender': True, 'vip': True}, 'sexting')['allowed'] is True
    assert permission_context(snap['instructions'], {'spender': False, 'vip': False}, 'sexting')['allowed'] is False
    assert cfg.resolve('sexting', {'spender': True, 'vip': True}).value is True


def test_writer_context_excludes_unresolved_operator_register():
    _, snap = sample()
    sentinel = 'SYNTHETIC_UNRESOLVED_OPERATOR_ONLY_3829'
    snap['instructions']['unresolved'].append(sentinel)
    prompt = messages(snap)
    assert sentinel not in json.dumps(prompt)


def test_unreviewed_scheduler_prose_stops_before_model_without_mutating_authority():
    cfg, snap = sample()
    rule = next(r for r in snap['instructions']['rules'] if r['key'] == 'talker_timing')
    sentinel = 'SYNTHETIC_SCHEDULER_ONLY_2911'
    rule['instruction'] = sentinel
    with pytest.raises(ActionBlocked, match='writer_rule_review_required:talker_timing'):
        messages(snap)
    assert cfg.resolve('talker_timing', {'talker_label': True, 'currently_spending': False}).value['minimum_reply_gap_seconds'] > 0


def test_writer_examples_do_not_instruct_learning_business_decisions():
    _, snap = sample()
    system = messages(snap)[0]['content']
    assert 'Learn their decisions and segmentation' not in system
