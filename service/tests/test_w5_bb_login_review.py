"""W5 review (rwl/W5-delta-browserbase-login): red test for W5BB-1.

Same fakes as test_browserbase_login.py (no network, no browser, synthetic ids).
Green once the job binds the captured owner to the account it was started for.
"""
import pytest  # noqa: F401  (runner: pytest detection)
import test_browserbase_login as t
from test_browserbase_login import world, client  # noqa: F401  (pytest fixtures)


def test_w5bb1_capture_of_another_creator_is_not_offered(client, world):
    # Account path id is of-<owner>; the live-view login lands on owner UID (123456).
    job = client.post(t.base(t.OTHER), headers=t.H, json={}).json()['jobId']
    r = t.wait_state(client, job, {'done', 'failed'}, account=t.OTHER)
    assert r['state'] == 'failed' and 'capture' not in r, r.get('ownerId')
