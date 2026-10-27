"""Carrier authority is checked at each external boundary, including cleanup."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_offer_import_pg import ready  # noqa: F401
from test_onlyfans_content_sync_pg import synced
from test_onlyfans_connections_pg import TENANT, ACTOR
from db.models import OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_offer_import_models import OfContentSyncAsset as Asset, OfContentSyncOperation as Operation
from onlyfans_runtime import content_sync_media as media, content_sync_carrier as carrier
from onlyfans_runtime.store import Refused, utcnow


def leased(store, account):
    identity, token = str(uuid.uuid4()), str(uuid.uuid4())
    with Session(store.engine) as s, s.begin():
        binding = s.get(OfRuntime, account).binding_id
        now = utcnow()
        s.add(Asset(account_id=account, asset_id=identity, binding_id=binding, kind="photo", name="synthetic.jpg",
            size_bytes=3, content_hash=uuid.uuid4().hex * 2, upload_token=token, state="queued", attempts=0,
            created_at=now, updated_at=now))
        s.add(Operation(account_id=account, operation_id=str(uuid.uuid4()), binding_id=binding,
            command_key=uuid.uuid4().hex * 2, request_hash="d" * 64, actor=ACTOR, kind="upload", status="queued",
            asset_id=identity, coverage_json="{}", result_json="{}", attempts=0, created_at=now, updated_at=now))
    return media._lease(store, TENANT, account, identity, utcnow())


def test_current_staff_permission_role_asset_and_binding_are_rechecked(synced, monkeypatch):
    store, account = synced
    lease = leased(store, account)
    calls = []
    store.content_upload_permission_reader = lambda **kw: calls.append(kw) or True
    media._authorize_upload_boundary(store, TENANT, lease)
    assert calls[-1]["actor"] == ACTOR and calls[-1]["permission"] == "content.upload"
    assert calls[-1]["binding_id"] == lease.binding
    store.content_upload_permission_reader = lambda **kw: False
    with pytest.raises(Refused, match="permission unavailable"):
        media._authorize_upload_boundary(store, TENANT, lease)
    store.content_upload_permission_reader = lambda **kw: True
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    with pytest.raises(Refused, match="worker role"):
        media._authorize_upload_boundary(store, TENANT, lease)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    with pytest.raises(Refused, match="scope changed"):
        media._authorize_upload_boundary(store, TENANT, replace(lease, upload_token=str(uuid.uuid4())))
    with Session(store.engine) as s, s.begin():
        s.get(OfCreatorBinding, lease.binding).revoked_at = utcnow()
    with pytest.raises(Refused):
        media._authorize_upload_boundary(store, TENANT, lease)


def test_expired_upload_lease_cannot_authorize_or_commit(synced):
    store, account = synced
    lease = leased(store, account)
    store.content_upload_permission_reader = lambda **kw: True
    with Session(store.engine) as s, s.begin():
        s.get(Asset, (account, lease.asset_id)).lease_until = utcnow() - timedelta(seconds=1)
    with pytest.raises(Refused, match="scope changed"):
        media._authorize_upload_boundary(store, TENANT, lease)
    assert media._commit(store, TENANT, lease, lambda *a: pytest.fail("expired completion applied")) is False


def test_connection_recheck_hold_blocks_new_upload_and_existing_authority(synced):
    from db.onlyfans_connection_models import OfProviderConnection
    store, account = synced
    lease = leased(store, account)
    store.content_upload_permission_reader = lambda **kw: True
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).last_error = "provider_connection_check_retry"
    with pytest.raises(Refused, match="scope changed"):
        media._authorize_upload_boundary(store, TENANT, lease)
    media._commit(store, TENANT, lease, lambda *a: pytest.fail("held connection completion applied"))
    with Session(store.engine) as s:
        asset = s.get(Asset, (account, lease.asset_id))
        assert asset.state == "connection_required" and asset.last_error == "connection_required"
    assert leased(store, account) == 1, "a new upload parks until connection verification is current"


def test_cleanup_authentication_loss_persists_connection_required(synced):
    store, account = synced
    lease = leased(store, account)
    transport = SimpleNamespace(lookup=lambda **kw: (_ for _ in ()).throw(media.UploadUnknown()),
        cleanup=lambda **kw: (_ for _ in ()).throw(media.AuthRequired()))
    assert media._step(store, TENANT, replace(lease, started=True), lambda *a: transport, None)
    with Session(store.engine) as s:
        asset = s.get(Asset, (account, lease.asset_id))
        assert asset.state == "connection_required" and asset.last_error == "connection_required"
        assert asset.lease_token is None and asset.provider_media_id is None


def test_carrier_authorizer_is_live_for_every_adapter_boundary(monkeypatch):
    from onlyfans_runtime.content_sync_media import PermissionRequired
    from onlyfans_runtime.vault_upload_transport import NativeVaultUpload
    monkeypatch.setenv(carrier.ENV, "acc-a")
    t = object.__new__(carrier.CarrierTransport)
    t.account, t._authority, t._token, t._media = "acc-a", None, None, None
    with pytest.raises(PermissionRequired):
        t._authorize()
    allowed = {"value": True}
    t.set_authority(lambda: None if allowed["value"] else (_ for _ in ()).throw(Refused("revoked")),
        upload_token="token-a", provider_media_id=17)
    t._authorize(upload_token="token-a")
    with pytest.raises(PermissionRequired, match="asset authority"):
        t._authorize(upload_token="token-b")
    with pytest.raises(PermissionRequired, match="asset authority"):
        t.verify(provider_media_id=18)
    adapter = object.__new__(NativeVaultUpload)
    adapter.transport = SimpleNamespace(_ready=lambda: None, _handle=SimpleNamespace(current=lambda: None))
    adapter.authorize = t._authorize
    adapter._require_upload_transport()
    allowed["value"] = False
    with pytest.raises(PermissionRequired, match="permission changed"):
        adapter._require_upload_transport()
    with pytest.raises(PermissionRequired):
        t.cleanup(upload_token="token-a")


def test_cleanup_requires_current_authority_and_retains_pending_write_evidence(monkeypatch):
    import vault_upload
    from onlyfans_runtime.content_sync_media import UploadUnknown
    monkeypatch.setenv(carrier.ENV, "acc-a")
    t = object.__new__(carrier.CarrierTransport)
    t.account, t._authority, t._token, t._media = "acc-a", None, None, None
    t.set_authority(lambda: None, upload_token="token-a")
    state = {"account_id": "acc-a", "status": "interrupted", "items": [{
        "carrier_intent": {"marker": "m"}, "carrier_write_pending": True, "carrier_deleted": False}]}
    monkeypatch.setattr(t, "_states", lambda token: [(0, state)])
    monkeypatch.setattr(vault_upload, "read_state", lambda run: state if run == carrier._run_id("acc-a", "token-a") else None)
    saved = []
    monkeypatch.setattr(vault_upload, "_save", lambda s: saved.append(s))
    calls = []
    client = SimpleNamespace(_posts=set(), _VAULT_MARKER="m", transport=SimpleNamespace(verify_identity=lambda: calls.append("identity")))
    monkeypatch.setattr(t, "_client", lambda token: client)
    monkeypatch.setattr(vault_upload, "_cleanup_carriers", lambda *a: calls.append("cleanup"))
    monkeypatch.setattr(vault_upload, "_sweep_scheduled", lambda *a, **kw: (0, 0, True))
    with pytest.raises(UploadUnknown):
        t.cleanup(upload_token="token-a")
    assert saved and state["items"][0]["carrier_write_pending"] is True
    assert not state["items"][0].get("carrier_scan_clear"), "a scan cannot settle a pending provider write"
    assert calls == ["identity", "cleanup"]


def test_vault_id_does_not_override_unresolved_carrier_cleanup(monkeypatch):
    import vault_upload
    from onlyfans_runtime.content_sync_media import UploadUnknown
    t = object.__new__(carrier.CarrierTransport)
    t.account, t._authority, t._token, t._media = "acc-a", None, None, None
    state = {"account_id": "acc-a", "status": "done", "items": [{"status": "done", "vault_id": 17,
        "carrier_post_id": 901, "carrier_deleted": False}]}
    monkeypatch.setattr(t, "_states", lambda token: [(0, state)])
    monkeypatch.setattr(vault_upload, "read_state", lambda run: state if run == carrier._run_id("acc-a", "token-a") else None)
    with pytest.raises(UploadUnknown, match="cleanup"):
        t.lookup(upload_token="token-a")
    state["items"][0]["carrier_deleted"] = True
    assert t.lookup(upload_token="token-a") == 17
    state["items"][0]["carrier_write_pending"] = True
    with pytest.raises(UploadUnknown):
        t.lookup(upload_token="token-a")
