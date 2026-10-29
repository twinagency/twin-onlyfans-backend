"""Independent, account-isolated live inbox reads. No planner or sender is run."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import OfCreatorBinding
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from .connections import _open
from .provider_connector import NativeProviderConnector, session_from_capture
from .recent_inbox import poll_recent
from .store import Refused, utcnow
from .inbox_health import HEALTH_PATH, healthy

ROSTER_SECONDS = 5
STALE_SECONDS = 120


def account_ids(store, tenant):
    """Discover every verified binding, independently of AI and history state."""
    with Session(store.engine) as s:
        return s.scalars(select(OfProviderConnection.account_id).join(
            OfCreatorBinding, OfCreatorBinding.id == OfProviderConnection.binding_id).where(
                OfCreatorBinding.tenant_id == tenant,
                OfCreatorBinding.channel == "onlyfans",
                OfCreatorBinding.revoked_at.is_(None),
                OfProviderConnection.status == "verified",
                OfProviderConnection.expires_at > utcnow()
            ).order_by(OfProviderConnection.account_id)).all()


def poll_account(store, tenant, account):
    # The account/session is re-read for every turn. A reconnect, pause or
    # revoked binding is fenced again by the shared lease and commit path.
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row = s.get(OfProviderConnection, account)
        if row is None:
            raise Refused("inbox connection unavailable")
        store._read_connection(s, runtime, row.revision)
        clear = _open(row, tenant)
        revision = row.revision
        policy = s.get(OfDeliveryPolicy, account)
        policy_revision = policy.revision if policy is not None else 1
        outgoing = bool(runtime.enabled and policy is not None and policy.enabled
                        and policy.transport_kind == "native")
        expires = row.expires_at.replace(tzinfo=timezone.utc)
    reader = NativeProviderConnector(session_from_capture(account_id=account,
        owner_id=clear["ownerId"], policy_revision=policy_revision,
        capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
        expires_at=expires))
    reader.connection_revision = revision
    applied = poll_recent(store, tenant, account, reader,
        connection_revision=None if outgoing else revision)
    state = getattr(store, "_recent_inbox_states", {}).get((tenant, account, revision))
    return {"imported": applied, "revision": revision,
            "lastHeadAt": state.last_head_at.isoformat() + "Z" if state and state.last_head_at else None,
            "pendingConversations": len(state.pending) if state else 0,
            "conversationFailures": state.failures if state else 0}


class InboxWorker(threading.Thread):
    """One future per account; slow provider reads do not block other accounts."""
    def __init__(self, store, tenant, stop, *, observe=None, workers=8,
                 roster=account_ids, poll=poll_account, clock=time.monotonic,
                 health_path=HEALTH_PATH, owns_store=False):
        super().__init__(name="onlyfans-inbox", daemon=True)
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("inbox concurrency must be 1-32")
        self.store, self.tenant, self.stop = store, tenant, stop
        self.observe = observe or (lambda value: None)
        self.workers, self.roster, self.poll, self.clock = workers, roster, poll, clock
        self.health_path, self.owns_store = health_path, owns_store
        self.futures, self.due, self.health, self.first_seen = {}, {}, {}, {}
        self.accounts, self.next_roster, self.last_roster = [], 0, None
        self.last_health = 0

    def step(self, pool):
        now = self.clock()
        if now >= self.next_roster:
            try:
                self.accounts = self.roster(self.store, self.tenant)
                self.last_roster = time.time()
                for account in self.accounts:
                    self.first_seen.setdefault(account, time.time())
                for account in list(self.health):
                    if account not in self.accounts and account not in self.futures:
                        self.health.pop(account, None)
                        self.first_seen.pop(account, None)
                        self.due.pop(account, None)
                # Retired credentials should not leave unbounded process state.
                states = getattr(self.store, "_recent_inbox_states", {})
                for key in list(states):
                    if key[1] not in self.accounts and key[1] not in self.futures:
                        states.pop(key, None)
            except Exception as exc:
                self.observe({"schemaVersion": "of-inbox-health.v1", "state": "roster_failed",
                              "errorType": type(exc).__name__})
            self.next_roster = now + ROSTER_SECONDS
        for account, future in list(self.futures.items()):
            if not future.done():
                continue
            self.futures.pop(account)
            try:
                result = future.result()
                self.health[account] = {**result, "errorType": None}
            except Exception as exc:
                self.health.setdefault(account, {})["errorType"] = type(exc).__name__
                self.observe({"schemaVersion": "of-inbox-health.v1", "account": account,
                              "state": "read_deferred", "errorType": type(exc).__name__})
            self.due[account] = now + 1
        # Do not queue unbounded futures. Completed/slow accounts cannot monopolize
        # the pool; least-recently-served accounts get the next available slot.
        for account in sorted(self.accounts, key=lambda a: self.due.get(a, 0)):
            if self.stop.is_set() or len(self.futures) >= self.workers:
                break
            if account in self.futures or self.due.get(account, 0) > now:
                continue
            self.futures[account] = pool.submit(self.poll, self.store, self.tenant, account)
        if now - self.last_health >= 5:
            self.write_health()
            self.last_health = now

    def write_health(self):
        now = time.time()
        accounts = {}
        for account in self.accounts:
            row = dict(self.health.get(account, {}))
            stamp = row.get("lastHeadAt")
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else self.first_seen[account]
            row["stale"] = now - last > STALE_SECONDS
            row["headAgeSeconds"] = round(now - last, 1)
            accounts[account] = row
            if row["stale"]:
                self.observe({"schemaVersion": "of-inbox-health.v1", "account": account,
                              "state": "stale", "headAgeSeconds": row["headAgeSeconds"]})
        payload = {"schemaVersion": "of-inbox-health.v1", "observedAt": now,
                   "rosterUpdatedAt": self.last_roster, "accounts": accounts}
        if self.health_path:
            target = Path(self.health_path)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(json.dumps(payload))
            temporary.replace(target)

    def run(self):
        try:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="of-inbox-read") as pool:
                while not self.stop.is_set():
                    try:
                        self.step(pool)
                    except Exception as exc:
                        self.observe({"schemaVersion": "of-inbox-health.v1", "state": "scheduler_failed",
                                      "errorType": type(exc).__name__})
                    self.stop.wait(.5)
        finally:
            if self.owns_store:
                self.store.close()
