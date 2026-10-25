"""Start read-only account imports after a captured connection is verified.

The durable content-sync and vault owners retain their jobs, progress and
retry policy. This hook creates no reply, model, delivery or sales activation.
"""
import hashlib
import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection, OfConnectionChange
from db.onlyfans_offer_import_models import OfContentSyncAccount
from db.onlyfans_vault_models import OfVaultSyncJob
from .store import Refused, utcnow


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def ensure_started(store, tenant, account, *, connection_revision):
    """One idempotent bootstrap per account, never overriding an explicit off.

    Reconnection of an enabled account is already handled by the content-sync
    scheduler, which retains its archive cursors. A mismatched generation or
    unverified connection cannot create work here.
    """
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker"
            or type(connection_revision) is not int or connection_revision < 1):
        raise Refused("automatic imports require the worker role")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        store._read_connection(s, runtime, connection_revision)
        connection = s.get(OfProviderConnection, account)
        if s.get(OfContentSyncAccount, account) is not None:
            return False
        capture = s.scalar(select(OfConnectionChange).where(
            OfConnectionChange.account_id == account,
            OfConnectionChange.revision <= connection.revision,
            OfConnectionChange.action.in_(("save", "session")))
            .order_by(OfConnectionChange.revision.desc(), OfConnectionChange.created_at.desc()).limit(1))
        if capture is None:
            raise Refused("automatic imports require an audited connection capture")
        key = _digest(["automatic-account-import.v1", tenant, account, runtime.binding_id,
                       connection.revision, capture.command_key])
    # This names the worker task itself; it never impersonates a user session.
    actor = "worker-import:" + key
    from .content_sync import set_settings
    try:
        set_settings(store, tenant, account, enabled=True, expected_revision=0,
                     command_key=key, actor=actor,
                     expected_connection_revision=connection_revision)
    except Refused:
        # Another loader or a user may have made the initial decision while we
        # released the runtime lock. Any such explicit setting takes precedence.
        with Session(store.engine) as s:
            runtime = store._runtime(s, tenant, account)
            store._read_connection(s, runtime, connection_revision)
            row = s.get(OfContentSyncAccount, account)
            if row is None or row.binding_id != runtime.binding_id:
                raise
        return False
    # Queue the vault immediately, without waiting for the first periodic pass.
    # A pre-existing job is already doing this work and must keep its cursor.
    with Session(store.engine) as s:
        active = s.scalar(select(OfVaultSyncJob.job_id).where(
            OfVaultSyncJob.account_id == account,
            OfVaultSyncJob.status.in_(("pending", "running"))).limit(1))
    if active is None:
        from .vault_management import request_sync
        try:
            request_sync(store, tenant, account, expected_connection_revision=connection_revision,
                         command_key=_digest([key, "vault"]), actor=actor)
        except Refused:
            # The enabled scheduler retries or reuses the winning vault job.
            pass
    print(json.dumps({"schemaVersion": "of-automatic-import.v1", "account": account,
                      "state": "started", "connectionRevision": connection_revision}), flush=True)
    return True
