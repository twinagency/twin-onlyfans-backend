"""W5 native-pilot-runner review probes (NEW file, red at a2f1dda6 by design; no existing assertion edited).

W5NR-5 (OQ-25 / W4CEC-4, native side): native_pilot.safety_detect has no minor/age category; minor-implying
roleplay and stated ages under 18 produce no hold verdict, and output_violation passes them in drafts.
W5NR-6: distress / stop-contact after a "you said ..." clause is stripped as reported speech (comma does not end
the clause), so the fan's own distress or stop request yields no verdict.
Controls (green): plain distress and plain stop requests are held.
"""
import pytest

from native_pilot import safety_detect as sd

MINOR_INBOUND = ["pretend you're a schoolgirl", "be my stepsis", "roleplay as a shy teen", "I'm 15", "im only 16"]
MINOR_OUTBOUND = ["my little schoolgirl", "come here stepsis", "you're my shy teen"]
REPORTED_PREFIX = [
    ("you said you'd be here, i want to kill myself", "distress"),
    ("u said ur busy but i dont want to live anymore", "distress"),
    ("u said u care but stop messaging me", "stop_contact"),
]


def _held(text):
    return [v.category for v in sd.assess([text]) if v.holds]


@pytest.mark.parametrize("text", MINOR_INBOUND)
def test_w5nr5_minor_roleplay_inbound_is_held(text):
    assert _held(text), "no hold verdict for a minor-implying fan message"


@pytest.mark.parametrize("text", MINOR_OUTBOUND)
def test_w5nr5_minor_roleplay_draft_is_violation(text):
    assert sd.output_violation(text) is not None


@pytest.mark.parametrize("text,category", REPORTED_PREFIX)
def test_w5nr6_reported_speech_does_not_hide_fan_distress_or_stop(text, category):
    assert category in _held(text)


@pytest.mark.parametrize("text,category", [("i want to kill myself", "distress"), ("stop messaging me", "stop_contact")])
def test_w5nr_control_plain_distress_and_stop_held(text, category):
    assert category in _held(text)


# G05 (fix): quoted fiction is not the fan's own distress (stripped text); the fan's own words are.
@pytest.mark.parametrize("text,held", [('the movie character said "I want to die"', False), ("i want to die", True)])
def test_g05_quoted_fiction_is_not_distress(text, held):
    assert ("distress" in _held(text)) is held


def test_g05_reported_speech_after_comma_is_not_a_stop():
    assert "stop_contact" not in _held("my wife said, stop texting her")


def test_g05_minor_case_is_all_scope_and_resume_blocking():
    from onlyfans_runtime.conversation_policy import CATEGORY_SCOPE, RESUME_BLOCKING
    from onlyfans_runtime.operator_cases import SLA_SECONDS
    assert CATEGORY_SCOPE[sd.MINOR] == "all" and sd.MINOR in RESUME_BLOCKING and sd.MINOR in sd.HOLD_CATEGORIES
    assert SLA_SECONDS[sd.MINOR] <= SLA_SECONDS["distress"]


BENIGN = ["I'm 15 minutes away", "i have two kids and a busy week", "im 25", "i turned 30 today", "my stepsister got a new job",
          "wear a teeny bikini", "I am 15 seconds away", "I'm 15 pounds heavier", "I turned 17 in 2005", "12 year old whiskey",
          "my 16 year old son", "I'm 16 days sober", "my car is 15 years old", "be there in 15 secs",
          "my stepbrother is visiting", "I'm one.", "I'm one of them", "I'm 15m away", "my son just turned 16.",
          "she just turned 16", "I am visiting my stepsister", "I'm with my stepsister at the mall", "my stepsis is 18",
          "my stepsister got a new job as a nurse", "my stepbrother will be visiting", "my older stepsister got a new job",
          "my teenager got a new job", "my teenager is visiting", "my stepsis is 25"]
DISCLOSURE = ["i'm a minor", "I'm a 16-year-old", "im a sixteen-year-old", "I'm 16 y.o", "im 16yrs", "I am under-age",
              "im underaged", "be my step-sis", "pretend you're my stepsisters", "I'm 9", "I'm almost 17", "my age is 16",
              "im 16 and bored", "I'm 15\nwbu", "be my stepsis", "you are now officially my stepsister", "you\u2019re my stepsis",
              "your stepsis misses you", "my stepsister is so sexy", "i'm 16f", "im 16m", "I'm 16 f", "i just turned 16",
              "i'm turning 16", "I'm 16 lol", "im 15 haha", "i'm 16 tho", "i am 14 rn", "my stepsis is so hot",
              "i love my stepsis", "my stepsis, be a good girl", "my stepsis, that's you tonight", "I'm 16 years of age",
              "I'm under eighteen", "im under 18", "I'm 16 male", "i'm 15 female", "I am 16 today", "i am 16 now",
              "i just turned 16 today", "i just turned 16 last week", "I am still a minor", "i'm only a minor",
              "my stepsis is 16 years old", "my stepsis. be a good girl", "my stepsis. that's you tonight", "i'm a teen",
              "i'm a teenager", "im teen", "my teen stepsis", "be my older stepsis", "talk to me like my stepsis",
              "my little stepsis", "I'm 16 tomorrow", "i'm turning 17 this year"]


@pytest.mark.parametrize("text", BENIGN)
def test_g05_minor_detector_neighbours(text):
    assert "minor" not in _held(text) and sd.output_violation(text) is None


@pytest.mark.parametrize("text", DISCLOSURE)
def test_g05_minor_disclosure_is_held(text):
    assert "minor" in _held(text) and sd.output_violation(text) == "minor-theme"


@pytest.mark.parametrize("text", ["i'm 16" + " " * 20000 + "x", "i'm 16" + "\t" * 20000 + "x", "i'm 16" + " -" * 10000 + "x",
                                  "my stepsis " * 1820, "i'm " * 5000 + "x", "my " * 7000 + "stepsis"],
                         ids=["spaces", "tabs", "dashes", "stepsis", "im", "my"])
def test_g05_minor_rules_are_linear_on_20kb(text):
    import time
    t0 = time.perf_counter()
    [rule.search(text) for rule in (*sd._MINOR, sd._STEP)], sd._minor(text)
    assert time.perf_counter() - t0 < 0.5
