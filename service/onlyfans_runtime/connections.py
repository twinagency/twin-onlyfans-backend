"""Explicit scoped credential storage and worker-side provider verification.

Credentials are encrypted with an operator-owned key. API commands never make
provider requests, and uploading a capture never enables outgoing messages.
"""
from __future__ import annotations
import base64,hashlib,json,os,re,uuid
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from db.models import Account,AccountAiConfig,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfDeliveryPolicyChange
from db.onlyfans_connection_models import OfProviderConnection,OfConnectionChange
from onlyfans_read.contract import ID_PATTERN
from .store import Refused,utcnow
from .native_context import canonical
from .provider_connector import session_from_capture,NativeProviderConnector
from .hosted_provider import session_from_saved

def encryption_key():
    raw=os.environ.get('ONLYFANS_CONNECTION_KEY','')
    if not re.fullmatch(r'[a-fA-F0-9]{64}',raw):raise Refused('connection encryption is not configured')
    return bytes.fromhex(raw)

def _aad(tenant,account,binding,revision):
    return canonical([tenant,account,binding,revision]).encode()

def _seal(value,tenant,account,binding,revision):
    nonce=os.urandom(12)
    return base64.b64encode(nonce+AESGCM(encryption_key()).encrypt(nonce,canonical(value).encode(),_aad(tenant,account,binding,revision))).decode()

def _open(row,tenant):
    try:
        raw=base64.b64decode(row.sealed_config,validate=True)
        value=json.loads(AESGCM(encryption_key()).decrypt(raw[:12],raw[12:],_aad(tenant,row.account_id,row.binding_id,row.revision)))
        saved_expiry=datetime.fromisoformat(value['expiresAt'].replace('Z','+00:00'))
        if (value['ownerId']!=row.owner_id or saved_expiry.tzinfo is None
            or saved_expiry.astimezone(timezone.utc).replace(tzinfo=None)!=row.expires_at):raise ValueError()
        return value
    except Exception:
        raise Refused('stored connection credentials are unavailable') from None

def _request(action,payload,expected_revision):
    return hashlib.sha256(canonical({'action':action,'payload':payload,'expectedRevision':expected_revision}).encode()).hexdigest()

def _audit(s,row,key,actor,action,fingerprint):
    s.add(OfConnectionChange(account_id=row.account_id,command_key=key,revision=row.revision,
        action=action,request_hash=fingerprint,actor=actor,created_at=utcnow()))

def _validate_identity(key,actor,revision):
    if (type(key) is not str or not re.fullmatch(r'[a-f0-9]{64}',key) or type(actor) is not str
        or not re.fullmatch(r'(?:op-session|creator-session):[a-f0-9]{64}',actor) or type(revision) is not int or not 0<=revision<2**31-1):
        raise Refused('invalid connection command')

