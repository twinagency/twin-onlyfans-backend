"""Explicitly approved scheduled-carrier upload for CRM Save (handoff 15.3, option b).

OnlyFans has no unpublished "save to vault" call. The existing native uploader
puts a file in the vault by creating a far-future SCHEDULED post marked "DO NOT
PUBLISH", reading the vault id off it and deleting it; the intent is recorded
before the post exists and a sweep removes any orphan. Ronan approved this path
on 23 Oct 2026 for Save, per account only: an account is eligible only when it
is listed in ONLYFANS_CARRIER_UPLOAD_ACCOUNTS. Unlisted accounts keep the
no-transport default (needs review). This never sends a fan message.

The run state written by vault_upload is the durable receipt: `lookup` reads it
by the asset's upload token, so a lost acknowledgement is reconciled instead of
uploading the file twice.
"""
import os
from datetime import timezone

from .store import Refused

ENV = "ONLYFANS_CARRIER_UPLOAD_ACCOUNTS"


def approved(account):
    return account in {a.strip() for a in os.environ.get(ENV, "").split(",") if a.strip()}


def _run_id(account, upload_token):
    return account + "-native-" + upload_token


class CarrierTransport:
    def __init__(self, connection, tenant, account):
        from .connections import _open
        self.account, self.revision, self.binding = account, connection.revision, connection.binding_id
        self.owner, self.expires = connection.owner_id, connection.expires_at
        self.clear = _open(connection, tenant)

    def _client(self, upload_token):
        from .provider_connector import session_from_capture
        from .vault_upload_transport import NativeVaultUpload
        session = session_from_capture(account_id=self.account, owner_id=self.owner, policy_revision=1,
            capture=self.clear["capture"], x_hash=self.clear["xHash"], proxy_url=self.clear["proxyUrl"],
            expires_at=self.expires.replace(tzinfo=timezone.utc))
        # Authority is the content-sync asset lease, rechecked by its owner after I/O.
        return NativeVaultUpload(session, authorize=lambda: None, job_id=upload_token)

    def lookup(self, *, upload_token):
        import vault_upload
        from .content_sync_media import UploadUnknown
        state = vault_upload.read_state(_run_id(self.account, upload_token))
        if state is None:
            return None
        item = (state.get("items") or [None])[0]
        if item and item.get("status") == "done" and item.get("vault_id"):
            return int(item["vault_id"])
        if state.get("status") == "running" or (item and item.get("carrier_intent") and not item.get("carrier_deleted")):
            raise UploadUnknown("carrier outcome not established")
        return None

    def upload(self, *, upload_token, path, kind):
        import vault_upload
        from .content_sync_media import AuthRequired, TransientUpload, UploadUnknown
        from .provider_connector import NativeAuthenticationError
        run_id = _run_id(self.account, upload_token)
        try:
            client = self._client(upload_token)
            client.transport.verify_identity()
            vault_upload.begin(self.account, run_id=run_id)
            result = vault_upload.start(self.account, client=client, local_paths=[str(path)], run_id=run_id)
        except NativeAuthenticationError:
            raise AuthRequired("connection required") from None
        except Refused:
            raise TransientUpload("upload refused") from None
        except Exception:
            raise UploadUnknown("carrier upload outcome uncertain") from None
        item = (result.get("items") or [None])[0]
        if item and item.get("status") == "done" and item.get("vault_id"):
            return int(item["vault_id"])
        if item and item.get("carrier_intent") and not item.get("carrier_deleted"):
            raise UploadUnknown("carrier left for the sweep")
        raise TransientUpload((item or {}).get("error") or "upload failed")

    def verify(self, *, provider_media_id):
        # Sendability is proven by the account's own vault observation, which
        # content_sync_media requires before marking an asset synced.
        return "ready"


def transport_for(connection, tenant, account):
    if not approved(account):
        return None
    return CarrierTransport(connection, tenant, account)
