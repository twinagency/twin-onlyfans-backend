"""Optional source-linked conversation progress; no new model or DB table."""
import json

import pytest

from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.runtime import validate_decision
from moxy_replacement.writer import parse
from test_moxy_foundation_authority_regressions import authority_scene
from test_moxy_integrated_pg import decision


def proposal(ids):
    return parse(json.dumps(decision(text='That sounds good.', unanswered_input_ids=ids)))


def test_current_pending_input_can_be_marked_unanswered():
    settings, snap, _ = authority_scene('When did I say that?')
    result = proposal([1001])
    assert validate_decision(settings, snap, result, planning=True) is None


@pytest.mark.parametrize('ids', [[9999], [1001, 1001], [True], ['1001'], None])
def test_invalid_or_unscoped_input_ids_cannot_create_progress(ids):
    settings, snap, _ = authority_scene('When did I say that?')
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, proposal(ids), planning=True)


def test_legacy_decision_without_progress_remains_accepted():
    settings, snap, _ = authority_scene('How was your day?')
    old = parse(json.dumps(decision(text='That sounds good.')))
    assert validate_decision(settings, snap, old, planning=True) is None


def test_resend_instruction_matches_current_request_authority():
    from moxy_replacement.writer import messages
    settings, snap, _ = authority_scene('Please send the first photo.')
    text = messages(snap)[0]['content']
    assert 'a current fan request uniquely identifies that item' in text
    assert 'unless he explicitly asks for a resend' not in text
