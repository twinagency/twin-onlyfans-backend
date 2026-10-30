"""Opt-in, account-scoped import turns between archive work.

No new workers, credentials, provider limits or outgoing authority. Each lane
retains its existing durable cursor, fencing and retry deadlines.
"""
import os


def enabled(account):
    return account in {value.strip() for value in
        os.environ.get('ONLYFANS_FAIR_SYNC_ACCOUNTS', '').split(',') if value.strip()}


def turn(store, tenant, account):
    if not enabled(account):
        return
    # Store survives cohort reconstruction. Rotate even when a lane has no work
    # or is deferred, so a large archive cannot monopolise the next opportunity.
    turns = getattr(store, '_fair_sync_turns', None)
    if turns is None:
        turns = store._fair_sync_turns = {}
    key = (tenant, account)
    index = turns.get(key, 0)
    turns[key] = (index + 1) % 3
    if index == 0:
        from .vault_management import process_vault_jobs
        process_vault_jobs(store, tenant, limit=1, account_id=account)
    elif index == 1:
        if os.environ.get('ONLYFANS_OFFER_IMPORT_ENABLED') != '1':
            return
        from .offer_import_worker import process_refresh_jobs
        process_refresh_jobs(store, tenant, limit=1, account_id=account)
    else:
        if not any(os.environ.get(flag) == '1' for flag in
                   ('ONLYFANS_CONTENT_SYNC_ENABLED', 'ONLYFANS_AUTOMATIC_IMPORTS_ENABLED')):
            return
        from .offer_post_source import process_posts
        process_posts(store, tenant, limit=1, account_id=account)
