"""Simulation runner: pure flag checks plus the 30-step fixture journey end to end (offline, synthetic)."""
import json

import pytest

from test_onlyfans_foundation_pg import database
from tools import sim_journey as sj


def rec(n=1, bubbles=(), mode_before='open', signal=None, hold=False, kind='fan'):
    return {'n': n, 'kind': kind, 'bubbles': list(bubbles), 'rejections': [],
            'decision': {'kind': 'reply', 'sales_signal': signal},
            'sales': {'mode_before': mode_before, 'mode': mode_before, 'scope': []},
            'handover': {'hold_active': hold, 'requested': hold}}


def b(text, key='k1', card=None):
    return {'text': text, 'idempotency_key': key, 'card': card}


PRICES = {600, 1800}


def kinds(r, history=(), hold_before=False):
    return {f['flag'] for f in sj.analyse(r, list(history), PRICES, human_hold_before=hold_before)}


def test_flags_no_reply_unless_human_holds():
    assert 'no_reply' in kinds(rec())
    assert 'no_reply' not in kinds(rec(hold=True))


def test_flags_bubble_count_price_and_opener():
    assert 'more_than_2_bubbles' in kinds(rec(bubbles=[b('a', 'k1'), b('b', 'k2'), b('c', 'k3')]))
    assert 'price_not_in_catalogue' in kinds(rec(bubbles=[b('it is $5 for you')]))
    assert 'price_not_in_catalogue' not in kinds(rec(bubbles=[b('it is $18 for you')]))
    card = {'item': 'x', 'label': 'X', 'price_cents': 999, 'media_count': 2, 'previews': 1}
    assert 'price_not_in_catalogue' in kinds(rec(bubbles=[b('here', card=card)]))
    earlier = rec(n=1, bubbles=[b('hey jake how are you', 'k0')])
    assert 'repeated_opener' in kinds(rec(n=2, bubbles=[b('hey jake what is up')]), [earlier])
    assert 'repeated_opener' not in kinds(rec(n=2, bubbles=[b('so what now')]), [earlier])


def test_flags_card_after_refusal_duplicate_and_hold_reply():
    card = {'item': 'x', 'label': 'X', 'price_cents': 600, 'media_count': 2, 'previews': 1}
    assert 'card_after_refusal' in kinds(rec(bubbles=[b('here', card=card)], mode_before='paused'))
    assert 'card_after_refusal' not in kinds(rec(bubbles=[b('here', card=card)], mode_before='paused', signal='reopen'))
    assert 'duplicate_dispatch' in kinds(rec(bubbles=[b('a', 'k1'), b('b', 'k1')]))
    assert 'duplicate_dispatch' in kinds(rec(bubbles=[b('same', 'k1'), b('same', 'k2')]))
    old = rec(n=1, bubbles=[b('hello there', 'k9')])
    assert 'duplicate_dispatch' in kinds(rec(n=2, bubbles=[b('other words', 'k9')]), [old])
    assert 'reply_while_human_holds' in kinds(rec(bubbles=[b('hi')]), hold_before=True)


def test_scripted_journey_has_thirty_steps_and_catalogue_prices():
    s = sj.load_script()
    assert len(s['steps']) == 30 and 3 <= len(s['catalogue']) <= 4


def test_real_adapter_is_a_documented_stub_until_a6_is_wired():
    class Client:
        def generate(self, snapshot, *, provider):
            return json.dumps({'body': 'hello from ' + provider})
    out = sj.RealModelAdapter(Client()).propose(sj.StepContext(1, {}, 'C'), {})
    assert out.body == 'hello from C'
    try:
        sj.RealModelAdapter()
    except sj.NotWired:
        pass
    else:
        pytest.skip('A6 client is importable; adapter has been wired')


@pytest.mark.parametrize('provider', ['A', 'C'])
def test_thirty_step_fixture_journey_end_to_end(database, provider, tmp_path):
    result = sj.run_journey(sj.load_script(), sj.FixtureModel(), provider, database=database)
    steps = result['steps']
    assert len(steps) == 30
    bad = {f['flag'] for f in result['flags']}
    assert 'duplicate_dispatch' not in bad
    assert 'price_not_in_catalogue' not in bad
    assert 'card_after_refusal' not in bad
    assert 'reply_while_human_holds' not in bad
    assert 'more_than_2_bubbles' not in bad
    # Every dispatched bubble reached the synthetic provider exactly once, with its own key.
    keys = [x['idempotency_key'] for r in steps for x in r['bubbles']]
    assert len(keys) == len(set(keys)) == result['totals']['provider_calls'] > 20
    # Cards carry catalogue prices; the three business events are visible.
    prices = {c['price_cents'] for c in sj.load_script()['catalogue']}
    cards = [x['card'] for r in steps for x in r['bubbles'] if x['card']]
    assert cards and all(c['price_cents'] in prices and c['media_count'] >= 1 for c in cards)
    assert result['totals']['spend_cents'] == 100000
    # Human-hold steps send nothing, and selling pauses after the refusal.
    holds = [r for r in steps if r['decision']['kind'] == 'held_for_human']
    assert len(holds) == 5 and all(not r['bubbles'] for r in holds)
    assert steps[18]['sales']['mode'] == 'paused'
    assert steps[29]['handover']['hold_active'] and not steps[29]['bubbles']
    # The checker rejection is shown, then the rewrite goes out.
    rejected = steps[14] if provider == 'A' else steps[16]
    assert rejected['rejections'] and rejected['bubbles'] and len(rejected['attempts']) == 2
    # Both outputs render.
    assert 'FAN: hey' in sj.render_text(result)
    json.dumps(result)
