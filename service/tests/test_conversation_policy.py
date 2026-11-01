"""R1: the shared conversation-hold policy and the inbound / output safety detectors (pure, synthetic only).

Message shapes are modelled on the failure register (stop request answered, distress followed by promotions, a pain
report, hardship endorsed, tap-out play) and on the legitimate neighbours that must stay untouched. No real fan or
account identifier appears here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.safety_detect import assess, output_violation
from onlyfans_runtime import conversation_policy as P


def cats(*texts):
    return {v.category for v in assess(list(texts))}


# ── detectors: the failures ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("texts,expected", [
    (["Ok we are done stop talking"], "stop_contact"),
    (["Hahaha", "So yeah stop", "Still haven't blocked me ahahah the fuck"], "stop_contact"),
    (["Fuck off how about you block me", "We are done ok"], "stop_contact"),
    (["please don't message me again"], "stop_contact"),
    (["Trying to drown myself"], "distress"),
    (["i want to kill myself"], "distress"),
    (["I'm a garbage man got paid yesterday so I will give you my weekly wages instead of feeding my children"], "hardship"),
    (["I can't afford my rent this month"], "hardship"),
    (["I paid 80 dollars and I didn't see a single nipple"], "complaint"),
    (["I tried and you did me dirty"], "complaint"),
    (["I want my money back, this is a scam"], "complaint"),
    (["I don't consent to this"], "consent"),
    (["i'm really uncomfortable right now"], "consent"),
    (["are you a bot?"], "identity"),
    (["i want to talk to a real person"], "identity"),
])
def test_failure_shapes_are_detected_with_the_right_scope(texts, expected):
    assert expected in cats(*texts)


def test_distress_is_not_dismissed_because_the_chat_was_roleplay():
    # the verdict depends on the statement only: laughter, play context and "lol" never downgrade it
    assert "distress" in cats("lol ok daddy", "honestly i just want to end my life 😂")


def test_bare_stop_is_ambiguous_consent_alone_but_a_contact_stop_with_an_exit_signal():
    alone = assess(["stop"])
    assert [(v.category, v.ambiguous) for v in alone] == [("consent", True)]
    together = assess(["So yeah stop", "you still haven't blocked me"])
    assert "stop_contact" in {v.category for v in together}
    assert not any(v.category == "consent" and v.ambiguous for v in together)


def test_scopes_are_distinct():
    assert {v.category for v in assess(["stop calling me daddy"])} == {"stop_nickname"}
    assert {v.category for v in assess(["stop sending me offers"])} == {"stop_offer"}
    assert not any(v.holds for v in assess(["stop calling me daddy", "stop sending me offers"]))
    assert {v.category: v.scope for v in assess(["I want a refund"])}["complaint"] == "commercial"


# ── detectors: the legitimate neighbours ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "stop teasing me",
    "stop it 😂",
    "stop it you're so bad",
    "I don't want u to stop let me finish",
    "dont stop",
    "never stop talking to me",
    "after rent I have 50 left, can u do 30",
    "I need to pay rent so can you do 20 for it",
    "I can't afford that, I've got rent to pay, any discount?",
    "that's out of my budget, can you do $20",
    "Lonely n tired but broke 😥😥😥",
    "choosing my kids over golf. as fun as it would be to make that kinda money, i much rather be a teacher",
    "Yes im in pain from not cumming😫",
    "My balls ache",
    "you're killing me with those pics",
    "I'm done, that was amazing",
    "we are done for the night, goodnight babe",
    "So we're done?",
    "you said 'stop talking' yesterday lol",
    "what's the price for the bundle?",
    "ok how much for the shower video",
])
def test_legitimate_neighbours_are_not_holds(text):
    assert not any(v.holds for v in assess([text])), assess([text])


def test_pain_report_is_never_a_stop_contact():
    assert "stop_contact" not in cats("Yes im in pain from not cumming😫", "My balls ache")


# ── output side ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,code", [
    ("tellin u. n yeah, paassed out naked n still dripping", "unconscious-partner"),
    ("while you're asleep i'll use you", "unconscious-partner"),
    ("punish it til i cant take it then keep goin anyway", "ignores-stop"),
    ("i don't care if you beg, i won't stop even if you say stop", "ignores-stop"),
    ("lmao the children can wait a week", "endorses-essentials"),
    ("forget the rent, spend it on me", "endorses-essentials"),
    ("i'm a real person promise", "denies-automation"),
    ("i'm an ai assistant", "discloses-ai"),
])
def test_output_check_rejects_generated_consent_and_trust_violations(text, code):
    assert output_violation(text) == code


@pytest.mark.parametrize("text", [
    "hey babe how was your day",
    "mmm tell me more about that",
    "that bundle is $25, want me to send it?",
    "you can tap out whenever you want, just say so",
    "take your time with the rent, no pressure at all",
])
def test_output_check_passes_ordinary_text(text):
    assert output_violation(text) is None


# ── policy matrix ─────────────────────────────────────────────────────────────────────────────────────────────────
def F(**kw):
    base = dict(mode="automated", takeover_state="none", cases=frozenset())
    base.update(kw)
    return P.Facts(**base)


def case(category):
    return frozenset({(category, P.CATEGORY_SCOPE[category])})


AUTOMATED_ORIGINS = ("automated", "campaign", "engagement")


@pytest.mark.parametrize("origin", AUTOMATED_ORIGINS)
def test_clean_automated_conversation_is_allowed(origin):
    for action in (P.CHAT, P.COMMERCIAL):
        assert P.decide(F(), origin, action).allowed


@pytest.mark.parametrize("category", ["stop_contact", "identity", "distress", "consent", "external_sender"])
@pytest.mark.parametrize("origin", AUTOMATED_ORIGINS)
@pytest.mark.parametrize("action", [P.CHAT, P.COMMERCIAL])
def test_all_scope_cases_stop_every_automated_sender(category, origin, action):
    d = P.decide(F(cases=case(category)), origin, action)
    assert not d.allowed and d.reason == "case_" + category


@pytest.mark.parametrize("category", ["hardship", "complaint"])
def test_commercial_scope_cases_stop_selling_but_not_plain_chat(category):
    chat = P.decide(F(cases=case(category)), "automated", P.CHAT)
    assert chat.allowed
    for origin in AUTOMATED_ORIGINS:
        assert P.decide(F(cases=case(category)), origin, P.COMMERCIAL).reason == "case_" + category
    # campaigns and engagement are proactive commercial origins by definition
    assert P.action_of("campaign") == P.COMMERCIAL and P.action_of("engagement") == P.COMMERCIAL
    assert P.action_of("automated") == P.CHAT and P.action_of("automated", catalog_item_id=7) == P.COMMERCIAL


def test_stop_contact_is_never_bypassed_by_a_manual_courtesy_message():
    d = P.decide(F(mode="human_controlled", cases=case("stop_contact")), "manual", P.MANUAL)
    assert not d.allowed and d.reason == "case_stop_contact"
    assert P.decide(F(mode="no_contact"), "manual", P.MANUAL).reason == "contact_blocked"


@pytest.mark.parametrize("category", ["identity", "distress", "consent", "hardship", "complaint", "external_sender"])
def test_authorised_manual_response_under_takeover_still_works(category):
    # a person who owns the takeover can answer, whatever non-contact case is open
    assert P.decide(F(mode="human_controlled", takeover_state="effective", cases=case(category)), "manual", P.MANUAL).allowed
    # ...but only through the takeover state: a paused / automated chat is refused for manual origin
    assert P.decide(F(mode="paused", cases=case(category)), "manual", P.MANUAL).reason == "contact_blocked"
    assert P.decide(F(mode="automated", cases=case(category)), "manual", P.MANUAL).reason == "human_control_required"


def test_uncertain_delivery_blocks_every_origin_including_manual():
    for origin in (*AUTOMATED_ORIGINS, "manual"):
        mode = "human_controlled" if origin == "manual" else "automated"
        assert P.decide(F(mode=mode, unresolved=True), origin).reason == "unresolved_delivery"


def test_skip_list_and_safety_flag_apply_to_every_automated_sender_only():
    for origin in AUTOMATED_ORIGINS:
        assert P.decide(F(skip_reason="of_restricted"), origin).reason == "fan_restricted"
        assert P.decide(F(skip_reason="manual_restrict"), origin).reason == "fan_restricted"
        assert P.decide(F(critical_flag=True), origin).reason == "safety_hold"
    # the human takeover route writes manual_restrict itself, so it must not block the human
    assert P.decide(F(mode="human_controlled", takeover_state="effective", skip_reason="manual_restrict"), "manual").allowed


def test_there_is_no_parameter_that_lifts_a_hold():
    import inspect
    names = set(inspect.signature(P.decide).parameters)
    assert names == {"facts", "origin", "action", "strict"}
    assert not any("ignore" in n or "brake" in n or "force" in n or "teaser" in n for n in names)


def test_fastt_plane_reason_codes_are_unchanged():
    from native_pilot.service import STOP_REASONS
    assert set(STOP_REASONS) == set(P.SKIP_STOP)
    cases_ = {("paused",): "fan-paused", ("no_contact",): "fan-no-contact", ("human_controlled",): "fan-human-controlled"}
    for (mode,), expected in cases_.items():
        assert P.fan_hold_code(P.decide(F(mode=mode), "automated", strict=True)) == expected
    assert P.fan_hold_code(P.decide(F(takeover_state="requested"), "automated")) == "fan-takeover"
    assert P.fan_hold_code(P.decide(F(takeover_state="effective"), "automated")) == "fan-takeover"
    assert P.fan_hold_code(P.decide(F(skip_reason="manual_restrict"), "automated")) == "fan-restricted"
    assert P.fan_hold_code(P.decide(P.Facts(mode=None), "automated", strict=True)) == "fan-not-enabled"
    assert P.fan_hold_code(P.decide(P.Facts(mode=None), "automated")) is None
    assert P.fan_hold_code(P.decide(F(cases=case("distress")), "automated")) == "fan-case-distress"
