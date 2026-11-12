"""Step 5.1: the per-account coexistence setting lives in the EXISTING commercial.v1 scope contract.

Absent == today's behaviour (an unexplained creator-account send is a takeover request). Present it must be explicit, exact and
commercial only; it never reaches the legacy two-fan trial contract and it never changes an existing fingerprint.
"""
import copy
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope, scope_contract as sc
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused
from test_commercial_scope_contract import ACTOR, KEY, body, legacy_config, prep_config, promoted
from test_onlyfans_foundation_pg import database  # noqa: F401
from coexistence_fixture import build_world, key as hexkey, OPERATOR


def test_absent_setting_is_the_takeover_default_and_changes_no_fingerprint():
    plain = promoted()
    assert 'coexistence' not in plain
    assert sc.coexistence_mode(plain) == 'takeover'
    with_key = promoted(coexistence={'mode': 'coexist'})
    assert with_key['coexistence'] == {'mode': 'coexist'} and sc.coexistence_mode(with_key) == 'coexist'
    assert sc.fingerprint(plain) != sc.fingerprint(with_key)
    # a config that never carried the key validates to exactly the bytes it always did
    assert sc.encoded(sc.validate_commercial(copy.deepcopy(plain))) == sc.encoded(plain)


@pytest.mark.parametrize('bad', [{'mode': 'ignore'}, {'mode': 'COEXIST'}, {'mode': True}, {}, {'mode': 'coexist', 'extra': 1},
                                 'coexist', True, None, [], {'staff': 'context'}])
def test_the_setting_is_explicit_and_exact_and_anything_else_is_refused(bad):
    with pytest.raises(Refused):
        promoted(coexistence=bad)


def test_the_explicit_takeover_value_is_accepted_and_means_the_default():
    config = promoted(coexistence={'mode': 'takeover'})
    assert sc.coexistence_mode(config) == 'takeover'


def test_the_legacy_trial_contract_never_carries_coexistence():
    with pytest.raises(Refused):
        pilot_scope.validate({**legacy_config(), 'coexistence': {'mode': 'coexist'}})
    with pytest.raises(Refused):
        sc.validate_commercial({**promoted(), 'contract_version': 'preparation.v1'})


def test_preparation_never_carries_coexistence():
    from conversation_engine import preparation
    with pytest.raises(Refused):
        preparation.validate({**prep_config(), 'coexistence': {'mode': 'coexist'}})


def test_configure_changes_it_only_through_the_named_operator_while_stopped_and_audits_it(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch)
    promoted_view = w.promote()
    assert 'coexistence' not in pilot_scope.describe(w.store, 'tenant-a', w.account)['config']
    config = pilot_scope.describe(w.store, 'tenant-a', w.account)['config']
    with pytest.raises(Refused):                                    # another actor cannot change it
        pilot_scope.configure(w.store, 'tenant-a', w.account, {**config, 'coexistence': {'mode': 'coexist'}},
                              expected_revision=promoted_view['revision'], command_key=hexkey(0x9001),
                              actor='op-session:' + 'b' * 64, reason='not the named operator')
    done = pilot_scope.configure(w.store, 'tenant-a', w.account, {**config, 'coexistence': {'mode': 'coexist'}},
                                 expected_revision=promoted_view['revision'], command_key=hexkey(0x9002), actor=OPERATOR,
                                 reason='Operator opts this account into staff coexistence')
    assert done['revision'] == promoted_view['revision'] + 1 and done['status'] == 'configured'
    shown = pilot_scope.describe(w.store, 'tenant-a', w.account)
    assert shown['config']['coexistence'] == {'mode': 'coexist'}
    with Session(database[1]) as s:
        audit = s.get(OfLaunchCommand, (w.account, hexkey(0x9002)))
        assert audit is not None and audit.actor == OPERATOR and audit.kind == 'plan_c_scope'
    # identity and lineage are retained: the setting does not widen what an edit may touch
    with pytest.raises(Refused):
        pilot_scope.configure(w.store, 'tenant-a', w.account, {**shown['config'], 'creator_id': '999'},
                              expected_revision=shown['revision'], command_key=hexkey(0x9003), actor=OPERATOR, reason='identity change')
    # once active it cannot be edited under the running worker
    w.activate(shown['revision'])
    live = pilot_scope.describe(w.store, 'tenant-a', w.account)
    with pytest.raises(Refused):
        pilot_scope.configure(w.store, 'tenant-a', w.account, {**live['config'], 'coexistence': {'mode': 'takeover'}},
                              expected_revision=live['revision'], command_key=hexkey(0x9004), actor=OPERATOR, reason='live flip')
    w.store.close()
