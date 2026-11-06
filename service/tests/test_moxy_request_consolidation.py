"""Request/correction regressions from the 6 November drift review.

Synthetic authority and in-memory calls only: these do not measure model quality,
contact a provider, or establish Amelia's live catalogue or purchase state.
"""
from copy import deepcopy
import json

import pytest

from moxy_replacement.catalogue_context import project
from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.observations import apply, observations
from moxy_replacement.runtime import correction_brief, validate_decision
from moxy_replacement.writer import parse
from test_moxy_foundation_authority_regressions import authority_scene, parsed_offer
from test_moxy_integrated_pg import decision


@pytest.mark.parametrize('text', [
    'If I get tired, stop sending me offers.',
    'My friend said, stop sending me offers.',
])
def test_conditional_or_reported_stop_cannot_bypass_observation_evidence(text):
    from moxy_replacement.input_batch import current_requests, explicit_updates
    _, snap, _ = authority_scene(text)
    previous = {'id': 1000, 'direction': 'in', 'text': 'Please send me the first photo.'}
    incoming = [previous, snap['pending_fan_messages'][0]]
    proposed = decision(observations=[dict(kind='ordinary_chat_only', active=True,
        message_id=1001, quote=text, product_id=None)])
    assert not apply(snap['fan_state'], observations(proposed, snap))['ordinary_chat_only']
    assert not explicit_updates(incoming)
    assert previous in current_requests(incoming)


@pytest.mark.parametrize('text', [
    'What time is it? Stop sending me offers.',
    'My friend said hello. Stop sending me offers.',
])
def test_independent_sentence_keeps_its_actual_stop_authority(text):
    from moxy_replacement.input_batch import cancelled, explicit_updates
    _, snap, _ = authority_scene(text)
    message = snap['pending_fan_messages'][0]
    assert cancelled(message)
    assert apply(snap['fan_state'], explicit_updates([message]))['ordinary_chat_only'] is True


@pytest.mark.parametrize("prior_text", [
    "I like your feet.", "Please send me a feet video.",
])
def test_current_explicit_item_request_does_not_inherit_an_earlier_body_topic(prior_text):
    current = "Please send me the first photo."
    settings, snap, _ = authority_scene(current, no_push=True)
    prior = {"id": 1000, "direction": "in", "text": prior_text}
    snap["pending_fan_messages"].insert(0, prior)
    snap["history"] = list(snap["pending_fan_messages"])
    before = deepcopy(snap["fan_state"])

    terms = validate_decision(settings, snap, parsed_offer(request=current), planning=True)

    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500
    assert snap["fan_state"] == before


@pytest.mark.parametrize("current", [
    "Please send me the first photo, thanks babe.",
    "Please send me the first photo. Thanks babe.",
    "Please send me the first photo, thank you Jasmine 😊.",
    "Please send me the first photo thanks Jasmine 😊.",
    "Please send me the first photo thank you Amelia.",
])
def test_polite_closing_does_not_cancel_an_explicit_item_request(current):
    settings, snap, _ = authority_scene(current, no_push=True)
    terms = validate_decision(settings, snap, parsed_offer(request=current), planning=True)
    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500
    assert snap["fan_state"]["no_push"] is True


@pytest.mark.parametrize("reason", [
    "current_request_authority_unverified", "current_request_evidence_missing",
])
def test_evidence_correction_keeps_valid_item_available_and_revalidates_exact_terms(reason):
    current = "Please send me the first photo."
    settings, snap, _ = authority_scene(current, no_push=True)
    rejected = parsed_offer(request="How was your day?")
    brief = correction_brief(reason, rejected, snap)
    snap["event"]["payload"]["rejected"] = brief

    projected = project(snap)
    assert "first" in projected["products"]
    assert "first" in projected["new_card_product_ids"]
    repaired = parsed_offer("Here is the first photo for $75.", request=current)
    terms = validate_decision(settings, snap, repaired, planning=True)
    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500


