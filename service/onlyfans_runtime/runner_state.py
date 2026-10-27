"""Read-only view of the FASTT supervisor's health file, per account.

A verified connection says the provider session works. It does not say an AI
runner is attached and sending. The supervisor (``native_pilot.supervisor``)
rewrites ``FASTT_SUPERVISOR_STATUS`` (default ``/var/lib/fastt/supervisor-status.json``)
every reconcile; this module only reads it. Anything we cannot prove is reported as
``unknown`` and never as ``running``: a missing, unreadable, malformed or stale file,
or a path this process cannot see (the API container may not mount it).
"""
from __future__ import annotations
import json,os,re
from datetime import datetime,timedelta,timezone

DEFAULT_STATUS='/var/lib/fastt/supervisor-status.json'
MAX_BYTES=262144
KNOWN={'running','held','restarting','stopping'}
REASON=re.compile(r'[A-Za-z0-9:_.\-]{1,80}')

def _unknown(reason):
    return {'state':'unknown','reason':reason,'observedAt':None,'stale':False,'restarts':None}

def runner_state(account,*,path=None,now=None):
    """{'state','reason','observedAt','stale','restarts'} for one ``of-<owner>`` account.

    ``state`` is one of running, held, restarting, stopping, not_supervised, unknown.
    ``not_supervised`` means a fresh health file exists and does not list the account.
    """
    path=path or os.environ.get('FASTT_SUPERVISOR_STATUS') or DEFAULT_STATUS
    now=now or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with open(path,'rb') as f:raw=f.read(MAX_BYTES+1)
    except OSError:return _unknown('status_unavailable')
    if len(raw)>MAX_BYTES:return _unknown('status_invalid')
    try:
        body=json.loads(raw)
        stamp=body['updated_at'];accounts=body['accounts'];interval=body.get('interval_s',30)
        if type(accounts)is not dict or type(stamp)is not str or type(interval)not in (int,float) or isinstance(interval,bool):raise ValueError()
        seen=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        seen=seen.astimezone(timezone.utc).replace(tzinfo=None) if seen.tzinfo else seen
    except (ValueError,KeyError,TypeError,RecursionError):return _unknown('status_invalid')
    observed=seen.isoformat(timespec='seconds')+'Z'
    # The supervisor rewrites on every reconcile; three missed beats means it is not running.
    limit=timedelta(seconds=max(120.0,3*min(max(float(interval),5.0),600.0)))
    if now-seen>limit or seen-now>timedelta(minutes=5):
        return {'state':'unknown','reason':'status_stale','observedAt':observed,'stale':True,'restarts':None}
    entry=accounts.get(account)
    if entry is None:
        return {'state':'not_supervised','reason':None,'observedAt':observed,'stale':False,'restarts':None}
    if type(entry)is not dict or entry.get('state')not in KNOWN:return _unknown('status_invalid')
    reason=entry.get('reason');restarts=entry.get('restarts')
    return {'state':entry['state'],'reason':reason if type(reason)is str and REASON.fullmatch(reason) else None,
        'observedAt':observed,'stale':False,'restarts':restarts if type(restarts)is int and not isinstance(restarts,bool) and 0<=restarts<10**9 else None}
