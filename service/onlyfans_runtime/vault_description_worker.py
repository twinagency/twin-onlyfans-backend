"""Automatic, resumable vault descriptions in an isolated child process.

Pending work is derived from canonical imported rows, so restarts and pages
imported after startup need no second queue. Chat work never waits for vision.
"""
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session
from db.models import AccountAiConfig, OfCreatorBinding, VaultItem
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from .store import utcnow


def pending(now):
    """Work still owed: never-described items, retry-eligible failures, and items whose
    CONTENT changed since they were described. Successful/operator descriptions,
    refusals and attempt-exhausted items (explicitly unresolved) are left alone."""
    fields = func.coalesce(VaultItem.ai_fields_json, "")
    stamp = literal('"_content_stamp": "') + func.coalesce(VaultItem.content_hash_kind, "") + ":" + func.coalesce(VaultItem.content_hash, "") + '"'
    exhausted_now = or_(
        and_(VaultItem.content_hash.is_not(None), fields.like(
            literal('%"exhausted": true, "stamp": "') + func.coalesce(VaultItem.content_hash_kind, "") + ":" + func.coalesce(VaultItem.content_hash, "") + '"%')),
        and_(VaultItem.content_hash.is_(None), fields.like('%"exhausted": true, "stamp": null%')))
    failure_retry = or_(
        and_(VaultItem.describe_status.in_(("failed", "fetch_failed")),
             or_(VaultItem.describe_generated_at.is_(None),
                 VaultItem.describe_generated_at < now - timedelta(minutes=30))),
        and_(VaultItem.describe_status.in_(("blocked_drm", "poster_only")),
             or_(VaultItem.describe_generated_at.is_(None),
                 VaultItem.describe_generated_at < now - timedelta(days=7))))
    locked = func.coalesce(VaultItem.locked_fields_json, "")
    unlocked = and_(locked.not_like('%"description"%'), locked.not_like('%"video_description"%'))
    blank = and_(func.trim(func.coalesce(VaultItem.description, "")) == "",
        func.trim(func.coalesce(VaultItem.video_description, "")) == "",
        or_(VaultItem.describe_status.is_(None), failure_retry))
    changed = and_(VaultItem.content_hash.is_not(None), fields.like('%"_content_stamp"%'),
        fields.not_like(literal('%') + stamp + '%'),
        or_(VaultItem.describe_status.in_(("described", "refused")) | VaultItem.describe_status.is_(None), failure_retry))
    return and_(VaultItem.removed_at.is_(None), VaultItem.kind.in_(("photo", "video")),
        unlocked, ~exhausted_now, or_(blank, changed))


def accounts(store, tenant, after=None):
    """Only this tenant's current verified, canonically observed accounts."""
    now = utcnow()
    query = (select(OfRuntime.account_id, OfRuntime.binding_id, OfProviderConnection.revision)
        .join(OfCreatorBinding, OfCreatorBinding.id == OfRuntime.binding_id)
        .join(OfProviderConnection, OfProviderConnection.account_id == OfRuntime.account_id)
        .join(VaultItem, VaultItem.account_id == OfRuntime.account_id)
        .join(OfVaultObservation, and_(OfVaultObservation.account_id == VaultItem.account_id,
                                      OfVaultObservation.media_id == VaultItem.media_id))
        .join(OfVaultSyncJob, and_(OfVaultSyncJob.account_id == OfVaultObservation.account_id,
                                  OfVaultSyncJob.job_id == OfVaultObservation.job_id))
        .where(OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
            OfCreatorBinding.channel == "onlyfans",
            OfProviderConnection.binding_id == OfRuntime.binding_id,
            OfProviderConnection.status == "verified", OfProviderConnection.verified_at.is_not(None),
            OfProviderConnection.last_error.is_(None), OfProviderConnection.expires_at > now,
            OfVaultSyncJob.binding_id == OfRuntime.binding_id,
            OfVaultSyncJob.owner_id == OfProviderConnection.owner_id, pending(now)))
    if after:
        query = query.where(OfRuntime.account_id > after)
    included = {value.strip() for value in os.environ.get("ONLYFANS_VAULT_DESCRIPTION_INCLUDE_ACCOUNTS", "").split(",") if value.strip()}
    if included:
        query = query.where(OfRuntime.account_id.in_(included))
    excluded = {value.strip() for value in os.environ.get("ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS", "").split(",")}
    with Session(store.engine) as session:
        result = []
        scanned = list(session.execute(query.distinct().order_by(OfRuntime.account_id).limit(32)))
        for account, binding, revision in scanned:
            cfg = session.get(AccountAiConfig, account)
            try:
                settings = json.loads(cfg.vault_ai_config_json or "{}") if cfg else {}
            except (ValueError, TypeError):
                continue
            if account not in excluded and isinstance(settings, dict) and settings.get("auto_describe_on_import") is not False:
                result.append((account, binding, revision))
        return result, scanned[-1][0] if scanned else None


