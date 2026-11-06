"""Pet-name and emoji habit: refused once when it fills the last creator lines."""
import json

from moxy_replacement.parts import habits, repeated_habit
from moxy_replacement.runtime import wording_problem, correction_brief
from moxy_replacement.writer import parse
from test_moxy_foundation_authority_regressions import authority_scene
from test_moxy_integrated_pg import decision


def planned(text):
    return parse(json.dumps(decision(text=text)))


def history(*lines):
    return [{'id': 10 + i, 'direction': 'out', 'text': t} for i, t in enumerate(lines)]


def test_habits_folds_baby_family_and_ignores_ordinary_words():
    assert habits('of course babe 🖤') == {'baby', '🖤'}
    assert habits('babyyy!') == {'baby'}
    assert habits('i love that, sexy handsome') == set()


def test_third_use_of_same_pet_name_is_refused():
    recent = ['here it is baby', '$75.00 USD', 'of course baby']
    assert repeated_habit(['one more for you, baby'], recent) == 'baby'


def test_one_earlier_use_is_still_allowed():
    assert repeated_habit(['thanks baby'], ['hey baby', 'how was your day', 'tell me more']) is None


def test_same_emoji_on_two_recent_lines_is_refused_but_a_different_one_is_not():
    recent = ['say less \U0001F60F', 'its a photo \U0001F60F']
    assert repeated_habit(['here you go \U0001F60F'], recent) == '\U0001F60F'
    assert repeated_habit(['here you go \U0001F525'], recent) is None


def test_only_the_last_four_lines_count():
    recent = ['baby a', 'baby b', 'ok', 'fine', 'sure', 'yes']
    assert repeated_habit(['hi baby'], recent) is None


def test_fan_asking_for_the_pet_name_is_exempt():
    recent = ['yes daddy', 'sure daddy']
    assert repeated_habit(['of course daddy'], recent, 'call me daddy please') is None


def test_wording_problem_uses_the_existing_correction_path():
    _, snap, _ = authority_scene('How was your day?')
    snap['history'] = history('here it is baby \U0001F5A4', '$75.00 USD', 'of course baby \U0001F5A4') + snap['history']
    result = planned('quiet one, baby \U0001F5A4')
    assert wording_problem(snap, result) == 'repeated_habit'
    brief = correction_brief('repeated_habit', result, snap)
    assert brief['kind'] == 'wording' and 'no pet name' in brief['guidance']


def test_corrected_reply_without_the_habit_passes():
    _, snap, _ = authority_scene('How was your day?')
    snap['history'] = history('here it is baby \U0001F5A4', 'of course baby \U0001F5A4') + snap['history']
    assert wording_problem(snap, planned('quiet one, what about yours?')) is None


def test_price_stays_statable_next_to_a_dropped_habit():
    # the habit check never looks at digits or prices
    assert repeated_habit(['it is $75.00 on that card'], ['baby', 'baby']) is None
