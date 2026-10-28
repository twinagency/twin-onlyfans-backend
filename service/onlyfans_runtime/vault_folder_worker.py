"""Catch up existing imported metadata and classify newly imported media."""
import os
import threading
from sqlalchemy import select, or_, func
from sqlalchemy.orm import Session
from db.models import OfCreatorBinding, VaultItem
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from .store import Refused
from .vault_previews import asset_scope
from vault_auto_folders import assign_sync, CURRENT_MARKER, PENDING_MARKER


def accounts(store, tenant):
    with Session(store.engine) as session:
        return list(session.scalars(select(OfRuntime.account_id)
            .join(OfCreatorBinding, OfCreatorBinding.id == OfRuntime.binding_id)
            .where(OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
                OfCreatorBinding.channel == "onlyfans").order_by(OfRuntime.account_id)))


def classify_batch(store, tenant, account, *, after=0, on_scanned=lambda mid: None):
    with Session(store.engine) as session:
        store._runtime(session, tenant, account)
        ids=list(session.scalars(select(VaultItem.media_id).where(
            VaultItem.account_id == account, VaultItem.removed_at.is_(None), VaultItem.media_id > after,
            VaultItem.kind.in_(("photo", "video", "gif", "audio")),
            func.coalesce(VaultItem.ai_fields_json, "").not_like('%"'+CURRENT_MARKER+'"%'),
            or_(func.coalesce(VaultItem.ai_fields_json, "").not_like('%"'+PENDING_MARKER+'"%'),
                func.trim(func.coalesce(VaultItem.description,"")) != "",
                func.trim(func.coalesce(VaultItem.video_description,"")) != "",
                func.coalesce(VaultItem.tags,"[]").not_in(("[]","null",""))))
            .order_by(VaultItem.media_id).limit(200)))
    on_scanned(ids[-1] if ids else 0)
    count=0
    for mid in ids:
        try:
            with Session(store.engine) as session, session.begin():
                # Same runtime-first lock order as native import/caption guards.
                session.get(OfRuntime,account,with_for_update=True)
                asset_scope(session,store,tenant,account,str(mid),allow_audio=True)
                assign_sync(session,account,mid)
                count+=1
        except (Refused, ValueError):
            continue
    return count


class VaultFolderWorker:
    def __init__(self, store, tenant, stop):
        self.store,self.tenant,self.stop=store,tenant,stop
        self.thread=None
        self.cursors={}

    def start(self):
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("folder classification requires worker role")
        self.thread=threading.Thread(target=self.run,name="native-vault-folders",daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                for account in accounts(self.store,self.tenant):
                    if self.stop.is_set():break
                    classify_batch(self.store,self.tenant,account,after=self.cursors.get(account,0),
                        on_scanned=lambda mid,account=account:self.cursors.__setitem__(account,mid))
            except Exception:
                pass  # metadata stays intact; next pass retries without provider/model I/O
            self.stop.wait(3)

    def join(self):
        if self.thread is not None:self.thread.join(timeout=30)