def provision_account(store,tenant,*,creator_id,creator_name,owner_id,handle,command_key,actor):
    """Create a new scoped account disabled. Existing legacy accounts are never adopted."""
    _validate_identity(command_key,actor,0)
    try:
        valid=(type(tenant) is str and tenant!='deployment' and bool(ID_PATTERN.fullmatch(tenant))
            and type(owner_id) is int and 0<owner_id<2**63 and type(creator_id) is str and bool(ID_PATTERN.fullmatch(creator_id))
            and type(creator_name) is str and bool(creator_name.strip()) and '\x00' not in creator_name and len(creator_name.encode())<=160
            and type(handle) is str and '\x00' not in handle and len(handle.encode())<=160)
    except UnicodeError:valid=False
    if not valid:raise Refused('invalid account registration')
    account='of-'+str(owner_id)
    binding_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([tenant,creator_id,account,command_key])))
    fingerprint=_request('provision',{'creatorId':creator_id,'creatorName':creator_name,'ownerId':owner_id,'handle':handle},0)
    with Session(store.engine) as s,s.begin():
        # The account does not yet have a runtime row to lock. Serialize its
        # creation so identical concurrent commands return the same event.
        lock=int.from_bytes(hashlib.sha256(account.encode()).digest()[:8],'big',signed=True)
        s.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
        existing=s.get(Account,account)
        if existing is not None:
            binding=s.scalar(select(OfCreatorBinding).where(OfCreatorBinding.id==binding_id,
                OfCreatorBinding.account_id==account,OfCreatorBinding.tenant_id==tenant,OfCreatorBinding.revoked_at.is_(None)))
            prior=s.get(OfConnectionChange,(account,command_key))
            if (binding is None or binding.creator_id!=creator_id or prior is None or prior.action!='provision'
                or prior.request_hash!=fingerprint or prior.actor!=actor):raise Refused('account registration conflict')
            return account
        s.add(Account(id=account,nickname=creator_name));s.flush()
        s.add(OfCreatorBinding(id=binding_id,tenant_id=tenant,creator_id=creator_id,creator_name=creator_name,
            account_id=account,handle=handle,channel='onlyfans'));s.flush()
        s.add(OfRuntime(account_id=account,binding_id=binding_id,activated_at=utcnow(),enabled=False))
        s.add(AccountAiConfig(account_id=account));s.flush()
        s.add(OfConnectionChange(account_id=account,command_key=command_key,revision=0,action='provision',
            request_hash=fingerprint,actor=actor,created_at=utcnow()))
    return account

def get_connection(store,tenant,account):
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        row=s.get(OfProviderConnection,account);policy=s.get(OfDeliveryPolicy,account)
        if row is not None and row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
        now=utcnow()
        credentials_valid=True
        if row is not None and row.expires_at>now:
            try:_open(row,tenant)
            except Refused:credentials_valid=False
        return {'accountId':account,'revision':row.revision if row else 0,
            'status':('expired' if row and row.expires_at<=now else row.status if credentials_valid else 'failed') if row else 'unconfigured',
            'ownerId':str(row.owner_id) if row else None,'expiresAt':row.expires_at.isoformat()+'Z' if row else None,
            'verifiedAt':row.verified_at.isoformat()+'Z' if row and row.verified_at else None,
            'lastError':('stored_credentials_unavailable' if not credentials_valid else row.last_error) if row else None,
            'outgoingEnabled':bool(row and credentials_valid and row.expires_at>now and row.status=='verified' and runtime.enabled and policy and policy.enabled and policy.transport_kind=='native'),
            'policyRevision':policy.revision if policy else 0,'usedUnits':policy.used_units if policy else 0,
            'limitUnits':policy.limit_units if policy else 0,'mediaEnabled':bool(policy and policy.media_enabled),
            'automateNewConversations':bool(row and row.automate_new_conversations)}

