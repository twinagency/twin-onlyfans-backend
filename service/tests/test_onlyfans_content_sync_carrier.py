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
    monkeypatch.setenv(carrier.ENV, "acc-a")
    monkeypatch.setattr("onlyfans_runtime.connections._open", lambda row, tenant: {"capture": {}, "xHash": "x", "proxyUrl": None})
    t = carrier.CarrierTransport(CONNECTION, "tenant-a", "acc-a")
    t.set_authority(lambda: None, upload_token=TOKEN)
    client = SimpleNamespace(transport=SimpleNamespace(verify_identity=lambda: None))
    monkeypatch.setattr(t, "_client", lambda token, attempt=0: client)
    return t


def state(run="done", item=None):
    return {"account_id": "acc-a", "status": run, "items": [item] if item else []}


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
    runs[run] = state("failed", {"status": "failed", "carrier_intent": {"marker": "m"},
        "carrier_deleted": True, "carrier_scan_clear": True})
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


def test_clean_failed_receipt_uses_new_child_run_and_preserves_lost_ack(transport, monkeypatch):
    import vault_upload
    first = carrier._run_id("acc-a", TOKEN)
    child = carrier._run_id("acc-a", TOKEN, 1)
    failed = state("failed", {"status": "failed", "carrier_intent": {"marker": "m"},
        "carrier_deleted": True, "carrier_scan_clear": True})
    runs, calls = {first: failed}, []
    monkeypatch.setattr(vault_upload, "read_state", lambda run: runs.get(run))
    def begin(account, run_id):
        assert run_id not in runs, "an existing run receipt must never be restarted"
        calls.append(("begin", run_id))
        runs[run_id] = state("running")
    def start(account, **kw):
        calls.append(("start", kw["run_id"]))
        runs[kw["run_id"]] = state("done", {"status": "done", "vault_id": 901})
        return runs[kw["run_id"]]
    monkeypatch.setattr(vault_upload, "begin", begin)
    monkeypatch.setattr(vault_upload, "start", start)
    assert transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo") == 901
    assert calls == [("begin", child), ("start", child)]
    assert runs[first] is failed and runs[first]["status"] == "failed"
    # A crash after the child landed is reconciled across all attempts. The
    # second call must not begin a third run or repeat any external write.
    assert transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo") == 901
    assert len(calls) == 2


@pytest.mark.parametrize("uncertain", [
    state("running"),
    state("interrupted", {"carrier_intent": {"marker": "m"}, "carrier_write_pending": True}),
    state("done", {"vault_id": 901, "carrier_post_id": 19, "carrier_deleted": False}),
])
def test_any_earlier_uncertain_attempt_blocks_a_new_write(transport, monkeypatch, uncertain):
    import vault_upload
    runs = {carrier._run_id("acc-a", TOKEN): uncertain,
        carrier._run_id("acc-a", TOKEN, 1): state("done", {"vault_id": 901, "status": "done"})}
    monkeypatch.setattr(vault_upload, "read_state", lambda run: runs.get(run))
    monkeypatch.setattr(vault_upload, "begin", lambda *a, **kw: pytest.fail("uncertain receipt repeated a write"))
    with pytest.raises(UploadUnknown):
        transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo")


def test_conflicting_prior_media_and_exhausted_receipts_require_review(transport, monkeypatch):
    import vault_upload
    runs = {carrier._run_id("acc-a", TOKEN, i): state("done", {"vault_id": 900 + i}) for i in range(2)}
    monkeypatch.setattr(vault_upload, "read_state", lambda run: runs.get(run))
    with pytest.raises(UploadUnknown, match="conflicting"):
        transport.lookup(upload_token=TOKEN)
    runs.clear()
    runs.update({carrier._run_id("acc-a", TOKEN, i): state("failed") for i in range(carrier.MAX_RUN_ATTEMPTS)})
    with pytest.raises(TransientUpload, match="attempts exhausted"):
        transport.upload(upload_token=TOKEN, path=Path("/spool/a.jpg"), kind="photo")


def test_child_carrier_markers_are_deterministic_and_isolated(transport, monkeypatch):
    from onlyfans_runtime import provider_connector, vault_upload_transport
    monkeypatch.setattr(provider_connector, "session_from_capture", lambda **kw: None)
    monkeypatch.setattr(vault_upload_transport, "NativeVaultUpload", lambda session, **kw: kw)
    # Bypass only the fake client installed by the fixture so the real marker
    # derivation is exercised without opening a provider session.
    a = carrier.CarrierTransport._client(transport, TOKEN, 1)
    b = carrier.CarrierTransport._client(transport, TOKEN, 1)
    c = carrier.CarrierTransport._client(transport, TOKEN, 2)
    assert a["job_id"] == b["job_id"] and a["job_id"] != c["job_id"] != TOKEN
    import vault_upload
    assert all(vault_upload._RUN_ID_RE.fullmatch(carrier._run_id("acc-a", TOKEN, i))
        for i in range(carrier.MAX_RUN_ATTEMPTS)), "every retry must satisfy the real durable uploader run guard"


def test_cleanup_authentication_failure_is_classified(transport, monkeypatch):
    import vault_upload
    monkeypatch.setattr(vault_upload, "read_state", lambda run: state("failed") if run == carrier._run_id("acc-a", TOKEN) else None)
    client = SimpleNamespace(transport=SimpleNamespace(verify_identity=lambda: (_ for _ in ()).throw(NativeAuthenticationError("rejected"))))
    monkeypatch.setattr(transport, "_client", lambda *a: client)
    with pytest.raises(AuthRequired):
        transport.cleanup(upload_token=TOKEN)


def test_corrupt_earlier_receipt_blocks_accepting_a_later_landed_id(transport, monkeypatch):
    import vault_upload
    monkeypatch.setattr(vault_upload, "read_state", lambda run: state("done", {"vault_id": 901})
        if run == carrier._run_id("acc-a", TOKEN, 1) else None)
    monkeypatch.setattr(vault_upload, "_state_path", lambda run: SimpleNamespace(
        exists=lambda: run == carrier._run_id("acc-a", TOKEN)))
    with pytest.raises(UploadUnknown, match="receipt unavailable"):
        transport.lookup(upload_token=TOKEN)
