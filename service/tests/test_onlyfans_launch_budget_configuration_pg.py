"""Real audited AI budget edits; no provider or model spending."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_configuration_pg import setup, save, MODEL
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import AccountAiConfig
from db.onlyfans_models import OfConfigurationChange
from onlyfans_runtime.configuration import get_configuration, InvalidConfiguration


def test_budget_and_effort_are_audited_and_identical_retry_preserves_zero(database):
    store = setup(database)
    before = get_configuration(store, TENANT, ACCOUNT)
    patch = {"dailyCostCapCents": 0, "reasoningEffort": None, "replyModelOverride": None}
    ack = save(store, before, patch)
    assert save(store, before, patch) == ack
    after = get_configuration(store, TENANT, ACCOUNT)
    assert after["dailyCostCapCents"] == 0
    assert after["reasoningEffort"] is None
    assert MODEL in after["reasoningOptions"]
    with Session(database[1]) as session:
        audit = session.scalar(select(OfConfigurationChange))
        assert json.loads(audit.after_json)["dailyCostCapCents"] == 0
        assert session.get(AccountAiConfig, ACCOUNT).daily_cost_cap_cents == 0
    store.close()


@pytest.mark.parametrize("patch", [
    {"dailyCostCapCents": True}, {"dailyCostCapCents": -1},
    {"dailyCostCapCents": 1000001}, {"dailyCostCapCents": 1.1},
    {"reasoningEffort": ""}, {"reasoningEffort": True},
    {"reasoningEffort": "not-supported", "replyModelOverride": None},
])
def test_invalid_budget_or_effort_rolls_back_the_entire_command(database, patch):
    store = setup(database)
    before = get_configuration(store, TENANT, ACCOUNT)
    with pytest.raises(InvalidConfiguration):
        save(store, before, patch)
    assert get_configuration(store, TENANT, ACCOUNT) == before
    with Session(database[1]) as session:
        assert session.scalar(select(OfConfigurationChange)) is None
    store.close()


def test_effort_is_validated_against_resulting_model_in_same_command(database):
    store = setup(database)
    before = get_configuration(store, TENANT, ACCOUNT)
    options = before["reasoningOptions"]
    model, choices = next((key, value) for key, value in options.items() if value)
    save(store, before, {"replyModelOverride": model, "reasoningEffort": choices[-1], "dailyCostCapCents": 250})
    after = get_configuration(store, TENANT, ACCOUNT)
    assert (after["resolvedReplyModel"], after["reasoningEffort"], after["dailyCostCapCents"]) == (model, choices[-1], 250)
    store.close()
