"""Serialized incident records. Resolution never creates a sender permit."""
import contextlib
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

def incident_spec(incident_hash):
    # Only these two independently identified incidents are approved for this owner.
    if incident_hash==APPROVED_INCIDENT:
        return {'classification':'credit_monitor_unavailable','scope_revision':12}
    if incident_hash==WEBHOOK_INCIDENT:
        return {'classification':'provider_webhook_health_unavailable','scope_revision':16}
    return None

ACCOUNT='of-323358988'
BINDING='01612323-b06f-5320-b623-4e032f474a6e'

def classify_journal(raw,expected='credit_monitor_unavailable'):
    failures=re.findall(rb'pilot_supervisor_stopped:([A-Za-z0-9_]+)',raw)
    if expected not in ('credit_monitor_unavailable','provider_webhook_health_unavailable') or failures != [expected.encode()]:
        raise RuntimeError('additional or different supervisor failure requires its own evidence')
    return expected

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def _valid_resolution(value, incident_hash):
    spec=incident_spec(incident_hash)
    return (spec is not None and type(value) is dict and value.get('version')=='plan-c-monitor-resolution.v1'
        and value.get('incident_sha256')==incident_hash
        and value.get('account_id')==ACCOUNT and value.get('binding_id')==BINDING
        and type(value.get('connection_epoch')) is int and value['connection_epoch']==1 and type(value.get('scope_revision')) is int
        and value['scope_revision']==spec['scope_revision'] and value.get('classification')==spec['classification']
        and re.fullmatch(r'op-session:[a-f0-9]{64}',str(value.get('actor',''))) is not None
        and all(re.fullmatch('[a-f0-9]{64}',str(value.get(k,''))) is not None
                for k in ('audit_command_key','evidence_sha256'))
        and type(value.get('resolved_at')) is str and bool(value['resolved_at']))

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
        return not (_valid_resolution(resolution,digest(raw)) and archived==raw and immutable==encoded(resolution))
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
        if incident_spec(h) is None or not _valid_resolution(receipt,h):
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
                    valid=(_valid_resolution(saved,prior_hash) and digest(prior_raw)==prior_hash
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
