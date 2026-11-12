"""Explicit account-bound deployment references for the existing supervisor.

No credentials are stored here. Each process receives its own reviewed file.
The same provider team may name several accounts for conservative shared usage.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

_FIELDS = {'version','root','tenant','account','binding','epoch','providerAccount','webhookId',
           'publicUrl','receiverUrl','senderUrl','databaseContainer','databaseRole','databaseName',
           'supervisorUnit','senderUnit','senderContainer','teamAccounts','creditStop',
           'initialExternalReserved','reservations','fans'}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')


def validate(value):
    if type(value) is not dict or set(value) != _FIELDS or value['version'] != 'twin-supervision.v1':
        raise ValueError('explicit supervision configuration required')
    out = dict(value)
    for key in ('tenant','account','binding','providerAccount','webhookId','databaseContainer',
                'databaseRole','databaseName','supervisorUnit','senderUnit','senderContainer'):
        if type(out[key]) is not str or not _ID.fullmatch(out[key]) or "'" in out[key]:
            raise ValueError('invalid supervision identity')
    if type(out['epoch']) is not int or out['epoch'] < 1:
        raise ValueError('explicit connection epoch required')
    root = Path(out['root'])
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('absolute account-specific root required')
    out['root'] = root
    for key in ('publicUrl','receiverUrl','senderUrl'):
        parsed=urlsplit(out[key])
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError('invalid supervisor endpoint')
        if key=='publicUrl':
            if parsed.scheme!='https' or parsed.hostname.endswith('trycloudflare.com'):
                raise ValueError('stable HTTPS webhook endpoint required')
        elif parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('loopback worker endpoint required')
    fans=out['fans']
    if (type(fans) is not list or not 1<=len(fans)<=100 or any(type(f) is not int or f<=0 for f in fans)
            or len(set(fans))!=len(fans)):
        raise ValueError('one to 100 explicit fans required')
    team=out['teamAccounts']
    if (type(team) is not list or not 1<=len(team)<=10 or len(set(team))!=len(team)
            or out['account'] not in team or any(type(a) is not str or not _ID.fullmatch(a) for a in team)):
        raise ValueError('explicit provider-team account list required')
    for key in ('creditStop','initialExternalReserved'):
        if type(out[key]) is not int or not 0<=out[key]<10**9:
            raise ValueError('invalid reviewed provider allowance')
    if out['creditStop']<=out['initialExternalReserved']:
        raise ValueError('provider allowance exhausted')
    if type(out['reservations']) is not list or len(out['reservations'])>1000:
        raise ValueError('invalid external reservation list')
    for row in out['reservations']:
        if type(row) is not dict or set(row)!={'path','amount','optional'}:
            raise ValueError('invalid external reservation')
        path=Path(row['path'])
        if path.is_absolute() or '..' in path.parts or type(row['amount']) is not int or row['amount']<0 or type(row['optional']) is not bool:
            raise ValueError('invalid external reservation')
    return out


def load():
    path=os.environ.get('TWIN_SUPERVISOR_CONFIG')
    if not path:
        raise ValueError('TWIN_SUPERVISOR_CONFIG is required')
    return validate(json.loads(Path(path).read_text()))
