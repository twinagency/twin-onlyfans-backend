"""Synthetic audience-boundary regressions; no provider or model is used."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import smart_lists_api as lists


FANS = [
    {"fan_id": 1, "lifetime_spend": 0, "recent_spend": {30: 0}, "status": "active", "tags": []},
    {"fan_id": 2, "lifetime_spend": 5000, "recent_spend": {"30": 1200}, "status": "expired", "tags": ["VIP"]},
    {"fan_id": 3, "lifetime_spend": None, "recent_spend": {}, "status": None, "tags": []},
]


def rules(field, op, value, **extra):
    return {"match": "all", "rules": [{"field": field, "op": op, "value": value, **extra}]}


@pytest.mark.parametrize("stored", [None, "", "{}", '{"match":"all","rules":[]}'])
def test_deliberately_blank_legacy_lists_remain_everyone(stored):
    assert lists.resolve(lists._rules_of(SimpleNamespace(rules_json=stored)), FANS) == [1, 2, 3]


@pytest.mark.parametrize("stored", [
    "{broken", "null", "[]", "false", '"text"',
    '{"rules":false}', '{"rules":null}', '{"rules":{}}',
    '{"match":false}', '{"match":"unknown"}',
    '{"version":2,"rules":[]}',
    '{"rules":[{"field":"future_field","op":"==","value":0}]}',
])
def test_invalid_saved_include_or_exclusion_blocks_instead_of_expanding(stored):
    with pytest.raises(HTTPException) as caught:
        lists._rules_of(SimpleNamespace(rules_json=stored))
    assert caught.value.status_code == 409


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", True])
def test_nonfinite_or_boolean_threshold_rejected_on_write_and_saved_read(amount):
    definition = rules("lifetime_spend", "!=", amount)
    with pytest.raises(HTTPException) as caught:
        lists._validate_rules(definition)
    assert caught.value.status_code == 422
    with pytest.raises(HTTPException) as caught:
        lists._rules_of(SimpleNamespace(rules_json=json.dumps(definition)))
    assert caught.value.status_code == 409


@pytest.mark.parametrize("definition, expected", [
    (rules("lifetime_spend", ">=", "5000"), [2]),
    (rules("recent_spend", ">=", 1000, days=30), [2]),
    (rules("status", "==", "EXPIRED"), [2]),
    (rules("tag", "has", "vip"), [2]),
    ({"match":"any","rules":[{"field":"status","op":"==","value":"active"},
        {"field":"tag","op":"has","value":"VIP"}]}, [1, 2]),
])
def test_valid_legacy_rules_keep_existing_results(definition, expected):
    stored = SimpleNamespace(rules_json=json.dumps(definition))
    assert lists.resolve(lists._rules_of(stored), FANS) == expected


@pytest.mark.parametrize("bad", [
    {"rules":False}, {"match":0}, {"rules":[{"field":[],"op":"==","value":0}]},
    {"rules":[{"field":"status","op":[],"value":"active"}]},
    {"rules":[{"field":"status","op":"==","value":"active","extra_condition":True}]},
    rules("recent_spend", ">", 0, days=float("inf")),
])
def test_direct_resolver_validates_before_reading_fans(bad):
    def must_not_iterate():
        raise AssertionError("An invalid audience must fail before fan loading/evaluation")
        yield
    with pytest.raises(HTTPException) as caught:
        lists.resolve(bad, must_not_iterate())
    assert caught.value.status_code == 422


def test_saved_resolve_route_blocks_before_fan_loader(monkeypatch):
    @asynccontextmanager
    async def session():
        async def get(*_):
            return SimpleNamespace(account_id="synthetic", rules_json="{broken")
        yield SimpleNamespace(get=get)
    async def must_not_load(*_):
        raise AssertionError("Corrupt saved audience must not load or return fans")
    monkeypatch.setattr(lists, "get_session", session)
    monkeypatch.setattr(lists, "assert_account_owned", lambda _: None)
    monkeypatch.setattr(lists, "_load_normalised_fans", must_not_load)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(lists.resolve_smart_list(1))
    assert caught.value.status_code == 409
