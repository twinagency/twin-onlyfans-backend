"""Fixed proxy reservations. Server-only credentials; no network calls or AI activation."""
from __future__ import annotations
import base64, hashlib, json, os, re
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from db.onlyfans_connection_models import OfProxyAssignment, OfProviderConnection
from db.models import OfCreatorBinding
from .store import Refused, utcnow
from .connections import _aad, _seal, _open, encryption_key, capture_network_fingerprint

def inventory(raw=None):
    """Validate the entire operator-supplied fixed pool without leaking bad values."""
    try:
        rows=json.loads(os.environ.get('ONLYFANS_FIXED_PROXY_POOL_JSON','[]') if raw is None else raw)
        if type(rows) is not list or len(rows)>10000:raise ValueError()
        result=[]; ids=set(); urls=set()
        for row in rows:
            if type(row) is not dict or set(row)!={'id','url'}:raise ValueError()
            identity,url=row['id'],row['url']
            if type(identity) is not str or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',identity):raise ValueError()
            if type(url) is not str or not re.fullmatch(r'[\x21-\x7e]{1,2048}',url):raise ValueError()
            parsed=urlsplit(url)
            if parsed.scheme not in {'http','https','socks5'} or not parsed.hostname or not parsed.port or parsed.path not in {'','/'} or parsed.query or parsed.fragment:raise ValueError()
            # Canonicalize equivalent URLs so spelling cannot assign the same endpoint twice.
            host=parsed.hostname.lower()
            host='['+host+']' if ':' in host else host
            login=url.split('://',1)[1].rsplit('@',1)[0]+'@' if '@' in url else ''
            url=parsed.scheme+'://'+login+host+':'+str(parsed.port)
            fingerprint=capture_network_fingerprint(url)
            if identity in ids or fingerprint in urls:raise ValueError()
            ids.add(identity);urls.add(fingerprint);result.append((identity,url,fingerprint))
        return result
    except Exception:
        raise Refused('proxy pool configuration is unavailable') from None

def _clear(row,tenant,creator):
    try:
        raw=base64.b64decode(row.sealed_proxy,validate=True)
        value=json.loads(AESGCM(encryption_key()).decrypt(raw[:12],raw[12:],_aad(tenant,'proxy-pool',creator,1)))
        url=value['proxyUrl']
        if capture_network_fingerprint(url)!=row.proxy_fingerprint:raise ValueError()
        return url
    except Exception:
        raise Refused('reserved proxy is unavailable') from None

def reserve_proxy(store,tenant,creator):
    """One stable proxy per tenant+creator, unique across the deployment, including retries."""
    if any(type(x) is not str or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}',x) for x in (tenant,creator)) or tenant=='deployment':
        raise Refused('scope unavailable')
    with Session(store.engine) as s,s.begin():
        # Serialize pool allocation, not a browser session or provider call.
        s.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':418526033947})
        prior=s.get(OfProxyAssignment,(tenant,creator))
        if prior is not None:return _clear(prior,tenant,creator)
        choices=inventory()
        used_ids=set(s.scalars(select(OfProxyAssignment.pool_id)))
        used_urls=set(s.scalars(select(OfProxyAssignment.proxy_fingerprint)))
        # Existing manually connected accounts must not be assigned to another creator.
        for connection,binding in s.execute(select(OfProviderConnection,OfCreatorBinding).join(
                OfCreatorBinding,OfProviderConnection.binding_id==OfCreatorBinding.id)):
            proxy=_open(connection,binding.tenant_id)['proxyUrl']
            if proxy:used_urls.add(capture_network_fingerprint(proxy))
        for identity,url,fingerprint in choices:
            if identity in used_ids or fingerprint in used_urls:continue
            s.add(OfProxyAssignment(tenant_id=tenant,creator_id=creator,pool_id=identity,
                proxy_fingerprint=fingerprint,sealed_proxy=_seal({'proxyUrl':url},tenant,'proxy-pool',creator,1),
                created_at=utcnow()))
            s.flush()
            return url
    raise Refused('proxy pool exhausted')
