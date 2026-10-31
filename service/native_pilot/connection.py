"""Read-only bridge from Twin's verified account connection to the pilot's runtime.

Twin keeps one encrypted, per-account record (``of_provider_connection``) holding
the captured OnlyFans session, its signing hash, the fixed proxy and the
account-scoped model credential. The pilot reuses exactly that record instead of
a session file, the proxy registry or a house/dotenv key:

* nothing is written, renewed, verified or re-enabled here;
* anything not currently ``verified`` (pending, failed, paused, expired, retry
  state, lease elapsed, revoked binding, owner mismatch, undecryptable) is
  refused, with a fixed public code and no credential text;
* the decryption key is read from the process environment only
  (``ONLYFANS_CONNECTION_KEY``), never from a dotenv file.

Secrets live only in the returned dataclass (fields marked ``repr=False``) and
in the in-memory client/model objects built from it.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ledger import Refused
from .scope import AccountScope

MAX_SAVED_CAP_CENTS = 1_000_000


@dataclass(frozen=True)
class PilotConnection:
    scope: AccountScope
    tenant_id: str
    revision: int
    owner_id: int
    expires_at: datetime            # aware UTC
    capture: dict = field(repr=False)
    x_hash: str = field(repr=False)
    proxy_url: str | None = field(repr=False)
    model_id: str                   # registry id of the saved account model
    model_api_key: str = field(repr=False)
    daily_cost_cap_cents: int = 0


def supports_reply_model(model_id: str) -> bool:
    """The existing transport's actual route; no credential or model call."""
    from llm_models import MODELS
    from llm_providers import PROVIDERS
    from .model import ENDPOINT, MODEL
    spec=MODELS.get(model_id)
    provider=PROVIDERS.get(spec.provider) if spec is not None else None
    return bool(provider is not None and spec.wire_model()==MODEL
        and provider.base_url.rstrip("/")+"/chat/completions"==ENDPOINT)


