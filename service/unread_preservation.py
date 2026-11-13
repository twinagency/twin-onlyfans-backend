"""Restore human inbox unread flags after OnlyFans background message GETs.

Pending undo markers live on the existing persistent spool volume. A failed
restore remains pending and must complete before this fan can be read again.
Only unread flags are restored; upstream does not expose an exact count setter.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
from contextlib import contextmanager


class UnreadPreservationError(RuntimeError):
    pass


def _paced(error):
    """A provider pacing hint (retry_after: 429, read transient) means stop calling the provider now.

    Such errors carry fixed non-secret messages, so they propagate unwrapped and
    the caller's read budget honours Retry-After; the pending undo waits."""
    return getattr(error, 'retry_after', None) is not None


def chat_was_unread(chat, fan_id):
    if not isinstance(chat, dict):
        raise UnreadPreservationError('unread snapshot is unavailable')
    user = chat.get('withUser')
    if not isinstance(user, dict) or str(user.get('id')) != str(fan_id):
        raise UnreadPreservationError('unread snapshot chat mismatch')
    count = chat.get('unreadMessagesCount')
    flag = chat.get('hasUnreadMessages')
    if type(count) is int and count >= 0:
        return count > 0 or flag is True
    if type(flag) is bool:
        return flag
    raise UnreadPreservationError('unread snapshot has no valid read state')


@contextmanager
def _journal(account_id, fan_id):
    account = str(account_id)
    fan = str(fan_id)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', account) or not re.fullmatch(r'[1-9][0-9]{0,19}', fan):
        raise UnreadPreservationError('invalid unread journal scope')
    root = Path(os.environ.get('ONLYFANS_UNREAD_JOURNAL_DIR', '/var/lib/twin-vault/spool/unread-preservation'))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = root / (account + '-' + fan + '.pending')
    fd = os.open(str(root / (account + '-' + fan + '.lock')), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield pending
    finally:
        os.close(fd)


def _restore(restore, fan_id, confirm=None):
    try:
        # A pending undo may already have reached the provider before its
        # response was lost. Resolve it only from an actual scoped snapshot.
        if confirm is not None and chat_was_unread(confirm(), fan_id):return
        try:
            result = restore(fan_id)
        except Exception as error:
            if _paced(error):
                raise
            if confirm is not None and chat_was_unread(confirm(), fan_id):return
            raise
        if confirm is not None:
            if not chat_was_unread(confirm(), fan_id):
                raise UnreadPreservationError('unconfirmed unread restoration')
        elif not isinstance(result, dict) or result.get('success') is not True:
            raise UnreadPreservationError('unconfirmed unread restoration')
    except Exception as error:
        if _paced(error):
            raise error from None
        # Provider exceptions may contain session/proxy credentials.
        raise UnreadPreservationError('OnlyFans unread restoration failed; pending undo retained') from None


def preserve_unread(*, account_id, fan_id, snapshot, read, restore, confirm=None):
    with _journal(account_id, fan_id) as pending:
        # Recover an interrupted or failed undo before taking a new snapshot.
        if pending.exists():
            _restore(restore, fan_id, confirm)
            pending.unlink()
        was_unread = chat_was_unread(snapshot(), fan_id)
        if was_unread:
            fd = os.open(str(pending), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as marker:
                marker.write('pending\n')
                marker.flush()
                os.fsync(marker.fileno())
        try:
            page = read()
        except Exception as error:
            if was_unread and not _paced(error):
                _restore(restore, fan_id, confirm)
                pending.unlink()
            raise
        if was_unread:
            _restore(restore, fan_id, confirm)
            pending.unlink()
        return page
