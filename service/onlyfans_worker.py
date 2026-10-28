"""Standalone native OnlyFans worker with explicit tenant and credentials.

Run schema migration separately before startup. This entry point imports no
legacy server/executor, creates no credentials and activates no account. The
worker stops between durable phases; bounded HTTP/DB calls may finish first.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _emit(value):
    print(json.dumps(value), flush=True)


def _process_connection_enrollments(store, tenant, *, limit, stop):
    from onlyfans_runtime.enrollments import process_enrollments
    return process_enrollments(store, tenant, limit=limit, stop=stop)


def _process_vault_jobs(store, tenant, *, limit, stop):
    from onlyfans_runtime.vault_management import process_vault_jobs
    return process_vault_jobs(store, tenant, limit=limit, stop=stop)


def _process_ledger_jobs(store, tenant, *, limit, stop):
    from onlyfans_runtime.ledger import process_ledger_pages
    return process_ledger_pages(store, tenant, limit=limit, stop=stop)


def _process_offer_jobs(store, tenant, *, limit, stop):
    # Inventory needs no provider session; refresh reads one bounded history
    # page per account interval. Neither sends, plans or calls a model.
    from onlyfans_runtime.offer_import import process_inventory_jobs
    from onlyfans_runtime.offer_import_worker import process_refresh_jobs
    return (process_inventory_jobs(store, tenant, limit=limit, stop=stop)
            + process_refresh_jobs(store, tenant, limit=limit, stop=stop))


def _process_import_sync(store, tenant, *, limit, stop):
    """Automatic account reading never starts uploads or publishing."""
    from onlyfans_runtime.content_sync import process_due
    from onlyfans_runtime.offer_post_source import process_posts
    processed = process_due(store, tenant, limit=limit, stop=stop)
    if stop.is_set():
        return processed
    return processed + process_posts(store, tenant, limit=limit, stop=stop)


def _process_content_sync(store, tenant, *, limit, stop):
    # Scheduling is DB-only and enqueues the bounded refresh/vault jobs above;
    # uploads run through the media owner. Neither sends a message or publishes.
    from onlyfans_runtime.content_sync import process_due
    from onlyfans_runtime.offer_post_source import process_posts
    processed = process_due(store, tenant, limit=limit, stop=stop)
    # Bounded read of the creator's own paid feed posts (read-only, one pass per 300s).
    processed += process_posts(store, tenant, limit=limit, stop=stop)
    try:
        content_sync_media = importlib.import_module("onlyfans_runtime.content_sync_media")
    except ModuleNotFoundError as exc:
        if exc.name != "onlyfans_runtime.content_sync_media":
            raise
        return processed
    # Uploads use the approved scheduled-carrier path only for accounts listed in
    # ONLYFANS_CARRIER_UPLOAD_ACCOUNTS; every other account keeps "needs review".
    from onlyfans_runtime.content_sync_carrier import transport_for
    return processed + content_sync_media.process_uploads(store, tenant, limit=limit, stop=stop, transport_for=transport_for)


def run_loop(store, tenant, stop, *, once=False, refresh_seconds=15,
             load_cohort=None, worker_type=None, observe=None, vault_jobs=None, ledger_jobs=None,facts_reader=None,enrollment_jobs=None,
             offer_jobs=None, content_sync_jobs=None, worker_options=None):
    """Refresh bounded ordered cohorts without starving accounts after row 32.

    ``worker_options`` carries the launch feature flags (history_recovery, history_memory, engagement);
    absent means every launch feature is off.

    Cursor advancement is over scanned candidates, including invalid/expired
    credentials. Verified inactive accounts import history without sending.
    The explicit dependency seams support network-free lifecycle tests.
    """
    if load_cohort is None:
        from onlyfans_runtime.connections import load_worker_cohort
        from functools import partial
        load_cohort = partial(load_worker_cohort, include_read_only=True)
    if worker_type is None:
        from onlyfans_runtime.native_worker import NativeWorker
        worker_type = NativeWorker
    observe = _emit if observe is None else observe
    cursor = None
    maintenance_cursor = 0
    maintenance_backoff = {}

    def maintain(job, phase):
        nonlocal failed
        count, due = maintenance_backoff.get(phase, (0, 0))
        if time.monotonic() < due:
            return
        try:
            job(store, tenant, limit=1, stop=stop)
        except Exception as exc:
            from onlyfans_runtime.store import Refused
            from sqlalchemy.exc import DBAPIError
            # A statement/lock timeout rolls back this job's transaction.
            # Connection loss, schema failures and unclassified errors still
            # abort the cycle through the existing outer failure boundary.
            local = isinstance(exc, Refused) or (
                isinstance(exc, DBAPIError) and not exc.connection_invalidated
                and getattr(exc.orig, "pgcode", None) in {"55P03", "57014"})
            if not local:
                raise
            failed = True
            count = min(count + 1, 6)
            delay = min(60, 2 ** count)
            maintenance_backoff[phase] = (count, time.monotonic() + delay)
            observe({"schemaVersion":"of-worker.v1", "state":"maintenance_deferred",
                     "phase":phase, "retrySeconds":delay})
        else:
            maintenance_backoff.pop(phase, None)

    while not stop.is_set():
        failed = False
        try:
            # Operator-requested vault reads need a verified connection, not an
            # outgoing-message activation or AI key. The job owner rechecks its
            # frozen scope/generation and bounds each page before persistence.
            if enrollment_jobs is not None:
                maintain(enrollment_jobs, "enrollment")
                if stop.is_set():
                    break
            # Rotate the first background turn so a long vault/archive catch-up
            # cannot repeatedly consume the shared spare token before ledger or
            # content work. Recent intake retains its separate token reserve.
            maintenance = tuple((phase, job) for phase, job in (
                ("vault", vault_jobs), ("ledger", ledger_jobs),
                ("offer", offer_jobs), ("content_sync", content_sync_jobs)) if job is not None)
            if maintenance:
                first = maintenance_cursor % len(maintenance)
                maintenance_cursor += 1
                for phase, job in maintenance[first:] + maintenance[:first]:
                    maintain(job, phase)
                    if stop.is_set():
                        break
                if stop.is_set():
                    break
            accounts, next_cursor, scanned = load_cohort(store, tenant, limit=32, after_account=cursor, stop=stop,
                **({"facts_reader":facts_reader} if facts_reader is not None else {}))
            cursor = next_cursor
            if stop.is_set():
                break
            if accounts:
                from onlyfans_runtime.store import Refused
                for account in accounts:
                    if stop.is_set():
                        break
                    revision=getattr(account,"read_connection_revision",None)
                    try:
                        store.request_poll(tenant, account.account_id,
                            **({"connection_revision":revision} if revision is not None else {}))
                    except Refused:
                        # Scope/health can change after cohort loading. The
                        # worker fences that account too; still poll the rest.
                        continue
                if stop.is_set():
                    break
                worker = worker_type(store, tenant, tuple(accounts),**({"facts_reader":facts_reader} if facts_reader is not None else {}),**(worker_options or {}))
                # Finish at least one pass over the selected cohort before
                # advancing its DB cursor, even for a short refresh interval.
                # --once intentionally performs one bounded four-account cycle.
                cycles = 1 if once else max(math.ceil(len(accounts)/4), math.ceil(refresh_seconds/.5))
                worker.run(stop, max_cycles=cycles, poll_seconds=.5, observe=observe,
                    **({"max_seconds":refresh_seconds,"minimum_cycles":max(1,math.ceil(len(accounts)/4))} if not once else {}))
            else:
                observe({"schemaVersion":"of-worker.v1", "state":"idle", "connectedAccounts":0,
                         "scannedAccounts":scanned})
                if not once:
                    stop.wait(refresh_seconds)
        except Exception:
            failed = True
            # Never log credential-bearing exception text, DB URLs, capture
            # bodies or provider responses. Historical rows retain outcomes.
            observe({"schemaVersion":"of-worker.v1", "state":"unavailable"})
            if not once:
                stop.wait(min(refresh_seconds, 5))
        if once:
            return 1 if failed else 0
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="One bounded credential refresh and four-account cycle")
    parser.add_argument("--refresh-seconds", type=float, default=15,
                        help="Minimum refresh window in seconds; a cohort completes its bounded pass first")
    parser.add_argument("--verify-post-cursor-account", help="Only verify the scoped account's paid-post cursor using two read pages")
    parser.add_argument("--command-key", help="Exact durable command identity for paid-post cursor verification")
    parser.add_argument("--actor", help="Existing revocable operator identity for paid-post cursor verification")
    args = parser.parse_args(argv)
    if args.verify_post_cursor_account is not None:
        from onlyfans_read.agency_scope import actor_allowed
        if (args.once or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", args.verify_post_cursor_account)
                or type(args.command_key) is not str or re.fullmatch(r"[a-f0-9]{64}", args.command_key) is None
                or not actor_allowed(args.actor)):
            parser.error("Cursor verification requires an account, exact command key and managing actor; it cannot run with --once")
    elif args.command_key is not None or args.actor is not None:
        parser.error("Command identity requires --verify-post-cursor-account")
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        parser.error("ONLYFANS_PROCESS_ROLE=worker is required")
    tenant = os.environ.get("ONLYFANS_WORKER_TENANT", "")
    if tenant == "deployment" or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", tenant):
        parser.error("An explicit ONLYFANS_WORKER_TENANT is required")
    if not math.isfinite(args.refresh_seconds) or not 1 <= args.refresh_seconds <= 60:
        parser.error("Refresh interval must be 1–60 seconds")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", os.environ.get("ONLYFANS_CONNECTION_KEY", "")):
        parser.error("An explicit 32-byte ONLYFANS_CONNECTION_KEY in hexadecimal is required")
    from sqlalchemy.engine import make_url
    try:
        parsed = make_url(os.environ.get("DATABASE_URL", ""))
        if parsed.drivername not in {"postgresql", "postgresql+psycopg2"} or not parsed.database:
            raise ValueError()
        # Bound startup/reconnect connection establishment too. Store already
        # bounds DB statements, locks and pool waits and verifies the schema.
        parsed = parsed.update_query_dict({"connect_timeout":"5"})
        url = parsed.render_as_string(hide_password=False)
    except Exception:
        parser.error("An explicit PostgreSQL DATABASE_URL is required")

    from onlyfans_runtime.store import Store
    stop = threading.Event()
    previous = {}
    store = None
    media_worker = None
    ledger_worker = None
    description_worker = None
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        try:
            store = Store(url)
        except Exception:
            _emit({"schemaVersion":"of-worker.v1", "state":"startup_unavailable"})
            return 1
        if args.verify_post_cursor_account is not None:
            # This mode never enters ordinary intake, planning, uploads or
            # dispatch, even if their feature flags are configured. The owner
            # performs two bounded read pages and records session-bound proof.
            from onlyfans_runtime.offer_post_source import verify_cursor_contract
            try:
                if stop.is_set():
                    return 1
                result = verify_cursor_contract(store, tenant, args.verify_post_cursor_account,
                    command_key=args.command_key, actor=args.actor)
                if (result.get("verified") is not True or result.get("contract") != "beforePublishTime.v1"
                        or type(result.get("connectionRevision")) is not int):
                    raise ValueError("unconfirmed proof")
            except Exception:
                _emit({"schemaVersion":"of-worker.v1", "state":"post_cursor_verification_unavailable"})
                return 1
            _emit({"schemaVersion":"of-worker.v1", "state":"post_cursor_contract_verified",
                   "contract": result["contract"], "connectionRevision": result["connectionRevision"]})
            return 0
        # The same private Twin admin connection can revalidate upload roles.
        # Missing configuration leaves uploads held; it never creates authority.
        if os.environ.get("TWIN_FACTS_BASE_URL") and os.environ.get("TWIN_ADMIN_API_KEY"):
            from onlyfans_runtime.upload_permission import UploadPermissionReader
            store.content_upload_permission_reader=UploadPermissionReader(
                os.environ["TWIN_FACTS_BASE_URL"],os.environ["TWIN_ADMIN_API_KEY"])
        facts_reader=None
        if os.environ.get("ONLYFANS_TWIN_FACTS_ENABLED")=="1":
            from onlyfans_runtime.creator_facts import TwinFactsReader
            try:facts_reader=TwinFactsReader(os.environ.get("TWIN_FACTS_BASE_URL",""),os.environ.get("TWIN_ADMIN_API_KEY",""))
            except Exception:
                _emit({"schemaVersion":"of-worker.v1","state":"creator_source_unavailable"})
                return 1
        if os.environ.get("ONLYFANS_VAULT_MEDIA_ENABLED") == "1" and not args.once:
            from onlyfans_runtime.vault_media_worker import MediaWorker
            media_worker = MediaWorker(store, tenant, stop)
            media_worker.start()
        if os.environ.get("ONLYFANS_LEDGER_ENABLED") == "1" and not args.once:
            from onlyfans_runtime.ledger_worker import LedgerWorker
            ledger_worker = LedgerWorker(store, tenant, stop, observe=_emit)
            ledger_worker.start()
        if (os.environ.get("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED") == "1"
                and os.environ.get("ONLYFANS_VAULT_DESCRIPTIONS_ENABLED", "1") == "1" and not args.once):
            from onlyfans_runtime.vault_description_worker import VaultDescriptionWorker
            description_worker = VaultDescriptionWorker(store, tenant, stop, observe=_emit)
            description_worker.start()
        # Launch features stay off unless explicitly enabled; engagement additionally needs saved per-account settings.
        worker_options = {name: True for name, flag in (("history_recovery", "ONLYFANS_HISTORY_RECOVERY_ENABLED"),
            ("history_memory", "ONLYFANS_HISTORY_MEMORY_ENABLED"), ("engagement", "ONLYFANS_ENGAGEMENT_ENABLED"))
            if os.environ.get(flag) == "1"}
        return run_loop(store, tenant, stop, once=args.once, refresh_seconds=args.refresh_seconds,facts_reader=facts_reader,
                        vault_jobs=_process_vault_jobs, enrollment_jobs=_process_connection_enrollments,
                        ledger_jobs=_process_ledger_jobs if args.once and os.environ.get("ONLYFANS_LEDGER_ENABLED") == "1" else None,
                        offer_jobs=_process_offer_jobs if os.environ.get("ONLYFANS_OFFER_IMPORT_ENABLED") == "1" else None,
                        content_sync_jobs=(_process_content_sync if os.environ.get("ONLYFANS_CONTENT_SYNC_ENABLED") == "1"
                            else _process_import_sync if os.environ.get("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED") == "1" else None),
                        worker_options=worker_options)
    finally:
        stop.set()
        if description_worker is not None:
            description_worker.join()
        if media_worker is not None:
            media_worker.join()
        if ledger_worker is not None:
            ledger_worker.join()
        try:
            if store is not None:
                store.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())

