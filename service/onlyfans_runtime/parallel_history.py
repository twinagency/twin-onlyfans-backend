"""Dedicated bounded archive pool. Uses the same durable provider budget as live work."""
import concurrent.futures as futures
import json
import os
import signal
import threading
import time

from .import_capacity import capacity


def _transient_database_error(exc):
    from sqlalchemy.exc import DBAPIError
    if not isinstance(exc, DBAPIError):
        return False
    code = getattr(exc.orig, 'pgcode', None)
    return (exc.connection_invalidated or code in {'55P03', '57014', '40001', '40P01'}
            or isinstance(code, str) and code.startswith('08'))


def run(store, tenant, accounts, stop, *, workers=64, refresh=None):
    """I/O concurrency across fans; cursors and session fences remain in HistoryRecovery.

    ``accounts`` contains only explicitly configured creator IDs. Refresh is invoked
    only in the coordinator; workers never load or mutate credentials.
    """
    from .launch_history_memory.recovery import HistoryRecovery
    if type(workers) is not int or not 1 <= workers <= 256:
        raise ValueError('history workers must be 1–256')
    recovery = HistoryRecovery(store)
    active, next_at, scanning, last_scan = {}, {}, set(), {}
    refresh_due = time.monotonic() + (60 if accounts else 0)
    refresh_failures = 0
    seed_failures = {}
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while not stop.is_set():
            now = time.monotonic()
            for task in list(active):
                if not task.done():
                    continue
                account, discovery = active.pop(task)
                if discovery:
                    scanning.discard(account)
                try:
                    pages = task.result()
                    state = 'progress' if pages else 'waiting'
                    if not pages:
                        next_at[account] = max(next_at.get(account, 0), now + .25)
                    error = None
                except Exception as exc:
                    pages, state, error = 0, 'deferred', type(exc).__name__
                    next_at[account] = max(next_at.get(account, 0), now + 1)
                print(json.dumps({'component':'parallel-history','account':account,
                    'state':state,'pages':pages,'errorType':error}), flush=True)
            if refresh is not None and now >= refresh_due:
                try:
                    accounts = refresh()
                except Exception as exc:
                    if not _transient_database_error(exc):
                        raise
                    # No new reads may use a cohort whose credentials could
                    # not be refreshed. Already admitted reads retain their
                    # existing durable generation and provider fences.
                    accounts = {}
                    refresh_failures = min(refresh_failures + 1, 7)
                    delay = min(60, 2 ** (refresh_failures - 1))
                    print(json.dumps({'component':'parallel-history','state':'cohort_refresh_deferred',
                        'errorType':type(exc).__name__,'retrySeconds':delay}), flush=True)
                else:
                    refresh_failures, delay = 0, 60
                refresh_due = time.monotonic() + delay
            if stop.is_set():
                break
            counts = {}
            for account, _ in active.values():
                counts[account] = counts.get(account, 0) + 1
            # Rotate iteration order after each submission using next_at, so a
            # large first account cannot occupy every global worker indefinitely.
            for account in sorted(accounts, key=lambda a: next_at.get(a, 0)):
                if len(active) >= workers:
                    break
                policy = capacity(account)
                if now < next_at.get(account, 0) or counts.get(account, 0) >= max(1,policy.inflight-1):
                    continue
                bound = accounts[account]
                revision = bound.read_connection_revision or getattr(bound.reader, 'connection_revision', None)
                discovery = account not in scanning and now-last_scan.get(account, -60) >= 2
                if discovery:
                    # One coordinator owns seeding/discovery for each account.
                    try:
                        recovery.request_account(tenant, account, connection_revision=revision)
                    except Exception as exc:
                        if not _transient_database_error(exc):
                            raise
                        failures = min(seed_failures.get(account, 0) + 1, 7)
                        seed_failures[account] = failures
                        delay = min(60, 2 ** (failures - 1))
                        next_at[account] = now + delay
                        print(json.dumps({'component':'parallel-history','account':account,
                            'state':'discovery_deferred','errorType':type(exc).__name__,
                            'retrySeconds':delay}), flush=True)
                        continue
                    seed_failures.pop(account, None)
                    scanning.add(account)
                    last_scan[account] = now
                task = pool.submit(recovery.step, tenant, account, bound.reader,
                    connection_revision=revision, archive_only=not discovery)
                active[task] = account, discovery
                next_at[account] = now + 1 / policy.rps
            stop.wait(.01)
        # Let bounded outstanding reads settle before disposing the database pool.
        for task in active:
            try:
                task.result()
            except Exception:
                pass


def main():
    tenant = os.environ.get('ONLYFANS_WORKER_TENANT', '')
    if (os.environ.get('ONLYFANS_PROCESS_ROLE') != 'worker' or not tenant or tenant == 'deployment'
            or os.environ.get('ONLYFANS_PARALLEL_IMPORT_ENABLED') != '1'):
        raise SystemExit('explicit worker tenant and parallel import enablement required')
    configured = json.loads(os.environ.get('ONLYFANS_IMPORT_CAPACITY', '{}'))
    from .history_policy import automatic_new_accounts
    if not isinstance(configured, dict) or (not configured and not automatic_new_accounts()):
        raise SystemExit('explicit account capacity map or new-account rollout required')
    for account in configured:
        capacity(account)
    from .store import Store
    from .connections import load_worker_cohort
    store = Store(os.environ['DATABASE_URL'], pool_size=16)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    def refresh():
        result, cursor = {}, None
        while not stop.is_set():
            rows, cursor, _ = load_worker_cohort(store, tenant, after_account=cursor,
                stop=stop, include_read_only=True)
            # Explicit capacity overrides remain unchanged. Newly enrolled
            # accounts use the existing conservative default governor limits.
            from sqlalchemy.orm import Session
            from .history_policy import automatic_new_accounts, policy
            with Session(store.engine) as session:
                for account in rows:
                    if account.account_id in configured or (automatic_new_accounts()
                            and policy(session,account.account_id) is not None):
                        result[account.account_id] = account
            if cursor is None:
                return result
        return result
    try:
        run(store, tenant, {}, stop,
            workers=int(os.environ.get('ONLYFANS_IMPORT_WORKERS', '64')), refresh=refresh)
    finally:
        store.close()


if __name__ == '__main__':
    main()