async def load_verified_connection(scope: AccountScope, *, now: datetime | None = None) -> PilotConnection:
    from sqlalchemy import select
    from db.engine import get_session
    from db.models import AccountAiConfig, OfCreatorBinding
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_models import OfRuntime
    from llm_models import MODELS
    from llm_providers import PROVIDERS
    from model_selection import explicit_model
    from onlyfans_runtime import connections
    from onlyfans_runtime.provider_connector import session_from_capture
    from onlyfans_runtime.store import Refused as TwinRefused
    from .model import ENDPOINT, MODEL

    if not isinstance(scope, AccountScope) or not scope.twin_form:
        # Only a Twin-keyed account (of-<owner>) has a connection record.
        raise Refused("connection-account-form")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    async with get_session() as s:
        row = await s.get(OfProviderConnection, scope.db_id)
        if row is None:
            raise Refused("connection-missing")
        runtime = await s.get(OfRuntime, scope.db_id)
        binding = await s.get(OfCreatorBinding, row.binding_id)
        cfg = await s.get(AccountAiConfig, scope.db_id)
        # Plain-value copies: nothing below needs the session or the ORM rows.
        sealed = SimpleNamespace(sealed_config=row.sealed_config, account_id=row.account_id,
                                 binding_id=row.binding_id, revision=row.revision,
                                 owner_id=row.owner_id, expires_at=row.expires_at)
        fields = dict(binding_id=row.binding_id, owner_id=row.owner_id, revision=row.revision,
                      status=row.status, verified_at=row.verified_at, last_error=row.last_error,
                      expires_at=row.expires_at)
        runtime = None if runtime is None else SimpleNamespace(binding_id=runtime.binding_id)
        binding = None if binding is None else SimpleNamespace(
            account_id=binding.account_id, channel=binding.channel, revoked_at=binding.revoked_at,
            tenant_id=binding.tenant_id)
        cfg = None if cfg is None else SimpleNamespace(
            model=cfg.model, model_by_purpose=cfg.model_by_purpose,
            daily_cost_cap_cents=cfg.daily_cost_cap_cents)

    if (runtime is None or binding is None or runtime.binding_id != fields["binding_id"]
            or binding.account_id != scope.db_id or binding.channel != "onlyfans"
            or binding.revoked_at is not None or type(binding.tenant_id) is not str):
        raise Refused("connection-scope")
    if fields["status"] != "verified" or fields["verified_at"] is None:
        raise Refused("connection-not-verified")
    if fields["last_error"] is not None:
        raise Refused("connection-check-failing")
    if fields["expires_at"] <= now:
        raise Refused("connection-expired")
    if str(fields["owner_id"]) != scope.provider_id:
        raise Refused("connection-owner-mismatch")

    try:
        clear = connections._open(sealed, binding.tenant_id)
    except TwinRefused:
        # The key may be unset or the ciphertext/AAD wrong; both are the same
        # fixed public outcome and never reveal which.
        raise Refused("connection-credentials-unavailable") from None

    expires = fields["expires_at"].replace(tzinfo=timezone.utc)
    try:
        # Reuse Twin's own validation of capture shape, cookies, signing rules,
        # proxy URL and the 24 h credential bound. Result is discarded: the
        # OFClient is built from the same capture by build_client().
        session_from_capture(account_id=scope.db_id, owner_id=clear["ownerId"], policy_revision=1,
                             capture=clear["capture"], x_hash=clear["xHash"],
                             proxy_url=clear["proxyUrl"], expires_at=expires)
        if str(clear["ownerId"]) != scope.provider_id:
            raise ValueError()
    except Exception:
        raise Refused("connection-session-invalid") from None

    # ── account-scoped model credential, pinned to the saved account model ──
    model = clear.get("model")
    if type(model) is not dict or set(model) != {"modelId", "apiKey"} or type(model["modelId"]) is not str \
            or type(model["apiKey"]) is not str:
        raise Refused("model-credential-missing")
    try:
        saved = explicit_model(cfg, "ai_chatter", MODELS)
    except (ValueError, TypeError, RecursionError):
        raise Refused("model-config-invalid") from None
    if model["modelId"] != saved:
        raise Refused("model-credential-mismatch")
    if not supports_reply_model(saved):
        # The pilot's transport is pinned to one wire model and one endpoint; a
        # different saved model must not silently receive this credential.
        raise Refused("model-route-unsupported")
    cap = cfg.daily_cost_cap_cents if cfg is not None else None
    if type(cap) is not int or not 0 <= cap <= MAX_SAVED_CAP_CENTS:
        raise Refused("model-budget-unset")

    return PilotConnection(
        scope=scope, tenant_id=binding.tenant_id, revision=fields["revision"], owner_id=int(fields["owner_id"]),
        expires_at=expires, capture=copy.deepcopy(clear["capture"]), x_hash=clear["xHash"],
        proxy_url=clear["proxyUrl"], model_id=saved, model_api_key=model["apiKey"],
        daily_cost_cap_cents=cap)


def build_client(conn: PilotConnection, *, timeout_s: int = 20):
    """Construct the pilot's OFClient from the verified connection. No network I/O.

    The legacy role guard is opened for this one provider account only (see
    ``onlyfans_runtime.role_guard``). The signing hash saved with the connection
    is installed directly so the client never issues its own warm-up request.
    """
    from of_client import OFClient
    from onlyfans_runtime.role_guard import allow_native_pilot_provider_access

    if datetime.now(timezone.utc) >= conn.expires_at:
        raise Refused("connection-expired")
    allow_native_pilot_provider_access(conn.scope.provider_id)
    proxies = {"http": conn.proxy_url, "https": conn.proxy_url} if conn.proxy_url else None
    client = OFClient(copy.deepcopy(conn.capture), proxies=proxies,
                      proxy_label="twin-connection" if proxies else None, timeout_s=timeout_s)
    if str(client.account_id) != conn.scope.provider_id or str(client.user_id) != conn.scope.provider_id:
        from of_client import close_client_http
        close_client_http(client)
        raise Refused("session-account-mismatch")
    client._x_hash = conn.x_hash
    return client
