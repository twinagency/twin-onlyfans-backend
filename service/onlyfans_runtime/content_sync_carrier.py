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
import uuid
from datetime import timezone

from .store import Refused

ENV = "ONLYFANS_CARRIER_UPLOAD_ACCOUNTS"
MAX_RUN_ATTEMPTS = 6


def approved(account):
    return account in {a.strip() for a in os.environ.get(ENV, "").split(",") if a.strip()}


def _attempt_token(upload_token, attempt):
    if attempt == 0:
        return upload_token
    derived = uuid.uuid5(uuid.UUID(upload_token), str(attempt))
    # Keep the existing uploader's deliberately narrow UUIDv4 run grammar.
    # Version bits do not make this a new random token: retries deterministically
    # recover the same child receipt and scheduled-carrier marker.
    return str(uuid.UUID(bytes=derived.bytes, version=4))


def _run_id(account, upload_token, attempt=0):
    return account + "-native-" + _attempt_token(upload_token, attempt)


class CarrierTransport:
    def __init__(self, connection, tenant, account):
        from .connections import _open
        self.account, self.revision, self.binding = account, connection.revision, connection.binding_id
        self.owner, self.expires = connection.owner_id, connection.expires_at
        self.clear = _open(connection, tenant)
        self._authority = None
        self._token, self._media = None, None

    def set_authority(self, authority, *, upload_token=None, provider_media_id=None):
        if not callable(authority):
            raise Refused("carrier upload authority required")
        self._authority = authority
        self._token, self._media = upload_token, provider_media_id

    def _authorize(self, *, upload_token=None, provider_media_id=None):
        from .content_sync_media import PermissionRequired
        if not approved(self.account) or not callable(self._authority):
            raise PermissionRequired("carrier upload authority required")
        if ((upload_token is not None and self._token is not None and upload_token != self._token)
                or (provider_media_id is not None and provider_media_id != self._media)):
            raise PermissionRequired("carrier upload asset authority changed")
        try:
            self._authority()
        except Refused:
            raise PermissionRequired("carrier upload permission changed") from None

    def _client(self, upload_token, attempt=0):
        from .provider_connector import session_from_capture
        from .vault_upload_transport import NativeVaultUpload
        self._authorize(upload_token=upload_token)
        session = session_from_capture(account_id=self.account, owner_id=self.owner, policy_revision=1,
            capture=self.clear["capture"], x_hash=self.clear["xHash"], proxy_url=self.clear["proxyUrl"],
            expires_at=self.expires.replace(tzinfo=timezone.utc))
        # NativeVaultUpload invokes this before every signed API operation,
        # multipart action, scheduled carrier creation and cleanup deletion.
        job_token = _attempt_token(upload_token, attempt)
        return NativeVaultUpload(session, authorize=self._authorize, job_id=job_token)

    def _states(self, upload_token):
        import vault_upload
        from .content_sync_media import UploadUnknown
        states = []
        for attempt in range(MAX_RUN_ATTEMPTS):
            run_id = _run_id(self.account, upload_token, attempt)
            try:
                state = vault_upload.read_state(run_id)
                unreadable = state is None and vault_upload._state_path(run_id).exists()
            except Exception:
                raise UploadUnknown("carrier receipt unavailable") from None
            if state is not None:
                states.append((attempt, state))
            elif unreadable:
                # read_state deliberately returns None for both absent and
                # corrupt receipts. A corrupt prior attempt is not permission
                # to accept a newer ID or repeat an uncertain carrier write.
                raise UploadUnknown("carrier receipt unavailable")
        return states

    def _receipt(self, state):
        import vault_upload
        from .content_sync_media import UploadUnknown
        if not isinstance(state, dict) or state.get("account_id") != self.account:
            raise UploadUnknown("carrier receipt account changed")
        items = state.get("items") or []
        if not isinstance(items, list) or len(items) > 1 or any(not isinstance(i, dict) for i in items):
            raise UploadUnknown("carrier receipt item identity changed")
        item = items[0] if items else None
        if state.get("status") == "running" or (item and (
                (item.get("carrier_post_id") and not item.get("carrier_deleted"))
                or vault_upload._intent_unresolved(item))):
            raise UploadUnknown("carrier cleanup outcome not established")
        if state.get("status") not in {"done", "failed", "interrupted", "cancelled", "complete"}:
            raise UploadUnknown("carrier run outcome not established")
        if item and item.get("vault_id"):
            try:
                if type(item["vault_id"]) not in {int, str}:
                    raise ValueError()
                value = int(item["vault_id"])
                if not 0 < value < 2**63:
                    raise ValueError()
                return value
            except (TypeError, ValueError):
                raise UploadUnknown("carrier receipt media identity invalid") from None
        return None

    def lookup(self, *, upload_token):
        from .content_sync_media import UploadUnknown, PermissionRequired
        if self._token is not None and self._token != upload_token:
            raise PermissionRequired("carrier upload asset authority changed")
        landed = {value for _, state in self._states(upload_token) if (value := self._receipt(state)) is not None}
        if len(landed) > 1:
            raise UploadUnknown("carrier attempts have conflicting media identities")
        return next(iter(landed), None)

    def cleanup(self, *, upload_token):
        from .content_sync_media import AuthRequired, UploadUnknown, PermissionRequired
        from .provider_connector import NativeAuthenticationError
        try:
            return self._cleanup(upload_token=upload_token)
        except NativeAuthenticationError:
            raise AuthRequired("connection required") from None
        except (AuthRequired, UploadUnknown, PermissionRequired, Refused):
            raise
        except Exception:
            raise UploadUnknown("carrier cleanup outcome uncertain") from None

    def _cleanup(self, *, upload_token):
        """Reconcile only this durable run's recorded/marked carriers.

        Revoked authority blocks cleanup too. Pending writes remain unknown
        even after a clean scan; a scan cannot prove an in-flight create lost.
        """
        import vault_upload
        from .content_sync_media import UploadUnknown, PermissionRequired
        self._authorize(upload_token=upload_token)
        unresolved = False
        for attempt, state in self._states(upload_token):
            if state.get("account_id") != self.account or state.get("status") == "running":
                raise UploadUnknown("carrier cleanup cannot establish exclusive ownership")
            client = self._client(upload_token) if attempt == 0 else self._client(upload_token, attempt)
            for item in state.get("items", []):
                if item.get("carrier_post_id"):
                    client._posts.add(str(item["carrier_post_id"]))
            client.transport.verify_identity()
            vault_upload._cleanup_carriers(state, client)
            def recheck(attempt=attempt):
                try:
                    self._authorize()
                    latest = vault_upload.read_state(_run_id(self.account, upload_token, attempt))
                    return set() if latest and latest.get("status") != "running" else None
                except (Refused, PermissionRequired):
                    return None
            _, _, complete = vault_upload._sweep_scheduled(client, self.account, recheck, marker=client._VAULT_MARKER)
            if complete:
                for item in state.get("items", []):
                    if vault_upload._intent_unresolved(item) and not item.get("carrier_write_pending"):
                        item["carrier_scan_clear"] = True
            vault_upload._save(state)
            unresolved |= any((i.get("carrier_post_id") and not i.get("carrier_deleted")) or vault_upload._intent_unresolved(i)
                for i in state.get("items", []))
        if unresolved:
            raise UploadUnknown("carrier cleanup outcome not established")

    def upload(self, *, upload_token, path, kind):
        import vault_upload
        from .content_sync_media import AuthRequired, TransientUpload, UploadUnknown, PermissionRequired
        from .provider_connector import NativeAuthenticationError
        try:
            self._authorize(upload_token=upload_token)
            landed = self.lookup(upload_token=upload_token)
            if landed is not None:
                return landed
            existing = {attempt for attempt, _ in self._states(upload_token)}
            attempt = next((i for i in range(MAX_RUN_ATTEMPTS) if i not in existing), None)
            if attempt is None:
                raise TransientUpload("carrier attempts exhausted; review required")
            run_id = _run_id(self.account, upload_token, attempt)
            client = self._client(upload_token) if attempt == 0 else self._client(upload_token, attempt)
            client.transport.verify_identity()
            vault_upload.begin(self.account, run_id=run_id)
            result = vault_upload.start(self.account, client=client, local_paths=[str(path)], run_id=run_id)
        except NativeAuthenticationError:
            raise AuthRequired("connection required") from None
        except PermissionRequired:
            raise
        except TransientUpload:
            raise
        except Refused:
            raise TransientUpload("upload refused") from None
        except Exception:
            raise UploadUnknown("carrier upload outcome uncertain") from None
        landed = self._receipt(result)
        if landed is not None:
            return landed
        item = (result.get("items") or [None])[0]
        self._authorize(upload_token=upload_token)
        raise TransientUpload((item or {}).get("error") or "upload failed")

    def verify(self, *, provider_media_id):
        # Sendability is proven by the account's own vault observation, which
        # content_sync_media requires before marking an asset synced.
        self._authorize(provider_media_id=provider_media_id)
        return "ready"


def transport_for(connection, tenant, account):
    if not approved(account):
        return None
    return CarrierTransport(connection, tenant, account)