def save_connection(store,tenant,account,*,payload,expected_revision,command_key,actor,session_only=False):
    _validate_identity(command_key,actor,expected_revision)
    session_keys={'ownerId','hosted','expiresAt'} if type(payload)is dict and 'hosted' in payload else {'ownerId','capture','xHash','expiresAt'}
    required=session_keys if session_only else session_keys|{'proxyUrl','model'}
    if type(payload) is not dict or set(payload)!=required:raise Refused('invalid connection configuration')
    action='session' if session_only else 'save'
    fingerprint=_request(action,payload,expected_revision)
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=s.get(OfConnectionChange,(account,command_key))
        if prior:
            if prior.request_hash!=fingerprint or prior.actor!=actor:raise Refused('connection idempotency conflict')
            return prior.revision
        row=s.get(OfProviderConnection,account)
        if row is not None and row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
        if (row.revision if row else 0)!=expected_revision:raise Refused('connection revision conflict')
        if row is not None and row.owner_id!=payload['ownerId']:raise Refused('connection owner cannot change')
        if session_only:
            # A creator refreshes authentication only. Keep operator-owned network
            # and model settings inside the encrypted store, never in a response.
            previous=_open(row,tenant) if row is not None else {}
            payload={**payload,'proxyUrl':previous.get('proxyUrl'),'model':previous.get('model')}
        try:
            expires=datetime.fromisoformat(payload['expiresAt'].replace('Z','+00:00'))
            session_from_saved(payload=payload,account_id=account,policy_revision=1,expires_at=expires)
            model=payload['model']
            if model is not None:
                if type(model) is not dict or set(model)!={'modelId','apiKey'}:raise Refused('invalid selected model credential')
                from .provider_model import NativeProviderModel
                NativeProviderModel(account_id=account,model_id=model['modelId'],api_key=model['apiKey'],expires_at=expires)
        except Exception:raise Refused('invalid connection configuration') from None
        owner_lock=int.from_bytes(hashlib.sha256(('of-'+str(payload['ownerId'])).encode()).digest()[:8],'big',signed=True)
        s.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':owner_lock})
        duplicate=s.scalar(select(OfProviderConnection.account_id).where(
            OfProviderConnection.owner_id==payload['ownerId'],OfProviderConnection.account_id!=account).limit(1))
        if duplicate is not None:raise Refused('provider owner is already registered to another account')
        row=s.get(OfProviderConnection,account)
        if row is not None and row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
        if (row.revision if row else 0)!=expected_revision:raise Refused('connection revision conflict')
        if row is not None and row.owner_id!=payload['ownerId']:raise Refused('connection owner cannot change')
        policy=s.get(OfDeliveryPolicy,account)
        if policy and (policy.active_intent_id or policy.queued_count):raise Refused('delivery must be quiescent before replacing credentials')
        revision=expected_revision+1
        if row is None:
            row=OfProviderConnection(account_id=account,binding_id=runtime.binding_id,owner_id=payload['ownerId'])
            s.add(row)
        row.revision=revision;row.sealed_config=_seal(payload,tenant,account,runtime.binding_id,revision)
        row.expires_at=expires.astimezone(timezone.utc).replace(tzinfo=None);row.status='pending'
        row.verified_at=None;row.last_error=None;row.updated_at=utcnow();runtime.enabled=False;runtime.recovery_needed=True
        if policy:policy.enabled=False;policy.revision+=1
        _audit(s,row,command_key,actor,action,fingerprint)
    store.begin_scan(tenant,account)
    return revision

