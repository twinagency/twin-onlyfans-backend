"""The agency-facing surface of the history import: coverage, ownership drill-down and idempotent commands.

Real PostgreSQL behind the existing configuration-purpose credential; no new credential, no provider, no model.
"""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT, ACTOR, config
from history_support import Provider, card, chat_page, fan_message, key, prepared, run, spec, tx_row
from test_history_import_pg import cursor_of, drive, gaps_of, hist
from test_history_coverage_pg import read_everything

TOKEN = "fixture-history-token"
FAN = 401


@pytest.fixture
def api(database, monkeypatch):
    from onlyfans_commands.configuration_api import router, HEADER, SCHEMA
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN", TOKEN)
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT", TENANT)
    store, account, cfg = prepared(database, begin=False)
    app = FastAPI()
    app.state.control_store = store
    app.include_router(router)
    client = TestClient(app)
    base = "/onlyfans/configuration/v1/accounts/" + account
    headers = {"Authorization": "Bearer " + TOKEN, HEADER: SCHEMA}
    yield client, base, headers, store, account, cfg
    store.close()


def post(client, base, headers, body):
    return client.post(base + "/history-import", headers=headers, json=body)


def test_the_surface_requires_the_configuration_credential_and_contract_version(api):
    client, base, headers, store, account, cfg = api
    for url in (base + "/history-coverage", base + "/ownership/401?mediaIds=1"):
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer wrong", **{k: v for k, v in headers.items() if k != "Authorization"}}).status_code == 401
        assert client.get(url, headers={"Authorization": headers["Authorization"]}).status_code == 400
    assert client.post(base + "/history-import", json={}).status_code == 401
    assert client.get("/onlyfans/configuration/v1/accounts/another-account/history-coverage", headers=headers).status_code == 404


def test_coverage_reads_pending_before_an_import_and_the_derived_state_after(api, database):
    client, base, headers, store, account, cfg = api
    assert client.get(base + "/history-coverage", headers=headers).json()["coverage"] == {"configured": False}
    started = post(client, base, headers, {"action": "configure", "expectedRevision": 0, "commandKey": key(1), "actor": ACTOR,
                                           "reason": "Start the import", "spec": spec()})
    assert started.status_code == 200 and started.json()["history"]["revision"] == 1 and started.json()["history"]["replayed"] is False
    replay = post(client, base, headers, {"action": "configure", "expectedRevision": 0, "commandKey": key(1), "actor": ACTOR,
                                          "reason": "Start the import", "spec": spec()})
    assert replay.status_code == 200 and replay.json()["history"]["replayed"] is True
    coverage = client.get(base + "/history-coverage", headers=headers).json()["coverage"]
    assert coverage["configured"] is True and coverage["state"] == "unknown" and coverage["fans"]["known"] == 0
    assert set(coverage["sources"]) == {"vault_window", "chats", "transactions", "chargebacks"}
    assert client.get(base + "/history-coverage", headers=headers).headers["cache-control"] == "private, no-store"


def test_configure_refusals_and_malformed_requests_get_the_house_error_shapes(api):
    client, base, headers, store, account, cfg = api
    good = {"action": "configure", "expectedRevision": 0, "commandKey": key(2), "actor": ACTOR, "reason": "Start", "spec": spec()}
    assert post(client, base, headers, {**good, "action": "explode"}).status_code == 400
    assert post(client, base, headers, {k: v for k, v in good.items() if k != "spec"}).status_code == 400
    assert post(client, base, headers, {**good, "extra": 1}).status_code == 400
    assert client.post(base + "/history-import", headers=headers, content=b"{not json", ).status_code == 400
    assert post(client, base, headers, {**good, "actor": "nobody"}).status_code == 403
    assert post(client, base, headers, {**good, "spec": {**good["spec"], "start_date": "tomorrow"}}).status_code == 409
    assert post(client, base, headers, {**good, "expectedRevision": 5}).status_code == 409
    assert post(client, base, headers, {**good, "reason": "x" * 600}).status_code == 409
    body = post(client, base, headers, {**good, "spec": {**good["spec"], "start_date": "tomorrow"}}).json()
    assert body["ok"] is False and body["code"] == "history_import_refused" and "tomorrow" not in json.dumps(body)


def test_gap_commands_over_http_are_idempotent_and_refuse_the_wrong_kind(api, database):
    client, base, headers, store, account, cfg = api
    assert post(client, base, headers, {"action": "configure", "expectedRevision": 0, "commandKey": key(3), "actor": ACTOR,
                                        "reason": "Start", "spec": spec()}).status_code == 200
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(8001, FAN), fan_message(8002, FAN, price=4)])]
    read_everything(store, database, account, wire)
    gap = gaps_of(database, account, kind="row")[0]
    page = {"action": "retry-gap", "commandKey": key(4), "actor": ACTOR, "reason": "x", "gapId": gap.gap_id}
    assert post(client, base, headers, page).status_code == 409, "a row gap is dismissed by review, not retried"
    dismiss = {"action": "dismiss-gap", "commandKey": key(5), "actor": ACTOR, "reason": "Reviewed", "gapId": gap.gap_id}
    first = post(client, base, headers, dismiss)
    assert first.status_code == 200 and first.json()["history"]["outcome"] == "reviewed"
    assert post(client, base, headers, dismiss).json()["history"]["replayed"] is True
    assert post(client, base, headers, {**dismiss, "commandKey": key(6)}).status_code == 409
    assert post(client, base, headers, {**dismiss, "gapId": "z" * 40}).status_code == 409
    after = client.get(base + "/history-coverage", headers=headers).json()["coverage"]
    assert after["gaps"]["openRows"] == 0 and after["fans"]["complete"] == 1


def test_the_ownership_drill_down_answers_per_media_with_reasons_and_never_says_unowned(api, database):
    client, base, headers, store, account, cfg = api
    post(client, base, headers, {"action": "configure", "expectedRevision": 0, "commandKey": key(7), "actor": ACTOR, "reason": "Start", "spec": spec()})
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([card(8101, cfg, [111, 112], 15, previews=[111], opened=True), card(8102, cfg, [121], 15, opened=True)])]
    read_everything(store, database, account, wire)
    answer = client.get(base + "/ownership/401?mediaIds=111,112,121,131", headers=headers)
    assert answer.status_code == 200
    body = answer.json()
    assert {m["mediaId"]: m["state"] for m in body["ownership"]["media"]} == {111: "clear_as_of", 112: "owned", 121: "unknown", 131: "clear_as_of"}
    assert body["ownership"]["fan"]["state"] == "complete" and "unowned" not in json.dumps(body)
    reasons = {m["mediaId"]: m["reasons"] for m in body["ownership"]["media"]}
    assert reasons[121] == ["previews_unknown"] and body["ownership"]["media"][0]["asOf"] is not None
    for bad in ("", "?mediaIds=", "?mediaIds=a,b", "?mediaIds=" + ",".join(str(i) for i in range(1, 52)), "?mediaIds=1&mediaIds=2", "?mediaIds=1&x=2"):
        assert client.get(base + "/ownership/401" + bad, headers=headers).status_code == 400, bad
    assert client.get(base + "/ownership/not-a-fan?mediaIds=1", headers=headers).status_code == 400
    assert client.get(base + "/ownership/999?mediaIds=1", headers=headers).json()["ownership"]["media"][0]["state"] == "unknown"
