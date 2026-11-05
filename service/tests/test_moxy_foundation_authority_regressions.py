"""Source-reproduced authority gaps, without a database, model or transport.

These are ordinary regression requirements, not xfails or model-quality claims.
Fixtures carry explicit synthetic authority; no live creator approval is inferred.
"""
from copy import deepcopy
import json

import pytest

from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.input_batch import current_requests, explicit_updates
from moxy_replacement.observations import apply, observations
from moxy_replacement.runtime import configuration, validate_decision
from moxy_replacement.writer import parse
from test_moxy_integrated_pg import decision
from test_moxy_replacement_configuration import product, state


def authority_scene(text="Please send me the first photo.", **facts):
    products = [product("first", 101),
                product("second", 102, category="bikini_lingerie_booty", price_cents=8500)]
    settings = {
        "creator_key": "amelia", "products": products, "sequences": {},
        "catalogue_bindings": {}, "operational": {},
    }
    cfg, authority = configuration(settings)
    incoming = {"id": 1001, "direction": "in", "text": text}
    inventory = {
        p["id"]: {
            "price_cents": p["price_cents"], "media_ids": [p["assets"][0]["id"]],
            "preview_ids": [], "item": {"id": i + 1, "label": p["id"]},
            "files": [{"media_id": p["assets"][0]["id"], "kind": "photo",
                       "description_basis": {"frames": 1}}],
        }
        for i, p in enumerate(products)
    }
    snapshot = {
        "fan_id": 101, "pilot": None, "campaign": None,
        "instructions": cfg.public_context(), "fan_state": state(**facts),
        "event": {"kind": "turn", "payload": {}}, "history": [incoming],
        "pending_fan_messages": [incoming], "products": {p["id"]: p for p in products},
        "inventory": inventory, "operational": {}, "obligations": [],
        "approved_remedies": {}, "card_state": [], "public_facts": {},
        "invitation": None,
    }
    return settings, snapshot, authority


def parsed_offer(text="Here is the first photo.", *, key="first", request=None,
                 kind="direct", parts=None):
    draft = decision(
        "offer", text, key, request_kind=kind,
        request_quote=request or "Please send me the first photo.",
        messages=parts or [{"text": text, "media": True}],
    )
    return parse(json.dumps(draft))


@pytest.mark.parametrize("facts", [
    {"no_push": True}, {"ordinary_chat_only": True},
    {"no_push": True, "ordinary_chat_only": True, "sales_refused": True},
])
def test_greeting_quote_cannot_authorize_a_restricted_offer(facts):
    settings, snap, _ = authority_scene("How was your day?", **facts)
    before = deepcopy(snap["fan_state"])
    proposed = parsed_offer(request="How was your day?")
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, proposed, planning=True)
    assert snap["fan_state"] == before


def test_real_direct_request_can_use_existing_no_push_exception():
    settings, snap, _ = authority_scene(no_push=True)
    before = deepcopy(snap["fan_state"])
    terms = validate_decision(settings, snap, parsed_offer(), planning=True)
    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500
    assert snap["fan_state"] == before


def test_restricted_fan_can_still_receive_a_plain_conversational_reply():
    settings, snap, _ = authority_scene("How was your day?", no_push=True,
                                        ordinary_chat_only=True)
    proposed = parse(json.dumps(decision(text="It is good to hear from you.")))
    assert validate_decision(settings, snap, proposed, planning=True) is None
    # A non-authorizing annotation on plain text must not turn it into a held
    # sale or lift a saved preference.
    proposed['request_kind'] = 'direct'
    proposed['request_quote'] = 'How was your day?'
    assert validate_decision(settings, snap, proposed, planning=True) is None
    assert snap['fan_state']['ordinary_chat_only'] is True


@pytest.mark.parametrize("hold", ["human_owned", "safety_hold", "financial_hardship"])
def test_real_request_does_not_clear_independent_hard_holds(hold):
    settings, snap, _ = authority_scene(**{hold: True})
    with pytest.raises(ActionBlocked, match=hold):
        validate_decision(settings, snap, parsed_offer(), planning=True)
    assert snap["fan_state"][hold] is True