def change_connection(store,tenant,account,*,action,payload,expected_revision,command_key,actor):
    _validate_identity(command_key,actor,expected_revision)
    if action not in {'activate','pause','verify'} or type(payload) is not dict:raise Refused('invalid connection action')
    if action=='activate':
        if (not {'limitUnits','throttleSeconds','mediaEnabled'}<=set(payload) or not set(payload)<={'limitUnits','throttleSeconds','mediaEnabled','automateNewConversations'}
            or type(payload.get('automateNewConversations',False)) is not bool or type(payload['limitUnits']) is not int
            or not 1<=payload['limitUnits']<=10000 or type(payload['throttleSeconds']) is not int
            or not 1<=payload['throttleSeconds']<=86400 or type(payload['mediaEnabled']) is not bool):raise Refused('invalid delivery limits')
    elif payload:raise Refused('invalid connection action payload')
    fingerprint=_request(action,payload,expected_revision)
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
        if row is not None and row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
        prior=s.get(OfConnectionChange,(account,command_key))
        if prior:
            if prior.request_hash!=fingerprint or prior.actor!=actor:raise Refused('connection idempotency conflict')
            return prior.revision
        if row is None or row.revision!=expected_revision:raise Refused('connection revision conflict')
        if action=='verify' and row.expires_at<=utcnow():raise Refused('replace expired connection credentials before verification')
        policy=s.get(OfDeliveryPolicy,account)
        if action=='activate':
            if row.status!='verified' or row.expires_at<=utcnow() or row.verified_at is None:raise Refused('verify the selected provider connection before activation')
            if policy and (policy.active_intent_id or policy.queued_count):raise Refused('delivery is unresolved')
            if policy and payload['limitUnits']<policy.used_units:raise Refused('used allowance cannot be reset')
            if policy is None:policy=OfDeliveryPolicy(account_id=account);s.add(policy);s.flush()
            before={name:getattr(policy,name) for name in ['enabled','limit_units','throttle_seconds','transport_kind','media_enabled']}
            if payload['throttleSeconds']>policy.throttle_seconds and policy.next_dispatch_at is not None:
                policy.next_dispatch_at=max(policy.next_dispatch_at,utcnow()+timedelta(seconds=payload['throttleSeconds']))
            policy.revision+=1;policy.enabled=True;policy.transport_kind='native'
            policy.limit_units=payload['limitUnits'];policy.throttle_seconds=payload['throttleSeconds'];policy.media_enabled=payload['mediaEnabled']
            after={name:getattr(policy,name) for name in before}
            s.add(OfDeliveryPolicyChange(account_id=account,revision=policy.revision,actor=actor,reason='Explicit dashboard account activation',before_json=canonical(before),after_json=canonical(after),created_at=utcnow()))
            runtime.enabled=True;runtime.recovery_needed=True
            row.automate_new_conversations=payload.get('automateNewConversations',False)
            row.automation_starts_at=utcnow() if row.automate_new_conversations else None
        else:
            runtime.enabled=False
            if policy:policy.enabled=False;policy.revision+=1
            row.status='paused' if action=='pause' else 'pending';row.last_error=None
            if action=='verify':row.verified_at=None
        # Re-encrypt with the new revision used by authenticated associated data.
        clear=_open(row,tenant);row.revision+=1
        row.sealed_config=_seal(clear,tenant,account,runtime.binding_id,row.revision);row.updated_at=utcnow()
        _audit(s,row,command_key,actor,action,fingerprint)
        revision=row.revision
    if action in {'verify','activate'}:store.begin_scan(tenant,account)
    return revision

def load_worker_accounts(store,tenant,*,limit=32):
    """Backward-compatible first-cohort list; fleet workers retain the cursor."""
    return load_worker_cohort(store,tenant,limit=limit)[0]

