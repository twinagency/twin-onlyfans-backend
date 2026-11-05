"""Focused in-memory regressions for positive facts before a negative clause."""
import pytest

from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.observations import apply, observations
from moxy_replacement.runtime import validate_decision
from test_moxy_foundation_authority_regressions import authority_scene, parsed_offer
from test_moxy_integrated_pg import decision


def fact_scene(text, fact):
    settings, snap, _ = authority_scene(text, no_push=True)
    snap["products"]["first"]["assets"][0]["facts"] = [fact]
    return settings, snap


@pytest.mark.parametrize("fact", [
    "An outfit is visible, no live/custom claim.",
    "Outfit visible, no toy, feet, or nudity.",
    "Outfit visible, no toy, and feet.",
])
def test_positive_outfit_prefix_still_supports_explicit_first_item_request(fact):
    text = "Please send me the first outfit photo."
    settings, snap = fact_scene(text, fact)
    terms = validate_decision(
        settings, snap, parsed_offer("Here is the first outfit photo.", request=text),
        planning=True)
    assert terms["media_ids"] == [101]
    assert terms["price_cents"] == 7500


def test_positive_outfit_prefix_still_supports_correctly_scoped_refusal():
    text = "I don't want the first outfit photo."
    _, snap = fact_scene(text, "An outfit is visible, no live/custom claim.")
    proposed = decision(observations=[{
        "kind": "sales_refused", "active": True, "message_id": 1001,
        "quote": text, "product_id": "first",
    }])
    updated = apply(snap["fan_state"], observations(proposed, snap))
    assert updated["rejected_products"] == ["first"]
    assert updated["sales_refused"] is False


@pytest.mark.parametrize("fact", [
    "No toy, feet, or nudity.",
    "No toy, and feet.",
    "Outfit visible, no toy, feet, or nudity.",
    "Outfit visible, no toy, and feet.",
])
def test_negative_enumeration_does_not_make_feet_a_positive_asset_fact(fact):
    text = "Please send me the first feet photo."
    settings, snap = fact_scene(text, fact)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, parsed_offer(request=text), planning=True)
