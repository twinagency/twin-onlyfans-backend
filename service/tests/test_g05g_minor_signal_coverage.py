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
    # r2 (Opus DA + Flash on r1): base-held 1/2/4 and "I'm a N" ages, turning/will be, filler, singular year-old, unicode
    "im 4", "i'm 2", "i am 4", "im 2.", "i'm turning 4", "my age is 2", "im 4 and horny", "i just turned 2",
    "i am 2 next month", "she's 4", "pretend you're 2", "2 & horny", "I'm a 16", "im a 14", "I'm a 16 and horny",
    "i'm 1 6", "im 16 and a half", "ur 16 lol", "ur 16 tbh", "u 16 rn", "u 16 \U0001F618", "u 16 right?",
    "you're turning 16", "you'll be 16", "are you turning 15", "my friend is 15", "16 year old girl", "16-year-old girl", "a 16 year old girl", "16 yr old",
    "roleplay as a 16-year-old", "fifteen year old", "my 16 year old daughter is so horny", "you were 16", "im like 15", "im nearly 16",
    "\uff11\uff16f here", "\uff11\uff16 years old", "\u0661\u0666f here", "1\u200b6f here", "16 and single",
]
CLEAR = [
    # text-speak 2/4/1, scores, quantities, heights, money, times
    "im 2 drunk", "love u 2", "ur 2 cute", "u 4 real", "you're 1 in a million", "you're a 10", "I'm a 10", "ur 10/10",
    "she's 10/10", "16 inches", "$16", "16 min", "5'6", "she's 5'6", "top 16", "I'm 19", "she's 21", "you're 18",
    "see you 5 pm", "send u 3 pics", "ur 16 pics are hot", "you're 16 minutes late", "he's 6 ft", "my car is 15 years old",
    "16 and 17 are my lucky numbers", "16 pics for $10", "my 16 year old son", "12 year old whiskey", "she was here 16 years ago",
    # r2: tips, prices, ratings and haggling are not ages (Opus DA F3-F5)
    "i'll tip you 15", "can i pay u 12?", "i paid you 12.", "owe u 10.", "i'll tip you 15 and more", "rate u 10",
    "i give you 9", "deal. 15 and you send the video", "15 and the video?", "lol. 15 and done", "15 and up",
    "16 and counting", "you are 10 out of 10", "you're 10 outta 10", "you're 5 stars", "he's 6 and a half inches",
    "he's 6 4", "you're 9 to 5?", "im 4 real", "16m views", "a 12 year old scotch",
]


@pytest.mark.parametrize("text", FLAGGED)
def test_g05g_minor_age_flagged_on_every_path(text):
    assert verdicts(text) == dict.fromkeys(_ALL, True)


@pytest.mark.parametrize("text", CLEAR)
def test_g05g_age_neighbour_clear_on_every_path(text):
    assert verdicts(text) == dict.fromkeys(_ALL, False)


def test_g05g_minor_match_is_linear_on_whitespace_runs():
    # Opus DA r1 F2: "(?<=\\n)\\s*" re-scanned every newline run (20k newlines: 44.8s).
    import time
    from conversation_engine.minor_signal import minor_match
    for text in ("\n" * 20000, "\n \t" * 7000, "u " * 10000, "a 16 year old " * 3000):
        start = time.perf_counter()
        minor_match(text)
        assert time.perf_counter() - start < 0.5, repr(text[:12])
