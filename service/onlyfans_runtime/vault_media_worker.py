"""Bounded background media work; long uploads never occupy the chat loop."""
import json
import os
import shutil
import threading
import time
from datetime import timedelta, timezone

from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session
from db.models import OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_media_models import OfVaultMediaJob
from .store import Refused, utcnow
from .vault_media import ACTIVE, _dir, _run, process_upload
from .vault_video import process_video, expire_videos
from .message_media import process_one as process_message_media
from vault_upload_state import intent_unresolved


def recover(store, tenant):
    """Expire abandoned intake and HOLD interrupted provider writes, never replay them."""
    now = utcnow()
    with Session(store.engine) as session, session.begin():
        jobs = session.scalars(select(OfVaultMediaJob).join(OfCreatorBinding,
            OfCreatorBinding.id == OfVaultMediaJob.binding_id).outerjoin(OfRuntime,
            OfRuntime.account_id == OfVaultMediaJob.account_id).outerjoin(OfProviderConnection,
            OfProviderConnection.account_id == OfVaultMediaJob.account_id).where(
            OfCreatorBinding.tenant_id == tenant, OfVaultMediaJob.status.in_(ACTIVE), or_(
                OfRuntime.account_id.is_(None), OfRuntime.binding_id != OfVaultMediaJob.binding_id,
                OfCreatorBinding.revoked_at.is_not(None),
                and_(OfVaultMediaJob.status == "running", OfVaultMediaJob.lease_until <= now),
                and_(OfVaultMediaJob.status.in_(["receiving", "pending"]), or_(
                    OfVaultMediaJob.requested_at < now - timedelta(hours=24),
                    OfProviderConnection.account_id.is_(None), OfProviderConnection.status != "verified",
                    OfProviderConnection.expires_at <= now,
                    OfProviderConnection.revision != OfVaultMediaJob.connection_revision))))
            .order_by(OfVaultMediaJob.updated_at).with_for_update(of=OfVaultMediaJob).limit(100)).all()
        for job in jobs:
            runtime = session.get(OfRuntime, job.account_id)
            binding = session.get(OfCreatorBinding, job.binding_id)
            connection = session.get(OfProviderConnection, job.account_id)
            reason = None
            if not runtime or runtime.binding_id != job.binding_id or binding.revoked_at is not None:
                job.status, reason = "superseded", "media_binding_changed"
            elif job.status == "running" and job.lease_until is not None and job.lease_until <= now:
                job.status, reason = "interrupted", "worker_interrupted_review_required"
            elif job.status in {"receiving", "pending"} and now - job.requested_at > timedelta(hours=24):
                job.status, reason = "expired", "media_request_expired"
            elif job.status != "running" and (not connection or connection.status != "verified"
                    or connection.expires_at <= now or connection.revision != job.connection_revision):
                job.status, reason = "superseded", "media_connection_changed"
            if reason:
                job.last_error, job.updated_at, job.completed_at = reason, now, now
                job.lease_token, job.lease_until = None, None
                if job.kind == "upload":
                    state = _run(job)
                    job.cleanup_required = bool(state and any((item.get("carrier_post_id") and not item.get("carrier_deleted"))
                        or intent_unresolved(item) for item in state.get("items", [])))
                    shutil.rmtree(_dir(job.job_id), ignore_errors=True)


