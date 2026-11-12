"""Serialized incident records. Resolution never creates a sender permit."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

APPROVED_INCIDENT='c59fd2459671cc3e0d3a82a1a8de26b9675cc24a4e0567e3f712ea3b89016725'
WEBHOOK_INCIDENT='c240c781574ad075939782179ab926c9f0a69cc3fc40a292f81f7d3f57e3ad14'

REPEATED_WEBHOOK_INCIDENT='df20805ad9525b3ccc54b5dba161940cbdca78bda053d916d0578bbb42a27f33'

def pinned_incidents():
    # Old pinned incidents remain readable only for their original identity.
    if (ACCOUNT, BINDING, EPOCH) != (LEGACY_ACCOUNT, LEGACY_BINDING, 1):
        return {}
    # Read the module constants at call time so the three pinned incidents stay the single table.
    return {
        APPROVED_INCIDENT:{'classification':'credit_monitor_unavailable','scope_revision':12},
        WEBHOOK_INCIDENT:{'classification':'provider_webhook_health_unavailable','scope_revision':16},
        REPEATED_WEBHOOK_INCIDENT:{'classification':'provider_webhook_health_unavailable','scope_revision':20},
    }
# Classes that class/evidence-based resolution may cover. Must equal the service owner's set.
# Network-check incidents use the same manual, interval/evidence-bound no-permit recovery.
# readiness_unavailable (the sender's readiness check was not healthy) is deliberately NOT a thin v2 class: a stopped
# sender loop can leave an unresolved send behind and its cause is unproven, so it resolves only through the v3 receipt below.
READINESS='readiness_unavailable'
V2_CLASSES=('credit_monitor_unavailable','provider_webhook_health_unavailable','sender_not_ready','network_check_failed')
CLASSES=V2_CLASSES+(READINESS,)
V1='plan-c-monitor-resolution.v1'
V2='plan-c-monitor-resolution.v2'
V3='plan-c-monitor-resolution.v3'
V1_FIELDS=frozenset(('version','incident_sha256','account_id','binding_id','connection_epoch','scope_revision',
    'classification','actor','audit_command_key','evidence_sha256','resolved_at'))
V2_FIELDS=V1_FIELDS|{'incident_id','affected_interval','delivery_url_sha256','config_version'}
V3_FIELDS=V2_FIELDS|{'evidence_reference','named_sends','cause_caveat'}
MAX_INTERVAL=datetime.timedelta(days=7)
READINESS_MAX_INTERVAL=datetime.timedelta(hours=24)
# Retained in every v3 receipt and printed by the resolver. The supervisor recorded readiness_unavailable; the original readiness
# HTTP status, code and body were not retained, so no specific response is stated or implied here.
READINESS_CAVEAT=('UNPROVEN CAUSE: the supervisor recorded readiness_unavailable; the original readiness HTTP status, code and body '
    'were not retained, so the cause is not established. '
    'This record resolves the monitor stop only: it does not prove the cause, does not fix it, '
    'releases no hold, settles no send and grants no send permission.')
EVIDENCE_PREFIX='operator:fansapi:'
SEND_ID=re.compile(r'[A-Za-z0-9_-]{1,36}')

def evidence_reference_ok(ref):
    """The same rule as conversation_engine.operator.evidence_allowed('fansapi', ref): a named human's statement."""
    return (type(ref) is str and re.search(r'[\x00-\x1f]',ref) is None and ref.startswith(EVIDENCE_PREFIX)
        and 6<=len(ref)-len(EVIDENCE_PREFIX)<=108 and bool(ref[len(EVIDENCE_PREFIX):].strip()))

def named_sends_ok(named):
    """Sorted, unique, well-formed send identifiers (text intent ids or independent admission attempt ids)."""
    return (type(named) is list and all(type(x) is str and SEND_ID.fullmatch(x) is not None for x in named)
        and named==sorted(set(named)))

def incident_spec(incident_hash,marker_raw=None):
    """The three pinned incidents keep their exact specs. Any other incident is class-based
    and is accepted only if its own marker durably recorded the scope revision and identity."""
    pinned=pinned_incidents().get(incident_hash)
    if pinned is not None:return dict(pinned,pinned=True)
    if marker_raw is None or digest(marker_raw)!=incident_hash:return None
    try:marker=json.loads(marker_raw)
    except ValueError:return None
    if (type(marker) is not dict or not re.fullmatch('[a-f0-9]{32}',str(marker.get('incident_id','')))
            or type(marker.get('last_confirmed_healthy_utc')) is not str):return None
    # scope_revision None: the expected revision is carried by the receipt and must equal the live
    # stopped revision at collection (resolver) and at audit (database owner).
    return {'classification':None,'scope_revision':None,'pinned':False,
            'incident_id':marker['incident_id'],'interval_start':marker['last_confirmed_healthy_utc']}

