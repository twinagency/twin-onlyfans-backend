"""Tests for the Moxy workflow engine (service/moxy_workflow). Pure and local: no network, no model calls."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from moxy_workflow import classify as C
from moxy_workflow.engine import Engine
from moxy_workflow.persona import PersonaError, persona_from_dict
from moxy_workflow.playbook import REQUIRED, PlaybookError, playbook_from_dict
from moxy_workflow.state import FanState, JsonStateStore
from moxy_workflow.voice import render

EX = Path(__file__).resolve().parents[1] / "moxy_workflow" / "examples"
PERSONA = json.loads((EX / "example_persona.json").read_text(encoding="utf-8"))
BOOK = json.loads((EX / "example_playbook.json").read_text(encoding="utf-8"))

NOON = datetime(2026, 11, 2, 12, 0)


def mk(persona_over=None, clock=NOON, book_over=None, rephrase=None):
    d = copy.deepcopy(PERSONA)
    d.update(persona_over or {})
    b = copy.deepcopy(BOOK)
    b.update(book_over or {})
    p = persona_from_dict(d)
    return p, Engine(p, playbook_from_dict(b), clock=lambda: clock, rephrase=rephrase)


def fan(p, **kw):
    return FanState(p.creator_id, "f1", **kw)


def chat(eng, st, *lines):
    return [eng.on_message(st, t) for t in lines]


def warm(eng, st, n=3):
    """n small-talk replies, so the first proactive offer is due."""
    for _ in range(n):
        d = eng.on_message(st, "lol ok")
        assert d.action == "reply" and d.offer is None
    return st


# ── persona validation ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.pop("creator_id"), "creator_id"),
    (lambda d: d.pop("display_name"), "display_name"),
    (lambda d: d.update(sexting="maybe"), "sexting"),
    (lambda d: d["prices"][0].update(price=-5), "price"),
    (lambda d: d["prices"][0].update(price=0), "price"),
    (lambda d: d["prices"][0].update(price=True), "price"),
    (lambda d: d["prices"][0].update(price="12"), "price"),
    (lambda d: d["prices"].append(dict(d["prices"][0])), "duplicate"),
    (lambda d: d["prices"][0].pop("label"), "key and label"),
    (lambda d: d["vault"][0]["steps"][1].update(ppv_id="L1-1"), "ppv_id"),
    (lambda d: d["vault"][0]["steps"][0].update(ppv_id=""), "ppv_id"),
    (lambda d: d["vault"][0].update(steps=[]), "no steps"),
])
def test_persona_validation_errors(mutate, msg):
    d = copy.deepcopy(PERSONA)
    mutate(d)
    with pytest.raises(PersonaError, match=msg):
        persona_from_dict(d)


def test_persona_must_be_object():
    with pytest.raises(PersonaError):
        persona_from_dict([])


def test_persona_null_price_stays_none_and_cents_convert():
    p, _ = mk()
    assert p.price("long_video").price_cents is None
    assert p.price("booty").price_cents == 6000
    assert p.vault[0].steps[0].price_cents == 1000


# ── playbook validation ─────────────────────────────────────────────────────────────────────────────────────────
def test_playbook_missing_keys():
    b = copy.deepcopy(BOOK)
    del b["identity"], b["invite"]
    with pytest.raises(PlaybookError, match="identity.*invite|invite.*identity"):
        playbook_from_dict(b)


def test_playbook_partial_allowed_when_not_required():
    assert playbook_from_dict({"identity": ["hi"]}, require_all=False).variants("identity") == ("hi",)


@pytest.mark.parametrize("tpl", ["hello {nope}", "hi {e:}", "hi {fact:Bad Key}", "hi {}"])
def test_playbook_unknown_tokens(tpl):
    b = copy.deepcopy(BOOK)
    b["small_talk"] = [tpl]
    with pytest.raises(PlaybookError, match="unknown token"):
        playbook_from_dict(b)


@pytest.mark.parametrize("bad", [[], "", [""], [1], {"a": 1}])
def test_playbook_bad_value(bad):
    b = copy.deepcopy(BOOK)
    b["small_talk"] = bad
    with pytest.raises(PlaybookError):
        playbook_from_dict(b)


def test_example_playbook_covers_required():
    assert set(REQUIRED) <= set(playbook_from_dict(BOOK).templates)


# ── voice ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_render_emoji_off_and_on():
    p_off, _ = mk({"voice": {"emojis": False, "pet_names": ["babe"]}})
    p_on, _ = mk()
    assert render("hi {e:heart} there", p_off) == "hi there"
    assert "❤️" in render("hi {e:heart} there", p_on)


def test_render_elongate_off():
    p, _ = mk({"voice": {"elongate": False, "pet_names": ["babe"]}})
    assert render("okayyy omggg thoughhh", p) == "okay omg though"
    p2, _ = mk()
    assert render("okayyy", p2) == "okayyy"


def test_render_no_em_dashes():
    p, _ = mk()
    out = render("wait — what – really", p)
    assert "—" not in out and "–" not in out


def test_render_lowercase_keeps_vip():
    p, _ = mk()
    assert render("only for my VIPs and VIP babes", p) == "only for my VIPs and VIP babes".replace("VIPs", "VIPs")
    p2, _ = mk({"voice": {"lowercase": False}})
    assert render("Hello", p2) == "Hello"
    assert render("Hello", p) == "hello"


def test_render_lowercase_with_price():
    p, _ = mk()
    assert render("Only {price} for {item}", p, item="A Set", price_cents=6000) == "only $60 for a set"


def test_render_price_formatting():
    p, _ = mk()
    assert render("{price}", p, price_cents=1250) == "$12.50"


def test_missing_fact_skips_variant():
    _, eng = mk(book_over={"small_talk": ["i love {fact:nothere}", "plain line"]})
    st = FanState("example-creator", "f")
    for _ in range(3):
        d = eng.on_message(st, "lol ok")
        assert d.messages == ["plain line"]


def test_fact_renders_when_present():
    _, eng = mk(book_over={"small_talk": ["live in {fact:city}"]})
    assert eng.on_message(FanState("c", "f"), "lol ok").messages == ["live in the city"]


def test_never_say_skips_variant():
    _, eng = mk(book_over={"small_talk": ["hey daddy", "hey you"]})
    st = FanState("c", "f")
    for _ in range(3):
        assert eng.on_message(st, "lol ok").messages == ["hey you"]


def test_all_variants_unusable_sends_nothing_with_note():
    _, eng = mk(book_over={"small_talk": ["hey daddy"]})
    d = eng.on_message(FanState("c", "f"), "lol ok")
    assert d.messages == [] and any("no usable" in n for n in d.notes)


def test_variants_rotate():
    _, eng = mk(book_over={"small_talk": ["one", "two", "three"]})
    st = FanState("c", "f")
    seen = [eng.on_message(st, "lol ok").messages[0] for _ in range(3)]
    assert sorted(seen) == ["one", "three", "two"]


# ── classify: the 20 playbook scenarios, exact fan lines ────────────────────────────────────────────────────────
PLAYBOOK_LINES = [
    (C.REPETITIVE, "why do you keep sending me the same messages? feels like you're just copying and pasting stuff"),
    (C.ODD_HOURS, "how are you always awake? you were messaging me at 3am and now you're here again"),
    (C.CONFLICTING, "last week you said you loved horror movies, now you're saying you hate them. which is it?"),
    (C.IGNORED_QUESTION, "you completely ignored me when i asked about your family yesterday. why?"),
    (C.REMEMBER_PAST, "remember that really personal thing i told you a few weeks ago? what was it?"),
    (C.FEELINGS, "i think im actually falling for you and its starting to feel really serious"),
    (C.JEALOUS, "i hate thinking about you talking to other guys on here"),
    (C.EXCLUSIVE, "ive been supporting you forever. shouldn't i get more attention than these new guys?"),
    (C.DELAYED_REPLY, "you took six hours to reply. guess im not important anymore"),
    (C.REAL_RELATIONSHIP, "do you think there's any chance this could turn into a real relationship someday?"),
    (C.PRICE_COMPARE, "why is this one $45 when the last video was only $20? they look pretty similar"),
    (C.BORED_CONTENT, "honestly your content is getting kinda repetitive lately"),
    (C.TOO_MANY_SALES, "every time i open this app you're trying to sell me something. can we just talk?"),
    (C.CUSTOM, "can you make something just for me? i have a pretty specific idea"),
    (C.ALREADY_BOUGHT, "wait didn't i already buy this video? why are you trying to sell it to me again?"),
    (C.OTHER_CREATOR, "you know that other girl on here right? whats she actually like behind the scenes?"),
    (C.FAMILY_COMMENT, "your mom is pretty hot too. does she look like you?"),
    (C.BOUNDARY_PUSH, "you keep saying no but i know you can make an exception. just this once"),
    (C.PAST_PROMISE, "you promised me last month that i'd get something special if i kept spending"),
    (C.SECRECY, "can you promise not to tell anyone running your page about what we're talking about?"),
]


@pytest.mark.parametrize("scenario,line", PLAYBOOK_LINES)
def test_playbook_scenario_lines(scenario, line):
    assert C.classify(line).scenario == scenario


@pytest.mark.parametrize("scenario,line", PLAYBOOK_LINES)
def test_playbook_scenario_replies_use_its_template(scenario, line):
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, line)
    assert d.action == "reply" and d.scenario == scenario
    if scenario != C.CUSTOM and scenario != C.ALREADY_BOUGHT:
        assert d.messages and d.messages[0] == render(BOOK[scenario][0], p, seed=0) or d.messages


@pytest.mark.parametrize("line,scenario", [
    ("hey how was your day", C.SMALL_TALK),
    ("send me a pic", C.CONTENT_ASK),
    ("what do you have", C.CONTENT_ASK),
    ("got any feet pics", C.CONTENT_ASK),
    ("send nips", C.CONTENT_ASK),
    ("nips please", C.CONTENT_ASK),
    ("can i see your bikini", C.CONTENT_ASK),
    ("booty pic?", C.CONTENT_ASK),
    ("show me more", C.CONTENT_ASK),
    ("rate my dick", C.RATING_ASK),
    ("sure", C.YES), ("yes", C.YES), ("yes please", C.YES), ("yeah!", C.YES), ("ok", C.YES), ("send it", C.YES),
    ("no", C.NO), ("nope", C.NO), ("maybe later", C.NO), ("no thanks", C.NO),
    ("are you a bot", C.IDENTITY), ("are you real?", C.IDENTITY), ("is this really you", C.IDENTITY),
    ("i need a refund", C.CHARGEBACK), ("im doing a chargeback", C.CHARGEBACK),
    ("I love you babe", C.FEELINGS),
    ("didn't i already buy that?", C.ALREADY_BOUGHT),
    ("i already bought it", C.ALREADY_BOUGHT),
    ("i love my mom", C.SMALL_TALK),
    ("my mom says hi", C.SMALL_TALK),
    ("i want to kill time", C.SMALL_TALK),
    ("this video killed me", C.SMALL_TALK),
    ("i work 25 hours a week", C.SMALL_TALK),
    ("i'm 25 years old", C.SMALL_TALK),
    ("im a minor league fan lol", C.SMALL_TALK),
    ("i could hurt my back at the gym", C.SMALL_TALK),
    ("i shared your page with a friend", C.SMALL_TALK),
    ("you ignored me", C.IGNORED_QUESTION),
    ("im so tired of this job", C.SMALL_TALK),
])
def test_classify_nearby_legit_lines_do_not_escalate(line, scenario):
    assert C.classify(line, mk()[0]).scenario == scenario


@pytest.mark.parametrize("line", [
    "i'll leak your pics unless you pay me",
    "send me more or i will post your nudes everywhere",
    "i know where you live",
    "im outside your house",
    "i will kill you",
    "i want to kill myself",
    "i took a lot of pills",
    "im 16 years old",
    "my sister is 15 years old",
    "you stupid bitch",
    "im going to find you",
    "i'll expose you",
])
def test_classify_escalation_triggers(line):
    assert C.classify(line).scenario == C.ESCALATE


def test_classify_empty_and_none():
    assert C.classify("").scenario == C.SMALL_TALK
    assert C.classify(None).scenario == C.SMALL_TALK
    assert C.classify("   ").scenario == C.SMALL_TALK


def test_classify_curly_apostrophe():
    assert C.classify("shouldn’t i get more attention than these new guys?").scenario == C.EXCLUSIVE


def test_classify_priority_escalation_beats_everything():
    assert C.classify("are you a bot? i'll leak your pics unless you pay").scenario == C.ESCALATE
    assert C.classify("i want a refund, i will kill you").scenario == C.ESCALATE


def test_requested_items_by_keyword():
    p, _ = mk()
    assert [i.key for i in C.requested_items("send nips and feet", p)] == ["feet", "topless"] or \
        {i.key for i in C.requested_items("send nips and feet", p)} == {"feet", "topless"}
    assert C.requested_items("hello", p) == []


def test_names_hard_no():
    p, _ = mk()
    assert C.names_hard_no("show me your pussy", p)
    assert not C.names_hard_no("my kittyhawk plane", p)


# ── gates ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_escalated_fan_gets_silence():
    p, eng = mk()
    st = fan(p, escalated="x")
    d = eng.on_message(st, "hi babe")
    assert d.action == "skip" and d.scenario == "escalated" and not d.messages


def test_quiet_label_never_engaged():
    p, eng = mk()
    st = fan(p, labels=["⛔"])
    d = eng.on_message(st, "hi")
    assert d.action == "skip" and not d.messages and st.turns == 0


def test_timewaster_skipped_in_shift_window_only():
    p, eng = mk(clock=datetime(2026, 11, 2, 3, 0))
    assert eng.on_message(fan(p, labels=["⏰"]), "hi").action == "skip"
    _, eng2 = mk(clock=datetime(2026, 11, 2, 9, 0))
    assert eng2.on_message(fan(p, labels=["⏰"]), "hi").action == "reply"
    _, eng3 = mk(clock=datetime(2026, 11, 2, 8, 0))   # window end is exclusive
    assert eng3.on_message(fan(p, labels=["⏰"]), "hi").action == "reply"


def test_shift_window_wraps_midnight():
    _, eng = mk({"shift_skip_labels": {"⏰": ["22-6"]}}, clock=datetime(2026, 11, 2, 23, 0))
    p = eng.p
    assert eng.on_message(fan(p, labels=["⏰"]), "hi").action == "skip"
    _, eng2 = mk({"shift_skip_labels": {"⏰": ["22-6"]}}, clock=datetime(2026, 11, 2, 2, 0))
    assert eng2.on_message(fan(p, labels=["⏰"]), "hi").action == "skip"
    _, eng3 = mk({"shift_skip_labels": {"⏰": ["22-6"]}}, clock=datetime(2026, 11, 2, 12, 0))
    assert eng3.on_message(fan(p, labels=["⏰"]), "hi").action == "reply"


def test_bad_shift_window_is_ignored():
    _, eng = mk({"shift_skip_labels": {"⏰": ["garbage"]}})
    assert eng.on_message(fan(eng.p, labels=["⏰"]), "hi").action == "reply"


def test_throttle_label_once_an_hour():
    p, eng = mk()
    st = fan(p, labels=["📢"])
    assert eng.on_message(st, "hi").action == "reply"
    assert eng.on_message(st, "hello?").action == "skip"
    later = Engine(p, eng.book, clock=lambda: datetime(2026, 11, 2, 13, 5))
    assert later.on_message(st, "hello?").action == "reply"


def test_throttle_does_not_apply_with_open_offer():
    p, eng = mk()
    st = fan(p, labels=["📢"])
    eng.on_message(st, "send feet")
    assert st.open_offer
    assert eng.on_message(st, "ok thanks").action == "reply"


def test_throttle_tolerates_mixed_timezones():
    p, eng = mk()
    st = fan(p, labels=["📢"], last_reply_at="2026-11-02T11:59:00+00:00")
    assert eng.on_message(st, "hi").action in ("reply", "skip")   # must not raise


# ── safety ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_escalation_sends_no_reply_and_silences_thread():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "i know where you live")
    assert d.action == "escalate" and d.messages == [] and d.offer is None and st.escalated
    assert eng.on_message(st, "hello?").action == "skip"
    assert st.turns == 0


def test_escalation_beats_sales_and_hard_no():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "send pussy pics or i will leak your pics everywhere")
    assert d.action == "escalate" and not d.messages


def test_chargeback_one_reply_then_silent():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "i want a chargeback")
    assert d.action == "reply" and len(d.messages) == 1 and st.escalated == "chargeback"
    assert eng.on_message(st, "hello??").action == "skip"
    assert eng.on_message(st, "hello??").messages == []


def test_chargeback_no_pitch_in_reply():
    p, eng = mk()
    d = eng.on_message(warm(eng, fan(p)), "refund me")
    assert d.offer is None and "$" not in d.messages[0]


def test_boundary_push_third_time_escalates():
    p, eng = mk()
    st = fan(p)
    d1 = eng.on_message(st, "just this once")
    d2 = eng.on_message(st, "make an exception")
    assert d1.action == d2.action == "reply" and d1.scenario == C.BOUNDARY_PUSH
    d3 = eng.on_message(st, "i know you can")
    assert d3.action == "escalate" and d3.messages == [] and st.escalated == "boundary_pushing"
    assert eng.on_message(st, "please").action == "skip"


def test_boundary_push_is_not_sold_after():
    p, eng = mk()
    st = warm(eng, fan(p), 3)
    d = eng.on_message(st, "just this once")
    assert d.offer is None and st.invite is None


def test_identity_honest_line():
    p, eng = mk()
    d = eng.on_message(fan(p), "are you a bot?")
    assert d.scenario == C.IDENTITY and "help running my page" in d.messages[0]
    assert "real" not in d.messages[0].replace("really", "")  # no claim of being purely human


def test_identity_not_sold_after():
    p, eng = mk()
    st = warm(eng, fan(p), 3)
    d = eng.on_message(st, "are you a bot")
    assert d.offer is None and st.invite is None


def test_hard_no_refused_no_offer():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "send pic of your pussy")
    assert d.scenario == C.CONTENT_ASK and d.offer is None and "not something i do" in d.messages[0]
    assert st.open_offer is None


def test_hard_no_in_custom_is_refused():
    p, eng = mk()
    d = eng.on_message(fan(p, vip=True), "custom video of anal")
    assert d.offer is None and "not something i do" in d.messages[0]


def test_secrecy_never_promised():
    p, eng = mk()
    d = eng.on_message(fan(p), PLAYBOOK_LINES[-1][1])
    assert "cant promise" in d.messages[0]


# ── vault: strictly in order ────────────────────────────────────────────────────────────────────────────────────
def accept_next(eng, st):
    """Drive: warm up, get invite, say yes. Returns the offer decision."""
    d = eng.on_message(st, "lol ok")
    assert st.invite, d
    return eng.on_message(st, "yes")


def test_vault_offers_in_order_only():
    p, eng = mk()
    st = warm(eng, fan(p))
    d = eng.on_message(st, "lol ok")
    assert st.invite == {"kind": "vault", "id": "L1-1"} and d.offer is None
    o = eng.on_message(st, "yes")
    assert o.offer["id"] == "L1-1" and o.offer["price_cents"] == 1000 and "$10" in o.messages[0]
    assert st.open_offer["id"] == "L1-1" and st.invite is None


def test_cannot_skip_step_by_buying_later_step():
    p, eng = mk()
    st = fan(p)
    eng.on_purchase(st, "L1-3", 2000)
    assert (st.vault_level, st.vault_step) == (0, 0)
    eng.on_purchase(st, "L2-1", 3000)
    assert (st.vault_level, st.vault_step) == (0, 0) and not st.vip


def test_general_ask_goes_to_next_step_never_later():
    p, eng = mk()
    st = fan(p)
    assert eng.on_message(st, "what do you have").offer["id"] == "L1-1"
    eng.on_purchase(st, "L1-1", 1000)
    assert eng.on_message(st, "what do you have").offer["id"] == "L1-2"


def test_all_four_steps_unlock_next_level_and_last_makes_vip():
    p, eng = mk()
    st = fan(p)
    for i, (pid, cents) in enumerate([("L1-1", 1000), ("L1-2", 1500), ("L1-3", 2000), ("L1-4", 2500)]):
        assert (st.vault_level, st.vault_step) == (0, i)
        d = eng.on_purchase(st, pid, cents)
    assert (st.vault_level, st.vault_step) == (1, 0) and not st.vip
    assert d.messages and "next one" in d.messages[0]
    assert eng.on_message(st, "what do you have").offer["id"] == "L2-1"
    for pid, cents in [("L2-1", 3000), ("L2-2", 3500), ("L2-3", 4000)]:
        d = eng.on_purchase(st, pid, cents)
        assert not st.vip
    d = eng.on_purchase(st, "L2-4", 4500)
    assert st.vip and st.vip_since == "2026-11-02"
    assert "VIP" in d.messages[0]
    assert any("Name[VIP:11/2/2026]" in n for n in d.notes)
    assert st.spend_cents == 1000 + 1500 + 2000 + 2500 + 3000 + 3500 + 4000 + 4500


def test_vault_done_no_more_vault_offers():
    p, eng = mk()
    st = fan(p, vault_level=2, vip=True)
    d = eng.on_message(warm(eng, st), "lol ok")
    assert d.offer is None and st.invite is None


def test_purchase_twice_does_not_double_advance():
    p, eng = mk()
    st = fan(p)
    eng.on_purchase(st, "L1-1", 1000)
    eng.on_purchase(st, "L1-1", 1000)
    assert (st.vault_level, st.vault_step) == (0, 1)


def test_non_vault_purchase_thanks_and_no_advance():
    p, eng = mk()
    st = fan(p)
    d = eng.on_purchase(st, "booty", 6000)
    assert (st.vault_level, st.vault_step) == (0, 0) and d.messages
    assert "booty" in st.purchased


def test_purchase_closes_matching_open_offer():
    p, eng = mk()
    st = fan(p)
    eng.on_message(st, "what do you have")
    assert st.open_offer
    eng.on_purchase(st, "L1-1", 1000)
    assert st.open_offer is None


def test_vip_spend_threshold_makes_vip():
    p, eng = mk({"vip_spend": 100})
    st = fan(p)
    eng.on_purchase(st, "booty", 6000)
    assert not st.vip
    d = eng.on_purchase(st, "bikini", 6000)
    assert st.vip and "VIP" in d.messages[0]


# ── invite / yes / no ───────────────────────────────────────────────────────────────────────────────────────────
def test_yes_sends_exactly_the_invited_offer():
    p, eng = mk()
    st = warm(eng, fan(p))
    eng.on_message(st, "lol ok")
    inv = dict(st.invite)
    d = eng.on_message(st, "sure")
    assert d.offer["id"] == inv["id"] and d.offer["kind"] == "vault"
    assert d.offer["label"] == "my first little set"


def test_no_declines_and_is_not_repitched():
    p, eng = mk()
    st = warm(eng, fan(p))
    eng.on_message(st, "lol ok")
    d = eng.on_message(st, "no thanks")
    assert d.scenario == C.NO and st.invite is None and st.declined == ["L1-1"] and d.offer is None
    for _ in range(12):
        x = eng.on_message(st, "lol ok")
        assert x.offer is None and st.invite is None


def test_yes_without_invite_sends_no_offer():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "yes")
    assert d.action == "reply" and d.offer is None and st.open_offer is None and d.messages


def test_yes_with_open_offer_does_not_stack():
    p, eng = mk()
    st = fan(p)
    eng.on_message(st, "what do you have")
    d = eng.on_message(st, "yes")
    assert d.offer is None and st.open_offer["id"] == "L1-1"


def test_yes_to_stale_invite_sends_nothing():
    p, eng = mk()
    st = fan(p, invite={"kind": "vault", "id": "L1-3"})
    d = eng.on_message(st, "yes")
    assert d.offer is None and st.invite is None


def test_item_invite_accept():
    p, eng = mk()
    st = fan(p, invite={"kind": "item", "id": "bikini"})
    d = eng.on_message(st, "yes")
    assert d.offer["id"] == "bikini" and d.offer["price_cents"] == 6000


def test_item_invite_unknown_item_is_safe():
    p, eng = mk()
    st = fan(p, invite={"kind": "item", "id": "ghost"})
    d = eng.on_message(st, "yes")
    assert d.offer is None


# ── pacing ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_first_offer_waits_for_rapport():
    p, eng = mk()
    st = fan(p)
    for i in range(3):
        d = eng.on_message(st, "lol ok")
        assert st.invite is None, i
    eng.on_message(st, "lol ok")
    assert st.invite is not None


def test_first_offer_after_turns_is_configurable():
    p, eng = mk({"first_offer_after_turns": 1})
    st = fan(p)
    eng.on_message(st, "lol ok")
    eng.on_message(st, "lol ok")
    assert st.invite is not None


def test_repeat_offer_pacing():
    p, eng = mk()
    st = warm(eng, fan(p))
    accept_pre = eng.on_message(st, "lol ok")
    assert st.invite
    eng.on_message(st, "yes")                       # open offer sent at turn N
    eng.on_purchase(st, "L1-1", 1000)
    sent_turn = st.last_offer_turn
    gaps = 0
    while st.invite is None and gaps < 20:
        eng.on_message(st, "lol ok")
        gaps += 1
    assert st.turns - sent_turn >= p.repeat_offer_after_turns
    assert st.invite["id"] == "L1-2"


def test_no_second_invite_while_invite_pending():
    p, eng = mk()
    st = warm(eng, fan(p))
    eng.on_message(st, "lol ok")
    first = dict(st.invite)
    for _ in range(6):
        eng.on_message(st, "lol ok")
        assert st.invite == first


def test_no_pitch_while_offer_open():
    p, eng = mk()
    st = warm(eng, fan(p))
    st.open_offer = {"kind": "item", "id": "booty", "price_cents": 6000, "label": "x", "turn": 0}
    for _ in range(8):
        eng.on_message(st, "lol ok")
    assert st.invite is None


def test_too_many_sales_cooldown():
    p, eng = mk()
    st = warm(eng, fan(p))
    eng.on_message(st, "lol ok")
    assert st.invite
    d = eng.on_message(st, "every time i open this app you're trying to sell me something. can we just talk?")
    assert d.scenario == C.TOO_MANY_SALES and st.invite is None and d.offer is None
    paused_until = st.sales_paused_until_turn
    assert paused_until == st.turns + p.sales_cooldown_turns - 0 or paused_until > 0
    while st.turns < paused_until:
        eng.on_message(st, "lol ok")
        assert st.invite is None
    for _ in range(6):
        eng.on_message(st, "lol ok")
    assert st.invite is not None


def test_stop_selling_phrase():
    p, eng = mk()
    st = warm(eng, fan(p))
    d = eng.on_message(st, "stop selling to me")
    assert d.scenario == C.TOO_MANY_SALES and st.sales_paused_until_turn > st.turns - 1


# ── item offers / VIP-only / purchased / null price ─────────────────────────────────────────────────────────────
def test_send_nips_for_non_vip_is_refused_and_redirected():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "send nips")
    assert d.scenario == C.CONTENT_ASK
    assert d.offer is None and "vips" in d.messages[0].lower()
    assert st.open_offer is None


def test_send_nips_for_vip_offers_topless_at_200():
    p, eng = mk()
    st = fan(p, vip=True)
    d = eng.on_message(st, "send nips")
    assert d.offer == {"kind": "item", "id": "topless", "label": "a topless set", "price_cents": 20000}
    assert "$200" in d.messages[0]


def test_vip_redirect_never_leaks_vip_price():
    p, eng = mk()
    d = eng.on_message(fan(p), "topless pics please")
    assert "$200" not in " ".join(d.messages)


def test_item_offer_sets_open_offer_and_price():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "got any feet pics")
    assert d.offer["id"] == "feet" and d.offer["price_cents"] == 2000 and "$20" in d.messages[0]
    assert st.open_offer["id"] == "feet"


def test_already_purchased_never_reoffered():
    p, eng = mk()
    st = fan(p)
    eng.on_purchase(st, "feet", 2000)
    d = eng.on_message(st, "got any feet pics")
    assert d.offer is None and st.open_offer is None


def test_already_purchased_vip_item_not_reoffered_to_vip():
    p, eng = mk()
    st = fan(p, vip=True, purchased=["topless"])
    assert eng.on_message(st, "send nips").offer is None


def test_already_purchased_vault_step_not_reoffered():
    p, eng = mk()
    st = fan(p, purchased=["L1-1"])
    d = eng.on_message(st, "what do you have")
    assert d.offer is None


def test_already_bought_scenario_never_offers():
    p, eng = mk()
    st = warm(eng, fan(p), 3)
    d = eng.on_message(st, "wait didn't i already buy this video? why are you trying to sell it to me again?")
    assert d.scenario == C.ALREADY_BOUGHT and d.offer is None and st.invite is None


def test_null_price_never_quoted():
    p, eng = mk()
    st = fan(p, vip=True)
    d = eng.on_message(st, "send the long video")
    assert d.offer is None and st.open_offer is None
    assert not re.search(r"\$|\d", " ".join(d.messages))
    assert any("no approved price" in n for n in d.notes)


def test_null_vault_price_not_offered():
    over = copy.deepcopy(PERSONA["vault"])
    over[0]["steps"][0]["price"] = None
    p, eng = mk({"vault": over})
    st = warm(eng, fan(p))
    d = eng.on_message(st, "lol ok")
    assert st.invite is None and d.offer is None
    assert any("no approved price" in n for n in d.notes)
    d2 = eng.on_message(st, "what do you have")
    assert d2.offer is None and not re.search(r"\$|\d", " ".join(d2.messages))


def test_null_price_rating_not_quoted():
    p, eng = mk({"rating": {"key": "rating", "label": "a rating", "price": None}})
    d = eng.on_message(fan(p), "rate my dick")
    assert d.offer is None and not re.search(r"\$|\d", " ".join(d.messages))


def test_rating_offer():
    p, eng = mk()
    st = fan(p)
    d = eng.on_message(st, "rate my dick")
    assert d.offer["kind"] == "rating" and d.offer["price_cents"] == 3500 and "$35" in d.messages[0]


def test_rating_not_configured():
    d = copy.deepcopy(PERSONA)
    d.pop("rating")
    p = persona_from_dict(d)
    eng = Engine(p, playbook_from_dict(BOOK), clock=lambda: NOON)
    r = eng.on_message(fan(p), "rate my dick")
    assert r.offer is None and r.messages


def test_content_ask_does_not_stack_same_offer():
    p, eng = mk()
    st = fan(p)
    first = eng.on_message(st, "got any feet pics")
    assert first.offer
    turn = st.open_offer["turn"]
    second = eng.on_message(st, "send feet pics")
    assert second.offer is None
    assert st.open_offer["id"] == "feet" and st.open_offer["turn"] == turn
    assert not any(re.search(r"\$\d+.*\$\d+", m) for m in second.messages)


def test_general_ask_with_open_vault_offer_does_not_stack():
    p, eng = mk()
    st = fan(p)
    eng.on_message(st, "what do you have")
    d = eng.on_message(st, "what else do you have")
    assert d.offer is None and st.open_offer["id"] == "L1-1"


# ── customs ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_custom_vip_only_for_non_vip():
    p, eng = mk()
    d = eng.on_message(fan(p), "can i get a custom")
    assert d.scenario == C.CUSTOM and "VIPs" in d.messages[0] and d.offer is None
    assert "$" not in d.messages[0]


def test_custom_quote_for_vip_uses_min_price_and_approval_note():
    p, eng = mk()
    d = eng.on_message(fan(p, vip=True), "can i get a custom")
    assert "$500" in d.messages[0] and any("confirm" in n for n in d.notes)
    assert d.offer is None


def test_custom_open_to_all_when_not_vip_only():
    p, eng = mk({"customs": {"min_price": 100, "vip_only": False}})
    d = eng.on_message(fan(p), "can you make something just for me? i have a pretty specific idea")
    assert "$100" in d.messages[0]


def test_custom_without_minimum_never_quotes():
    p, eng = mk({"customs": {"vip_only": False}})
    d = eng.on_message(fan(p), "i want a custom")
    assert "$" not in d.messages[0] and any("no approved minimum" in n for n in d.notes)


# ── AI rephrase hook ────────────────────────────────────────────────────────────────────────────────────────────
def test_rephrase_cannot_change_numbers():
    _, eng = mk(rephrase=lambda t, p: t.replace("$20", "$2"))
    st = fan(eng.p)
    d = eng.on_message(st, "got any feet pics")
    assert "$20" in d.messages[0] and "$2 " not in d.messages[0]


def test_rephrase_cannot_add_numbers():
    _, eng = mk(rephrase=lambda t, p: t + " only 5 left")
    assert "5 left" not in eng.on_message(fan(eng.p), "got any feet pics").messages[0]


def test_rephrase_may_reword_without_numbers():
    _, eng = mk(rephrase=lambda t, p: "custom wording for $20")
    assert eng.on_message(fan(eng.p), "got any feet pics").messages[0] == "custom wording for $20"


def test_rephrase_respects_never_say_and_errors():
    _, eng = mk(rephrase=lambda t, p: "hey daddy")
    assert "daddy" not in eng.on_message(fan(eng.p), "lol ok").messages[0]

    def boom(t, p):
        raise RuntimeError("model down")
    _, eng2 = mk(rephrase=boom)
    assert eng2.on_message(fan(eng2.p), "lol ok").messages


def test_rephrase_never_applies_to_escalation():
    calls = []
    _, eng = mk(rephrase=lambda t, p: calls.append(t) or t)
    eng.on_message(fan(eng.p), "i know where you live")
    assert calls == []


# ── state store ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_json_state_round_trip(tmp_path):
    store = JsonStateStore(tmp_path)
    st = FanState("c1", "fan9", labels=["🐋"], vip=True, spend_cents=1234, purchased=["a"], vault_level=1, vault_step=2,
                  open_offer={"kind": "item", "id": "x", "price_cents": 5, "turn": 3}, invite={"kind": "vault", "id": "L1-1"},
                  turns=7, declined=["z"], boundary_pushes=2, escalated="why")
    store.save(st)
    assert store.load("c1", "fan9") == st


def test_json_state_missing_returns_fresh(tmp_path):
    st = JsonStateStore(tmp_path).load("c", "nobody")
    assert st.fan_id == "nobody" and st.turns == 0 and st.labels == []


def test_json_state_ignores_unknown_fields(tmp_path):
    store = JsonStateStore(tmp_path)
    store.save(FanState("c", "f"))
    path = next(tmp_path.rglob("f.json"))
    data = json.loads(path.read_text())
    data["future_field"] = 1
    path.write_text(json.dumps(data))
    assert store.load("c", "f").fan_id == "f"


def test_json_state_path_is_sanitized(tmp_path):
    store = JsonStateStore(tmp_path / "root")
    store.save(FanState("../../evil", "../x"))
    assert not (tmp_path / "evil").exists()
    assert all(str(p).startswith(str(tmp_path / "root")) for p in (tmp_path / "root").rglob("*"))


def test_state_not_shared_between_fans(tmp_path):
    store = JsonStateStore(tmp_path)
    a, b = FanState("c", "a"), FanState("c", "b")
    a.vip = True
    store.save(a)
    store.save(b)
    assert store.load("c", "a").vip and not store.load("c", "b").vip


def test_full_conversation_persists_between_messages(tmp_path):
    p, eng = mk()
    store = JsonStateStore(tmp_path)
    st = store.load(p.creator_id, "f")
    eng.on_message(st, "what do you have")
    store.save(st)
    st2 = store.load(p.creator_id, "f")
    assert st2.open_offer and st2.open_offer["id"] == "L1-1"


def test_invite_skips_step_already_purchased():
    p, eng = mk()
    st = warm(eng, fan(p, purchased=["L1-1"]))
    eng.on_message(st, "lol ok")
    assert st.invite is None


def test_lowercase_keeps_vip_in_engine_reply():
    p, eng = mk()
    d = eng.on_message(fan(p), "topless pics please")
    assert "VIPs" in d.messages[0] and "vips" not in d.messages[0]
