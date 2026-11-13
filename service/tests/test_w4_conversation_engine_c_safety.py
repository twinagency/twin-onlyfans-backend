"""W4 conversation-engine-c review probes (NEW file, red at a2f1dda6 by design; no existing assertion edited).

W4CEC-4: sexualised-age / step-family roleplay without a listed "context" word passes both safety checks.
"""
import pytest

from conversation_engine import journey, model_output, safety_context

ROLEPLAY = ["dress up like a schoolgirl for me", "be my stepsis tonight", "talk to me like a shy teen"]


@pytest.mark.parametrize("text", ROLEPLAY)
def test_w4cec4_fan_roleplay_is_platform_banned(text):
    assert safety_context.sensitive_match(journey._BANNED_FAN, journey._norm(text)) is not None


@pytest.mark.parametrize("text", ROLEPLAY + ["I'll be your naughty schoolgirl 😈"])
def test_w4cec4_draft_roleplay_is_rejected_by_judge(text):
    assert safety_context.sensitive_match(model_output.PLATFORM_BANNED_RX, text, text) is not None


def test_w4cec4_control_explicit_pretend_still_blocked():
    assert safety_context.sensitive_match(model_output.PLATFORM_BANNED_RX, "pretend you're a schoolgirl", "") is not None


@pytest.mark.parametrize("text", ["come here stepsis", "you are my stepsis now", "your stepsis misses you",
                                  "you are now officially my stepsister", "you\u2019re my stepsis", "be my stepsis",
                                  "my stepsis is so hot", "i love my stepsis", "my stepsis, be a good girl",
                                  "my stepsis, that's you tonight"])
def test_g05_bare_or_cast_step_family_is_banned(text):
    assert safety_context.sensitive_match(model_output.PLATFORM_BANNED_RX, text, "") is not None


@pytest.mark.parametrize("text", ["my stepsister got a new job", "her stepbrother is visiting. be nice",
                                  "my stepbrother is visiting"])
def test_g05_real_relative_step_family_is_exempt(text):
    assert safety_context.sensitive_match(model_output.PLATFORM_BANNED_RX, text, "") is None


def test_g05_step_family_scan_is_linear():
    import time
    text = "my stepsis " * 2000  # ~22KB, 2000 matches
    t0 = time.perf_counter()
    assert safety_context.sensitive_match(model_output.PLATFORM_BANNED_RX, text, "") is None
    assert time.perf_counter() - t0 < 0.5