def clean_one(store, tenant):
    """Use recorded carrier IDs and the SAME scheduled-marker sweep after failure."""
    with Session(store.engine) as session, session.begin():
        jobs = session.scalars(select(OfVaultMediaJob).join(OfCreatorBinding,
            OfCreatorBinding.id == OfVaultMediaJob.binding_id).where(OfCreatorBinding.tenant_id == tenant,
            OfCreatorBinding.revoked_at.is_(None), OfVaultMediaJob.kind == "upload",
            OfVaultMediaJob.cleanup_required.is_(True),
            OfVaultMediaJob.status.in_(["failed", "interrupted", "complete", "superseded"]))
            .order_by(OfVaultMediaJob.updated_at).limit(1)).all()
        for job in jobs:
            job.updated_at = utcnow()  # Failed cleanup rotates behind other accounts.
        candidate = next(((job.account_id, job.job_id) for job in jobs if _run(job)
            and any((item.get("carrier_post_id") and not item.get("carrier_deleted"))
                or intent_unresolved(item)
                for item in _run(job).get("items", []))), None)
    if candidate is None:
        return
    account, identity = candidate
    with Session(store.engine) as session, session.begin():
        runtime = store._runtime(session, tenant, account)
        job = session.get(OfVaultMediaJob, (account, identity))
        connection = session.get(OfProviderConnection, account)
        if (not connection or connection.status != "verified" or connection.expires_at <= utcnow()
                or runtime.binding_id != job.binding_id or connection.owner_id != job.owner_id):
            return
        if session.scalar(select(OfVaultMediaJob.job_id).where(OfVaultMediaJob.account_id == account,
                OfVaultMediaJob.kind == "upload", OfVaultMediaJob.status.in_(ACTIVE)).limit(1)):
            return
        from .connections import _open
        clear = _open(connection, tenant)
        owner, expires, state = connection.owner_id, connection.expires_at, _run(job)
        binding, revision = job.binding_id, connection.revision
    def authorize():
        with Session(store.engine) as session, session.begin():
            runtime = store._runtime(session, tenant, account)
            connection = session.get(OfProviderConnection, account)
            if (not connection or runtime.binding_id != binding or connection.owner_id != owner or connection.status != "verified"
                    or connection.revision != revision):
                raise Refused("carrier cleanup scope changed")
            if session.scalar(select(OfVaultMediaJob.job_id).where(OfVaultMediaJob.account_id == account,
                    OfVaultMediaJob.kind == "upload", OfVaultMediaJob.status.in_(ACTIVE)).limit(1)):
                raise Refused("an upload is active")
    from .provider_connector import session_from_capture
    from .vault_upload_transport import NativeVaultUpload
    import vault_upload
    client = NativeVaultUpload(session_from_capture(account_id=account, owner_id=owner, policy_revision=1,
        capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
        expires_at=expires.replace(tzinfo=timezone.utc)), authorize=authorize, job_id=identity,
        known_posts=[item["carrier_post_id"] for item in state["items"] if item.get("carrier_post_id")])
    client.transport.verify_identity()
    vault_upload._cleanup_carriers(state, client)
    def recheck():
        try:
            authorize()
            return set()
        except Refused:
            return None
    _, _, complete = vault_upload._sweep_scheduled(client, account, recheck, marker=client._VAULT_MARKER)
    if complete:
        for item in state["items"]:
            if vault_upload._intent_unresolved(item) and not item.get("carrier_write_pending"):
                item["carrier_scan_clear"] = True
        vault_upload._save(state)
    with Session(store.engine) as session, session.begin():
        job = session.get(OfVaultMediaJob, (account, identity))
        job.cleanup_required = any((item.get("carrier_post_id") and not item.get("carrier_deleted"))
            or intent_unresolved(item) for item in state["items"])


class MediaWorker:
    def __init__(self, store, tenant, stop):
        self.store, self.tenant, self.stop = store, tenant, stop
        self.threads = []

    def start(self):
        for name, operation in [("upload", process_upload), ("video", process_video), ("message", process_message_media)]:
            def work(operation=operation, name=name):
                swept_at = 0
                while not self.stop.is_set():
                    try:
                        if name == "upload":
                            recover(self.store, self.tenant)
                            clean_one(self.store, self.tenant)
                        elif name == "video" and time.monotonic() - swept_at >= 60:
                            expire_videos(self.store, self.tenant)
                            swept_at = time.monotonic()
                        operation(self.store, self.tenant, stop=self.stop)
                    except Exception:
                        # Provider URLs, file paths and credentials stay out of logs.
                        pass
                    self.stop.wait(2)
            thread = threading.Thread(target=work, name="native-vault-" + name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def join(self, timeout=15):
        for thread in self.threads:
            thread.join(timeout)
