"""Approved scheduled-carrier upload: per-account opt-in and receipt-based reconciliation.

No provider request: vault_upload's run functions and the client are faked.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from onlyfans_runtime import content_sync_carrier as carrier
from onlyfans_runtime.content_sync_media import AuthRequired, TransientUpload, UploadUnknown
from onlyfans_runtime.provider_connector import NativeAuthenticationError

TOKEN = "0f0e0d0c-0b0a-4908-8706-050403020100"
CONNECTION = SimpleNamespace(revision=2, binding_id="binding-a", owner_id=7001, expires_at=datetime(2030, 1, 1))


def test_only_listed_accounts_get_the_carrier_transport(monkeypatch):
    monkeypatch.delenv(carrier.ENV, raising=False)
    assert carrier.transport_for(CONNECTION, "tenant-a", "acc-a") is None
    monkeypatch.setenv(carrier.ENV, "acc-b, acc-c")
    assert carrier.transport_for(CONNECTION, "tenant-a", "acc-a") is None
    monkeypatch.setattr(carrier.CarrierTransport, "__init__", lambda self, *a: None)
    assert isinstance(carrier.transport_for(CONNECTION, "tenant-a", "acc-c"), carrier.CarrierTransport)


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr("onlyfans_runtime.connections._open", lambda row, tenant: {"capture": {}, "xHash": "x", "proxyUrl": None})
    t = carrier.CarrierTransport(CONNECTION, "tenant-a", "acc-a")
    client = SimpleNamespace(transport=SimpleNamespace(verify_identity=lambda: None))
    monkeypatch.setattr(t, "_client", lambda token: client)
    return t


def state(run="done", item=None):
    return {"status": run, "items": [item] if item else []}


def test_lookup_reconciles_from_the_durable_run_receipt(transport, monkeypatch):
    import vault_upload
    runs = {}
    monkeypatch.setattr(vault_upload, "read_state", lambda run_id: runs.get(run_id))
    run = "acc-a-native-" + TOKEN
    assert transport.lookup(upload_token=TOKEN) is None, "no run: never landed"
    runs[run] = state("done", {"status": "done", "vault_id": 555})
    assert transport.lookup(upload_token=TOKEN) == 555, "a lost acknowledgement is recovered, not re-uploaded"
    runs[run] = state("running", {"status": "uploading"})
    with pytest.raises(UploadUnknown):
        transport.lookup(upload_token=TOKEN)
    runs[run] = state("interrupted", {"status": "failed", "carrier_intent": {"marker": "m"}, "carrier_deleted": False})
    with pytest.raises(UploadUnknown):
        transport.lookup(upload_token=TOKEN)
    runs[run] = state("failed", {"status": "failed", "carrier_intent": {"marker": "m"}, "carrier_deleted": True})
    assert transport.lookup(upload_token=TOKEN) is None, "a cleaned-up failed carrier can be retried"


def test_upload_maps_runner_outcomes_without_hiding_uncertainty(transport, monkeypatch):
    import vault_upload
    calls = []
    monkeypatch.setattr(vault_upload, "begin", lambda account, run_id: calls.append(("begin", account, run_id)) or run_id)
    outcome = {}
    monkeypatch.setattr(vault_upload, "start", lambda account, **kw: calls.append(("start", kw["local_paths"], kw["run_id"])) or outcome["value"])
    outcome["value"] = state("done", {"status": "done", "vault_id": 901})
    assert transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo") == 901
    assert calls[:2] == [("begin", "acc-a", "acc-a-native-" + TOKEN), ("start", ["/spool/a.jpg"], "acc-a-native-" + TOKEN)]
    outcome["value"] = state("done", {"status": "failed", "error": "too_large"})
    with pytest.raises(TransientUpload):
        transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo")
    outcome["value"] = state("done", {"status": "failed", "carrier_intent": {"marker": "m"}, "carrier_deleted": False})
    with pytest.raises(UploadUnknown):
        transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo")
    def boom(account, **kw):
        raise NativeAuthenticationError("rejected")
    monkeypatch.setattr(vault_upload, "start", boom)
    with pytest.raises(AuthRequired):
        transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo")
    monkeypatch.setattr(vault_upload, "start", lambda account, **kw: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(UploadUnknown):
        transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo")
