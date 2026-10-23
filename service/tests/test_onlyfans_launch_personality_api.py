"""HTTP boundary tests against a stub workflow; no database or provider I/O."""
import importlib
import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


BASE = "/onlyfans/configuration/v1/accounts/synthetic-account/personality"
ACTOR = "op-session:" + "a" * 64
COMMAND = "b" * 64
HEADERS = {"Authorization": "Bearer synthetic-configuration-only-token",
           "X-OnlyFans-Configuration-Version": "of-configuration.v3"}
AGENCY_ACTOR = "agency-user:11111111-1111-1111-1111-111111111111:22222222-2222-2222-2222-222222222222"


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/unused")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN", "synthetic-configuration-only-token")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT", "synthetic-tenant")
    for key in ("ONLYFANS_API_TOKEN", "ONLYFANS_CONTROL_TOKEN", "ONLYFANS_CAMPAIGN_TOKEN",
                "ONLYFANS_CONTENT_TOKEN", "ONLYFANS_CONNECTION_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    api = importlib.import_module("onlyfans_runtime.launch_personality.api")
    from onlyfans_read.agency_scope import AgencyScopeMiddleware
    calls, constructors = [], []

    class Workflow:
        failure = None

        def __init__(self, store, **kwargs):
            constructors.append((store, kwargs))

        def invoke(self, name, *args, **kwargs):
            calls.append((name, args, kwargs, threading.get_ident()))
            if self.failure:
                raise self.failure
            return {"state": "synthetic", "version": "of-personality.v1"}

        def get(self, *args, **kwargs):
            return self.invoke("get", *args, **kwargs)

        def history(self, *args, **kwargs):
            return self.invoke("history", *args, **kwargs)

        def import_existing(self, *args, **kwargs):
            return self.invoke("import_existing", *args, **kwargs)

        def edit(self, *args, **kwargs):
            return self.invoke("edit", *args, **kwargs)

        def approve(self, *args, **kwargs):
            return self.invoke("approve", *args, **kwargs)

    monkeypatch.setattr(api, "PersonalityWorkflow", Workflow)
    app = FastAPI()
    app.state.control_store = object()
    app.add_middleware(AgencyScopeMiddleware)
    app.include_router(api.router)
    app.state.personality_test_event_loop_threads = []

    @app.middleware("http")
    async def observe_thread(request, call_next):
        app.state.personality_test_event_loop_threads.append(threading.get_ident())
        return await call_next(request)

    with TestClient(app) as client:
        yield client, api, app, Workflow, calls, constructors


def mutation(**extra):
    return {"expectedRevision": 0, "commandKey": COMMAND, "actor": ACTOR, **extra}


@pytest.mark.parametrize("suffix,method,kwargs", [
    ("", "get", {}), ("/history", "history", {"before": None}),
    ("/history?before=6", "history", {"before": 6}),
    ("/revisions/3", "get", {"revision": 3}),
])
def test_get_routes_use_existing_configuration_auth_and_contract(harness, suffix, method, kwargs):
    client, api, app, workflow, calls, constructors = harness
    response = client.get(BASE + suffix, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["X-OnlyFans-Configuration-Version"] == "of-configuration.v3"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {"schemaVersion": "of-configuration.v3", "ok": True,
        "accountId": "synthetic-account", "personalityVersion": "of-personality.v1",
        "personality": {"state": "synthetic", "version": "of-personality.v1"}}
    assert calls[0][:3] == (method, ("synthetic-tenant", "synthetic-account"), kwargs)
    assert constructors[0] == (app.state.control_store, {})


@pytest.mark.parametrize("suffix,name,extra", [
    ("/import", "import_existing", {}),
    ("/review", "edit", {"review": {"persona": "Friendly, brief, natural."}}),
    ("/approve", "approve", {"acknowledgements": {"mixedAuthorship": True, "runtimeRules": True, "factsVerified": True}}),
])
def test_post_routes_pass_exact_review_commands(harness, suffix, name, extra):
    client, api, app, workflow, calls, _ = harness
    response = client.post(BASE + suffix, headers=HEADERS, json=mutation(**extra))
    assert response.status_code == 200
    assert response.json()["commandKey"] == COMMAND
    assert calls[0][:3] == (name, ("synthetic-tenant", "synthetic-account"),
        {"expected_revision": 0, "command_key": COMMAND, "actor": ACTOR, **extra})


def test_workflow_is_run_outside_async_event_loop(harness):
    client, api, app, workflow, calls, _ = harness
    assert client.get(BASE, headers=HEADERS).status_code == 200
    assert calls[0][3] != app.state.personality_test_event_loop_threads[0]


def test_only_trusted_state_can_supply_credential_reader(harness):
    client, api, app, workflow, calls, constructors = harness
    reader = lambda *args: None
    app.state.personality_credential_status_reader = reader
    assert client.get(BASE, headers=HEADERS).status_code == 200
    assert constructors[0][1] == {"credential_status_reader": reader}
    response = client.post(BASE + "/import", headers=HEADERS, json=mutation(credentialStatus={"configured": True}))
    assert response.status_code == 400 and len(calls) == 1


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({**HEADERS, "Authorization": "Bearer wrong-token"}, 401),
    ({"Authorization": HEADERS["Authorization"]}, 400),
    ({**HEADERS, "X-OnlyFans-Configuration-Version": "of-personality.v1"}, 400),
])
def test_missing_or_wrong_credential_and_version(harness, headers, status):
    client, _, _, _, calls, _ = harness
    assert client.get(BASE, headers=headers).status_code == status
    assert calls == []


