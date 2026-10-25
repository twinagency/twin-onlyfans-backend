"""Capacity evidence must fail closed when replies, media or timing are incomplete."""
from copy import deepcopy
import pytest
from native_pilot.tools.load_test import single_creator_verdict

def result():
    return {
        "config": {"accounts": 1, "scale": 1, "native_engine": True, "media_every": 5},
        "offered_load": {"scheduled_messages": 2, "generated_messages": 2,
                         "persisted_messages": 2, "max_ingress_delay_s": 0.1},
        "accounts": {"of-900000001": {"reply_latency_s": {"n": 2, "max": 20},
            "unanswered_at_end": 0, "delivered_over_90s": 0, "model_failures": 0,
            "provider_429s": 0, "throttle_respected": True}},
        "native_engine_exercised": True, "model_purposes": {"ai_chatter": 2},
        "provider_posts_by_fan": {"1000000": 1, "1000001": 2}, "media_posts": 1,
        "complete_answer_latency_s": {"n": 2, "max": 35}, "incomplete_answers": 0,
        "service_crashed": [], "helper_errors": [], "runtime_errors": [], "validation_errors": []}

def test_complete_native_text_and_media_are_accepted():
    assert single_creator_verdict(result())["passed"]

@pytest.mark.parametrize("section,key,value", [
    ("config", "scale", 30),
    ("offered_load", "persisted_messages", 1),
    ("offered_load", "max_ingress_delay_s", 2),
    ("complete_answer_latency_s", "max", 91),
    ("complete_answer_latency_s", "n", 1),
    ("provider_posts_by_fan", "1000001", 1),
    ("provider_posts_by_fan", "1000001", 3),
])
def test_incomplete_or_distorted_evidence_is_rejected(section, key, value):
    data = result()
    data[section][key] = value
    assert not single_creator_verdict(data)["passed"]

@pytest.mark.parametrize("key,value", [
    ("media_posts", 0), ("incomplete_answers", 1), ("native_engine_exercised", False),
    ("validation_errors", ["wrong-fan"]), ("runtime_errors", ["turn-abandoned"]),
    ("service_crashed", ["worker-failed"]), ("helper_errors", ["ingress-failed"]),
])
def test_failure_evidence_cannot_be_hidden_by_fast_median_latency(key, value):
    data = result()
    data[key] = value
    assert not single_creator_verdict(data)["passed"]

@pytest.mark.parametrize("key,value", [
    ("unanswered_at_end", 1), ("delivered_over_90s", 1), ("model_failures", 1),
    ("provider_429s", 1), ("throttle_respected", False),
])
def test_each_delivery_failure_is_rejected(key, value):
    data = result()
    data["accounts"]["of-900000001"][key] = value
    assert not single_creator_verdict(data)["passed"]
