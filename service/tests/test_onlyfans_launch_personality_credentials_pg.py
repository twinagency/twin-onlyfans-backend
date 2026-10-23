"""Personality credential readiness reader against the owned PG; no provider I/O."""
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401  (fixture)
from test_onlyfans_text_delivery_pg import TENANT
import test_onlyfans_connections_pg as C
from test_onlyfans_provider_adapters import MODEL
from db.models import AccountAiConfig, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime import connections
from onlyfans_runtime.connections import _seal
from onlyfans_runtime.launch_personality import api as personality_api
from onlyfans_runtime.launch_personality.workflow import PersonalityWorkflow
from onlyfans_runtime.launch_personality_credentials import personality_credential_status as reader
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.provider_model import NativeProviderModel
from onlyfans_runtime.store import utcnow

SECRET = "synthetic-model-key-" + "x" * 20
KEYS = {"accountId", "modelId", "configured", "expiresAt"}


def verified(database, monkeypatch, **kwargs):
    # Capturing a scoped key does not select a reply model. Save that explicit
    # configuration independently, as required by the combined authority.
    result = C.verified(database, monkeypatch, **kwargs)
    store, account, _ = result
    with Session(store.engine) as s, s.begin():
        s.get(AccountAiConfig, account).model = MODEL
    return result


def model(**overrides):
    return {"modelId": MODEL, "apiKey": SECRET, **overrides}


def read(store, account):
    with Session(store.engine) as s, s.begin():
        binding = s.get(OfRuntime, account).binding_id
        return reader(s, account, binding)


def reseal(store, account, mutate):
    """Re-seal edited config through the existing sealing authority."""
    with Session(store.engine) as s, s.begin():
        row = s.get(OfProviderConnection, account)
        value = connections._open(row, TENANT)
        mutate(value)
        row.sealed_config = _seal(value, TENANT, account, row.binding_id, row.revision)


def arm(monkeypatch):
    """After setup, any provider/client construction or request fails the test."""
    def explode(*a, **k):
        raise AssertionError("provider or client touched")
    monkeypatch.setattr(NativeProviderModel, "__init__", explode)
    monkeypatch.setattr(NativeProviderConnector, "__init__", explode)
    monkeypatch.setattr(NativeProviderConnector, "verify_identity", explode)
    monkeypatch.setattr(NativeProviderModel, "_ready", explode)
    import httpx
    from curl_cffi import requests
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", explode)
    monkeypatch.setattr(requests, "Session", explode)