@pytest.mark.parametrize("restriction", ["ordinary_chat_only", "sales_refused"])
def test_greeting_observation_cannot_erase_a_durable_sales_preference(restriction):
    _, snap, _ = authority_scene("How was your day?", **{restriction: True})
    proposed = decision(observations=[{
        "kind": restriction, "active": False, "message_id": 1001,
        "quote": "How was your day?", "product_id": None,
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated[restriction] is True


@pytest.mark.parametrize("parts", [
    [{"text": "This photo is $5.", "media": True}],
    [{"text": "It costs $5.", "media": False},
     {"text": "Here is the first photo.", "media": True}],
    # $85 is approved for a different product, not for the selected $75 card.
    [{"text": "This photo is $85.", "media": True}],
])
def test_all_offer_parts_must_use_the_selected_items_price(parts):
    settings, snap, _ = authority_scene()
    proposed = parsed_offer(text=parts[0]["text"], parts=parts)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, proposed, planning=True)
    assert snap["inventory"]["first"]["price_cents"] == 7500


def test_correct_offer_price_is_accepted_without_repricing():
    settings, snap, _ = authority_scene()
    terms = validate_decision(settings, snap, parsed_offer("This photo is $75."),
                              planning=True)
    assert terms["price_cents"] == 7500
    assert terms["media_ids"] == [101]


def test_new_direct_request_is_not_forced_back_to_old_invitation():
    text = "Send me the second photo instead."
    settings, snap, authority = authority_scene(text)
    snap["invitation"] = {"product_id": "first", "fingerprint": authority.fingerprint,
                          "sequence": None}
    proposed = parsed_offer("Here is the second photo.", key="second", request=text)
    terms = validate_decision(settings, snap, proposed, planning=True)
    assert terms["media_ids"] == [102]
    assert terms["price_cents"] == 8500


def test_actual_acceptance_preserves_invitation_target_and_price():
    settings, snap, authority = authority_scene("Yes, send that one.")
    snap["invitation"] = {"product_id": "first", "fingerprint": authority.fingerprint,
                          "sequence": None}
    proposed = parsed_offer(request="Yes, send that one.", kind="accept")
    terms = validate_decision(settings, snap, proposed, planning=True)
    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500
    wrong = parsed_offer("Here is the second photo.", key="second",
                         request="Yes, send that one.", kind="accept")
    with pytest.raises(ActionBlocked, match="acceptance_product_mismatch"):
        validate_decision(settings, snap, wrong, planning=True)


def test_same_message_replacement_remains_current_without_a_general_sales_hold():
    old = {"id": 1, "direction": "in", "text": "Send the video."}
    replacement = {"id": 2, "direction": "in",
                   "text": "Do not send that video; send the photo instead."}
    requests = current_requests([old, replacement])
    assert [message["id"] for message in requests] == [2]
    assert "send the photo instead" in requests[0]["text"]
    assert not any(o["kind"] == "ordinary_chat_only" and o["active"]
                   for o in explicit_updates([old, replacement]))


def test_cancelling_an_unrelated_topic_does_not_set_a_sales_hold():
    message = {"id": 2, "direction": "in",
               "text": "Never mind the football score. How was your day?"}
    assert [m["id"] for m in current_requests([message])] == [2]
    assert not any(o["kind"] == "ordinary_chat_only" and o["active"]
                   for o in explicit_updates([message]))


def test_real_global_send_cancellation_still_fences_the_prior_request():
    messages = [
        {"id": 1, "direction": "in", "text": "Send the first photo."},
        {"id": 2, "direction": "in", "text": "Cancel that, do not send anything."},
    ]
    assert current_requests(messages) == []
    updated = apply(state(), explicit_updates(messages))
    assert updated["ordinary_chat_only"] is True


@pytest.mark.parametrize("text,quote", [
    ('You said "send me the first photo". I did not ask for it.', 'send me the first photo'),
    ('Do not send me the first photo.', 'send me the first photo'),
    ('If I say "send me the first photo", will you charge me?', 'send me the first photo'),
    ('Please show me how to cancel the first photo.', 'Please show me how to cancel the first photo.'),
    ('I want to know about the first photo.', 'I want to know about the first photo.'),
    ('I want your booty.', 'I want your booty.'),
])
def test_reported_negated_hypothetical_and_conversational_requests_cannot_lift_no_push(text, quote):
    settings, snap, _ = authority_scene(text, no_push=True)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=quote), planning=True)


def test_one_item_request_does_not_remove_global_preference_through_an_observation():
    _, snap, _ = authority_scene(ordinary_chat_only=True)
    proposed = decision(observations=[{
        'kind': 'ordinary_chat_only', 'active': False, 'message_id': 1001,
        'quote': 'Please send me the first photo.', 'product_id': None,
    }])
    assert apply(snap['fan_state'], observations(proposed, snap))['ordinary_chat_only'] is True


@pytest.mark.parametrize('restriction', ['ordinary_chat_only', 'sales_refused'])
def test_explicit_general_reversal_can_remove_a_general_sales_preference(restriction):
    text = 'You can send me offers again.'
    _, snap, _ = authority_scene(text, **{restriction: True})
    proposed = decision(observations=[{
        'kind': restriction, 'active': False, 'message_id': 1001,
        'quote': text, 'product_id': None,
    }])
    assert apply(snap['fan_state'], observations(proposed, snap))[restriction] is False


def test_scoped_cancellation_preserves_other_conversation_without_a_durable_global_hold():
    messages = [{'id': 1, 'direction': 'in', 'text': 'Send the first photo.'},
                {'id': 2, 'direction': 'in', 'text': 'Cancel that.'}]
    assert current_requests(messages) == []
    assert explicit_updates(messages) == []


