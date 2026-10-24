"""Audited scope selection and read-only onboarding projection on disposable PostgreSQL."""
import json
import pytest
from sqlalchemy.orm import Session
from db.models import AccountAiConfig, OfConversationState
from onlyfans_runtime.configuration import get_configuration, InvalidConfiguration
from onlyfans_runtime.store import Refused
from test_onlyfans_configuration_pg import database, setup, save, TENANT, ACCOUNT


def test_activation_projection_is_opt_in_scoped_and_does_not_activate(database, monkeypatch):
    store = setup(database)
    monkeypatch.setenv("ONLYFANS_FASTT_SCOPE", f"db:{ACCOUNT}")
    legacy = get_configuration(store, TENANT, ACCOUNT)
    assert "activation" not in legacy
    view = get_configuration(store, TENANT, ACCOUNT, include_activation=True)["activation"]
    assert view["deploymentAllowed"] and view["allFansAllowed"]
    assert view["selection"] is None and view["selectionValid"]
    assert view["noContactConversations"] >= 1
    assert view["modelCredentialReady"] is False and view["proxySaved"] is False
    assert "private" not in json.dumps(view)
    with pytest.raises(Refused): get_configuration(store, "another-tenant", ACCOUNT, include_activation=True)
    with Session(database[1]) as session:
        state = session.get(OfConversationState, (ACCOUNT,101))
        assert (state.mode,state.revision,state.unresolved_attempt_id) == ("no_contact",7,"retain-unknown")
    store.close()


def test_scope_save_is_audited_idempotent_and_preserves_sales_and_human_controls(database, monkeypatch):
    store = setup(database)
    monkeypatch.setenv("ONLYFANS_FASTT_SCOPE", f"db:{ACCOUNT}")
    with Session(database[1]) as session, session.begin():
        cfg = session.get(AccountAiConfig, ACCOUNT)
        raw = json.loads(cfg.ai_chatter_config_json)
        raw["fastt_post_buy_upsell"] = True
        cfg.ai_chatter_config_json = json.dumps(raw)
    original = get_configuration(store, TENANT, ACCOUNT)
    patch = {"fasttScope":{"mode":"selected","fans":[101]}}
    ack = save(store, original, patch)
    assert save(store, original, patch) == ack
    projected = get_configuration(store, TENANT, ACCOUNT, include_activation=True)["activation"]
    assert projected["selection"] == patch["fasttScope"]
    with Session(database[1]) as session:
        cfg = json.loads(session.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json)
        assert cfg["private"] == "retained" and cfg["fastt_post_buy_upsell"] is True
        state = session.get(OfConversationState,(ACCOUNT,101))
        assert (state.mode,state.revision,state.unresolved_attempt_id) == ("no_contact",7,"retain-unknown")
    monkeypatch.setenv("ONLYFANS_FASTT_SCOPE", f"{ACCOUNT}:101")
    current = get_configuration(store,TENANT,ACCOUNT)
    for forbidden in ({"mode":"all","fans":[]},{"mode":"selected","fans":[999]}):
        with pytest.raises(InvalidConfiguration): save(store,current,{"fasttScope":forbidden},key="f"*64)
    assert get_configuration(store,TENANT,ACCOUNT)["fingerprint"] == current["fingerprint"]
    store.close()