def test_verified_matching_binding_with_model_is_configured(database, monkeypatch):
    store, account, payload = verified(database, monkeypatch, model=model())
    for name in ("OPENROUTER_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "environment-fallback-must-be-ignored")
    arm(monkeypatch)
    meta = read(store, account)
    assert set(meta) == KEYS
    assert meta["configured"] is True and meta["accountId"] == account and meta["modelId"] == MODEL
    assert meta["expiresAt"].endswith("Z") and SECRET not in json.dumps(meta)


def test_no_provider_or_client_is_touched(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    arm(monkeypatch)
    assert read(store, account)["configured"] is True


def test_missing_connection_or_wrong_binding_is_unverified(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    with Session(store.engine) as s, s.begin():
        assert reader(s, account, "someone-elses-binding") is None
        assert reader(s, "of-999", s.get(OfRuntime, account).binding_id) is None
        assert reader(s, None, None) is None
        s.get(OfCreatorBinding, s.get(OfRuntime, account).binding_id).revoked_at = utcnow()
    assert read(store, account) is None


def test_connection_row_bound_to_another_binding_is_unverified(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    with Session(store.engine) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        s.add(OfCreatorBinding(id="other-binding", tenant_id=TENANT, creator_id="other-creator",
            creator_name="Other", account_id=account, handle="other", channel="onlyfans",
            revoked_at=utcnow()))
        s.flush()
        assert reader(s, account, "other-binding") is None  # a different (revoked) binding of the same account
        assert reader(s, account, runtime.binding_id)["configured"] is True
        assert binding.account_id == account


def test_unverified_pending_expired_and_paused_connections_are_not_configured(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    for status in ("pending", "failed", "paused", "expired"):
        with Session(store.engine) as s, s.begin():
            s.get(OfProviderConnection, account).status = status
        meta = read(store, account)
        assert set(meta) == KEYS and meta["configured"] is False, status
    with Session(store.engine) as s, s.begin():
        row = s.get(OfProviderConnection, account)
        row.status, row.verified_at = "verified", None
    assert read(store, account)["configured"] is False


def test_lapsed_lease_is_not_configured(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    # Sealed expiry must still match the column, so lapse both through the sealing authority.
    past = (utcnow() - timedelta(minutes=5)).replace(microsecond=0)
    reseal(store, account, lambda v: v.__setitem__("expiresAt", past.isoformat() + "Z"))
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).expires_at = past
    meta = read(store, account)
    assert meta["configured"] is False and set(meta) == KEYS


def test_missing_or_unusable_model_selection_is_not_configured(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=None)
    meta = read(store, account)
    assert meta["configured"] is False and meta["modelId"] == "" and set(meta) == KEYS
    for bad in (model(apiKey=""), model(modelId="not-a-known-model"), {"modelId": MODEL},
                model(apiKey="bad\nkey"), model(extra=1), "string"):
        reseal(store, account, lambda v, bad=bad: v.__setitem__("model", bad))
        assert read(store, account)["configured"] is False, bad
    reseal(store, account, lambda v: v.__setitem__("model", model()))
    assert read(store, account)["configured"] is True


def test_undecryptable_credentials_are_unverified(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    with Session(store.engine) as s, s.begin():
        row = s.get(OfProviderConnection, account)
        row.sealed_config = row.sealed_config[:-4] + ("AAAA" if not row.sealed_config.endswith("AAAA") else "BBBB")
    assert read(store, account) is None


def test_missing_or_wrong_encryption_key_is_unverified_without_env_fallback(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY", "43" * 32)
    assert read(store, account) is None
    monkeypatch.delenv("ONLYFANS_CONNECTION_KEY")
    assert read(store, account) is None


def test_sealed_config_is_bound_to_the_binding_tenant(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    with Session(store.engine) as s, s.begin():
        s.get(OfCreatorBinding, s.get(OfRuntime, account).binding_id).tenant_id = "another-tenant"
    assert read(store, account) is None


def harness(store, monkeypatch, reader_fn):
    from onlyfans_commands.configuration_api import HEADER, SCHEMA
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN", "synthetic-personality-configuration-purpose-token")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT", TENANT)
    app = FastAPI(); app.state.control_store = store
    if reader_fn is not None:
        app.state.personality_credential_status_reader = reader_fn
    app.include_router(personality_api.router)
    headers = {"Authorization": "Bearer synthetic-personality-configuration-purpose-token", HEADER: SCHEMA}
    return app, headers


def import_draft(store, account):
    with Session(store.engine) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        ids = (runtime.binding_id, binding.creator_id)
        s.get(AccountAiConfig, account).model = MODEL
    profile = {"accountId": account, "bindingId": ids[0], "creatorId": ids[1],
        "sourceId": "synthetic-profile", "fields": {"age": "28"}}
    messages = [{"accountId": account, "bindingId": ids[0], "sourceId": "synthetic-message",
        "direction": "out", "body": "hey lovely, how was your afternoon?", "author": "unknown",
        "fanKey": "synthetic-fan", "occurredAt": "2026-10-01T00:00:00Z"}]
    PersonalityWorkflow(store).import_draft(TENANT, account, profile=profile, messages=messages,
        expected_revision=0, command_key="1" * 64, actor=C.ACTOR)


def credential_view(client, headers, account):
    response = client.get(f"/onlyfans/configuration/v1/accounts/{account}/personality", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["personality"]["readiness"]["credential"]


def test_api_readiness_uses_the_reader_and_is_unverified_without_it(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model=model())
    import_draft(store, account)
    arm(monkeypatch)
    app, headers = harness(store, monkeypatch, reader)
    with TestClient(app) as client:
        credential = credential_view(client, headers, account)
        assert credential["metadataAvailable"] is True and credential["configured"] is True
        assert credential["state"] == "ready"
        assert SECRET not in json.dumps(client.get(f"/onlyfans/configuration/v1/accounts/{account}/personality",
            headers=headers).json())
    app, headers = harness(store, monkeypatch, None)
    with TestClient(app) as client:
        credential = credential_view(client, headers, account)
        assert credential["metadataAvailable"] is False and credential["configured"] is False
    # The production app wires the same function.
    import onlyfans_app
    assert onlyfans_app.app.state.personality_credential_status_reader is reader
