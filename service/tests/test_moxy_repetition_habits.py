"""Mechanical repetition (same pet name / same emoji on every one of the last N lines) and
required price answers. Creator voice that merely uses a pet name or emoji often is never refused."""
import json

import pytest

from moxy_replacement.parts import habits, repeated_habit, HABIT_NAME_RUN, HABIT_EMOJI_RUN
from moxy_replacement.price_assertions import price_problem, price_question_problem
from moxy_replacement.runtime import wording_problem, correction_brief, FACTUAL_REASONS, RECOVERABLE_REASONS
from moxy_replacement.writer import parse
from test_moxy_foundation_authority_regressions import authority_scene
from test_moxy_integrated_pg import decision

HEART = '\U0001F5A4'
KISS = '\U0001F618'


def planned(text):
    return parse(json.dumps(decision(text=text)))


def history(*lines):
    return [{'id': 10 + i, 'direction': 'out', 'text': t} for i, t in enumerate(lines)]


def test_habits_folds_baby_family_and_ignores_ordinary_words():
    assert habits('of course babe ' + HEART) == {'baby', HEART}
    assert habits('babyyy!') == {'baby'}
    assert habits('i love that, sexy handsome') == set()


# ---------------------------------------------------------------- mechanical repetition
def test_same_pet_name_on_every_one_of_last_four_lines_is_refused():
    recent = ['here it is baby', 'how was your day', 'of course baby', 'sure baby']   # only 3 baby
    assert repeated_habit(['ok baby'], recent) is None
    recent = ['x', 'here it is baby', 'of course baby', 'sure baby']
    assert repeated_habit(['one more, baby'], recent) == 'baby'


def test_models_baby_plus_heart_every_line_is_caught():
    recent = ['Of course baby — here it is ' + HEART, '$75.00 USD', 'It is a photo, baby ' + HEART,
              'On its way, baby ' + HEART, '$75.00 USD']
    assert repeated_habit(['that is the price, baby ' + HEART], recent) in {'baby', HEART}


def test_alternating_emoji_but_same_pet_name_is_caught_by_the_name():
    recent = ['here it is baby ' + KISS, "it is a photo, baby", 'of course baby ' + KISS]
    assert repeated_habit(["can't change the price, baby"], recent) == 'baby'


def test_three_in_a_row_pet_name_is_creator_voice_not_refused():
    assert HABIT_NAME_RUN == 4
    assert repeated_habit(['love you babyy'], ['hey babyy', 'miss u babyy']) is None


def test_same_emoji_on_every_one_of_last_three_lines_is_refused_two_are_not():
    assert HABIT_EMOJI_RUN == 3
    recent = ['say less \U0001F60F', 'its a photo \U0001F60F']
    assert repeated_habit(['here you go \U0001F60F'], recent) == '\U0001F60F'
    assert repeated_habit(['here you go \U0001F525'], recent) is None
    assert repeated_habit(['here you go \U0001F60F'], ['hmm', 'its a photo \U0001F60F']) is None


def test_an_interruption_breaks_the_run():
    assert repeated_habit(['hi baby'], ['baby a', 'baby b', 'ok', 'baby c']) is None


def test_price_only_captions_are_not_lines():
    recent = ['baby a', '$75.00 USD', 'baby b', '$75.00 USD', 'baby c']
    assert repeated_habit(['baby d'], recent[:-1]) is None      # only 2 baby lines + new = 3
    assert repeated_habit(['baby d'], ['baby 1'] + recent) == 'baby'


def test_fan_asking_for_the_pet_name_is_exempt():
    recent = ['yes daddy', 'sure daddy', 'ok daddy']
    assert repeated_habit(['of course daddy'], recent, 'call me daddy please') is None


def test_multi_bubble_reply_counts_its_own_lines():
    assert repeated_habit(['one baby', 'two baby'], ['x baby', 'y baby']) == 'baby'


def test_wording_problem_uses_the_existing_correction_path():
    _, snap, _ = authority_scene('How was your day?')
    snap['history'] = history('x baby ' + HEART, '$75.00 USD', 'here it is baby ' + HEART, 'of course baby ' + HEART) + snap['history']
    result = planned('quiet one, baby ' + HEART)
    assert wording_problem(snap, result) == 'repeated_habit'
    brief = correction_brief('repeated_habit', result, snap)
    assert brief['kind'] == 'wording' and 'different pet name or emoji' in brief['guidance']


def test_corrected_reply_without_the_habit_passes():
    _, snap, _ = authority_scene('How was your day?')
    snap['history'] = history('a baby', 'here it is baby', 'of course baby') + snap['history']
    assert wording_problem(snap, planned('quiet one, what about yours?')) is None


# ---------------------------------------------------------------- price answers required
def priced(text, cards=1, **kw):
    _, snap, _ = authority_scene(text)
    snap['card_state'] = [{'id': 500 + i, 'product_id': 'first', 'price_cents': 7500, 'currency': 'USD',
                           'at': f'2026-11-0{i+1}T00:00:00', 'paid': False} for i in range(cards)]
    return snap


def reply(text, key=None, action='reply'):
    return parse(json.dumps(decision(action, text, key)))


@pytest.mark.parametrize('ask', ['how much is it?', 'whats the price of that', 'how much is that card', 'what does it cost',
                                 'is that $?', 'How much for the card?'])
def test_asked_card_price_must_be_stated(ask):
    snap = priced(ask)
    assert price_question_problem(snap, reply("it's a photo, take your time")) == 'price_question_unanswered'
    assert price_question_problem(snap, reply("it's $75.00 on that card")) is None
    assert price_problem(snap, reply("it's $75.00 on that card")) is None


