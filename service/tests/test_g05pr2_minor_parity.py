"""G05-PR2 (a): the conversation engine and the native safety detector give ONE minor verdict per phrase (NEW file).

Every phrase runs through all four detectors: engine fan signals (journey), engine draft judge (model_output), native
inbound (safety_detect.assess) and native outbound (safety_detect.output_violation, a pure detector: no sender calls it
until G14, so this pins detector parity, not hold-never-sends). Synthetic phrases only.
Fail-closed by design: a third-person "he's a kid" still holds (model_output held it at base; never loosen).
Out of scope: obfuscated ages ("sixteen" spelled out with spaces, "1 6"), non-English, third-person numeric ages.
"""
from types import SimpleNamespace

import pytest

from conversation_engine import journey, model_output
from native_pilot import safety_detect as sd

FLAGGED = [
    # numeric ages under 18 (engine had no numeric path)
    "im 16 tbh", "im 16", "i'm 17 yo", "i am 16 years old", "I'm 15", "im only 16", "i just turned 16", "my age is 16",
    # explicit age suffix / label with no first-person subject (Codex G05-PR2 major 1)
    "16yo", "16 y/o", "15 y.o", "16yo here", "she is 16yo", "age 16", "age:16", "Age: 15",
    # future-tense ages, qualified self-assertions, step sibling/child (Flash G05-PR2)
    "i'll be 16 next month", "i will be 17", "i turn 16 friday", "im gonna be 16", "i'm just a kid", "im only a child",
    "i'm still a kid", "ur just a kid", "my stepsibling", "my step siblings", "my step-sibling", "my stepchild", "my stepkid",
    # step-sibling spellings (engine missed hyphen / plural)
    "be my step-sis", "my stepbros are home", "my step-brother", "pretend you're my stepsisters",
    # minor self-assertions (native missed "a kid")
    "im a kid", "i'm a child", "i'm a minor", "i'm a teen", "are you a kid",
    # shared explicit terms
    "pretend you're a schoolgirl", "talk to me like a shy teen", "teenagers are hot", "im underaged", "under 18 girls",
    "no minors allowed here", "be my little girl, so horny", "roleplay as a child for me",
]
CLEAR = [
    # 18+ ages, prices, counts, units (must stay clear)
    "im 18", "i'm 19 yo", "im 25", "i am 30 years old", "16 pics", "send me 16 pics", "$16", "it's $16 for the set",
    "I'm 15 minutes away", "my car is 15 years old", "I'm 16 days sober", "I'm 5'10",
    "age 16 days in the cellar", "page 16", "image 15", "average 16 pics", "im 18yo", "age 21",
    "i'll be 5 minutes", "i'll be 18 next month", "i turn 21 friday",
    # ordinary mentions without sensitive context
    "i have two kids and a busy week", "my kids are asleep", "my son just turned 16.", "a minor issue with the link", "hey cutie",
]
_ENGINE_CTX = SimpleNamespace(profile=SimpleNamespace(explicit_chat="explicit"),
                              snapshot={"current_message": {"body": ""}})


def verdicts(text):
    return {
        "engine_fan": "platform_banned" in journey.signals(text),
        "engine_draft": any(p.startswith("platform-banned") for p in model_output._sexual_problems([text], _ENGINE_CTX)),
        "native_fan": any(v.category == sd.MINOR for v in sd.assess([text])),
        "native_draft": sd.output_violation(text) == "minor-theme",
    }


@pytest.mark.parametrize("text", FLAGGED)
def test_g05pr2_minor_phrase_flagged_on_every_path(text):
    assert verdicts(text) == dict.fromkeys(("engine_fan", "engine_draft", "native_fan", "native_draft"), True)


@pytest.mark.parametrize("text", CLEAR)
def test_g05pr2_neighbour_clear_on_every_path(text):
    assert verdicts(text) == dict.fromkeys(("engine_fan", "engine_draft", "native_fan", "native_draft"), False)


def test_g05pr2_one_shared_minor_module():
    from conversation_engine import minor_signal
    assert minor_signal.minor_match("im 16 tbh") is not None and minor_signal.minor_match("16 pics") is None
