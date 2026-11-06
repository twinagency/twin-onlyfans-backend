"""Repair imported native previews independently of vision and chat work.

Only existing verified provider sessions and the permanent ownership receipt
writer are used. No full-video download, model call or provider write is performed.
"""
from .provider_read_budget import is_rate_limit
import json
import os
import threading
from datetime import timezone
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import VaultItem
from .vault_folder_worker import accounts
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from .store import Refused
from .vault_management import _connection
from .connections import _open
from . import vault_previews


class VaultPreviewWorker:
    def __init__(self, store, tenant, stop, *, observe=lambda value: None):
        self.store, self.tenant, self.stop, self.observe = store, tenant, stop, observe
        self.thread = None
        self.cursors = {}

    def start(self):
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("preview warming requires worker role")
        self.thread = threading.Thread(target=self.run, name="native-vault-previews", daemon=True)
        self.thread.start()

    def run(self):
        background_index = 0
        while not self.stop.is_set():
            try:
                scoped_accounts = accounts(self.store, self.tenant)
            except Exception:
                self.stop.wait(15)
                continue
            for account in scoped_accounts:
                if self.stop.is_set():
                    break
                try:
                    self.warm(account, requested_only=True)
                except Exception:
                    # Errors can contain authenticated URLs. Emit only fixed status.
                    self.observe({"schemaVersion": "of-vault-preview.v1", "state": "retry_pending"})
            if scoped_accounts and not self.stop.is_set():
                account=scoped_accounts[background_index % len(scoped_accounts)]
                background_index+=1
                try:
                    self.warm(account)
                except Exception:
                    pass
            self.stop.wait(2)

    def warm(self, account, *, requested_only=False):
        from .provider_connector import NativeProviderConnector, session_from_capture
        from .vault_management import parse_vault_page
        from .provider_read_budget import acquire, current, release
        from of_shapes import still_url
        with Session(self.store.engine) as session, session.begin():
            runtime = self.store._runtime(session, self.tenant, account)
            connection = _connection(session, runtime)
            binding, owner, revision = runtime.binding_id, connection.owner_id, connection.revision
            clear = _open(connection, self.tenant)
            snapshot = session_from_capture(account_id=account, owner_id=owner, policy_revision=1,
                expires_at=connection.expires_at.replace(tzinfo=timezone.utc),
                capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"])
            requested = vault_previews.requested_ids(account,binding,owner)
            from vault_import_policy import automatic_window
            background = [] if requested_only else list(session.scalars(select(VaultItem.media_id).where(
                VaultItem.account_id == account, VaultItem.removed_at.is_(None),
                automatic_window(),
                VaultItem.kind.in_(("photo", "video", "gif")),
                VaultItem.media_id > self.cursors.get(account, 0))
                .order_by(VaultItem.media_id).limit(96)))
            ids = requested + [mid for mid in background if mid not in requested]
        if not ids:
            if not requested_only:self.cursors[account] = 0
            return
        def checkpoint(mid):
            if mid not in requested:self.cursors[account]=mid
        client = None
        attempted = 0
        try:
            for mid in ids:
                if self.stop.is_set():
                    return
                if vault_previews.promote_cached(account, binding, owner, mid):
                    checkpoint(mid)
                    continue
                if attempted >= 4:
                    return
                attempted += 1
                # Refuse noncanonical rows before any provider I/O.
                try:
                    with Session(self.store.engine) as session, session.begin():
                        vault_previews.asset_scope(session, self.store, self.tenant, account, str(mid))
                except Refused:
                    vault_previews.clear_request(account,binding,owner,mid)
                    checkpoint(mid)
                    continue
                if client is None:
                    client = NativeProviderConnector(snapshot, timeout_seconds=8)
                claim = None
                error = None
                try:
                    claim = acquire(self.store, self.tenant, account,
                        connection_revision=revision, kind="vault")
                    media = client.vault_media_by_id(mid)
                    current(self.store, self.tenant, claim)
                except Exception as exc:
                    error = exc
                    raise
                finally:
                    if claim is not None:
                        release(self.store, self.tenant, claim, retry_after=getattr(error, "retry_after", None), rate_limited=is_rate_limit(error))
                if (type(media) is not dict or media.get("id") != mid
                    or media.get("type") not in {"photo", "video", "gif"}
                    or media.get("isDeleted") is True or media.get("isHidden") is True
                    or media.get("canView") is False
                    or "userId" in media and media["userId"] != owner):
                    vault_previews.clear_request(account,binding,owner,mid)
                    checkpoint(mid)
                    continue
                parse_vault_page(json.dumps({"list": [media], "hasMore": False}).encode(), owner_id=owner)
                url = still_url(media)
                try:
                    data = client.still_bytes(url, timeout_seconds=8) if url else None
                except Exception:
                    data = None
                if data is not None:
                    with Session(self.store.engine) as session, session.begin():
                        runtime, row, _, _ = vault_previews.asset_scope(session, self.store, self.tenant, account, str(mid))
                        verified = _connection(session, runtime)
                        if ((runtime.binding_id, verified.owner_id, verified.revision) != (binding, owner, revision)
                            or row.kind != media["type"]):
                            raise Refused("preview scope changed")
                        transport = SimpleNamespace(account_id=account, still_bytes=lambda ignored: data)
                        if vault_previews.warm_page(transport, [media], binding_id=binding, owner_id=owner):
                            vault_previews.clear_request(account,binding,owner,mid)
                checkpoint(mid)
                self.stop.wait(1)
        finally:
            # Connector owns one bounded HTTPS session per request.
            client = None

    def join(self):
        if self.thread is not None:
            self.thread.join(timeout=120)