def test_replacement_quote_cannot_resurrect_the_cancelled_target():
    text = 'Do not send the first photo; send the second photo instead.'
    settings, snap, _ = authority_scene(text, no_push=True)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request='send the first photo'), planning=True)
    terms = validate_decision(settings, snap,
                              parsed_offer('Here is the second photo.', key='second', request=text),
                              planning=True)
    assert terms['media_ids'] == [102]
    assert terms['price_cents'] == 8500


def test_saved_payload_request_without_current_authority_cannot_lift_no_push():
    settings, snap, _ = authority_scene('How was your day?', no_push=True)
    snap['event']['payload']['direct_request_product'] = 'first'
    proposed = parsed_offer(kind='none')
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, proposed, planning=True)


@pytest.mark.parametrize('text', [
    'Send me the first photo only if it is free.',
    'Would you send me the first photo if I ask later?',
    'Show me what the first photo includes.',
    'Send me the first photo tomorrow.',
    'I want the first photo after I get paid.',
    'Please send the first photo unless it costs money.',
    'Can you show me whether the first photo is available?',
])
def test_conditional_deferred_and_information_requests_do_not_lift_sales_restrictions(text):
    settings, snap, _ = authority_scene(text, no_push=True)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=text), planning=True)


@pytest.mark.parametrize('text', [
    'Send me a photo of your dog.', 'Send me a video.',
    'Send me an outdoor photograph.',
])
def test_sole_product_does_not_authorize_unmatched_subject_or_format(text):
    settings, snap, _ = authority_scene(text, no_push=True)
    settings['products'] = settings['products'][:1]
    snap['products'].pop('second'); snap['inventory'].pop('second')
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=text), planning=True)


@pytest.mark.parametrize('text', ["Don't remove my budget.", 'Do not ignore my budget.',
                                  'How was your day?'])
def test_negated_or_unrelated_budget_removal_does_not_lift_limit(text):
    _, snap, _ = authority_scene(text, budget_cents=1000)
    proposed = decision(observations=[{
        'kind': 'budget', 'active': False, 'message_id': 1001,
        'quote': text, 'product_id': None, 'amount_cents': 1000,
    }])
    assert apply(snap['fan_state'], observations(proposed, snap))['budget_cents'] == 1000


def test_explicit_budget_removal_can_lift_limit():
    text = 'Please remove my budget.'
    _, snap, _ = authority_scene(text, budget_cents=1000)
    proposed = decision(observations=[{
        'kind': 'budget', 'active': False, 'message_id': 1001,
        'quote': text, 'product_id': None, 'amount_cents': 1000,
    }])
    assert 'budget_cents' not in apply(snap['fan_state'], observations(proposed, snap))


def test_actual_cancellation_is_not_ignored_because_it_explains_prior_speech():
    messages = [
        {'id': 1, 'direction': 'in', 'text': 'Send me the first photo.'},
        {'id': 2, 'direction': 'in', 'text': 'Do not send anything because you said you would.'},
    ]
    assert current_requests(messages) == []
    assert apply(state(), explicit_updates(messages))['ordinary_chat_only'] is True


def test_global_stop_with_specific_exception_keeps_durable_preference():
    text = 'Do not send anything; send the first photo instead.'
    updates = explicit_updates([{'id': 1001, 'direction': 'in', 'text': text}])
    assert apply(state(), updates)['ordinary_chat_only'] is True
    settings, snap, _ = authority_scene(text, ordinary_chat_only=True)
    terms = validate_decision(settings, snap, parsed_offer(request=text), planning=True)
    assert terms['media_ids'] == [101]
    assert snap['fan_state']['ordinary_chat_only'] is True


@pytest.mark.parametrize('text', [
    'If I get paid, you can send me offers again.',
    'Tomorrow, you can send me offers again.',
    'My friend says you can send me offers again.',
    'You can send me offers again if I get paid.',
])
def test_conditional_deferred_or_reported_reversal_preserves_general_preference(text):
    _, snap, _ = authority_scene(text, ordinary_chat_only=True)
    proposed = decision(observations=[{
        'kind': 'ordinary_chat_only', 'active': False, 'message_id': 1001,
        'quote': text, 'product_id': None,
    }])
    assert apply(snap['fan_state'], observations(proposed, snap))['ordinary_chat_only'] is True


@pytest.mark.parametrize('text', ['Stop sending me videos.', 'Stop sending me photos.',
                                  'Do not send any more videos.'])
def test_format_specific_stop_does_not_become_a_durable_global_preference(text):
    updates = explicit_updates([{'id': 1001, 'direction': 'in', 'text': text}])
    assert not any(o['kind'] == 'ordinary_chat_only' and o['active'] for o in updates)


def test_named_cancellation_retains_provably_distinct_pending_request():
    messages = [
        {'id': 1, 'direction': 'in', 'text': 'Send the first photo.'},
        {'id': 2, 'direction': 'in', 'text': 'Send the second photo too.'},
        {'id': 3, 'direction': 'in', 'text': 'Do not send the first photo.'},
    ]
    assert [message['id'] for message in current_requests(messages)] == [2]
