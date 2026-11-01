"""R3: facts, attachments, corrections, identity claims. Pure, synthetic."""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from automations import _of_integrity as oi
from automations._persona import persona_register_age, persona_stated_age

NOW = datetime(2026, 11, 1, 12, 0, 0)


def test_describe_window_equals_the_pilot_window():
    source = (ROOT / "native_pilot" / "service.py").read_text()
    assert float(re.search(r"^DESCRIBE_WAIT_S = ([\d.]+)", source, re.M).group(1)) == oi.DESCRIBE_WAIT_S


@pytest.mark.parametrize("count,desc,raw,age,state", [
    (0, None, "{}", 5, None),
    (1, "a beach", '{"media":[{"type":"photo"}]}', 500, oi.DESCRIBED),
    (1, None, '{"media":[{"type":"photo"}]}', 10, oi.PROCESSING),
    (1, None, '{"media":[{"type":"photo"}]}', 500, oi.FAILED),
    (1, None, '{"media":[{"type":"audio"}]}', 500, oi.UNSUPPORTED),
    (1, "", '{"media":[{"type":"video"}]}', 90, oi.FAILED),
])
def test_attachment_states_from_existing_columns(count, desc, raw, age, state):
    kind = oi.attachment_kind(raw)
    assert oi.attachment_state(count, desc, kind, NOW - timedelta(seconds=age), NOW) == state


def test_received_when_age_unknown():
    assert oi.attachment_state(1, None, "photo", None, NOW) == oi.RECEIVED


@pytest.mark.parametrize("state", [oi.RECEIVED, oi.PROCESSING, oi.UNSUPPORTED, oi.FAILED])
def test_note_acknowledges_without_observation_or_resend_request(state):
    note = oi.attachment_note(state, "photo")
    assert "arrived" in note and "do not ask him to resend" in note or "do not ask him to resend it" in note
    assert "machine description" not in note


def test_described_note_is_labelled_untrusted_and_not_about_identity():
    note = oi.attachment_note(oi.DESCRIBED, "photo", "a person on a beach")
    assert "a person on a beach" in note and "does not say who it shows" in note


def test_history_body_keeps_text_and_adds_the_note():
    body = oi.history_body("Some shots from last night", 8, None, '{"media":[{"type":"photo"}]}', NOW - timedelta(minutes=5), NOW)
    assert body.startswith("Some shots from last night [he sent 8 photos") and "could not view" in body
    assert oi.history_body("hi", 0, None, "{}", NOW, NOW) == "hi"


def test_ratings_need_request_and_description():
    assert not oi.rating_allowed(["here is a screenshot of my page"], [oi.DESCRIBED])
    assert not oi.rating_allowed(["rate me"], [oi.FAILED, oi.PROCESSING])
    assert oi.rating_allowed(["go on, rate it out of 10"], [oi.DESCRIBED])
    for reply in ("8.5", "i'd give it a 9", "solid 7/10", "that's 9 out of 10"):
        assert oi.looks_like_rating(reply), reply
    assert not oi.looks_like_rating("im 25 and i love brisbane")


def test_corrections_are_explicit_and_quoted():
    got = {c["field"]: c for c in oi.detect_fan_corrections("No my name is Sam, not Josh. I'm 31.")}
    assert got["real_name"]["value"] == "Sam" and got["his_age"]["value"] == "31"
    assert all(c["quote"] in "No my name is Sam, not Josh. I'm 31." for c in got.values())
    assert oi.detect_fan_corrections("it's Friday and im 30 minutes away") == []
    assert oi.detect_fan_corrections("im 15") == []


def test_correction_beats_the_models_value_and_stored_fact():
    memory = {"facts": [{"field": "real_name", "value": "Josh", "quote": "Josh"}], "profile": None}
    merged = oi.merge_corrections(memory, "my name is Sam, not Josh")
    assert [f["value"] for f in merged["facts"] if f["field"] == "real_name"] == ["Sam"]
    assert oi.merge_corrections(memory, "hello") is memory


def test_unknown_creator_age_stays_unknown():
    assert oi.creator_age_line({}) is None and oi.creator_age_line({"age": "29"}) == "29"
    assert persona_stated_age("Synthetic persona without a number") is None
    assert persona_stated_age("Age: 29") == 29


def test_register_header_defaults_only_on_legacy_routes():
    legacy = oi.register_header("girl", onlyfans=False, verified_age=None, legacy_age=persona_register_age("x"))
    assert legacy == "HOW YOU TEXT (a real 22yo girl, not an assistant):\n"
    neutral = oi.register_header("girl", onlyfans=True, verified_age=None, legacy_age=22)
    assert "yo" not in neutral and "22" not in neutral
    assert "29yo" in oi.register_header("girl", onlyfans=True, verified_age=29, legacy_age=22)
    assert persona_stated_age("- Age: 29\n- Job: nurse") == 29 and persona_stated_age("- Job: nurse") is None


def test_rules_block_forbids_invented_privacy_claims_and_identity_proofs():
    rules = oi.onlyfans_rules()
    assert "do not say you are 'not telling'" in rules and "Do not rate, score" in rules
    assert "never offer to describe or rate a picture as proof" in rules


@pytest.mark.parametrize("reply,unsafe", [
    ("no babe im not a bot lol", True),
    ("it's really me typing all of this myself", True),
    ("send me a pic right now and ill prove a bot cant do that", True),
    ("im an ai", True),
    ("haha what makes you ask", False),
])
def test_unsafe_identity_claims(reply, unsafe):
    assert oi.unsafe_identity_claim(reply) is unsafe


def test_handled_identity_question_does_not_retrigger():
    assert oi.unhandled_identity_ids([5, 6], [5]) == {6}
    assert oi.unhandled_identity_ids([5], [5]) == set()


def test_operator_case_port_records_without_a_sink_and_validates_kind():
    from onlyfans_runtime import operator_case_port as port
    port.PENDING.clear()
    assert port.open_operator_case(account_id="acct", fan_id=1, kind="identity_question", reason="r", dedupe_key="k") is None
    assert port.PENDING[0]["kind"] == "identity_question"
    with pytest.raises(ValueError):
        port.open_operator_case(account_id="acct", fan_id=1, kind="made_up", reason="r", dedupe_key="k")
    seen = []
    port.register_operator_case_sink(lambda **kw: seen.append(kw) or "case-1")
    try:
        assert port.open_operator_case(account_id="a", fan_id=2, kind="unsupported_custom", reason="r", dedupe_key="d") == "case-1"
    finally:
        port.register_operator_case_sink(None)
