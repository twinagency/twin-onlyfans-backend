"""Explicit scoped credential storage and worker-side provider verification.

Credentials are encrypted with an operator-owned key. API commands never make
provider requests, and uploading a capture never enables outgoing messages.
"""
from __future__ import annotations
import base64,hashlib,json,os,re,uuid
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,text,func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from db.models import Account,AccountAiConfig,OfCreatorBinding
from db.onlyfans_models import OfRuntime,OfScan,OfRecovery,OfIngress
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfDeliveryPolicyChange
from db.onlyfans_connection_models import OfProviderConnection,OfConnectionChange
from onlyfans_read.contract import ID_PATTERN
from .store import Refused,utcnow
from .native_context import canonical
from .provider_connector import session_from_capture,NativeProviderConnector,NativeAuthenticationError,NativeProtocolError

# The stored deadline is a renewable verification lease, not a forced logout.
# Short in-memory transport deadlines and all existing send gates remain intact.
CONNECTION_LEASE = timedelta(hours=6)
CONNECTION_CHECK_INTERVAL = timedelta(minutes=15)
CONNECTION_RETRY_INTERVAL = timedelta(minutes=1)

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

def capture_network_fingerprint(proxy):
    return hashlib.sha256(('twin-capture-network.v1\x00'+(proxy or '')).encode()).hexdigest()


def get_capture_context(store,tenant,creator,account=None):
    """Private server-to-server login context, never a creator status response."""
    if (type(creator)is not str or not ID_PATTERN.fullmatch(creator)
            or type(tenant)is not str or tenant=='deployment' or not ID_PATTERN.fullmatch(tenant)):
        raise Refused('scope unavailable')
    proxy=None;owner=None
    if account is not None:
        if type(account)is not str or not ID_PATTERN.fullmatch(account):raise Refused('scope unavailable')
        with Session(store.engine) as s:
            runtime=store._runtime(s,tenant,account)
            binding=s.get(OfCreatorBinding,runtime.binding_id)
            if binding is None or binding.creator_id!=creator or binding.revoked_at is not None:raise Refused('scope unavailable')
            match=re.fullmatch(r'of-([1-9][0-9]{0,15})',account)
            if match is None:raise Refused('scope unavailable')
            owner=int(match[1])
            row=s.get(OfProviderConnection,account)
            if row is not None:
                if row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
                clear=_open(row,tenant);proxy=clear['proxyUrl'];owner=row.owner_id
    return {'accountId':account,'ownerId':owner,'proxyUrl':proxy,'fingerprint':capture_network_fingerprint(proxy)}


def get_connection(store,tenant,account,*,include_sync=False,include_runner=False,include_policy=False,runner_status_path=None):
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        row=s.get(OfProviderConnection,account);policy=s.get(OfDeliveryPolicy,account)
        if row is not None and row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
        now=utcnow()
        credentials_valid=True
        if row is not None and row.expires_at>now:
            try:_open(row,tenant)
            except Refused:credentials_valid=False
        result={'accountId':account,'revision':row.revision if row else 0,
            'status':('expired' if row and row.expires_at<=now and row.status not in {'paused','failed'} else row.status if credentials_valid else 'failed') if row else 'unconfigured',
            'ownerId':str(row.owner_id) if row else None,'expiresAt':row.expires_at.isoformat()+'Z' if row else None,
            'verifiedAt':row.verified_at.isoformat()+'Z' if row and row.verified_at else None,
            'lastError':('stored_credentials_unavailable' if not credentials_valid else row.last_error) if row else None,
            'outgoingEnabled':bool(row and credentials_valid and row.expires_at>now and row.status=='verified' and runtime.enabled and policy and policy.enabled and policy.transport_kind=='native'),
            'policyRevision':policy.revision if policy else 0,'usedUnits':policy.used_units if policy else 0,
            'limitUnits':policy.limit_units if policy else 0,'mediaEnabled':bool(policy and policy.media_enabled),
            'automateNewConversations':bool(row and row.automate_new_conversations)}
        if include_policy:
            result['throttleSeconds']=policy.throttle_seconds if policy else None
        if include_runner:
            # Connected is not live. Report the supervisor's runner state separately; the file is only read,
            # a missing file is 'unknown', and aiLive needs sending enabled AND a running runner.
            from .runner_state import runner_state
            result['runner']=runner_state(account,path=runner_status_path)
            result['aiLive']=bool(result['outgoingEnabled'] and result['runner']['state']=='running')
        if include_sync:
            scan=s.get(OfScan,account)
            recovery=dict(s.execute(select(OfRecovery.status,func.count()).where(OfRecovery.account_id==account).group_by(OfRecovery.status)).all())
            quarantined=s.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.account_id==account,OfIngress.status=='quarantined'))
            result['sync']={'discoveryStatus':scan.status if scan else 'not_started','pages':scan.page_count if scan else 0,
                'conversationsComplete':recovery.get('complete',0),'conversationsRemaining':sum(count for state,count in recovery.items() if state!='complete'),
                'pendingMessages':runtime.pending_events,'quarantinedMessages':quarantined,'recoveryRequired':runtime.recovery_needed,
                'lastCompleteSyncAt':runtime.last_sync_at.isoformat()+'Z' if runtime.last_sync_at else None}
        return result

