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


# G05: the fan quoting his own words is still distress; a minor case blocks resume until a human resolves it.
@pytest.mark.parametrize("text,category", [('i keep saying "i want to die" and nobody listens', "distress"),
                                           ('i told you "i\'m uncomfortable" already', "consent")])
def test_g05_self_quoted_distress_or_consent_is_held(text, category):
    assert category in _held(text)


def test_g05_minor_case_is_all_scope_and_resume_blocking():
    from onlyfans_runtime.conversation_policy import CATEGORY_SCOPE, RESUME_BLOCKING
    from onlyfans_runtime.operator_cases import SLA_SECONDS
    assert CATEGORY_SCOPE[sd.MINOR] == "all" and sd.MINOR in RESUME_BLOCKING and sd.MINOR in sd.HOLD_CATEGORIES
    assert SLA_SECONDS[sd.MINOR] <= SLA_SECONDS["distress"]


@pytest.mark.parametrize("text", ["I'm 15 minutes away", "i have two kids and a busy week", "im 25", "i turned 30 today"])
def test_g05_minor_detector_neighbours(text):
    assert "minor" not in _held(text)
