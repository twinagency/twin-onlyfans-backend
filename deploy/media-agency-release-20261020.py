"""Explicit release phases for the native media and agency runtime.

Run as root on the existing host through SSM. Private environment and backup
contents stay on that host. No provider calls or outgoing policy changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

REGISTRY = '323149985112.dkr.ecr.ap-southeast-2.amazonaws.com'
OLD_IMAGE = REGISTRY + '/twin-dashboard@sha256:e5611032fbf5d5b4f9b3962515b5332efcca06e39c9d267177b089ef94d23554'
OLD_BASE = '/opt/twin-onlyfans/releases/0525e51aa690b44d1c9f1dc0bc2b9aed6baa627a/deploy/onlyfans-compose.yml'
OLD_OVERLAY = '/etc/twin-onlyfans/creator-sign-in-release-compose.json'
ROOT = Path('/var/backups/twin-media-agency-20261020')
CONFIG = Path('/etc/twin-onlyfans/media-agency-release-compose.json')
API = 'twin-onlyfans-api-1'
WORKER = 'twin-onlyfans-worker-1'
KEEP = ['twin-onlyfans-database-1', 'twin-onlyfans-creator-sign-in-1', 'flirt-bot', 'flirt-dashboard']


def run(args, *, data=None, timeout=180):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Command errors can contain environment values. Keep details private.
        if ROOT.exists():
            write(ROOT / 'last-command-error.log', result.stderr)
        raise RuntimeError('Command failed: ' + args[0] + ', exit ' + str(result.returncode))
    return result.stdout


def write(path, value):
    value = value.encode() if isinstance(value, str) else value
    temp = path.with_suffix(path.suffix + '.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(value)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def records(names):
    return json.loads(run(['docker', 'inspect', *names]))


def environment(record):
    return dict(line.split('=', 1) for line in record['Config']['Env'] if '=' in line)


def compose(*args):
    return run(['docker', 'compose', '--project-name', 'twin-onlyfans',
                '-f', str(CONFIG), *args], timeout=300)


def schema(container=API):
    return run(['docker', 'exec', container, 'python', '-m', 'service.db.schema',
                'check', '--url-env', 'DATABASE_URL']).decode().strip()


def counts():
    code = '''import hashlib,json,os
from sqlalchemy import create_engine,select,func
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_connection_models import OfProviderConnection
from db.models import OfCreatorBinding
with create_engine(os.environ['DATABASE_URL']).connect() as c:
 policy=[sorted([tuple(x) for x in c.execute(select(OfRuntime.account_id,OfRuntime.enabled))],key=str),sorted([dict(x) for x in c.execute(select(OfDeliveryPolicy.__table__)).mappings()],key=lambda x:json.dumps(x,sort_keys=True,default=str))]
 value={'bindings':c.scalar(select(func.count()).select_from(OfCreatorBinding)),'connections':c.scalar(select(func.count()).select_from(OfProviderConnection)),'enabledRuntimes':c.scalar(select(func.count()).select_from(OfRuntime).where(OfRuntime.enabled.is_(True))),'enabledPolicies':c.scalar(select(func.count()).select_from(OfDeliveryPolicy).where(OfDeliveryPolicy.enabled.is_(True))),'policyHash':hashlib.sha256(json.dumps(policy,sort_keys=True,default=str).encode()).hexdigest()}
 print(json.dumps(value))
'''
    return json.loads(run(['docker', 'exec', '-i', API, 'python', '-'], data=code.encode()))


def backup():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    current = records([API, WORKER, *KEEP])
    if any(v['Config']['Image'] != OLD_IMAGE for v in current[:2]):
        raise RuntimeError('Native baseline changed; review before releasing')
    ready = schema()
    if '0087_creator_enrollment' not in ready:
        raise RuntimeError('Expected schema 0087')
    baseline = ROOT / 'baseline.json'
    if not baseline.exists():
        write(baseline, json.dumps({'containers': current, 'native': counts()}))
    for source in ['/etc/twin-onlyfans/compose.env', '/etc/twin-onlyfans/runtime.env', OLD_OVERLAY, OLD_BASE]:
        target = ROOT / source.replace('/', '_').lstrip('_')
        if not target.exists():
            write(target, Path(source).read_bytes())
    dump = ROOT / 'native-database.dump'
    if not dump.exists():
        temporary = ROOT / 'native-database.dump.partial'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            result = subprocess.run(['docker', 'exec', 'twin-onlyfans-database-1', 'sh', '-c',
                                     'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner'],
                                    stdout=handle, stderr=subprocess.PIPE, timeout=300)
        if result.returncode or temporary.stat().st_size < 100:
            raise RuntimeError('Database backup failed')
        os.replace(temporary, dump)
    with dump.open('rb') as handle:
        result = subprocess.run(['docker', 'exec', '-i', 'twin-onlyfans-database-1', 'pg_restore', '--list'],
                                stdin=handle, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        raise RuntimeError('Backup verification failed')
    original = json.loads(run(['docker', 'compose', '--project-name', 'twin-onlyfans',
                              '--env-file', '/etc/twin-onlyfans/compose.env', '-f', OLD_BASE,
                              '-f', OLD_OVERLAY, 'config', '--format', 'json']))
    write(ROOT / 'resolved-compose-before.json', json.dumps(original))
    print(json.dumps({'backupVerified': True, 'backupBytes': dump.stat().st_size,
                      'backupSha256': hashlib.sha256(dump.read_bytes()).hexdigest(),
                      'schema': ready, 'freeDiskMiB': shutil.disk_usage('/').free // 1024**2,
                      'native': counts(), 'services': sorted(original['services'])}), flush=True)


def prepare(image, commit):
    if not re.fullmatch(re.escape(REGISTRY) + r'/twin-dashboard@sha256:[0-9a-f]{64}', image):
        raise RuntimeError('Immutable existing-registry image required')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('Exact source commit required')
    if not (ROOT / 'native-database.dump').exists():
        raise RuntimeError('Verified backup required')
    if shutil.disk_usage('/').free < 2600 * 1024**2:
        raise RuntimeError('Insufficient disk headroom for image pull')
    password = run(['aws', 'ecr', 'get-login-password', '--region', 'ap-southeast-2'])
    run(['docker', 'login', '--username', 'AWS', '--password-stdin', REGISTRY], data=password)
    password = None
    run(['docker', 'pull', image], timeout=900)
    package = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    if package['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != commit:
        raise RuntimeError('Runtime source revision differs')
    if image not in package.get('RepoDigests', []):
        raise RuntimeError('Runtime digest differs')
    config = json.loads((ROOT / 'resolved-compose-before.json').read_text())
    if 'migrate' not in config['services']:
        full = json.loads(run(['docker', 'compose', '--project-name', 'twin-onlyfans',
                               '--env-file', '/etc/twin-onlyfans/compose.env', '--profile', 'ops',
                               '-f', OLD_BASE, '-f', OLD_OVERLAY, 'config', '--format', 'json']))
        config['services']['migrate'] = full['services']['migrate']
    api_env = environment(records([API])[0])
    service_env = {name: environment(record) for name, record in zip(['api', 'worker'], records([API, WORKER]))}
    for name in ['api', 'worker', 'migrate']:
        service = config['services'][name]
        service.pop('build', None)
        service['image'] = image
        service.setdefault('labels', {})['org.opencontainers.image.revision'] = commit
    for name in ['api', 'worker']:
        service = config['services'][name]
        service.setdefault('environment', {}).update(ONLYFANS_VAULT_MEDIA_ENABLED='0',
                                                    VAULT_SPOOL_DIR='/var/lib/twin-vault/spool',
                                                    VAULT_UPLOAD_PART_CONCURRENCY='2')
        if service['environment']['DATABASE_URL'] != service_env[name]['DATABASE_URL']:
            raise RuntimeError('Resolved database target differs')
        service.setdefault('volumes', []).append({'type': 'volume', 'source': 'onlyfans-vault-spool',
                                                  'target': '/var/lib/twin-vault/spool'})
    config.setdefault('volumes', {})['onlyfans-vault-spool'] = {'name': 'twin-onlyfans_onlyfans-vault-spool'}
    if config['services']['migrate']['environment']['ONLYFANS_MIGRATION_URL'] != api_env['DATABASE_URL']:
        raise RuntimeError('Migration target differs')
    write(CONFIG, json.dumps(config))
    compose('config', '--quiet')
    write(ROOT / 'candidate.json', json.dumps({'image': image, 'commit': commit}))
    print(json.dumps({'prepared': True, 'image': image, 'commit': commit,
                      'mediaEnabled': False, 'freeDiskMiB': shutil.disk_usage('/').free // 1024**2}), flush=True)


def healthy():
    for _ in range(60):
        try:
            value = json.loads(run(['curl', '--fail', '--silent', '--max-time', '3', 'http://127.0.0.1:8788/healthz']))
            if value.get('ok'):
                return
        except (RuntimeError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError('Native API readiness failed')


def deploy():
    candidate = json.loads((ROOT / 'candidate.json').read_text())
    current = records([API, WORKER])
    if any(v['Config']['Image'] != OLD_IMAGE for v in current):
        raise RuntimeError('Current runtime differs; inspect before retrying deployment')
    schema()
    run(['docker', 'stop', '--time', '45', WORKER, API])
    try:
        compose('run', '--rm', '--no-deps', 'migrate', 'upgrade')
    except Exception:
        # A failed transactional upgrade leaves the old schema usable. Startup
        # checks refuse it automatically if the migration actually committed.
        run(['docker', 'start', API, WORKER])
        raise
    write(ROOT / 'schema-upgraded.json', json.dumps(candidate))
    compose('up', '--no-build', '--no-deps', '-d', 'api', 'worker')
    healthy()
    verify()


def verify():
    candidate = json.loads((ROOT / 'candidate.json').read_text())
    before = json.loads((ROOT / 'baseline.json').read_text())
    current = records([API, WORKER, *KEEP])
    if any(not v['State']['Running'] for v in current):
        raise RuntimeError('An existing service is not running')
    if any(v['Config']['Image'] != candidate['image'] for v in current[:2]):
        raise RuntimeError('Native image differs from candidate')
    if [v['Id'] for v in current[2:]] != [v['Id'] for v in before['containers'][2:]]:
        raise RuntimeError('An unrelated service was replaced')
    mounts = []
    enabled = []
    for v in current[:2]:
        spool = [m for m in v['Mounts'] if m['Destination'] == '/var/lib/twin-vault/spool' and m['RW']]
        if len(spool) != 1:
            raise RuntimeError('Writable spool is missing')
        mounts.append(spool[0]['Source'])
        enabled.append(environment(v).get('ONLYFANS_VAULT_MEDIA_ENABLED'))
    if mounts[0] != mounts[1] or enabled[0] != enabled[1]:
        raise RuntimeError('API/worker media configuration differs')
    image_mount = next(m for m in current[0]['Mounts'] if m['Destination'] == '/var/lib/twin-vault/images')
    if image_mount['RW']:
        raise RuntimeError('API image-cache mount must remain read-only')
    ready = schema()
    worker_ready = schema(WORKER)
    if ('0088_native_vault_media' not in ready or '0088_native_vault_media' not in worker_ready
            or counts() != before['native']):
        raise RuntimeError('Schema or existing policy/binding baseline differs')
    code = '''import json,os,urllib.request,urllib.error
from onlyfans_read.contract import SCHEMA_VERSION,SCHEMA_VERSION_HEADER
scope=json.dumps({'creatorIds':['00000000-0000-4000-8000-000000000001'],'actor':'agency-user:00000000-0000-4000-8000-000000000002:00000000-0000-4000-8000-000000000003','canManage':False})
req=urllib.request.Request('http://127.0.0.1:8787/onlyfans/agency/v1/api/v1/status',headers={'Authorization':'Bearer '+os.environ['ONLYFANS_API_TOKEN'],SCHEMA_VERSION_HEADER:SCHEMA_VERSION,'X-OnlyFans-Agency-Scope':scope})
with urllib.request.urlopen(req,timeout=10) as r:
 assert r.status==200 and r.headers['X-OnlyFans-Access-Version']=='of-agency.v1'
req=urllib.request.Request('http://127.0.0.1:8787/onlyfans/agency/v1/api/v1/accounts',headers={'Authorization':'Bearer '+os.environ['ONLYFANS_API_TOKEN'],SCHEMA_VERSION_HEADER:SCHEMA_VERSION,'X-OnlyFans-Agency-Scope':scope})
with urllib.request.urlopen(req,timeout=10) as r:
 assert r.status==200 and json.load(r)['accounts']==[]
req=urllib.request.Request('http://127.0.0.1:8787/onlyfans/agency/v1/control/v1/accounts/deployment-probe',data=b'{}',headers={'Authorization':'Bearer '+os.environ['ONLYFANS_CONTROL_TOKEN'],'X-OnlyFans-Agency-Scope':scope,'Content-Type':'application/json'})
try:
 urllib.request.urlopen(req,timeout=10)
except urllib.error.HTTPError as e:
 assert e.code==403
else:
 raise AssertionError('Read-only write was not refused')
print('PASS authenticated agency scope, empty assignment, read-only refusal and version handshake')
'''
    print(run(['docker', 'exec', '-i', API, 'python', '-'], data=code.encode()).decode().strip(), flush=True)
    run(['docker', 'exec', WORKER, 'ffprobe', '-version'])
    result = {'nativeImage': candidate['image'], 'nativeCommit': candidate['commit'],
              'schema': ready, 'mediaEnabled': enabled[0] == '1', 'sharedSpool': True,
              'policiesPreserved': True, 'unrelatedContainersPreserved': True}
    write(ROOT / 'verified.json', json.dumps(result))
    print(json.dumps(result), flush=True)


def media(enabled):
    verify()
    config = json.loads(CONFIG.read_text())
    for name in ['api', 'worker']:
        config['services'][name]['environment']['ONLYFANS_VAULT_MEDIA_ENABLED'] = '1' if enabled else '0'
    write(CONFIG, json.dumps(config))
    compose('up', '--no-build', '--no-deps', '-d', 'api', 'worker')
    healthy()
    verify()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['backup', 'prepare', 'deploy', 'verify', 'enable', 'disable'])
    parser.add_argument('--image')
    parser.add_argument('--commit')
    args = parser.parse_args()
    if args.phase == 'prepare':
        prepare(args.image or '', args.commit or '')
    elif args.phase in ['enable', 'disable']:
        media(args.phase == 'enable')
    else:
        globals()[args.phase]()
