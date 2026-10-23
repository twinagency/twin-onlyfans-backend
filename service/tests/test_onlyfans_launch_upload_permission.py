"""Scoped current-role metadata, using a synthetic private HTTP response."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from onlyfans_runtime.upload_permission import UploadPermissionReader
from onlyfans_runtime.creator_facts import TwinFactsReader

ACTOR = "op-session:" + "a" * 64
SCOPE = dict(tenant_id="tenant-a", account_id="of-101", binding_id="binding-a",
    creator_id="creator-a", actor=ACTOR, permission="content.upload")


def test_permission_is_rechecked_and_revocation_denies_without_exposing_key(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    reader = UploadPermissionReader("http://127.0.0.1:3000", "synthetic-private-" + "x" * 40)
    calls = []
    state = {"permitted": True}
    def get(source, url):
        calls.append(url)
        return json.dumps({"version": "of-upload-permission.v1", "actor": ACTOR,
            "creatorId": "creator-a", "permitted": state["permitted"],
            "observedAt": datetime.now(timezone.utc).isoformat()}).encode()
    monkeypatch.setattr(TwinFactsReader, "_get_once", get)
    assert reader(**SCOPE) is True
    state["permitted"] = False
    assert reader(**SCOPE) is False
    assert len(calls) == 2 and all("synthetic-private" not in url for url in calls)


@pytest.mark.parametrize("change", [
    {"actor": "op-session:" + "b" * 64}, {"creatorId": "another-creator"},
    {"permitted": "true"}, {"extra": True}, {"version": "unknown"},
    {"observedAt": "invalid"},
    {"observedAt": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
])
def test_wrong_scope_invalid_or_stale_authority_never_permits(monkeypatch, change):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    reader = UploadPermissionReader("http://127.0.0.1:3000", "x" * 40)
    payload = {"version": "of-upload-permission.v1", "actor": ACTOR, "creatorId": "creator-a",
        "permitted": True, "observedAt": datetime.now(timezone.utc).isoformat(), **change}
    monkeypatch.setattr(TwinFactsReader, "_get_once", lambda *args: json.dumps(payload).encode())
    assert reader(**SCOPE) is False


def test_permission_authority_outage_or_wrong_role_is_closed(monkeypatch):
    reader = UploadPermissionReader("http://127.0.0.1:3000", "x" * 40)
    def unavailable(*args):
        raise TimeoutError("synthetic outage")
    monkeypatch.setattr(TwinFactsReader, "_get_once", unavailable)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    assert reader(**SCOPE) is False
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    assert reader(**SCOPE) is False
