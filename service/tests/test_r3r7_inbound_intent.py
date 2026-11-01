"""R7: per-message promotional-intent classification. Synthetic text only; ids are fabricated."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations._inbound_intent import (AMBIGUOUS, BUSINESS, CUSTOMER, PROMO, COHORT_AMBIGUOUS, COHORT_GENUINE,
    COHORT_KNOWN_TEST, COHORT_PROMO_BUSINESS, COHORT_SUSPECTED_TEST, IntentContext, burst_decision,
    classify_inbound_intent, counts_in_customer_metrics, known_test_fan_ids, reporting_cohort)

CREATOR = IntentContext(sender_is_creator=True)
ADS = "#ads go show her some love on her free page https://onlyfans.com/synthetic-page"


def test_confirmed_promo_needs_independent_signals():
    r = classify_inbound_intent(ADS, CREATOR)
    assert r.label == PROMO and r.suppress_reply and not r.allows_memory_write and not r.allows_rating
    assert not classify_inbound_intent(ADS, None).label == CUSTOMER


@pytest.mark.parametrize("text,ctx", [
    ("https://example.com/x", CREATOR),                 # creator + link only
    ("sfs?", IntentContext()),                         # one ad tag alone
    ("do you have a discount on the bundle?", CREATOR),  # discount question from a creator account
    ("hey baby", CREATOR),                              # creator status alone
    ("hey baby", IntentContext(same_text_other_fans=4)),  # repetition alone
    ("", IntentContext(attachment_kind="photo")),      # a screenshot alone
])
def test_no_single_signal_is_enough(text, ctx):
    r = classify_inbound_intent(text, ctx)
    assert r.label in (CUSTOMER, AMBIGUOUS) and not r.suppress_reply


def test_ambiguous_stays_eligible_and_reviewable():
    r = classify_inbound_intent("is your free page any good?", CREATOR)   # ad phrase + creator flag only
    assert r.label == AMBIGUOUS and not r.suppress_reply
    r2 = classify_inbound_intent("check my link https://example.com/a", IntentContext())
    assert r2.label == AMBIGUOUS and not r2.suppress_reply and r2.allows_memory_write


def test_repeat_across_fans_plus_ad_tag_is_promo_and_echo_needs_support():
    r = classify_inbound_intent("Hey baby, let's do SFS? I have 34.0k Fans", IntentContext(same_text_other_fans=3, same_text_self_count=5))
    assert r.label == PROMO
    assert classify_inbound_intent("so good", IntentContext(equals_our_recent_outbound=True)).label != PROMO
    assert classify_inbound_intent("so good", IntentContext(equals_our_recent_outbound=True, sender_is_creator=True)).label == PROMO


def test_spenders_and_prior_buyers_are_never_auto_excluded():
    for ctx in (IntentContext(lifetime_spend_cents=500, sender_is_creator=True), IntentContext(prior_purchases=1, sender_is_creator=True)):
        r = classify_inbound_intent(ADS, ctx)
        assert r.label == AMBIGUOUS and not r.suppress_reply


def test_mixed_message_keeps_customer_intent():
    r = classify_inbound_intent("how much for a custom video? #ads", CREATOR)
    assert r.label == AMBIGUOUS and r.customer_intent and not r.suppress_reply


def test_genuine_preview_request_amid_promo_traffic_stays_eligible():
    # p92-style: a promo message and a genuine request in the same unanswered burst. Message-level selection.
    ok, results = burst_decision([(ADS, CREATOR), ("can i get a preview first?", CREATOR)])
    assert ok and [r.label for r in results] == [PROMO, CUSTOMER]
    # p97-style: a custom enquiry amid creator promotional traffic
    ok, _ = burst_decision([(ADS, CREATOR), ("do you do customs? i want a custom video", CREATOR)])
    assert ok
    ok, _ = burst_decision([(ADS, CREATOR), (ADS + " again", CREATOR)])
    assert not ok


def test_non_promotional_attachment_is_not_promo():
    # p54-style: a screenshot with no promotional text is a customer-side attachment, never promo
    r = classify_inbound_intent("", IntentContext(attachment_kind="photo", sender_is_creator=True))
    assert r.label == CUSTOMER


def test_business_enquiry_is_separate_from_spam_and_buying_intent():
    r = classify_inbound_intent("Hi, I run an agency and would like to talk about managing your page", IntentContext())
    assert r.label == BUSINESS and r.suppress_reply
    assert classify_inbound_intent("my agency friend says you have a custom video, how much?", IntentContext()).label != BUSINESS


def test_cohorts_and_known_test_ids_come_from_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ONLYFANS_KNOWN_TEST_FAN_IDS", "11, 12")
    f = tmp_path / "ids.txt"; f.write_text("13\n14")
    monkeypatch.setenv("ONLYFANS_KNOWN_TEST_FAN_IDS_FILE", str(f))
    known = known_test_fan_ids()
    assert known == {11, 12, 13, 14}
    promo = [classify_inbound_intent(ADS, CREATOR)]
    assert reporting_cohort(11, promo, known_test=known) == COHORT_KNOWN_TEST
    assert reporting_cohort(21, promo, suspected_test={21}) == COHORT_SUSPECTED_TEST
    assert reporting_cohort(22, promo) == COHORT_PROMO_BUSINESS and not counts_in_customer_metrics(COHORT_PROMO_BUSINESS)
    amb = [classify_inbound_intent("check my link https://example.com/a", IntentContext())]
    assert reporting_cohort(23, amb) == COHORT_AMBIGUOUS and counts_in_customer_metrics(COHORT_AMBIGUOUS)
    assert reporting_cohort(24, promo + [classify_inbound_intent("hi how are you", None)]) == COHORT_GENUINE
    assert reporting_cohort(25, promo, is_buyer=True) == COHORT_AMBIGUOUS


def test_repo_commits_no_test_ids():
    source = (Path(__file__).resolve().parents[1] / "automations" / "_inbound_intent.py").read_text()
    import re
    assert not re.search(r"\b\d{8,}\b", source)
