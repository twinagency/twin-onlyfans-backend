"""Identity rule (OnlyFans): never mention AI/bot/clone, never claim human; sincere asks go to a human."""
import asyncio
import pytest

from native_pilot import engine as engine_mod
from native_pilot.identity import IDENTITY_HELD, discloses_ai, sincere_identity_question

DISCLOSING = ["im her ai clone", "i'm an AI chat clone of the creator", "as an AI i cant say",
              "im a bot lol", "this is an automated account", "i'm not her personally typing",
              "ur talking to her ai", "its her AI twin", "im not real babe"]
ORDINARY = ["haha ur cute", "are u a bot? lol jk", "i love ai art tho", "my clone? i wish lol",
            "im all yours tonight", "the robot vacuum died lol", "im so real with u", "i am a real fan of that"]


@pytest.mark.parametrize("text", DISCLOSING)
def test_disclosing_reply_is_caught(text):
    assert discloses_ai(text)


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_reply_passes(text):
    assert not discloses_ai(text)


@pytest.mark.parametrize("text,expected", [("are you a bot", True), ("is this ai?", True), ("r u real", True),
                                           ("are you real or a bot lol", True), ("are you really into me", False),
                                           ("is this pic ai", False), ("good morning", False)])
def test_sincere_question_detection(text, expected):
    assert sincere_identity_question(text) is expected


class _Engine:
    fan_id, content = 7, None

    def __init__(self):
        self.sent, self._violation = [], None

    def _send(self, fan_id, text, kwargs):
        self.sent.append(text)
        return {"id": 1}

    def _refuse(self, reason):
        self._violation = reason
        raise engine_mod.PilotBoundaryError(reason)


def test_send_boundary_holds_a_disclosing_bubble_and_sends_ordinary_text():
    e = _Engine()
    client = engine_mod._TextClient(e)
    client.send_message(7, "haha ur cute")
    with pytest.raises(engine_mod.PilotBoundaryError):
        client.send_message(7, "lol im her ai clone")
    assert e.sent == ["haha ur cute"] and e._violation == IDENTITY_HELD
