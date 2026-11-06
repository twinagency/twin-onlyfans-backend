"""Explicit monetary assertion binding; no database, model or transport."""
from copy import deepcopy

import pytest

from moxy_replacement.price_assertions import price_problem


def scene(latest="Please send the first photo."):
    message = {"id": 1, "direction": "in", "text": latest}
    return {
        "products": {"first": {"price_cents": 7500, "currency": "USD"},
                     "second": {"price_cents": 8500, "currency": "USD"}},
        "inventory": {"first": {"price_cents": 7500}, "second": {"price_cents": 8500}},
        "history": [message], "pending_fan_messages": [message], "fan_state": {}, "card_state": [],
    }


def draft(text, *, key="first", action="offer", extra=None):
    parts = [{"text": text, "media": action in {"offer", "correct", "fulfil"}}]
    if extra:
        parts.append({"text": extra, "media": False})
    return {"action": action, "text": text, "messages": parts, "product_id": key}


@pytest.mark.parametrize("text", ["This photo is $75.", "It costs USD 75.", "75 dollars for this photo.",
                                  "This photo is US$75.00.", "This has 5 photos, 2 previews and costs $75."])
def test_current_price_formats_and_unmarked_counts(text):
    assert price_problem(scene(), draft(text)) is None


@pytest.mark.parametrize("text", ["This photo is $5.", "This photo is $85.", "This photo is €75.",
                                  "This photo is $7,500.", "This photo is $75.009.",
                                  "This photo is AUD75.", "This photo is EUR $75.",
                                  "This photo is $-75."])
def test_wrong_amount_other_items_price_currency_units_and_precision(text):
    assert price_problem(scene(), draft(text)) == "price_claim_mismatch"


def test_money_in_later_plain_bubble_is_checked():
    assert price_problem(scene(), draft("Here is the photo.", extra="It costs $5.")) == "price_claim_mismatch"


def test_zero_price_authorized_remedy_uses_exact_terms_without_mutating_product():
    snap = scene()
    before = deepcopy(snap)
    assert price_problem(snap, draft("This replacement costs $0.", action="correct"),
                         {"price_cents": 0}) is None
    assert price_problem(snap, draft("This replacement costs $75.", action="correct"),
                         {"price_cents": 0}) == "price_claim_mismatch"
    assert snap == before


def test_evidenced_budget_and_current_offer_have_distinct_prices():
    snap = scene("My budget is $100.")
    snap["fan_state"]["budget_cents"] = 10000
    assert price_problem(snap, draft("Your budget is $100, and this photo costs $75.")) is None
    assert price_problem(snap, draft("Your budget is $100, and this photo costs $100.")) == "price_claim_mismatch"
    assert price_problem(snap, draft("Your budget is $5; this photo costs $75.")) == "price_claim_unbound"


def test_current_budget_quote_can_be_verified_before_persistence():
    assert price_problem(scene("My budget is $100."), draft("Your budget is $100; this is $75.")) is None
    assert price_problem(scene("My dog weighs 100 pounds."), draft("Your budget is $100; this is $75.")) == "price_claim_unbound"


@pytest.mark.parametrize("text", [
    "It is not true that my budget is $100.",
    'I never said "my budget is $100".',
    "If my budget is $100, what happens?",
    "My budget is $100, but my budget is $20.",
])
def test_nonaffirmative_or_replaced_budget_is_not_current_evidence(text):
    assert price_problem(scene(text), draft("Your budget is $100; this is $75.")) == "price_claim_unbound"


def test_stale_budget_in_history_does_not_override_current_state():
    snap = scene("My budget is $100.")
    snap["fan_state"]["budget_cents"] = 2000
    assert price_problem(snap, draft("Your budget is $100; this is $75.")) == "price_claim_unbound"


def test_exact_attributed_history_quote_reports_old_statement_not_current_terms():
    snap = scene()
    snap["history"].insert(0, {"id": 9, "direction": "out", "text": "It costs $5."})
    assert price_problem(snap, draft('I said "It costs $5."; that was wrong. This photo is $75.')) is None
    assert price_problem(snap, draft('I said "It costs $5."; this photo is $5.')) == "price_claim_mismatch"
    assert price_problem(snap, draft('You said "It costs $5."; this photo is $75.')) == "price_claim_mismatch"