def test_evidence_correction_does_not_invent_permission_when_source_is_only_chat():
    current = "How was your day?"
    settings, snap, _ = authority_scene(current, no_push=True)
    proposed = parsed_offer(request=current)
    snap["event"]["payload"]["rejected"] = correction_brief(
        "current_request_authority_unverified", proposed, snap)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, proposed, planning=True)
    assert snap["fan_state"]["no_push"] is True


@pytest.mark.parametrize("text,quote", [
    ("Only if it is free, send me the first photo, thanks babe.", "send me the first photo"),
    ('My friend said "send me the first photo", thanks babe.', "send me the first photo"),
    ("Don't send me the first photo, thanks babe.", "send me the first photo"),
    ("Send me the first photo tomorrow, thanks babe.", "Send me the first photo"),
    ("Please send me the first photo, thanks Jasmine 😊, only if it is free.", "Please send me the first photo"),
    ("Please send me the first photo thanks Jasmine 😊 but only if it is free.", "Please send me the first photo"),
    ("Please send me the first photo, was what my friend said.", "Please send me the first photo"),
])
def test_polite_closing_never_turns_qualified_or_reported_words_into_permission(text, quote):
    settings, snap, _ = authority_scene(text, no_push=True)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=quote), planning=True)
    assert snap["fan_state"]["no_push"] is True


def test_later_withdrawal_still_fences_an_earlier_explicit_request():
    current = "Please send me the first photo."
    settings, snap, _ = authority_scene(current, no_push=True)
    snap["pending_fan_messages"].append({
        "id": 1002, "direction": "in", "text": "Actually, don't send it, thanks babe.",
    })
    snap["history"] = list(snap["pending_fan_messages"])
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=current), planning=True)


@pytest.mark.parametrize("kind", ["ordinary_chat_only", "sales_refused"])
def test_ordinary_question_does_not_authorize_a_new_durable_sales_restriction(kind):
    current = "How was your day?"
    _, snap, _ = authority_scene(current)
    proposed = decision(observations=[{
        "kind": kind, "active": True, "message_id": 1001,
        "quote": current, "product_id": None,
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated[kind] is False


@pytest.mark.parametrize("kind,text", [
    ("ordinary_chat_only", "I only want to chat, don't send me any offers."),
    ("sales_refused", "Stop sending me offers."),
])
def test_actual_explicit_no_sales_preference_is_preserved(kind, text):
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": kind, "active": True, "message_id": 1001,
        "quote": text, "product_id": None,
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated[kind] is True


@pytest.mark.parametrize("kind,text", [
    ("ordinary_chat_only", 'You said "I only want to chat", but I did not.'),
    ("sales_refused", 'My friend said "stop sending me offers".'),
])
def test_reported_preference_is_not_the_current_fans_sales_restriction(kind, text):
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": kind, "active": True, "message_id": 1001,
        "quote": text, "product_id": None,
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated[kind] is False


def test_genuine_ineligible_item_stays_excluded_from_product_correction():
    settings, snap, _ = authority_scene()
    snap["event"]["payload"]["rejected"] = correction_brief(
        "product_unavailable", parsed_offer(), snap)
    projected = project(snap)
    assert "first" not in projected["new_card_product_ids"]
    with pytest.raises(ActionBlocked, match="correction_repeated_rejected_choice"):
        validate_decision(settings, snap, parsed_offer(), planning=True)


@pytest.mark.parametrize("reason", [
    "current_request_authority_unverified", "current_request_evidence_missing",
])
def test_evidence_correction_allows_text_only_clarification_without_a_sale(reason):
    settings, snap, _ = authority_scene("Send me that one.", no_push=True)
    snap["event"]["payload"]["rejected"] = correction_brief(
        reason, parsed_offer(request="Send me that one."), snap)
    clarified = parse(json.dumps(decision(text="Which photo do you mean?")))
    assert validate_decision(settings, snap, clarified, planning=True) is None
    assert snap["fan_state"]["no_push"] is True


def test_evidence_correction_cannot_substitute_another_otherwise_eligible_product():
    current = "Please send me a photo."
    settings, snap, _ = authority_scene(current)
    snap["event"]["payload"]["rejected"] = correction_brief(
        "current_request_evidence_missing", parsed_offer(request="missing source"), snap)
    changed = parsed_offer("Here is the second photo.", key="second", request=current)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, changed, planning=True)


def test_evidence_correction_cannot_silently_replace_retained_price():
    current = "Please send me the first photo."
    settings, snap, _ = authority_scene(current, no_push=True)
    snap["event"]["payload"]["rejected"] = correction_brief(
        "current_request_evidence_missing", parsed_offer(request="missing source"), snap)
    snap["inventory"]["first"]["price_cents"] = 6000
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=current), planning=True)


