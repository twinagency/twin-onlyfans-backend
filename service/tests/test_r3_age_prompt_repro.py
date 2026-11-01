"""R3 reproduction: which assembled OnlyFans prompt carries an age, with and without a verified date of birth."""
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations import ai_chatter as chat
from automations._persona import compose_persona
from db.models import Fan
from types import SimpleNamespace

NO_DOB = SimpleNamespace(persona="Synthetic Creator enjoys hiking and coffee.", location=None, voice=None, persona_facts_json=None)
WITH_DOB = SimpleNamespace(persona="Synthetic Creator enjoys hiking and coffee.", location=None, voice=None,
                           persona_facts_json='{"age":"29","job":"nurse"}')


def system(cfg, **kwargs):
    fan = Fan(account_id="synthetic-acct", fan_id=900002)
    candidate = chat._Cand(900002)
    candidate.messages = [("in", "how old are you?")]
    candidate.last_body = candidate.last_in_text = "how old are you?"
    candidate.last_in_at = datetime(2026, 11, 1, 4)
    random.seed(7)
    messages, _ = chat._build_messages(compose_persona(cfg, fallback=""), fan, candidate, set(), **kwargs)
    return messages[0]["content"]


def test_legacy_route_baseline_carries_the_default_22yo_without_a_dob(monkeypatch):
    monkeypatch.setenv("ONLYFANS_FACTS_SCOPE", "0")
    assert "a real 22yo" in system(NO_DOB)


def test_legacy_route_with_scope_has_no_invented_age_without_a_dob(monkeypatch):
    monkeypatch.delenv("ONLYFANS_FACTS_SCOPE", raising=False)
    text = system(NO_DOB)
    assert "22yo" not in text and "HOW YOU TEXT (the approved creator's casual voice" in text


def test_legacy_route_with_dob_states_the_verified_age_only(monkeypatch):
    monkeypatch.delenv("ONLYFANS_FACTS_SCOPE", raising=False)
    text = system(WITH_DOB)
    assert "Age: 29" in text and "29yo" in text and "22yo" not in text


def test_native_route_is_age_neutral_with_and_without_dob(monkeypatch):
    monkeypatch.delenv("ONLYFANS_FACTS_SCOPE", raising=False)
    for cfg in (NO_DOB, WITH_DOB):
        text = system(cfg, onlyfans_text_only=True, painful_on=False, onlyfans_memory=True)
        assert "yo " not in text.split("HOW YOU TEXT")[1].split("\n")[0] and "22yo" not in text
    assert "Age:" not in system(NO_DOB, onlyfans_text_only=True, painful_on=False, onlyfans_memory=True)
    assert "Age: 29" in system(WITH_DOB, onlyfans_text_only=True, painful_on=False, onlyfans_memory=True)
