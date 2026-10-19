"""Worker-verified first connections. Unverified owner IDs never reserve accounts."""
from __future__ import annotations
import base64,hashlib,json,re,uuid
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,func,text,or_
from sqlalchemy.orm import Session
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from db.models import Account,OfCreatorBinding
from db.onlyfans_connection_models import OfConnectionEnrollment,OfProviderConnection
from .connections import (_validate_identity,_request,_seal,_aad,_open,encryption_key,
    provision_account,save_connection,get_connection,capture_network_fingerprint)
from .provider_connector import session_from_capture,NativeProviderConnector
from .browser_capture import validate_browser_payload,normalize_browser_payload
from .store import Refused,utcnow

def _scope(tenant,creator):
    if (type(tenant)is not str or tenant=='deployment' or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}',tenant)
        or type(creator)is not str or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}',creator)):
        raise Refused('scope unavailable')

def _public(row):
    state='expired' if row.status in {'pending','checking'} and row.expires_at<=utcnow() else row.status
    return {'id':row.id,'status':state,'accountId':row.account_id if state=='accepted' else None,
        'error':row.last_error if state=='failed' else None,'expiresAt':row.expires_at.isoformat()+'Z'}

def submit_enrollment(store,tenant,*,creator_id,creator_name,handle,payload,command_key,actor):
    _scope(tenant,creator_id);_validate_identity(command_key,actor,0)
    if (not actor.startswith('creator-session:') or type(creator_name)is not str or not creator_name.strip()
        or len(creator_name.encode())>160 or '\x00' in creator_name or type(handle)is not str
        or not handle.strip() or len(handle.encode())>160 or '\x00' in handle
        or type(payload)is not dict):raise Refused('invalid connection enrollment')
    try:
        expires=datetime.fromisoformat(payload['expiresAt'].replace('Z','+00:00'))
        if 'browser' in payload:validate_browser_payload(payload)
        else:
            if set(payload) not in ({'ownerId','capture','xHash','expiresAt'},{'ownerId','capture','xHash','expiresAt','networkFingerprint'}):raise ValueError()
            if 'networkFingerprint' in payload and (type(payload['networkFingerprint']) is not str or not re.fullmatch(r'[a-f0-9]{64}',payload['networkFingerprint'])):raise ValueError()
            session_from_capture(account_id='of-'+str(payload['ownerId']),owner_id=payload['ownerId'],policy_revision=1,
                capture=payload['capture'],x_hash=payload['xHash'],proxy_url=None,expires_at=expires)
    except Exception:raise Refused('invalid connection enrollment') from None
    fingerprint=_request('enroll',{'creatorId':creator_id,'creatorName':creator_name,'handle':handle,'session':payload},0)
    with Session(store.engine) as s,s.begin():
        lock=int.from_bytes(hashlib.sha256(('enroll:'+tenant).encode()).digest()[:8],'big',signed=True)
        s.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
        prior=s.scalar(select(OfConnectionEnrollment).where(OfConnectionEnrollment.tenant_id==tenant,OfConnectionEnrollment.command_key==command_key))
        if prior:
            if prior.creator_id!=creator_id or prior.actor!=actor or prior.request_hash!=fingerprint:raise Refused('connection idempotency conflict')
            return _public(prior)
        recent=s.scalar(select(func.count()).select_from(OfConnectionEnrollment).where(OfConnectionEnrollment.tenant_id==tenant,
            OfConnectionEnrollment.creator_id==creator_id,OfConnectionEnrollment.created_at>utcnow()-timedelta(hours=1)))
        if recent>=6:raise Refused('connection enrollment limit')
        pending=s.scalar(select(func.count()).select_from(OfConnectionEnrollment).where(OfConnectionEnrollment.tenant_id==tenant,
            OfConnectionEnrollment.status.in_(['pending','checking']),OfConnectionEnrollment.expires_at>utcnow()))
        if pending>=256:raise Refused('connection enrollment limit')
        identity=str(uuid.uuid4())
        row=OfConnectionEnrollment(id=identity,tenant_id=tenant,creator_id=creator_id,creator_name=creator_name,handle=handle,
            owner_id=payload['ownerId'],actor=actor,command_key=command_key,request_hash=fingerprint,status='pending',
            sealed_config=_seal(payload,tenant,'enrollment:'+identity,creator_id,1),expires_at=expires.astimezone(timezone.utc).replace(tzinfo=None),
            generation=0,created_at=utcnow(),updated_at=utcnow())
        s.add(row);s.flush();return _public(row)

def get_enrollments(store,tenant,creator_id):
    _scope(tenant,creator_id)
    with Session(store.engine) as s:
        rows=s.scalars(select(OfConnectionEnrollment).where(OfConnectionEnrollment.tenant_id==tenant,OfConnectionEnrollment.creator_id==creator_id)
            .order_by(OfConnectionEnrollment.created_at.desc(),OfConnectionEnrollment.id).limit(5)).all()
        return [_public(row) for row in rows]

def _clear(row):
    try:
        raw=base64.b64decode(row.sealed_config,validate=True)
        payload=json.loads(AESGCM(encryption_key()).decrypt(raw[:12],raw[12:],_aad(row.tenant_id,'enrollment:'+row.id,row.creator_id,1)))
        if (type(payload)is not dict or payload.get('ownerId')!=row.owner_id
            or datetime.fromisoformat(payload['expiresAt'].replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)!=row.expires_at):raise ValueError()
        return payload
    except Exception:raise Refused('stored connection enrollment is unavailable') from None

