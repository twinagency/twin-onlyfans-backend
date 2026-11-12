"""W5 review (rwl/W5-delta-browserbase-login): red tests for W5BB-1.

Same fakes as test_browserbase_login.py (no network, no browser, synthetic ids).
Green once a capture is bound to the account it was started for.
"""
import pytest  # noqa: F401  (runner: pytest detection)
import test_browserbase_login as t
from test_browserbase_login import world, client  # noqa: F401  (pytest fixtures)


def test_w5bb1_capture_of_another_creator_is_not_offered(client, world):
    # Account path id is of-<owner>; the live-view login lands on owner UID (123456).
    job = client.post(t.base(t.OTHER), headers=t.H, json={}).json()['jobId']
    r = t.wait_state(client, job, {'done', 'failed'}, account=t.OTHER)
    assert r['state'] == 'failed' and 'capture' not in r, r.get('ownerId')
    assert 'owner_mismatch' in r['error']


def test_w5bb1_curl_route_refuses_another_owner(client, world):
    curl = ("curl 'https://onlyfans.com/api2/v2/users/me' -H 'user-id: " + t.UID + "' -H 'x-bc: " + 'b' * 40 + "'"
            " -H 'x-of-rev: 202611120000-abc' -H 'sign: " + t.SIGN + "' -H 'time: 1762900000000' -H 'user-agent: UA'"
            " -H 'x-hash: synthetic-xhash' -H 'x-relay-static-param: synthetic-static' -b 'auth_id=" + t.UID + "; sess=s'")
    url = '/onlyfans/connection/v1/accounts/%s/capture-from-curl'
    r = client.post(url % t.OTHER, headers=t.H, json={'curl': curl})
    assert r.status_code == 409 and r.json()['code'] == 'owner_mismatch' and 'capture' not in r.json()
    assert client.post(url % t.ACCOUNT, headers=t.H, json={'curl': curl}).json()['ownerId'] == int(t.UID)


def test_w5bb4_malformed_sign_not_echoed(client, world, monkeypatch, caplog):
    bad = 'SIGNEDMATERIALabcdefghABCDEFGH'  # no chars that collide with synthetic cookie values
    monkeypatch.setitem(t.CAPTURE['headers'], 'sign', bad)
    job = client.post(t.base(), headers=t.H, json={}).json()['jobId']
    r = t.wait_state(client, job, {'done', 'failed'})
    assert r['state'] == 'failed'
    assert bad not in (r.get('error') or '') and bad not in caplog.text
    assert 'Sign header malformed' in r['error']
