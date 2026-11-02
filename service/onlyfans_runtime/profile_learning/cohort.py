"""Optional process cohort for a single-account rollout; defaults to existing scope."""
import json
import os
from onlyfans_runtime.store import Refused


def accounts():
    raw = os.environ.get('ONLYFANS_PROFILE_ACCOUNTS')
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise Refused('invalid-profile-account-scope') from None
    if type(value) is not list or any(type(v) is not str or not 1 <= len(v) <= 128 for v in value):
        raise Refused('invalid-profile-account-scope')
    return frozenset(value)


def includes(account):
    selected = accounts()
    return selected is None or account in selected