def journal_class(raw):
    """The single supervisor failure class in a journal, for class-based incidents."""
    # Exactly one stop line, of one known class. A second line (even the same class) is a different incident.
    failures=re.findall(rb'pilot_supervisor_stopped:([A-Za-z0-9_]+)',raw)
    if len(failures)!=1 or failures[0].decode() not in CLASSES:
        raise RuntimeError('additional or different supervisor failure requires its own evidence')
    return failures[0].decode()

def command_key_v2(incident_sha256,evidence_sha256,start,end,scope_revision):
    """Idempotent command identity: bound to incident, evidence, interval and expected revision."""
    return hashlib.sha256(('monitor-resolution.v2:%s:%s:%s:%s:%d'%(incident_sha256,evidence_sha256,start,end,scope_revision)).encode()).hexdigest()

def command_key_v3(incident_sha256,evidence_sha256,start,end,scope_revision,evidence_reference,named_sends):
    """v3 identity: v2's bindings plus the operator's evidence reference and the exact sends the operator named."""
    return hashlib.sha256(('monitor-resolution.v3:%s:%s:%s:%s:%d:%s:%s'%(incident_sha256,evidence_sha256,start,end,scope_revision,
        hashlib.sha256(str(evidence_reference).encode()).hexdigest(),','.join(named_sends))).encode()).hexdigest()

LEGACY_ACCOUNT='of-323358988'
LEGACY_BINDING='01612323-b06f-5320-b623-4e032f474a6e'
ACCOUNT=os.environ.get('TWIN_SUPERVISION_ACCOUNT',LEGACY_ACCOUNT)
BINDING=os.environ.get('TWIN_SUPERVISION_BINDING',LEGACY_BINDING)
EPOCH=int(os.environ.get('TWIN_SUPERVISION_EPOCH','1'))

def configure(config):
    """Bind receipt validation to the reviewed per-account supervision configuration (the resolver's own path).

    Pinned legacy incidents then apply only to the legacy identity; every other identity is class/evidence based.
    """
    global ACCOUNT,BINDING,EPOCH
    ACCOUNT,BINDING,EPOCH=config['account'],config['binding'],config['epoch']

_ACCOUNT_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')

def scope_identity_sql(account):
    """The persisted identity the database audit will lock: runtime binding, active connection and the scope's fan list."""
    if type(account) is not str or _ACCOUNT_ID.fullmatch(account) is None:raise ValueError('invalid account identity')
    return ("select coalesce(json_agg(json_build_object('binding',r.binding_id,'epoch',c.epoch,'provider',c.provider_account_id,"
        "'fans',(s.config_json::jsonb)->'fans')),'[]'::json) from of_runtime r "
        "join of_fans_connection c on c.account_id=r.account_id and c.binding_id=r.binding_id and c.status='active' "
        "join of_engine_pilot_scope s on s.account_id=r.account_id and s.binding_id=r.binding_id "
        "where r.account_id='"+account+"';")

def check_scope_identity(config,observed):
    """The reviewed configuration must equal the persisted scope exactly; anything else is a different account."""
    if type(observed) is not list or len(observed)!=1 or type(observed[0]) is not dict:
        raise RuntimeError('persisted scope identity is missing or ambiguous')
    row=observed[0];fans=row.get('fans')
    if (set(row)!={'binding','epoch','provider','fans'} or row['binding']!=config['binding']
            or type(row['epoch']) is not int or row['epoch']!=config['epoch']
            or row['provider']!=config['providerAccount']
            or type(fans) is not list or any(type(f) is not int for f in fans)
            or sorted(fans)!=sorted(config['fans']) or len(set(fans))!=len(fans)):
        raise RuntimeError('reviewed supervision configuration does not match the persisted scope')

def classify_journal(raw,expected='credit_monitor_unavailable'):
    failures=re.findall(rb'pilot_supervisor_stopped:([A-Za-z0-9_]+)',raw)
    if expected not in ('credit_monitor_unavailable','provider_webhook_health_unavailable') or failures != [expected.encode()]:
        raise RuntimeError('additional or different supervisor failure requires its own evidence')
    return expected

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def _moment(value):
    try:
        parsed=datetime.datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)
    except (AttributeError,TypeError,ValueError):return None
    return parsed.astimezone(datetime.timezone.utc) if parsed.tzinfo is not None else None