def test_missing_store_or_shared_purpose_credential_is_unavailable(harness, monkeypatch):
    client, _, app, _, calls, _ = harness
    del app.state.control_store
    assert client.get(BASE, headers=HEADERS).status_code == 503
    app.state.control_store = object()
    monkeypatch.setenv("ONLYFANS_CONTROL_TOKEN", "synthetic-configuration-only-token")
    assert client.get(BASE, headers=HEADERS).status_code == 503
    assert calls == []


@pytest.mark.parametrize("suffix", ["/history?before=0", "/history?before=-1", "/history?before=1&before=2",
    "/history?unknown=1", "/history?before=2147483648", "/history?before=true",
    "/revisions/0", "/revisions/-1", "/revisions/no", "/revisions/01", "?before=1"])
def test_invalid_revision_cursor_and_queries_use_generic_envelope(harness, suffix):
    client, _, _, _, calls, _ = harness
    response = client.get(BASE + suffix, headers=HEADERS)
    assert response.status_code == 400 and response.json()["code"] == "invalid_configuration"
    assert calls == []


@pytest.mark.parametrize("payload", [
    mutation(profile={"bio": "untrusted"}), mutation(messages=[]), mutation(apiKey="do-not-echo-secret"),
    mutation(unknown=True), mutation(actor="forged"), mutation(actor=12), mutation(expectedRevision=True),
    mutation(expectedRevision=-1), mutation(commandKey="invalid"), [], {"actor": ACTOR},
])
def test_exact_fields_and_command_identity(harness, payload):
    client, _, _, _, calls, _ = harness
    response = client.post(BASE + "/import", headers=HEADERS, json=payload)
    assert response.status_code == 400
    assert "do-not-echo-secret" not in response.text and calls == []


@pytest.mark.parametrize("raw", [
    '{"actor":"first","actor":"second"}',
    '{"review":{"persona":"first","persona":"second"},"expectedRevision":0,"commandKey":"' + COMMAND + '","actor":"' + ACTOR + '"}',
    '{"expectedRevision":NaN,"commandKey":"' + COMMAND + '","actor":"' + ACTOR + '"}',
    '{"expectedRevision":Infinity,"commandKey":"' + COMMAND + '","actor":"' + ACTOR + '"}',
    '{broken', 'null',
])
def test_duplicate_keys_and_nonfinite_and_invalid_json_rejected(harness, raw):
    client, _, _, _, calls, _ = harness
    response = client.post(BASE + "/import", headers={**HEADERS, "Content-Type": "application/json"}, content=raw)
    assert response.status_code == 400 and calls == []


def test_body_content_type_and_size_budget(harness):
    client, _, _, _, calls, _ = harness
    assert client.post(BASE + "/import", headers=HEADERS, content=json.dumps(mutation())).status_code == 400
    response = client.post(BASE + "/import", headers={**HEADERS, "Content-Type": "application/json"}, content=" " * 65537)
    assert response.status_code == 413 and calls == []


@pytest.mark.parametrize("exception,status,code", [
    ("invalid", 400, "invalid_configuration"), ("unavailable", 404, "not_available"),
    ("conflict", 409, "personality_conflict"), ("unknown", 500, "outcome_unconfirmed"),
])
def test_internal_errors_have_no_private_details(harness, exception, status, code):
    client, api, _, workflow, _, _ = harness
    workflow.failure = {"invalid": api.InvalidConfiguration("private provider secret"),
        "unavailable": api.Refused("personality unavailable"),
        "conflict": api.Refused("private provider secret"),
        "unknown": RuntimeError("private provider secret")}[exception]
    response = client.post(BASE + "/import", headers=HEADERS, json=mutation())
    assert response.status_code == status and response.json()["code"] == code
    assert "private provider secret" not in response.text


def test_agency_namespace_is_recognized_and_readonly_cannot_write(harness):
    client, _, _, _, calls, _ = harness
    scope = {"creatorIds": ["33333333-3333-3333-3333-333333333333"], "actor": AGENCY_ACTOR, "canManage": False}
    headers = {**HEADERS, "x-onlyfans-agency-scope": json.dumps(scope)}
    assert client.get(BASE, headers=headers).status_code == 200
    response = client.post(BASE + "/import", headers=headers, json=mutation(actor=AGENCY_ACTOR))
    assert response.status_code == 403 and len(calls) == 1
    scope["canManage"] = True
    headers["x-onlyfans-agency-scope"] = json.dumps(scope)
    assert client.post(BASE + "/import", headers=headers, json=mutation(actor=ACTOR)).status_code == 400
    assert client.post(BASE + "/import", headers=headers, json=mutation(actor=AGENCY_ACTOR)).status_code == 200


def test_arbitrary_profile_messages_or_raw_endpoint_does_not_exist(harness):
    client, _, _, _, calls, _ = harness
    for suffix in ("/profile", "/messages", "/raw", "/export"):
        assert client.get(BASE + suffix, headers=HEADERS).status_code == 404
    assert calls == []
