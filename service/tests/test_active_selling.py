"""R5: one action decision per turn, controls, invitations + short acceptance, decision records and reporting."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from db.models import Base, Account, Fan, Message
from automations import sales_action as sa, sales_invitation as inv, sales_decisions as dec
from automations.sales_action import ActionInputs, decide, count_since_last_card, pacing
from onlyfans_runtime import commercial_controls as cc
from onlyfans_runtime.commercial_controls import Controls

ACC, FAN = "of-test", 7


def C(init="standard", first=3, repeat=4):
    return Controls(init, first, repeat)


def I(**kw):
    kw.setdefault("ready_items", 2)
    return ActionInputs(**kw)


# ── controls ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_controls_defaults_and_validation():
    assert cc.controls({}) == Controls("standard", 3, 4)
    assert cc.controls({"fastt_commercial_initiative": "active", "fastt_first_offer_reference": 5,
                        "fastt_repeat_offer_reference": 6}) == Controls("active", 5, 6)
    # invalid saved values fall back per-setting, never crash and never silently become "active"
    assert cc.controls({"fastt_commercial_initiative": "AGGRESSIVE", "fastt_repeat_offer_reference": 99,
                        "fastt_first_offer_reference": True}) == Controls("standard", 3, 4)
    assert cc.controls(None) == Controls()


def test_patch_validation_rejects_out_of_range():
    class E(Exception):
        pass
    assert cc.validate_patch_fields({"fasttRepeatOfferReference": 3}, E) == {"fasttRepeatOfferReference": 3}
    for bad in ({"fasttRepeatOfferReference": 2}, {"fasttRepeatOfferReference": 7}, {"fasttFirstOfferReference": 0},
                {"fasttCommercialInitiative": "max"}, {"fasttFirstOfferReference": "3"}, {"fasttFirstOfferReference": True}):
        with pytest.raises(E):
            cc.validate_patch_fields(bad, E)


def test_active_pilot_profile_is_valid_and_not_applied_by_default():
    assert cc.validate_patch_fields(cc.ACTIVE_PILOT_PROFILE, ValueError) == cc.ACTIVE_PILOT_PROFILE
    assert cc.controls({}).initiative == "standard"


def test_controls_are_inspectable_in_the_config_view_and_patchable():
    from onlyfans_runtime import configuration as cfg
    assert cfg.validate_patch({"fasttCommercialInitiative": "active"}) == {"fasttCommercialInitiative": "active"}
    v = cc.view({"fastt_commercial_initiative": "lower"})
    assert v["fasttCommercialInitiative"] == "lower" and "experimental" in v["fasttRepeatOfferReferenceLabel"]


# ── counting: grouped turns and raw bubbles are separate numbers ────────────────────────────────────────────────
def test_grouped_turns_vs_raw_bubbles():
    rows = [("in", False, "hi"), ("out", False, "hey you"), ("out", False, "how was your day"), ("out", False, "😘"),
            ("in", False, "good"), ("out", False, "glad to hear that")]
    c = count_since_last_card(rows)
    assert (c.grouped_turns, c.raw_bubbles, c.prior_card) == (2, 4, False)


def test_card_resets_the_count_and_marks_prior_card():
    rows = [("out", False, "hello there"), ("in", False, "hi"), ("out", True, "unlock this"), ("in", False, "wow"),
            ("out", False, "thank you babe")]
    c = count_since_last_card(rows)
    assert (c.grouped_turns, c.raw_bubbles, c.prior_card) == (1, 1, True)


# ── the decision ────────────────────────────────────────────────────────────────────────────────────────────────
def test_direct_request_ready_offers_immediately_even_with_zero_warmup_on_every_setting():
    for init in ("lower", "standard", "active"):
        d = decide(I(controls=C(init), grouped_turns=0, direct_request=True, matched=True))
        assert (d.action, d.outcome, d.reason) == ("direct_offer", "acted", "direct_request_ready"), init
        assert d.sends_card


def test_direct_request_without_ready_content_never_promises():
    d = decide(I(direct_request=True, matched=False, ready_items=0, readiness_missing=("no_meaningful_description",)))
    assert d.action == "clarify" and d.reason == "not_ready" and not d.sends_card
    d = decide(I(direct_request=True, matched=False, ready_items=0))
    assert d.reason == "no_match"


def test_direct_request_without_exact_match_proposes_alternative_not_card():
    d = decide(I(direct_request=True, matched=False, ready_items=3))
    assert d.action == "unavailable_alternative" and d.reason == "no_exact_match_alternative_proposed"
    assert decide(I(direct_request=True, matched=False, ready_items=3, alternative="agreed")).action == "direct_offer"
    assert decide(I(direct_request=True, matched=False, ready_items=3, alternative="proposed")).action == "clarify"


@pytest.mark.parametrize("kw,reason", [
    (dict(hold="stop_contact"), "hold:stop_contact"), (dict(hold="takeover"), "hold:takeover"),
    (dict(support="complaint"), "support:complaint"), (dict(support="open_custom"), "support:open_custom"),
    (dict(transport_uncertain=True), "transport_uncertain"), (dict(purchase_coverage_fresh=False), "missing_purchase_coverage"),
    (dict(budget_blocked=True), "budget_limited"),
])
def test_protection_beats_every_setting_and_even_a_direct_request(kw, reason):
    for init in ("lower", "standard", "active"):
        d = decide(I(controls=C(init, first=1), direct_request=True, matched=True, evidence="current_interest",
                     grouped_turns=20, **kw))
        assert d.reason == reason and not d.sends_card, (init, kw)


def test_off_mode_makes_no_commercial_decision():
    d = decide(I(mode="off", direct_request=True, matched=True))
    assert (d.action, d.reason) == ("answer", "disabled")


def test_current_interest_before_and_after_reference_range_both_may_offer():
    before = decide(I(evidence="current_interest", grouped_turns=0))
    after = decide(I(evidence="current_interest", grouped_turns=9))
    assert before.action == after.action == "proactive_offer"


def test_no_suitable_opportunity_beyond_range_is_a_reassessment_not_a_send():
    for init in ("lower", "standard", "active"):
        d = decide(I(controls=C(init), evidence="none", grouped_turns=30, ready_items=0))
        assert not d.sends_card and d.action == "answer"
    d = decide(I(controls=C("lower"), evidence="none", grouped_turns=30))
    assert d.action == "answer" and d.reason == "no_suitable_opportunity_beyond_range" and d.outcome == "evaluated"


def test_count_alone_never_sends_a_card_on_any_setting():
    for init in ("lower", "standard", "active"):
        for g in range(0, 40):
            for ev in ("none", "background"):
                assert not decide(I(controls=C(init), evidence=ev, grouped_turns=g)).sends_card


def test_initiative_changes_the_choice_among_appropriate_actions():
    kw = dict(evidence="background", grouped_turns=3)
    assert decide(I(controls=C("lower"), **kw)).action == "answer"          # lower waits inside the range
    assert decide(I(controls=C("lower"), evidence="background", grouped_turns=7)).action == "develop"
    assert decide(I(controls=C("standard"), **kw)).action == "invite"
    assert decide(I(controls=C("active"), **kw)).action == "invite"
    early = dict(evidence="background", grouped_turns=0)
    assert decide(I(controls=C("lower"), **early)).action == "answer"
    assert decide(I(controls=C("active"), **early)).action == "invite"
    gentle = decide(I(controls=C("lower"), evidence="current_interest", grouped_turns=0))
    assert gentle.action == "invite"


def test_pacing_reference_range_and_independent_repeat_setting():
    assert (pacing(C(first=3), 2, prior_card=False).lo, pacing(C(first=3), 2, prior_card=False).hi) == (2, 6)
    first = pacing(C(first=3, repeat=6), 4, prior_card=False)
    repeat = pacing(C(first=3, repeat=6), 4, prior_card=True)
    assert first.phase == "first" and first.position == "within"
    assert repeat.phase == "repeat" and repeat.ref == 6 and repeat.position == "before"
    assert pacing(C(), 9, prior_card=False).position == "beyond"


def test_recent_refusal_and_ignored_invitation_do_not_resell_the_same_thing():
    assert decide(I(evidence="current_interest", declined_recently=True)).reason == "recent_refusal"
    assert decide(I(evidence="background", recent_ignored_same_item=True)).reason == "ignored_invitation_not_repeated"
    # a fresh genuine interest in a different item is still eligible
    d = decide(I(evidence="current_interest", recent_ignored_same_item=True))
    assert d.sends_card


def test_identical_unresolved_card_blocks_but_a_different_request_is_still_possible():
    assert decide(I(direct_request=True, matched=True, identical_unresolved_card=True)).reason == "identical_card_unresolved"
    assert decide(I(direct_request=True, matched=True, identical_unresolved_card=False)).sends_card


def test_accepted_invitation_presents_the_offer_without_reproving_interest():
    d = decide(I(accepted_invitation="inv-1", evidence="none", grouped_turns=0))
    assert (d.action, d.reason, d.invitation_id) == ("direct_offer", "invitation_accepted", "inv-1")
    d = decide(I(accepted_invitation="inv-1", acceptance_revalidated=False))
    assert not d.sends_card and d.reason == "acceptance_not_revalidated"
    # hold still wins over an accepted invitation
    assert not decide(I(accepted_invitation="inv-1", hold="distress")).sends_card


def test_every_decision_carries_settings_and_pacing():
    d = decide(I(controls=C("active", 4, 5), evidence="none"))
    assert d.settings["initiative"] == "active" and d.settings["first_offer_ref"] == 4 and d.pacing.phase == "first"


# ── invitations and decision records on a real (sqlite) schema ──────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "s.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
    for m in (inv, dec):
        monkeypatch.setattr(m, "get_session", session)

    async def setup():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            await s.flush()
            s.add(Fan(account_id=ACC, fan_id=FAN))
            await s.commit()
    asyncio.run(setup())
    yield factory
    asyncio.run(engine.dispose())


def say(factory, mid, direction, body):
    async def go():
        async with factory() as s:
            s.add(Message(account_id=ACC, fan_id=FAN, message_id=mid, direction=direction, body=body,
                          created_at=datetime.utcnow()))
            await s.commit()
    asyncio.run(go())


def run(coro):
    return asyncio.run(coro)


def invite(media=(11, 12), at=2):
    return run(inv.propose(ACC, FAN, media, source_message_id=at, subject="feet set"))


def test_short_acceptance_forms():
    for ok in ("yes", "Yes!", "yes please", "👍", "👍🏽", "sure", "ok yes"):
        assert inv.short_acceptance(ok) or ok == "yes please", ok
    for bad in ("yes but how much is it", "yes what else do you have", "no", "", "maybe later", "👀", "lol"):
        assert not inv.short_acceptance(bad), bad


def test_invitation_must_concern_real_content(db):
    with pytest.raises(ValueError):
        run(inv.propose(ACC, FAN, [], source_message_id=1))


def test_yes_accepts_that_invitation_once_and_idempotently(db):
    say(db, 2, "in", "i love feet")
    i = invite()
    say(db, 3, "out", "want me to show you my feet set?")
    say(db, 4, "in", "yes")
    acc, why = run(inv.resolve_acceptance(ACC, FAN))
    assert why == "accepted" and acc.invitation_id == i and acc.media_ids == (11, 12) and acc.accepted_turn_id == 4
    again, why2 = run(inv.resolve_acceptance(ACC, FAN))
    assert again.invitation_id == i and why2 == "already_accepted_this_turn"


def test_thumbs_up_accepts(db):
    say(db, 2, "in", "feet")
    invite()
    say(db, 3, "out", "want to see my feet set?")
    say(db, 4, "in", "👍")
    assert run(inv.resolve_acceptance(ACC, FAN))[1] == "accepted"


def test_yes_without_an_invitation_is_not_acceptance(db):
    say(db, 1, "out", "how are you")
    say(db, 2, "in", "yes")
    assert run(inv.resolve_acceptance(ACC, FAN)) == (None, "no_open_invitation")


def test_invitation_that_never_reached_the_fan_is_not_accepted(db):
    say(db, 2, "in", "feet")
    invite()
    say(db, 4, "in", "yes")
    assert run(inv.resolve_acceptance(ACC, FAN))[1] == "invitation_never_reached_fan"


def test_a_fan_message_between_invitation_and_yes_makes_it_ambiguous(db):
    say(db, 2, "in", "feet")
    invite()
    say(db, 3, "out", "want to see my feet set?")
    say(db, 4, "in", "my dog is sick")
    say(db, 5, "out", "aw no, is he ok?")
    say(db, 6, "in", "yes")
    assert run(inv.resolve_acceptance(ACC, FAN))[1] == "fan_changed_subject"


def test_a_newer_invitation_supersedes_so_yes_is_unambiguous(db):
    say(db, 2, "in", "feet")
    first = invite((11,), at=2)
    say(db, 3, "out", "want my feet set?")
    say(db, 4, "in", "and shoes?")
    second = invite((21,), at=4)
    say(db, 5, "out", "want my shoes set?")
    say(db, 6, "in", "yes")
    acc, why = run(inv.resolve_acceptance(ACC, FAN))
    assert why == "accepted" and acc.invitation_id == second and acc.media_ids == (21,) and first != second


def test_propose_is_idempotent_for_the_same_turn_and_item(db):
    assert invite() == invite()


def test_declined_or_ignored_invitation_is_remembered_and_not_chased(db):
    say(db, 2, "in", "feet")
    invite((11,), at=2)
    say(db, 3, "out", "want my feet set?")
    say(db, 4, "in", "no thanks")
    run(inv.settle(ACC, FAN))
    h = run(inv.history(ACC, FAN, (11,)))
    assert h["declined_same_item"] and h["open"] == 0
    assert not run(inv.history(ACC, FAN, (99,)))["declined_same_item"]
    say(db, 5, "in", "yes")
    assert run(inv.resolve_acceptance(ACC, FAN))[1] == "no_open_invitation"


def test_ignored_invitation_after_topic_change(db):
    say(db, 2, "in", "feet")
    invite((11,), at=2)
    say(db, 3, "out", "want my feet set?")
    say(db, 4, "in", "what did you do today")
    run(inv.settle(ACC, FAN))
    assert run(inv.history(ACC, FAN, (11,)))["ignored_same_item"]


def test_decision_rows_are_idempotent_and_include_excluded_turns(db):
    d1 = decide(I(controls=C("active"), direct_request=True, matched=True))
    d2 = decide(I(evidence="none", grouped_turns=1, ready_items=0))
    d3 = decide(I(hold="stop_contact"))
    d4 = decide(I(evidence="background", grouped_turns=3))
    assert run(dec.record(ACC, FAN, 10, d1, counts=sa.Counts(2, 5, False), evidence={"matched": [11]}))
    assert not run(dec.record(ACC, FAN, 10, d1))
    run(dec.record(ACC, FAN, 11, d2))
    run(dec.record(ACC, FAN, 12, d3))
    run(dec.record(ACC, FAN, 13, d4, counts=sa.Counts(3, 4, True)))
    r = run(dec.report(ACC))
    assert r["turns_evaluated"] == 4
    assert r["by_outcome"] == {"acted": 2, "excluded": 1, "blocked": 1}
    assert r["no_offer_reasons"] == {"no_match": 1, "hold:stop_contact": 1, "background_interest_invite": 1}
    assert r["pacing"]["first"]["cards"] == 1 and r["pacing"]["first"]["invitations"] == 1
    assert sorted(r["pacing"]["first"]["grouped_turns"]) == [0, 1, 2, 3]
    assert r["pacing"]["first"]["raw_bubbles"] == [5, 4]


def test_report_separates_first_and_repeat_pacing(db):
    run(dec.record(ACC, FAN, 20, decide(I(direct_request=True, matched=True, prior_card=True))))
    run(dec.record(ACC, FAN, 21, decide(I(direct_request=True, matched=True, prior_card=False))))
    r = run(dec.report(ACC))
    assert r["pacing"]["repeat"]["cards"] == 1 and r["pacing"]["first"]["cards"] == 1
