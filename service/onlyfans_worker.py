"""Standalone native OnlyFans worker with explicit tenant and credentials.

Run schema migration separately before startup. This entry point imports no
legacy server/executor, creates no credentials and activates no account. The
worker stops between durable phases; bounded HTTP/DB calls may finish first.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _emit(value):
    print(json.dumps(value), flush=True)


def _process_vault_jobs(store, tenant, *, limit, stop):
    from onlyfans_runtime.vault_management import process_vault_jobs
    return process_vault_jobs(store, tenant, limit=limit, stop=stop)


def _process_ledger_jobs(store, tenant, *, limit, stop):
    from onlyfans_runtime.ledger import process_ledger_pages
    return process_ledger_pages(store, tenant, limit=limit, stop=stop)


def run_loop(store, tenant, stop, *, once=False, refresh_seconds=15,
             load_cohort=None, worker_type=None, observe=None, vault_jobs=None, ledger_jobs=None,facts_reader=None):
    """Refresh bounded ordered cohorts without starving accounts after row 32.

    Cursor advancement is over scanned candidates, including invalid/expired
    credentials. The loader performs no history work for inactive accounts.
    The explicit dependency seams support network-free lifecycle tests.
    """
    if load_cohort is None:
        from onlyfans_runtime.connections import load_worker_cohort
        load_cohort = load_worker_cohort
    if worker_type is None:
        from onlyfans_runtime.native_worker import NativeWorker
        worker_type = NativeWorker
    observe = _emit if observe is None else observe
    cursor = None
    while not stop.is_set():
        failed = False
        try:
            # Operator-requested vault reads need a verified connection, not an
            # outgoing-message activation or AI key. The job owner rechecks its
            # frozen scope/generation and bounds each page before persistence.
            if vault_jobs is not None:
                vault_jobs(store, tenant, limit=1, stop=stop)
                if stop.is_set():
                    break
            if ledger_jobs is not None:
                ledger_jobs(store, tenant, limit=1, stop=stop)
                if stop.is_set():
                    break
            accounts, next_cursor, scanned = load_cohort(store, tenant, limit=32, after_account=cursor, stop=stop,
                **({"facts_reader":facts_reader} if facts_reader is not None else {}))
            cursor = next_cursor
            if stop.is_set():
                break
            if accounts:
                for account in accounts:
                    if stop.is_set():
                        break
                    store.request_poll(tenant, account.account_id)
                if stop.is_set():
                    break
                worker = worker_type(store, tenant, tuple(accounts),**({"facts_reader":facts_reader} if facts_reader is not None else {}))
                # Finish at least one pass over the selected cohort before
                # advancing its DB cursor, even for a short refresh interval.
                # --once intentionally performs one bounded four-account cycle.
                cycles = 1 if once else max(math.ceil(len(accounts)/4), math.ceil(refresh_seconds/.5))
                worker.run(stop, max_cycles=cycles, poll_seconds=.5, observe=observe)
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
    args = parser.parse_args(argv)
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
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        try:
            store = Store(url)
        except Exception:
            _emit({"schemaVersion":"of-worker.v1", "state":"startup_unavailable"})
            return 1
        facts_reader=None
        if os.environ.get("ONLYFANS_TWIN_FACTS_ENABLED")=="1":
            from onlyfans_runtime.creator_facts import TwinFactsReader
            try:facts_reader=TwinFactsReader(os.environ.get("TWIN_FACTS_BASE_URL",""),os.environ.get("TWIN_ADMIN_API_KEY",""))
            except Exception:
                _emit({"schemaVersion":"of-worker.v1","state":"creator_source_unavailable"})
                return 1
        return run_loop(store, tenant, stop, once=args.once, refresh_seconds=args.refresh_seconds,facts_reader=facts_reader,
                        vault_jobs=_process_vault_jobs,
                        ledger_jobs=_process_ledger_jobs if os.environ.get("ONLYFANS_LEDGER_ENABLED") == "1" else None)
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