def personality_credential_status(s,account,binding_id,*,_unavailable_as_none=False):
    """Read non-secret readiness through the existing credential authority.

    The personality callback already runs inside its scoped transaction. Resolve
    that same live binding, then use the normal runtime/agency guard before
    opening encrypted data. This projection never verifies with a provider,
    renews a lease, enables sending, or consults environment model credentials.
    """
    from .store import Store
    from model_selection import explicit_model
    from llm_models import MODELS

    if (type(account) is not str or not ID_PATTERN.fullmatch(account)
            or type(binding_id) is not str or not ID_PATTERN.fullmatch(binding_id)):
        raise Refused('scope unavailable')
    binding=s.get(OfCreatorBinding,binding_id)
    if (binding is None or binding.account_id!=account or binding.channel!='onlyfans'
            or binding.revoked_at is not None or type(binding.tenant_id) is not str
            or not ID_PATTERN.fullmatch(binding.tenant_id)):
        raise Refused('scope unavailable')
    runtime=Store._runtime(s,binding.tenant_id,account)
    if runtime.binding_id!=binding_id:raise Refused('scope unavailable')
    config=s.get(AccountAiConfig,account)
    try:selected=explicit_model(config,'ai_chatter',MODELS)
    except (ValueError,TypeError,RecursionError):selected=''
    metadata={'accountId':account,'modelId':'','configured':False,
        'expiresAt':'1970-01-01T00:00:00Z'}
    row=s.get(OfProviderConnection,account)
    if row is None or row.binding_id!=runtime.binding_id:
        return None if _unavailable_as_none else metadata
    metadata['expiresAt']=row.expires_at.isoformat()+'Z'
    if (not selected or row.status!='verified' or row.verified_at is None
            or row.last_error is not None or row.expires_at<=utcnow()):return metadata
    try:clear=_open(row,binding.tenant_id)
    except Refused:
        return None if _unavailable_as_none else metadata
    try:
        model=clear.get('model')
        if (type(model) is not dict or set(model)!={'modelId','apiKey'}
                or type(model['modelId']) is not str):return metadata
        # Validate the same explicit registry/credential/deadline requirements
        # as the native adapter without constructing a model or provider client.
        from llm_client import _resolve
        from .provider_connector import _deadline, _header
        import httpx
        _,provider=_resolve(model['modelId'])
        endpoint=httpx.URL(provider.base_url.rstrip('/')+'/chat/completions')
        if (endpoint.scheme!='https' or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment):return metadata
        _header(model['apiKey'],'model credential',4096)
        _deadline(row.expires_at.replace(tzinfo=timezone.utc))
        metadata['modelId']=model['modelId']
        # "configured" means this exact scoped key is stored and valid. The
        # readiness policy compares this actual modelId with the saved/reviewed
        # selection and reports model_mismatch; presence never means usable.
        metadata['configured']=True
    except Exception:
        # Ciphertext/key/shape errors are readiness failures. Never expose the
        # decryption error or a credential-derived exception in an API result.
        return metadata
    return metadata