def load_worker_cohort(store,tenant,*,limit=32,after_account=None,stop=None,facts_reader=None):
    """Bounded keyset refresh; all HTTP happens outside DB transactions.

    The cursor describes scanned candidate IDs, including refused credentials,
    so a broken early account cannot starve later accounts. None means the end
    was reached and the next refresh starts again from the first cohort.
    """
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker':raise Refused('connection loading requires the worker role')
    if type(limit) is not int or not 1<=limit<=32:raise Refused('invalid worker account bound')
    if (type(tenant) is not str or tenant=='deployment' or not ID_PATTERN.fullmatch(tenant)
        or after_account is not None and (type(after_account) is not str or not ID_PATTERN.fullmatch(after_account))):
        raise Refused('invalid worker account cursor or tenant')
    if stop is not None and stop.is_set():return [],after_account,0
    from .native_worker import WorkerAccount
    result=[]
    with Session(store.engine) as s:
        query=select(OfProviderConnection.account_id).join(OfCreatorBinding,OfCreatorBinding.id==OfProviderConnection.binding_id).where(
            OfCreatorBinding.tenant_id==tenant,OfCreatorBinding.channel=='onlyfans',OfCreatorBinding.revoked_at.is_(None),OfProviderConnection.status!='paused')
        def candidate_ids(cursor):
            selection=query.where(OfProviderConnection.account_id>cursor) if cursor is not None else query
            return s.scalars(selection.order_by(OfProviderConnection.account_id).limit(limit+1)).all()
        ids=candidate_ids(after_account)
        if not ids and after_account is not None:ids=candidate_ids(None)
    more=len(ids)>limit;ids=ids[:limit]
    scanned=0;last_scanned=after_account
    for account in ids:
        if stop is not None and stop.is_set():break
        scanned+=1;last_scanned=account
        generation=None
        try:
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
                if row is None or row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
                if row.expires_at<=utcnow() and 'hosted' not in _open(row,tenant):
                    row.status='expired';runtime.enabled=False;continue
                clear=_open(row,tenant);generation=row.revision
                policy=s.get(OfDeliveryPolicy,account);policy_revision=policy.revision if policy else 1
                status=row.status;expires=row.expires_at.replace(tzinfo=timezone.utc)
                hosted='hosted' in clear
                probe=hosted and (row.verified_at is None or row.verified_at<utcnow()-timedelta(minutes=5) or status!='verified' or expires<=datetime.now(timezone.utc)+timedelta(hours=1))
                if hosted and expires<=datetime.now(timezone.utc)+timedelta(hours=1):expires=datetime.now(timezone.utc)+timedelta(hours=23)
            transport=NativeProviderConnector(session_from_saved(payload=clear,account_id=account,policy_revision=policy_revision,expires_at=expires))
            if status=='pending' or probe:
                if stop is not None and stop.is_set():break
                transport.verify_identity()
                with Session(store.engine) as s,s.begin():
                    runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
                    if (row is None or row.binding_id!=runtime.binding_id or row.revision!=generation
                        or (row.status!='pending' and not hosted) or row.status=='paused' or (row.expires_at<=utcnow() and not hosted)):continue
                    row.status='verified';row.verified_at=utcnow();row.last_error=None
                    if hosted:
                        # A renewable health lease; no account credentials or send allowance change.
                        clear={**clear,'expiresAt':expires.isoformat()}
                        row.expires_at=expires.astimezone(timezone.utc).replace(tzinfo=None)
                        row.sealed_config=_seal(clear,tenant,account,runtime.binding_id,row.revision)
                        if status=='pending':runtime.enabled=True
                        policy=s.get(OfDeliveryPolicy,account)
                        if policy is None:
                            s.add(OfDeliveryPolicy(account_id=account,revision=1,enabled=False,transport_kind='native'))
            elif status!='verified':continue
            if facts_reader is not None:
                from .creator_facts import refresh
                try:refresh(store,tenant,account,facts_reader)
                except (Refused,ValueError,TypeError):pass
            # Hosted accounts may sync history with a disabled delivery policy.
            # Native captures keep their existing activation gate. Sending still
            # requires the separate operator policy in both modes.
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account);policy=s.get(OfDeliveryPolicy,account)
                if (row is None or row.revision!=generation or row.binding_id!=runtime.binding_id
                    or row.status!='verified' or row.expires_at<=utcnow() or not runtime.enabled or policy is None
                    or (not policy.enabled and not hosted) or policy.transport_kind!='native' or policy.revision!=policy_revision):continue
            from .provider_model import NativeProviderModel
            model=clear['model'];model=NativeProviderModel(account_id=account,model_id=model['modelId'],api_key=model['apiKey'],expires_at=expires) if model else None
            result.append(WorkerAccount(account,model,transport,transport))
        except Refused:
            continue
        except SQLAlchemyError:
            raise
        except Exception:
            # Never put a provider response, exception URL or secret in health.
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
                if row and generation is not None and row.revision==generation:
                    row.status='failed';row.last_error='provider_verification_failed';runtime.enabled=False
    next_cursor=last_scanned if more or scanned<len(ids) else None
    return result,next_cursor,scanned


def get_hosted_account(store,tenant,account):
    with Session(store.engine) as s:
        runtime=store._runtime(s,tenant,account)
        row=s.get(OfProviderConnection,account)
        if row is None or row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
        clear=_open(row,tenant);value=clear.get('hosted')
        if type(value)is not dict or value.get('schemaVersion')!='ofapi.v1':raise Refused('scope unavailable')
        return {'accountId':account,'providerAccountId':value['accountId'],'ownerId':str(row.owner_id)}
