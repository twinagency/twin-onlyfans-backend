"""Dedicated bounded archive pool. Uses the same durable provider budget as live work."""
import concurrent.futures as futures
import json
import os
import signal
import threading
import time

from .import_capacity import capacity


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
    refreshed = time.monotonic()
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
            if refresh is not None and now-refreshed >= 60:
                accounts = refresh()
                refreshed = time.monotonic()
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
                    recovery.request_account(tenant, account, connection_revision=revision)
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
    if not isinstance(configured, dict) or not configured:
        raise SystemExit('explicit account capacity map required')
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
            result.update((a.account_id, a) for a in rows if a.account_id in configured)
            if cursor is None:
                return result
        return result
    try:
        run(store, tenant, refresh(), stop,
            workers=int(os.environ.get('ONLYFANS_IMPORT_WORKERS', '64')), refresh=refresh)
    finally:
        store.close()


if __name__ == '__main__':
    main()
