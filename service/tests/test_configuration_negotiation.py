"""HTTP compatibility for old dashboards and independently negotiated controls."""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from onlyfans_commands import configuration_api as api

@pytest.mark.parametrize("query,fields", [
    ("", set()),
    ("?activation=1", set()),
    ("?contextual=1", {"fasttContextualSalesMode"}),
    ("?replyPolicy=1", {"nativeReplyPacingEnabled", "nativeReplyFormatEnabled"}),
    ("?activation=1&contextual=1&replyPolicy=1", {"fasttContextualSalesMode", "nativeReplyPacingEnabled", "nativeReplyFormatEnabled"}),
    ("?contextual=true&replyPolicy=true", set()),
])
def test_old_and_new_dashboards_negotiate_independent_fields(monkeypatch, query, fields):
    stored = {"configurationRevision": 9, "fingerprint": "a"*64,
              "onRequestSalesEnabled": True, "fasttContextualSalesMode": "shadow",
              "nativeReplyPacingEnabled": True, "nativeReplyFormatEnabled": False}
    original = deepcopy(stored)
    def read(store, tenant, account_id, *, include_activation):
        assert tenant == "synthetic" and account_id == "acc-a"
        assert include_activation == ("activation=1" in query)
        return deepcopy(stored)
    monkeypatch.setattr(api, "authenticate", lambda *args: (object(), "synthetic"))
    monkeypatch.setattr(api, "get_configuration", read)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        result = client.get(api.PREFIX + "/accounts/acc-a" + query)
    assert result.status_code == 200
    body = result.json()
    expected = {"configurationRevision", "fingerprint", "onRequestSalesEnabled"} | fields
    assert set(body["configuration"]) == expected
    assert body["configuration"]["onRequestSalesEnabled"] is True
    for key in fields:
        assert body["configuration"][key] == stored[key]
    assert stored == original
    assert result.headers[api.HEADER] == api.SCHEMA
    assert result.headers["cache-control"] == "private, no-store"