def _common_valid(value,spec,incident_hash):
    return (spec is not None and type(value) is dict
        and value.get('incident_sha256')==incident_hash
        and value.get('account_id')==ACCOUNT and value.get('binding_id')==BINDING
        and type(value.get('connection_epoch')) is int and value['connection_epoch']==EPOCH and type(value.get('scope_revision')) is int
        and type(value['scope_revision']) is not bool and value['scope_revision']>=0 and (spec['scope_revision'] is None or value['scope_revision']==spec['scope_revision'])
        and re.fullmatch(r'op-session:[a-f0-9]{64}',str(value.get('actor',''))) is not None
        and all(re.fullmatch('[a-f0-9]{64}',str(value.get(k,''))) is not None
                for k in ('audit_command_key','evidence_sha256'))
        and type(value.get('resolved_at')) is str and bool(value['resolved_at']))

def _valid_resolution(value, incident_hash, marker_raw=None):
    spec=incident_spec(incident_hash,marker_raw)
    if type(value) is not dict or spec is None:return False
    if value.get('version')==V1:
        return (spec['pinned'] and set(value)==V1_FIELDS and _common_valid(value,spec,incident_hash)
                and value.get('classification')==spec['classification'])
    version=value.get('version')
    if version not in (V2,V3) or set(value)!=(V2_FIELDS if version==V2 else V3_FIELDS) or not _common_valid(value,spec,incident_hash):return False
    if spec['pinned']:return False   # pinned incidents keep their original receipt form
    interval=value.get('affected_interval')
    if type(interval) is not dict or set(interval)!={'start','end'}:return False
    start,end,done=_moment(interval['start']),_moment(interval['end']),_moment(value['resolved_at'])
    expected_start=_moment(spec['interval_start'])
    if version==V2:
        classes,longest=V2_CLASSES,MAX_INTERVAL
        key=command_key_v2(incident_hash,value['evidence_sha256'],interval['start'],interval['end'],value['scope_revision'])
    else:
        # readiness_unavailable only: a tighter interval, the operator's evidence reference, the named sends, the caveat.
        if not (evidence_reference_ok(value['evidence_reference']) and named_sends_ok(value['named_sends'])
                and value['cause_caveat']==READINESS_CAVEAT):return False
        classes,longest=(READINESS,),READINESS_MAX_INTERVAL
        key=command_key_v3(incident_hash,value['evidence_sha256'],interval['start'],interval['end'],value['scope_revision'],
            value['evidence_reference'],value['named_sends'])
    return (value['classification'] in classes and start is not None and end is not None and done is not None
        and start==expected_start and start<end<=done and end-start<=longest
        and value.get('incident_id')==spec['incident_id']
        and all(re.fullmatch('[a-f0-9]{64}',str(value.get(k,''))) is not None for k in ('delivery_url_sha256','config_version'))
        and value['audit_command_key']==key)

def unresolved(marker):
    marker=Path(marker)
    if not marker.exists():
        return False
    try:
        raw=marker.read_bytes()
        resolution=json.loads((marker.parent/'incident-resolution.json').read_bytes())
        # The immutable archive and published receipt must describe these exact bytes.
        archived=(marker.parent/'incident-history'/digest(raw)).read_bytes()
        immutable=(marker.parent/'incident-history'/('resolution-'+str(resolution.get('audit_command_key',''))+'.json')).read_bytes()
        return not (_valid_resolution(resolution,digest(raw),raw) and archived==raw and immutable==encoded(resolution))
    except (OSError,ValueError,TypeError):
        return True

@contextlib.contextmanager
def locked(root):
    root=Path(root)
    fd=os.open(root/'incident.lock',os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)

def atomic(path,raw):
    path=Path(path);tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
    if os.geteuid()==0:os.fchown(fd,0,65532)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if tmp.exists():tmp.unlink()