def test_unattributed_quote_cannot_hide_wrong_current_price():
    snap = scene()
    snap["history"].append({"id": 9, "direction": "out", "text": "It costs $5."})
    assert price_problem(snap, draft('"It costs $5."')) == "price_claim_mismatch"


def test_exact_fan_quote_preserves_source_but_not_another_claim():
    snap = scene('Why did you say "It costs $5"?')
    assert price_problem(snap, draft('You wrote "It costs $5". The actual card is $75.')) is None
    assert price_problem(snap, draft('You wrote "It costs $6". The actual card is $75.')) == "price_claim_mismatch"


def test_existing_card_price_uses_that_cards_receipt_not_current_catalogue():
    snap = scene("What was the price of card 9?")
    snap["card_state"] = [{"id": 9, "product_id": "first", "price_cents": 2000}]
    assert price_problem(snap, draft("That card was $20.", key=None, action="reply")) is None
    assert price_problem(snap, draft("That card was $75.", key=None, action="reply")) == "price_claim_mismatch"


def test_old_card_assertion_and_new_offer_are_bound_separately():
    snap = scene()
    snap["card_state"] = [{"id": 9, "product_id": "second", "price_cents": 2000}]
    assert price_problem(snap, draft("Card 9 was $20; this photo is $75.")) is None
    assert price_problem(snap, draft("Card 9 was $75; this photo is $75.")) == "price_claim_mismatch"
    assert price_problem(snap, draft("Card 99 was $75; this photo is $75.")) == "price_claim_unbound"


def test_ambiguous_card_reference_does_not_choose_any_matching_price():
    snap = scene("How much is that card?")
    snap["card_state"] = [{"id": 8, "price_cents": 7500}, {"id": 9, "price_cents": 2000}]
    assert price_problem(snap, draft("It is $75.", key=None, action="reply")) == "price_claim_unbound"


def test_price_without_item_or_evidenced_budget_is_not_invented():
    assert price_problem(scene(), draft("It is $75.", key=None, action="reply")) == "price_claim_unbound"
    assert price_problem(scene(), draft("There are 75 stars.", key=None, action="reply")) is None
    assert price_problem(scene(), draft("My dog weighs 75 pounds.", key=None, action="reply")) is None


def test_ordinary_expense_question_does_not_require_price_of_unpaid_card():
    from moxy_replacement.price_assertions import price_question_problem
    snap = scene("How much did dinner cost? Was it nice?")
    snap["card_state"] = [{"id": 9, "product_id": "first", "price_cents": 7500}]
    reply = draft("Dinner was $20, and it was lovely.", key=None, action="reply")
    assert price_problem(snap, reply) is None
    assert price_question_problem(snap, reply) is None


def test_earlier_pending_card_question_must_be_answered():
    from moxy_replacement.price_assertions import price_question_problem
    snap = scene("How are you?")
    first = {"id": 2, "direction": "in", "text": "How much was card 9?"}
    snap["pending_fan_messages"].insert(0, first)
    snap["card_state"] = [{"id": 9, "product_id": "first", "price_cents": 7500}]
    assert price_question_problem(snap, draft("I'm good!", key=None, action="reply")) == "price_question_unanswered"
    reply = draft("Card 9 was $75. I'm good!", key=None, action="reply")
    assert price_problem(snap, reply) is None
    assert price_question_problem(snap, reply) is None


def test_dinner_answer_cannot_answer_a_card_question_with_same_amount():
    from moxy_replacement.price_assertions import price_question_problem
    snap = scene("How much is card 9?")
    snap["card_state"] = [{"id": 9, "product_id": "first", "price_cents": 7500}]
    reply = draft("Dinner was $75.", key=None, action="reply")
    assert price_question_problem(snap, reply) == "price_question_unanswered"