def save_connection(store,tenant,account,*,payload,expected_revision,command_key,actor,session_only=False,expected_network_fingerprint=None):
    _validate_identity(command_key,actor,expected_revision)
    required={'ownerId','capture','xHash','expiresAt'} if session_only else {'ownerId','capture','xHash','proxyUrl','expiresAt','model'}
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
            if expected_network_fingerprint is not None and expected_network_fingerprint!=capture_network_fingerprint(previous.get('proxyUrl')):
                raise Refused('capture network changed')
            payload={**payload,'proxyUrl':previous.get('proxyUrl'),'model':previous.get('model')}
        try:
            expires=datetime.fromisoformat(payload['expiresAt'].replace('Z','+00:00'))
            session_from_capture(account_id=account,owner_id=payload['ownerId'],policy_revision=1,capture=payload['capture'],
                x_hash=payload['xHash'],proxy_url=payload['proxyUrl'],expires_at=expires)
            model=payload['model']
            if model is not None:
                if type(model) is not dict or set(model)!={'modelId','apiKey'}:raise Refused('invalid selected model credential')
                from .provider_model import NativeProviderModel
                NativeProviderModel(account_id=account,model_id=model['modelId'],api_key=model['apiKey'],expires_at=expires)
        except Exception:raise Refused('invalid connection configuration') from None
        from managed_proxy.reservation import reserve_network
        from managed_proxy.provider import ProxyError
        binding=s.get(OfCreatorBinding,runtime.binding_id)
        try:reserve_network(s,tenant,binding.creator_id,payload['proxyUrl'],encryption_key(),actor,captured_handle=binding.handle)
        except ProxyError:raise Refused('managed network reservation changed') from None
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
        from .launch_engagement.service import invalidate
        invalidate(s,runtime,reason='connection_changed',actor=actor)
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
        from .launch_engagement.service import invalidate
        invalidate(s,runtime,reason='connection_changed',actor=actor)
        revision=row.revision
    if action in {'verify','activate'}:store.begin_scan(tenant,account)
    return revision

def load_worker_accounts(store,tenant,*,limit=32):
    """Backward-compatible first-cohort list; fleet workers retain the cursor."""
    return load_worker_cohort(store,tenant,limit=limit)[0]