def retain(root,raw):
    archive=root/'incident-history'
    archive.mkdir(mode=0o750,exist_ok=True)
    if os.geteuid()==0:os.chown(archive,0,65532)
    path=archive/digest(raw)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
    except FileExistsError:
        if path.read_bytes()!=raw:raise RuntimeError('incident archive conflict')
        return
    if os.geteuid()==0:os.fchown(fd,0,65532)
    with os.fdopen(fd,'wb') as f:
        f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(archive,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def record_failure(root,payload):
    """Every failure gets distinct durable bytes, invalidating earlier evidence."""
    with locked(root) as root:
        marker=root/'outage-open.json'
        if marker.exists():
            old=marker.read_bytes()
            retain(root,old)
        new=encoded({**payload,'incident_id':uuid.uuid4().hex,'resolved':False})
        retain(root,new)
        atomic(marker,new)
        return digest(new)

def share_verified_marker(root,marker,raw,receipt):
    """Audited read access for this verified marker; root retains sole write."""
    if os.geteuid()!=0:raise RuntimeError('incident resolution requires existing root owner')
    fd=os.open(marker,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if before.st_uid!=0 or not stat.S_ISREG(before.st_mode) or os.pread(fd,len(raw)+1,0)!=raw:
            raise RuntimeError('verified incident marker changed')
        record=root/'incident-history'/('marker-access-'+receipt['audit_command_key']+'.json')
        identity={'incident_sha256':digest(raw),'audit_command_key':receipt['audit_command_key'],
                  'actor':receipt['actor'],'after':{'uid':0,'gid':65532,'mode':0o640}}
        entry={**identity,'before':{'uid':before.st_uid,'gid':before.st_gid,'mode':stat.S_IMODE(before.st_mode)}}
        if record.exists():
            saved=json.loads(record.read_bytes())
            if any(saved.get(k)!=v for k,v in identity.items()):raise RuntimeError('marker access audit conflict')
        else:
            out=os.open(record,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
            os.fchown(out,0,65532)
            with os.fdopen(out,'wb') as f:f.write(encoded(entry));f.flush();os.fsync(f.fileno())
            out=os.open(record.parent,os.O_RDONLY)
            try:os.fsync(out)
            finally:os.close(out)
        os.fchown(fd,0,65532);os.fchmod(fd,0o640);os.fsync(fd)
    finally:os.close(fd)

def resolve(root,receipt,audit,*,after_audit=None):
    """Publish only after the existing DB audit commits. Crash/replay stays fenced."""
    with locked(root) as root:
        marker=root/'outage-open.json';raw=marker.read_bytes();h=digest(raw)
        if incident_spec(h,raw) is None or not _valid_resolution(receipt,h,raw):
            raise RuntimeError('wrong or unsupported monitor incident')
        existing=root/'incident-resolution.json'
        if existing.exists():
            saved=json.loads(existing.read_bytes())
            if saved.get('incident_sha256')==h:
                if saved!=receipt:raise RuntimeError('resolution evidence conflict')
                if not unresolved(marker):return 'replayed'
            else:
                # Preserve an earlier approved incident's immutable history while
                # atomically replacing only the current resolution pointer.
                prior_hash=saved.get('incident_sha256','')
                try:
                    prior_raw=(root/'incident-history'/prior_hash).read_bytes()
                    prior_receipt=(root/'incident-history'/('resolution-'+saved.get('audit_command_key','')+'.json')).read_bytes()
                    valid=(_valid_resolution(saved,prior_hash,prior_raw) and digest(prior_raw)==prior_hash
                           and prior_receipt==encoded(saved))
                except (OSError,TypeError,ValueError):valid=False
                if not valid:raise RuntimeError('prior incident history is incomplete or conflicting')
        audit(receipt)
        if after_audit:after_audit()
        if marker.read_bytes()!=raw:raise RuntimeError('incident changed during resolution')
        retain(root,raw)
        share_verified_marker(root,marker,raw,receipt)
        record=root/'incident-history'/('resolution-'+receipt['audit_command_key']+'.json')
        blob=encoded(receipt)
        if record.exists():
            if record.read_bytes()!=blob:raise RuntimeError('resolution audit conflict')
        else:
            fd=os.open(record,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
            if os.geteuid()==0:os.fchown(fd,0,65532)
            with os.fdopen(fd,'wb') as f:f.write(blob);f.flush();os.fsync(f.fileno())
        fd=os.open(record.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        atomic(existing,blob)
        return 'resolved'

def resolved_intervals(root):
    """Intervals covered by fully valid, immutably retained resolutions (for delivery accounting).

    A resolution never grants send permission; it only records that failures inside the audited
    affected interval were reconciled, so they stop holding every future delivery check.
    """
    root=Path(root);found=[]
    history=root/'incident-history'
    if not history.is_dir():return found
    for path in sorted(history.glob('resolution-*.json')):
        try:
            receipt=json.loads(path.read_bytes())
            if path.read_bytes()!=encoded(receipt):continue
            h=receipt['incident_sha256'];raw=(history/h).read_bytes()
            if digest(raw)!=h or not _valid_resolution(receipt,h,raw):continue
            if receipt['version'] in (V2,V3):found.append((receipt['affected_interval']['start'],receipt['affected_interval']['end']))
            else:
                start=json.loads(raw).get('last_confirmed_healthy_utc')
                if type(start) is str:found.append((start,receipt['resolved_at']))
        except (OSError,ValueError,TypeError,KeyError):continue
    return found