def process_enrollments(store,tenant,*,limit=1,stop=None,connector_type=NativeProviderConnector):
    # Use precisely the existing worker/provider role boundary, never the API.
    NativeProviderConnector._ready()
    if type(limit)is not int or not 1<=limit<=4:raise Refused('invalid enrollment bound')
    _scope(tenant,'creator')
    completed=0
    for _ in range(limit):
        if stop is not None and stop.is_set():break
        with Session(store.engine) as s,s.begin():
            row=s.scalar(select(OfConnectionEnrollment).where(OfConnectionEnrollment.tenant_id==tenant,
                or_(OfConnectionEnrollment.status=='pending',
                    (OfConnectionEnrollment.status=='checking')&(OfConnectionEnrollment.lease_until<=utcnow())))
                .order_by(OfConnectionEnrollment.created_at,OfConnectionEnrollment.id).with_for_update(skip_locked=True).limit(1))
            if row is None:break
            if row.expires_at<=utcnow():
                row.status='expired';row.sealed_config=None;row.updated_at=utcnow();completed+=1;continue
            row.status='checking';row.generation+=1;row.lease_until=utcnow()+timedelta(seconds=60);row.updated_at=utcnow()
            identity,generation,creator_id,creator_name,handle,owner_id,actor=(row.id,row.generation,row.creator_id,row.creator_name,row.handle,row.owner_id,row.actor)
            try:payload=_clear(row)
            except Refused:
                row.status='failed';row.last_error='session_unavailable';row.sealed_config=None;completed+=1;continue
        account='of-'+str(owner_id)
        try:
            with Session(store.engine) as s:
                binding=s.scalar(select(OfCreatorBinding).where(OfCreatorBinding.account_id==account,OfCreatorBinding.revoked_at.is_(None)))
                existing=s.get(Account,account)
                if existing is not None and (binding is None or binding.tenant_id!=tenant or binding.creator_id!=creator_id):raise Refused('scope unavailable')
                connection=s.get(OfProviderConnection,account)
                proxy=_open(connection,tenant)['proxyUrl'] if connection is not None else None
            network_fingerprint=payload.pop('networkFingerprint',None)
            if network_fingerprint is not None and network_fingerprint!=capture_network_fingerprint(proxy):raise Refused('capture network changed')
            if 'browser' in payload:
                payload=normalize_browser_payload(payload,proxy=proxy)
                # Freeze normalized credentials before provider verification or
                # any canonical write. A reclaimed lease replays the exact same
                # credential command even if the CDN hash or revision changes.
                with Session(store.engine) as s,s.begin():
                    row=s.get(OfConnectionEnrollment,identity,with_for_update=True)
                    if row.status!='checking' or row.generation!=generation or row.lease_until<=utcnow() or row.expires_at<=utcnow():continue
                    frozen={**payload,'networkFingerprint':network_fingerprint} if network_fingerprint is not None else payload
                    row.sealed_config=_seal(frozen,tenant,'enrollment:'+identity,creator_id,1)
            session=session_from_capture(account_id=account,owner_id=owner_id,policy_revision=1,capture=payload['capture'],
                x_hash=payload['xHash'],proxy_url=proxy,expires_at=datetime.fromisoformat(payload['expiresAt'].replace('Z','+00:00')))
            connector_type(session).verify_identity()
            # No canonical account/binding is written until this exact owner is proven.
            if stop is not None and stop.is_set():break
            provision_key=hashlib.sha256(('enrollment:provision:'+identity).encode()).hexdigest()
            save_key=hashlib.sha256(('enrollment:session:'+identity).encode()).hexdigest()
            with Session(store.engine) as s,s.begin():
                row=s.get(OfConnectionEnrollment,identity,with_for_update=True)
                if row.status!='checking' or row.generation!=generation or row.lease_until<=utcnow() or row.expires_at<=utcnow():continue
                # Keep the claim locked during local writes; provider HTTP has
                # finished. Other workers cannot reclaim between this guard
                # and canonical provisioning. Provision has its own account lock.
                if existing is None:
                    provision_account(store,tenant,creator_id=creator_id,creator_name=creator_name,owner_id=owner_id,handle=handle,command_key=provision_key,actor=actor)
                if row.expected_revision is None:
                    connection=s.get(OfProviderConnection,account)
                    row.expected_revision=connection.revision if connection is not None else 0
                expected=row.expected_revision
            # Freeze the revision durably before save so a process crash after
            # its commit can replay precisely the same command and fingerprint.
            with Session(store.engine) as s,s.begin():
                row=s.get(OfConnectionEnrollment,identity,with_for_update=True)
                if row.status!='checking' or row.generation!=generation or row.lease_until<=utcnow() or row.expires_at<=utcnow():continue
                save_connection(store,tenant,account,payload=payload,expected_revision=expected,command_key=save_key,actor=actor,session_only=True,expected_network_fingerprint=network_fingerprint)
                row.status='accepted';row.account_id=account;row.last_error=None;row.sealed_config=None;row.updated_at=utcnow()
            completed+=1
        except Exception:
            # Unknown provider results never attach an account or authorize sends.
            # A crash after the durable save replays the same frozen command key.
            with Session(store.engine) as s,s.begin():
                row=s.get(OfConnectionEnrollment,identity,with_for_update=True)
                if row.status=='checking' and row.generation==generation:
                    row.status='failed';row.last_error='verification_unavailable';row.sealed_config=None;row.updated_at=utcnow()
            completed+=1
    return completed