def test_pointing_to_the_card_is_not_an_answer():
    assert price_question_problem(priced('how much is it?'), reply('the price is right on the card in the chat')) == 'price_question_unanswered'


def test_selected_product_price_is_required_and_other_amount_is_not_enough():
    _, snap, _ = authority_scene('how much is the first one?')
    offer = lambda t: parse(json.dumps(decision('offer', t, 'first', request_kind='direct',
        request_quote='how much is the first one?', messages=[{'text': t, 'media': True}])))
    assert price_question_problem(snap, offer('here you go')) == 'price_question_unanswered'
    assert price_question_problem(snap, offer('here you go, $75')) is None
    assert price_question_problem(snap, offer('here you go, $85')) == 'price_question_unanswered'   # wrong amount; price_problem refuses it


def test_ambiguous_card_is_not_forced():
    snap = priced('how much is it?', cards=2)
    snap['card_state'][1].update(product_id='second', price_cents=8500)
    assert price_question_problem(snap, reply('which one do you mean, the first or the second?')) is None


def test_two_cards_same_terms_resolve_and_explicit_card_id_wins():
    assert price_question_problem(priced('how much is it?', cards=2), reply('so cute')) == 'price_question_unanswered'
    snap = priced('how much is card 501?', cards=2)
    snap['card_state'][1].update(price_cents=9500)
    assert price_question_problem(snap, reply("that one is $95.00")) is None
    assert price_question_problem(snap, reply("that one is $75.00")) == 'price_question_unanswered'
    assert price_question_problem(priced('how much is card 999?'), reply('not sure about that one')) is None   # unknown card


def test_price_of_something_not_for_sale_is_not_forced():
    assert price_question_problem(priced('how much for a custom video call'), reply('i do not do calls')) is None
    _, snap, _ = authority_scene('how much for a custom video call')
    assert price_question_problem(snap, reply('i do not do calls')) is None   # no card at all


@pytest.mark.parametrize('ask', ['how much do you miss me', 'how much longer', 'how much did you like it', 'hey you'])
def test_non_price_questions_are_ignored(ask):
    assert price_question_problem(priced(ask), reply('mm so much')) is None


def test_quoted_old_price_is_not_the_answer():
    snap = priced('how much is it?')
    snap['history'].append({'id': 3, 'direction': 'out', 'text': 'earlier I said $75.00'})
    quote = reply('you asked “it is $75.00?”, let me think')
    assert price_question_problem(snap, quote) == 'price_question_unanswered'
    assert price_question_problem(snap, reply('it is $75.00 now, as before')) is None


def test_old_stale_price_reply_is_mismatch_not_unanswered():
    snap = priced('how much is it?')
    assert price_problem(snap, reply('it was $50 before')) is not None


def test_handover_and_none_are_not_forced():
    snap = priced('how much is it?')
    assert price_question_problem(snap, {'action': 'handover', 'text': 'x', 'messages': [{'text': 'x', 'media': False}], 'product_id': None}) is None


def test_reason_rides_the_single_factual_correction():
    assert 'price_question_unanswered' in FACTUAL_REASONS and 'price_question_unanswered' in RECOVERABLE_REASONS
    snap = priced('how much is it?')
    brief = correction_brief('price_question_unanswered', reply('nice'), snap)
    assert brief['kind'] == 'factual' and 'exact verified price' in brief['guidance']


def test_planning_removes_only_the_repeated_standalone_face_without_redrafting():
    from moxy_replacement.runtime import _validate_draft, validate_decision
    from moxy_replacement.configuration import ActionBlocked

    settings, snap, _ = authority_scene('How was your day?')
    face = '\U0001F60F'
    snap['history'] = history('hi ' + face, 'good morning ' + face) + snap['history']
    original = planned('quiet one ' + face + ' how about yours?')
    with pytest.raises(ActionBlocked, match='repeated_habit'):
        validate_decision(settings, snap, original, planning=True)
    revised = _validate_draft(settings, snap, original)
    assert revised['text'] == 'quiet one how about yours?'
    assert revised['messages'][0]['text'] == revised['text']
    assert original['text'].endswith('how about yours?') and face in original['text']
    assert {k: v for k, v in original.items() if k not in ('text', 'messages')} == {
        k: v for k, v in revised.items() if k not in ('text', 'messages')}


def test_emoji_repair_never_weakens_a_fact_or_an_explicit_emoji_request():
    from moxy_replacement.runtime import _validate_draft
    from moxy_replacement.configuration import ActionBlocked

    settings, snap, _ = authority_scene('Can you explain that emoji?')
    face = '\U0001F60F'
    snap['history'] = history('hi ' + face, 'good morning ' + face) + snap['history']
    with pytest.raises(ActionBlocked, match='repeated_habit'):
        _validate_draft(settings, snap, planned('i like this one ' + face))
    settings, snap, _ = authority_scene('How was your day?')
    snap['history'] = history('hi ' + face, 'good morning ' + face) + snap['history']
    with pytest.raises(ActionBlocked, match='unsupported_creator_routine'):
        _validate_draft(settings, snap, planned('i coach at the gym every morning ' + face))


def test_cosmetic_repair_keeps_refusals_on_the_normal_correction_path():
    from moxy_replacement.runtime import _validate_draft
    from moxy_replacement.configuration import ActionBlocked

    settings, snap, _ = authority_scene('How was your day?')
    face = '\U0001F60F'
    snap['history'] = history('hi ' + face, 'good morning ' + face) + snap['history']
    with pytest.raises(ActionBlocked, match='repeated_habit'):
        _validate_draft(settings, snap, planned("i don't feel like it today " + face))