class VaultDescriptionWorker:
    def __init__(self, store, tenant, stop, *, observe=lambda value: None):
        self.store, self.tenant, self.stop, self.observe = store, tenant, stop, observe
        self.thread = None
        self.child = None
        self.lock = threading.Lock()

    def start(self):
        self.thread = threading.Thread(target=self.run, name="native-vault-descriptions", daemon=True)
        self.thread.start()

    def run(self):
        cursor = None
        while not self.stop.is_set():
            try:
                rows, scanned = accounts(self.store, self.tenant, cursor)
                if not rows:
                    cursor = scanned
                    self.stop.wait(15)
                    continue
                account, binding, revision = rows[0]
                cursor = account
                args = [sys.executable, "-m", "native_pilot.tools.describe_imported_vault",
                    "--tenant", self.tenant, "--account", account, "--binding", binding,
                    "--revision", str(revision), "--limit", "4"]
                with self.lock:
                    if self.stop.is_set():
                        break
                    self.child = subprocess.Popen(args, cwd=Path(__file__).resolve().parents[1],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                try:
                    output, _ = self.child.communicate(timeout=900)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.communicate()
                    output = ""
                result = None
                if output:
                    # The child emits one fixed-shape summary, never model text or URLs.
                    try:
                        result = json.loads(output[-4096:].splitlines()[-1])
                    except (ValueError, IndexError):
                        result = None
                if isinstance(result, dict) and result.get("schemaVersion") == "of-vault-description.v1":
                    self.observe(result)
                else:
                    result = {"schemaVersion": "of-vault-description.v1", "account": account,
                              "state": "retry_pending"}
                    self.observe(result)
                # A spent budget or refused credential will not clear in seconds:
                # stop respawning children for it.
                if result.get("state") == "budget_paused" or result.get("reason") in ("auth", "budget"):
                    self.stop.wait(300)
            except Exception:
                self.observe({"schemaVersion": "of-vault-description.v1", "state": "unavailable"})
            finally:
                self.child = None
            self.stop.wait(2)

    def join(self):
        with self.lock:
            child = self.child
            if child is not None and child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        if self.thread is not None:
            self.thread.join(timeout=910)


def main():
    """Run the same import phase as a dedicated service during rollout."""
    import signal
    from .store import Store
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise RuntimeError("worker role required")
    if os.environ.get("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED") != "1":
        raise RuntimeError("automatic imports required")
    stop = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stop.set())
    store = Store(os.environ["DATABASE_URL"])
    worker = VaultDescriptionWorker(store, os.environ["ONLYFANS_WORKER_TENANT"], stop,
        observe=lambda value: print(json.dumps(value), flush=True))
    from .vault_preview_worker import VaultPreviewWorker
    from .vault_folder_worker import VaultFolderWorker
    previews = VaultPreviewWorker(store, os.environ["ONLYFANS_WORKER_TENANT"], stop)
    folders = VaultFolderWorker(store, os.environ["ONLYFANS_WORKER_TENANT"], stop)
    try:
        folders.start()
        previews.start()
        worker.start()
        stop.wait()
    finally:
        stop.set()
        worker.join()
        previews.join()
        folders.join()
        store.close()


if __name__ == "__main__":
    main()

