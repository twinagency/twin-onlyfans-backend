"""G05-G: the shared minor signal catches second-person, third-person, bare-numeric and NNf/NNm ages (NEW file).

At base ``minor_match`` needed a first-person subject, so "pretend you're 16", "she's 16", "16 & horny" and "16f here"
all passed. Every phrase runs through all four detectors (engine fan/draft, native fan/draft), which share
``conversation_engine.minor_signal``. Synthetic phrases only.
"""
import pytest

from test_g05pr2_minor_parity import verdicts

_ALL = ("engine_fan", "engine_draft", "native_fan", "native_draft")
FLAGGED = [
    # second person
    "pretend you're 16", "you're 16", "ur 16", "u 16?", "are you 16?", "r u 15", "you are only 15", "ur 16 and horny",
    "pretend you're 9", "you're a 16 year old",
    # third person
    "she's 16", "he is 15", "shes 14 lol", "is she 16?", "my daughter is 15", "my little sister is 12",
    # bare numeric / NNf / NNm self-IDs
    "16 & horny", "16 and horny", "16 years old", "she's 16 years old", "16f here", "15m here", "16 f", "17 yrs old",
    "under-18", "i'm 15/f",
]
CLEAR = [
    # text-speak 2/4/1, scores, quantities, heights, money, times
    "im 2 drunk", "love u 2", "ur 2 cute", "u 4 real", "you're 1 in a million", "you're a 10", "I'm a 10", "ur 10/10",
    "she's 10/10", "16 inches", "$16", "16 min", "5'6", "she's 5'6", "top 16", "I'm 19", "she's 21", "you're 18",
    "see you 5 pm", "send u 3 pics", "ur 16 pics are hot", "you're 16 minutes late", "he's 6 ft", "my car is 15 years old",
    "16 and 17 are my lucky numbers", "16 pics for $10", "my 16 year old son", "12 year old whiskey", "she was here 16 years ago",
]


@pytest.mark.parametrize("text", FLAGGED)
def test_g05g_minor_age_flagged_on_every_path(text):
    assert verdicts(text) == dict.fromkeys(_ALL, True)


@pytest.mark.parametrize("text", CLEAR)
def test_g05g_age_neighbour_clear_on_every_path(text):
    assert verdicts(text) == dict.fromkeys(_ALL, False)