def test_otherwise_permitted_proactive_offer_does_not_require_request_metadata():
    settings, snap, _ = authority_scene("How was your day?")
    proposed = parse(json.dumps(decision("offer", "Here is an outfit photo for you.", "first")))
    terms = validate_decision(settings, snap, proposed, planning=True)
    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500


@pytest.mark.parametrize("current", ["Please send me a feet photo.", "Please send me the first video."])
def test_current_request_still_rejects_wrong_body_or_actual_file_format(current):
    # No no-push exception is involved: request consolidation must retain the
    # product/body/format check for an ordinary permitted commercial decision.
    settings, snap, _ = authority_scene(current)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=current), planning=True)


def test_item_refusal_cannot_be_applied_to_a_different_item():
    text = "I don't want the first photo."
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": "sales_refused", "active": True, "message_id": 1001,
        "quote": text, "product_id": "second",
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert "second" not in updated["rejected_products"]
    assert updated["sales_refused"] is False


def test_actual_item_refusal_only_blocks_the_named_item():
    text = "I don't want the first photo."
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": "sales_refused", "active": True, "message_id": 1001,
        "quote": text, "product_id": "first",
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated["rejected_products"] == ["first"]
    assert updated["sales_refused"] is False
    assert updated["ordinary_chat_only"] is False


@pytest.mark.parametrize("text", [
    "I don't want to stop chatting.", "Not that football match.",
])
def test_unrelated_negative_statement_cannot_block_a_product(text):
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": "sales_refused", "active": True, "message_id": 1001,
        "quote": text, "product_id": "first",
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated["rejected_products"] == []
    assert updated["sales_refused"] is False
    assert updated["ordinary_chat_only"] is False


def test_ambiguous_item_description_does_not_select_a_product_to_reject():
    text = "Not that outfit photo."
    _, snap, _ = authority_scene(text)
    for product in snap["products"].values():
        product["assets"][0]["facts"] = ["Photograph of an outfit."]
    proposed = decision(observations=[{
        "kind": "sales_refused", "active": True, "message_id": 1001,
        "quote": text, "product_id": "first",
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated["rejected_products"] == []


@pytest.mark.parametrize("text", [
    "No offers please, because I am tired.",
    "No offers please, I will ask when I want one.",
])
def test_present_global_refusal_survives_its_explanation_or_future_request(text):
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": "ordinary_chat_only", "active": True, "message_id": 1001,
        "quote": text, "product_id": None,
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated["ordinary_chat_only"] is True


def test_hypothetical_global_refusal_is_not_a_current_preference():
    text = "If I say no offers please, will you stop?"
    _, snap, _ = authority_scene(text)
    proposed = decision(observations=[{
        "kind": "ordinary_chat_only", "active": True, "message_id": 1001,
        "quote": text, "product_id": None,
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated["ordinary_chat_only"] is False


@pytest.mark.parametrize("caption", [
    "Here is the video.",
    "This photo shows my booty and breasts.",
])
def test_accepted_card_cannot_claim_an_unsupported_format_or_extra_body_content(caption):
    settings, snap, authority = authority_scene("Yes.")
    snap["invitation"] = {
        "product_id": "first", "fingerprint": authority.fingerprint, "sequence": None,
    }
    proposed = parsed_offer(caption, request="Yes.", kind="accept")
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, proposed, planning=True)