def load_worker_cohort(store,tenant,*,limit=32,after_account=None,stop=None,facts_reader=None,include_read_only=False):
    """Bounded keyset refresh; all HTTP happens outside DB transactions.

    The cursor describes scanned candidate IDs, including refused credentials,
    so a broken early account cannot starve later accounts. None means the end
    was reached and the next refresh starts again from the first cohort.
    """
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker':raise Refused('connection loading requires the worker role')
    if type(limit) is not int or not 1<=limit<=32:raise Refused('invalid worker account bound')
    if type(include_read_only) is not bool:raise Refused('invalid inbox loading mode')
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
        generation=None;renewable=False;snapshot=None
        try:
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
                if row is None or row.binding_id!=runtime.binding_id:raise Refused('scope unavailable')
                now=utcnow()
                renewable=(row.status in {'verified','expired'} and row.verified_at is not None
                    and row.last_error in {None,'provider_connection_check_retry'})
                if row.status not in {'pending','verified','expired'}:continue
                if row.expires_at<=now and not renewable:
                    row.status='expired';runtime.enabled=False;continue
                if row.status=='expired' and not renewable:continue
                clear=_open(row,tenant);generation=row.revision;snapshot=row.sealed_config
                policy=s.get(OfDeliveryPolicy,account);policy_revision=policy.revision if policy else 1
                status=row.status;expires=row.expires_at.replace(tzinfo=timezone.utc)
                check_due=renewable and (row.verified_at<=now-CONNECTION_CHECK_INTERVAL
                    or row.expires_at<=now+CONNECTION_CHECK_INTERVAL
                    or row.last_error=='provider_connection_check_retry')
                if ((status=='pending' or check_due) and row.last_error=='provider_connection_check_retry'
                        and row.updated_at>now-CONNECTION_RETRY_INTERVAL):
                    # A still-valid lease preserves activation intent; it does
                    # not permit inbox/model/send work to evade retry pacing.
                    continue
            # An elapsed local lease may only be used for this identity GET.
            # No history, media, model or delivery work receives this probe.
            probe_expiry=(utcnow()+timedelta(minutes=1)).replace(tzinfo=timezone.utc) if check_due else expires
            transport=NativeProviderConnector(session_from_capture(account_id=account,owner_id=clear['ownerId'],policy_revision=policy_revision,
                capture=clear['capture'],x_hash=clear['xHash'],proxy_url=clear['proxyUrl'],expires_at=probe_expiry))
            if status=='pending' or check_due:
                if stop is not None and stop.is_set():break
                from .provider_read_budget import acquire,current,release,native_reader
                budget=check_error=None
                try:
                    if native_reader(transport):
                        budget=acquire(store,tenant,account,connection_revision=generation,kind='connection_check')
                    transport.verify_identity()
                    with Session(store.engine) as s,s.begin():
                        runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
                        if (row is None or row.binding_id!=runtime.binding_id or row.revision!=generation
                            or row.sealed_config!=snapshot or row.status!=status
                            or status=='pending' and row.expires_at<=utcnow()):continue
                        if budget is not None:current(store,tenant,budget,session=s)
                        if check_due:
                            expires=(utcnow()+CONNECTION_LEASE).replace(tzinfo=timezone.utc)
                            clear={**clear,'expiresAt':expires.isoformat()}
                            row.sealed_config=_seal(clear,tenant,account,runtime.binding_id,generation)
                            row.expires_at=expires.replace(tzinfo=None)
                            row.updated_at=utcnow()
                        row.status='verified';row.verified_at=utcnow();row.last_error=None
                except Exception as error:
                    check_error=error
                    raise
                finally:
                    if budget is not None:release(store,tenant,budget,retry_after=getattr(check_error,'retry_after',None))
                if check_due:
                    transport=NativeProviderConnector(session_from_capture(account_id=account,owner_id=clear['ownerId'],policy_revision=policy_revision,
                        capture=clear['capture'],x_hash=clear['xHash'],proxy_url=clear['proxyUrl'],expires_at=expires))
            elif status!='verified':continue
            if os.environ.get("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED") == "1":
                from .automatic_imports import ensure_started
                ensure_started(store, tenant, account, connection_revision=generation)
            if facts_reader is not None:
                from .creator_facts import refresh
                try:refresh(store,tenant,account,facts_reader)
                except (Refused,ValueError,TypeError):pass
            # Verified connections can load inbox history without an AI key or
            # outgoing activation. Sending keeps its existing policy gate.
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account);policy=s.get(OfDeliveryPolicy,account)
                if (row is None or row.revision!=generation or row.binding_id!=runtime.binding_id
                    or row.status!='verified' or row.verified_at is None or row.expires_at<=utcnow()):continue
                outgoing=bool(runtime.enabled and policy is not None and policy.enabled
                    and policy.transport_kind=='native' and policy.revision==policy_revision)
            # This is the connection generation, distinct from delivery policy
            # revision. Every account-bound read retains this snapshot through
            # the shared provider lease and its atomic result commit.
            transport.connection_revision=generation
            if not outgoing:
                if include_read_only:result.append(WorkerAccount(account,None,transport,transport,generation))
                continue
            from .provider_model import NativeProviderModel
            model=clear['model'];model=NativeProviderModel(account_id=account,model_id=model['modelId'],api_key=model['apiKey'],expires_at=expires) if model else None
            result.append(WorkerAccount(account,model,transport,transport))
        except Refused:
            continue
        except SQLAlchemyError:
            raise
        except Exception as error:
            # Never put a provider response, exception URL or secret in health.
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row=s.get(OfProviderConnection,account)
                if (row and generation is not None and row.revision==generation
                        and row.sealed_config==snapshot and row.status!='paused'):
                    if ((renewable or (row.status=='pending' and isinstance(error,NativeProtocolError)))
                            and not isinstance(error,NativeAuthenticationError)):
                        # A transient first verification must not strand a new
                        # connection. Retain its original capture deadline and
                        # pace retries; only verified leases may be renewed.
                        row.last_error='provider_connection_check_retry';row.updated_at=utcnow()
                    else:
                        row.status='failed';runtime.enabled=False
                        row.last_error='provider_authentication_required' if isinstance(error,NativeAuthenticationError) else 'provider_verification_failed'
    next_cursor=last_scanned if more or scanned<len(ids) else None
    return result,next_cursor,scanned
