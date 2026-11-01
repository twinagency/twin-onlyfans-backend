"""R6: one canonical quote, payment obligation, delivery lifecycle, wording gate, customs case requirements."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from automations import upsell
from db.models import Base, Account, ContentOffer, Fan
from db.onlyfans_case_models import OfOperatorCase
from onlyfans_runtime import quote as q

ACC, FAN = "of-test", 5
ASSETS = ((11, "h1"), (12, "h2"))
CTX = upsell.DiscountCtx(last_inbound_text="can you do it cheaper?")
ON = {"fastt_discount_resend": True}


def P(**kw):
    base = dict(route="locked_card", account_id=ACC, fan_id=FAN, item_id=1, assets=ASSETS, list_cents=1000)
    base.update(kw)
    return q.price(**base)


@pytest.fixture
def s(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "q.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Account(id=ACC, nickname="Synthetic"))
        session.flush()
        session.add(Fan(account_id=ACC, fan_id=FAN))
        session.commit()
        yield session
    engine.dispose()


# ── one price, rendered one way ─────────────────────────────────────────────────────────────────────────────────
def test_price_is_integer_cents_and_rendered_once():
    assert q.format_price(7999) == "$79.99" and q.format_price(8000) == "$80.00" and q.format_price(305) == "$3.05"
    assert q.amounts_in("unlock for $79.99 or usd 80") == [7999, 8000]


def test_chat_text_must_name_the_quoted_amount_not_a_near_miss():          # p236-237: $79.99 vs $80
    quote = P(list_cents=7999).quote
    assert q.text_consistent("it's $79.99 to unlock", quote)
    assert not q.text_consistent("it's $80 to unlock", quote)
    assert q.text_consistent("it's a special one for you", quote)


def test_price_without_discount_policy_is_the_list_price():
    out = P(cfg={}, discount_ctx=CTX)
    assert out.kind == "ok" and out.quote.price_cents == 1000 and out.quote.discount_pct == 0


def test_frozen_manual_vault_quote_preserves_the_operator_price_and_obligation():
    # Existing manual snapshots deliberately store no catalogue list price.
    terms = {"manual_vault": True, "item": {"id": 17, "account_id": ACC},
             "media_ids": [11, 12], "preview_ids": [12], "price_cents": 1234,
             "files": [{"media_id": 11, "content_hash": "h1"},
                       {"media_id": 12, "content_hash": "h2"}]}
    quote = q.from_terms(terms, account_id=ACC, fan_id=FAN)
    assert quote.list_cents == quote.price_cents == 1234
    assert quote.assets == ASSETS
    assert quote.obligation_id == q.obligation_id("locked_card", ACC, FAN, "item:17:11")
    assert q.format_price(quote.price_cents) == "$12.34"


def test_frozen_catalogue_quote_keeps_list_and_charged_prices_distinct():
    terms = {"item": {"id": 17, "price_cents": 1400}, "media_ids": [11],
             "preview_ids": [], "price_cents": 1234, "native_quote": {"reason": "approved"}}
    quote = q.from_terms(terms, account_id=ACC, fan_id=FAN)
    assert (quote.list_cents, quote.price_cents, quote.discount_reason) == (1400, 1234, "native_quote")


def test_discount_applies_only_where_this_creator_enabled_it_and_policy_qualifies():
    cut = P(cfg=ON, discount_ctx=CTX)
    assert cut.kind == "ok" and cut.quote.price_cents == 850 and cut.quote.discount_pct == 15 and cut.quote.discount_reason == "first_cut"
    assert P(cfg=ON, discount_ctx=upsell.DiscountCtx(last_inbound_text="hello")).quote.price_cents == 1000    # unprompted
    assert P(cfg=ON, discount_ctx=upsell.DiscountCtx(last_inbound_text="cheaper?", spend_regret=True)).quote.price_cents == 1000
    assert P(cfg={"fastt_discount_resend": False}, discount_ctx=CTX).quote.price_cents == 1000


def test_third_cut_is_refused_and_second_never_exceeds_twenty_percent():
    second = P(cfg=ON, discount_ctx=upsell.DiscountCtx(last_inbound_text="cheaper?", discount_count_this_item=1,
                                                       discount_original_cents=1000), discount_count=1, original_cents=1000)
    assert second.quote.price_cents == 800 and second.quote.discount_pct == 20
    third = P(cfg=ON, discount_ctx=upsell.DiscountCtx(last_inbound_text="cheaper?", discount_count_this_item=2,
                                                      discount_original_cents=1000), original_cents=1000)
    assert third.quote.price_cents == 1000 and third.quote.discount_reason == "refused:already_cut"


@pytest.mark.parametrize("listed,label", [(514, "p285-286 $4.11"), (525, "p307-308 $4.20")])
def test_stated_three_dollar_budget_is_honoured_not_exceeded(listed, label):
    out = P(list_cents=listed, cfg=ON, discount_ctx=CTX, budget_cents=300, budget_scope="per_item", cheaper=(7, 8))
    assert out.kind == "over_budget" and out.quote is None, label
    assert "$3.00" in out.explanation and out.smaller_items == (7, 8)


def test_budget_met_inside_policy_and_scope_is_required():
    ok = P(list_cents=350, budget_cents=400, budget_scope="lifetime")
    assert ok.kind == "ok" and ok.quote.budget_scope == "lifetime"
    with pytest.raises(q.QuoteError):
        P(budget_cents=400)


def test_fan_agreement_is_the_only_way_past_a_stated_budget():
    out = P(list_cents=514, budget_cents=300, budget_scope="per_item", agreed_over_budget=True)
    assert out.kind == "ok" and out.quote.price_cents == 514


def test_below_provider_minimum_or_floor_is_explained_with_alternatives():
    assert P(list_cents=200).kind == "invalid"
    out = P(list_cents=1000, floor_cents=1200, cheaper=(3,))
    assert out.kind == "below_floor" and out.smaller_items == (3,)


def test_accepted_quote_is_immutable_and_never_raised_silently():
    quote = q.accept(P().quote)
    with pytest.raises(q.QuoteError):
        q.reprice(quote, 900)
    with pytest.raises(q.QuoteError):
        q.reprice(P().quote, 1200)
    assert q.reprice(quote, 900, agreement=True).version == 2
    assert q.reprice(quote, 1000) is quote


# ── obligations: one request per obligation, whichever path asks ────────────────────────────────────────────────
def test_two_send_paths_for_one_obligation_share_one_request(s):
    a = P(subject="set-A").quote
    b = P(subject="set-A").quote
    assert a.obligation_id == b.obligation_id
    first, created1 = q.open_request(s, account_id=ACC, fan_id=FAN, quote=a)
    second, created2 = q.open_request(s, account_id=ACC, fan_id=FAN, quote=b)
    assert created1 and not created2 and first.id == second.id
    assert s.scalar(select(ContentOffer.id).where(ContentOffer.obligation_id == a.obligation_id).limit(1)) == first.id


def test_routes_are_separate_obligations():
    ids = {q.obligation_id(r, ACC, FAN, "x") for r in q.ROUTES}
    assert len(ids) == 3
    with pytest.raises(q.QuoteError):
        q.obligation_id("wire", ACC, FAN, "x")


def test_changed_price_on_an_accepted_open_quote_is_refused(s):
    accepted = q.accept(P().quote)
    q.open_request(s, account_id=ACC, fan_id=FAN, quote=accepted)
    changed = P(list_cents=1200).quote
    with pytest.raises(q.QuoteError):
        q.open_request(s, account_id=ACC, fan_id=FAN, quote=changed)


def test_offer_row_carries_the_whole_quote(s):
    quote = P().quote
    row, _ = q.open_request(s, account_id=ACC, fan_id=FAN, quote=quote)
    assert (row.price_cents, row.currency, row.lifecycle, row.quote_version) == (1000, "USD", "quoted", 1)
    assert q.Quote.from_json(row.quote_json) == quote
    assert q.dispute_evidence(row)["assets"] == [[11, "h1"], [12, "h2"]]


# ── lifecycle and dispatch ──────────────────────────────────────────────────────────────────────────────────────
def walk(s, row, *states):
    for st in states:
        q.advance(s, row, st)


def test_lifecycle_runs_forward_only(s):
    row, _ = q.open_request(s, account_id=ACC, fan_id=FAN, quote=P().quote)
    walk(s, row, "awaiting_payment", "dispatch_intent", "provider_accepted", "reconciled", "purchased", "fulfilled")
    assert row.lifecycle == "fulfilled"
    with pytest.raises(q.QuoteError):
        q.advance(s, row, "awaiting_payment")
    with pytest.raises(q.QuoteError):
        q.advance(s, row, "escalated")


def test_cannot_skip_to_purchased_or_accepted_without_intent(s):
    row, _ = q.open_request(s, account_id=ACC, fan_id=FAN, quote=P().quote)
    for bad in ("purchased", "provider_accepted", "reconciled", "fulfilled"):
        with pytest.raises(q.QuoteError):
            q.advance(s, row, bad)
    assert q.advance(s, row, "escalated") == "escalated"       # a human can always take over a live order


def test_timeout_after_provider_acceptance_never_creates_a_second_charge(s):
    row, _ = q.open_request(s, account_id=ACC, fan_id=FAN, quote=P().quote)
    assert q.may_dispatch(row) == (True, "ok")
    walk(s, row, "awaiting_payment", "dispatch_intent")
    key = q.idempotency_key(row)
    assert q.may_dispatch(row) == (False, "outcome_unreconciled")          # timeout: unknown outcome, no retry yet
    q.reconcile_unknown(s, row, provider_has_it=True)
    assert row.lifecycle == "provider_accepted" and q.may_dispatch(row) == (False, "already_provider_accepted")


def test_unknown_outcome_with_nothing_at_provider_retries_with_the_same_key(s):
    row, _ = q.open_request(s, account_id=ACC, fan_id=FAN, quote=P().quote)
    walk(s, row, "awaiting_payment", "dispatch_intent")
    key = q.idempotency_key(row)
    q.reconcile_unknown(s, row, provider_has_it=False)
    assert q.may_dispatch(row)[0] and q.idempotency_key(row) == key


def test_duplicate_receipt_or_webhook_is_idempotent_not_an_error_chain(s):
    row, _ = q.open_request(s, account_id=ACC, fan_id=FAN, quote=P().quote)
    walk(s, row, "awaiting_payment", "dispatch_intent", "provider_accepted", "reconciled")
    with pytest.raises(q.QuoteError):                                       # second receipt cannot re-run the hop
        q.advance(s, row, "provider_accepted")
    assert row.lifecycle == "reconciled"


# ── wording follows the verified state ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,state,ok", [
    ("you'll get it tonight", "dispatch_intent", False),
    ("i'll send you the video", "provider_accepted", False),
    ("i'll send you the video", "purchased", True),
    ("just sent it, check your dms", "provider_accepted", False),
    ("just sent it, check your dms", "reconciled", True),
    ("check your inbox", None, False),
    ("want me to send u one?", None, True),
    ("give me a sec to think", None, True),
    ("it's $5 to unlock babe", "quoted", True),
])
def test_promise_language_matches_order_state(text, state, ok):
    assert q.wording_gate(text, state, fulfilment_basis=True)[0] is ok


def test_deadline_needs_a_fulfilment_basis():
    assert q.wording_gate("i'll have it ready by friday", "purchased") == (False, "deadline_without_fulfilment_basis")
    assert q.wording_gate("i'll have it ready by friday", "purchased", fulfilment_basis=True)[0]


def test_refund_never_stated_complete_without_evidence():                   # p236-237
    assert q.wording_gate("i've refunded you", "purchased") == (False, "refund_without_evidence")
    assert q.wording_gate("i've refunded you", "purchased", refund_evidence=True)[0]
    assert q.wording_gate("a teammate will look at the refund", "purchased")[0]


# ── customs ─────────────────────────────────────────────────────────────────────────────────────────────────────
def custom(s, **kw):
    base = dict(account_id=ACC, fan_id=FAN, available_verified=True, scope="30s feet video", price_cents=5000,
                route="custom_service", owner="alex", deadline=datetime.utcnow() + timedelta(days=2), source_message_id=9)
    base.update(kw)
    return q.open_custom_case(s, **base)


def test_mentioning_a_team_is_not_a_custom_handover(s):
    case, missing = custom(s, owner=None, deadline=None, available_verified=False, scope="", price_cents=None, route="tip")
    assert case is None and set(missing) == {"availability_not_verified", "scope_not_agreed", "price_not_agreed",
                                             "route_not_permitted", "no_fulfilment_owner", "no_deadline"}
    assert s.scalar(select(OfOperatorCase.case_id).limit(1)) is None


def test_agreed_custom_opens_one_case_with_owner_and_deadline(s):
    case, missing = custom(s)
    assert not missing and case.owner == "alex" and case.due_at is not None and case.status == "open"
    again, _ = custom(s)
    assert again.case_id == case.case_id


@pytest.mark.parametrize("text,held", [
    ("i'll have your custom video ready by friday", "deadline-unbacked"),
    ("sending you the video tomorrow", "deadline-unbacked"),
    ("i'll film it tonight for you", "deadline-unbacked"),
    ("i've refunded you babe", "refund-claim-unbacked"),
    ("see you tomorrow babe", None),
    ("making dinner tonight, you hungry?", None),
    ("a teammate will check the refund", None),
])
def test_text_boundary_hold(text, held):
    assert q.boundary_hold(text) == held
