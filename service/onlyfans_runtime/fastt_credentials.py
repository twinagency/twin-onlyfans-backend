"""Load ONE account's sealed provider session and model key for the FASTT runner.

Read-only: it never verifies, renews or rewrites the connection (the existing worker owns that).
It returns the same objects the worker builds -- NativeProviderConnector (one POST attempt, no OFClient
retry or warmup) and NativeProviderModel -- so the runner has no credential path of its own.
"""
from __future__ import annotations

from datetime import timezone

from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from .connections import _open
from .provider_connector import NativeProviderConnector, session_from_capture
from .provider_model import NativeProviderModel
from .store import Refused, utcnow


def load_credentials(store, tenant_id, account_id, *, need_model=True):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant_id, account_id)
        row = s.get(OfProviderConnection, account_id)
        now = utcnow()
        if (row is None or row.binding_id != runtime.binding_id or row.status != "verified"
                or row.verified_at is None or row.expires_at <= now or row.last_error is not None):
            raise Refused("verified connection required")
        clear = _open(row, tenant_id)
        policy = s.get(OfDeliveryPolicy, account_id)
        policy_revision = policy.revision if policy is not None and policy.revision >= 1 else 1
        expires = row.expires_at.replace(tzinfo=timezone.utc)
    transport = NativeProviderConnector(session_from_capture(
        account_id=account_id, owner_id=clear["ownerId"], policy_revision=policy_revision,
        capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"], expires_at=expires))
    model = None
    if need_model:
        saved = clear.get("model")
        if not saved:
            raise Refused("an account-scoped model credential is required")
        model = NativeProviderModel(account_id=account_id, model_id=saved["modelId"], api_key=saved["apiKey"],
                                    expires_at=expires)
    return transport, model
