"""Jesse-authorized guarded rollout of the roster sync repair, 29 October 2026.

Each derivative preserves its exact observed service image. Configuration stays
on the host. Old containers, images, database backup and evidence are retained.
"""
import copy
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/var/lib/twin-release/roster-sync-20261029')
COMPOSE = Path('/etc/twin-onlyfans/media-agency-release-compose.json')
EXPECTED = {
    'twin-onlyfans-worker-1': 'sha256:e02594de92cfba0abd52cc53f77177b54cf16393b33a3101949e0fe37831cf59',
    'twin-onlyfans-api-1': 'sha256:341e18b253ecd06572153853c711083829496cb162562de4f3f65ca61bda740c',
    'twin-onlyfans-fastt-supervisor-1': 'sha256:8d6f19ba1c0220f5416de43827bd5ddd0ded39a8539197fe34d3983fdbe86e37',
    'twin-onlyfans-profile-learner': 'sha256:341e18b253ecd06572153853c711083829496cb162562de4f3f65ca61bda740c',
}
UNCHANGED = {
    'next-app': 'sha256:3e7aff70aa8d10c370c60000fcafd3c3be0e3a65347a3a0719c8412c9f9bca74',
    'twin-onlyfans-managed-proxy-1': 'sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec',
    'twin-onlyfans-creator-sign-in-1': 'sha256:b63d926bf03c7b6abe5a52c9bfa77b4d6821d7543d16ec1ab64155c1dc925b6e',
}
MIGRATION = 'service/db/migrations/versions/20261029_0200_fan_identity_sync.py'
SCHEMA_FILES = {'service/db/schema.py', 'service/db/models.py', MIGRATION}
API_FILES = SCHEMA_FILES | {'service/onlyfans_runtime/launch_history_memory/recovery.py',
                            'service/onlyfans_runtime/profiles.py',
                            'service/onlyfans_runtime/read_failures.py'}


def private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(90)
        self.sock.connect('/var/run/docker.sock')


def docker(method, path, data=None, missing=False):
    c = UnixHTTP('localhost')
    try:
        c.request(method, path, None if data is None else json.dumps(data).encode(),
                  {'Content-Type': 'application/json'})
        r = c.getresponse()
        body = r.read()
        if missing and r.status == 404:
            return None
        if not (200 <= r.status < 300 or r.status == 304):
            private(ROOT / 'docker-error.private', body.decode(errors='replace'))
            raise RuntimeError('Docker operation failed; private diagnostic retained')
        return json.loads(body) if body else None
    finally:
        c.close()


def inspect(name):
    return docker('GET', '/containers/' + name + '/json')


def networks(old):
    if old['HostConfig']['NetworkMode'] in {'host', 'none'}:
        return {}
    return {name: {'Aliases': [a for a in point.get('Aliases') or []
                              if a not in {old['Id'], old['Id'][:12]}]}
            for name, point in old['NetworkSettings']['Networks'].items()}


def baseline():
    result = {name: inspect(name) for name in {**EXPECTED, **UNCHANGED}}
    for name, image in {**EXPECTED, **UNCHANGED}.items():
        assert result[name]['Image'] == image and result[name]['State']['Running'], name + ' changed'
    return result


def run_private(argv, label, **kwargs):
    path = ROOT / (label + '.private.log')
    with path.open('wb') as f:
        os.chmod(path, 0o600)
        result = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT, **kwargs)
    if result.returncode:
        raise RuntimeError(label + ' failed; private diagnostic retained')


def stage():
    old = baseline()
    files = json.loads((ROOT / 'overlay.json').read_text())
    candidates = {}
    for name in EXPECTED:
        if name == 'twin-onlyfans-worker-1':
            selected = [x for x in files if not x['path'].startswith('service/tests/')]
        else:
            allowed = SCHEMA_FILES if name == 'twin-onlyfans-fastt-supervisor-1' else API_FILES
            selected = [x for x in files if x['path'] in allowed]
        dest = ROOT / ('build-' + name)
        dest.mkdir(mode=0o700, exist_ok=True)
        check = "import pathlib,hashlib,json\nresult={}\n"
        check += 'paths=' + repr([x['path'] for x in selected]) + '\n'
        check += "for rel in paths:\n p=pathlib.Path('/app')/rel\n result[rel]=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None\nprint(json.dumps(result))"
        hashes = json.loads(subprocess.check_output(['docker', 'exec', name, 'python', '-B', '-c', check]))
        for item in selected:
            assert hashes[item['path']] == item['before_sha256'], name + ': source changed: ' + item['path']
            data = item['content'].encode()
            assert hashlib.sha256(data).hexdigest() == item['after_sha256']
            target = dest / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        base_tag = 'twin-onlyfans:roster-sync-base-' + name
        tag = 'twin-onlyfans:roster-sync-20261029-' + name
        subprocess.run(['docker', 'tag', EXPECTED[name], base_tag], check=True)
        (dest / 'Dockerfile').write_text('FROM ' + base_tag + '\nCOPY service/ /app/service/\nLABEL io.twin.roster_sync="20261029"\n')
        run_private(['docker', 'build', '--pull=false', '--network', 'none', '-t', tag, str(dest)], 'build-' + name)
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]['Id']
        code = "from db.schema import HEAD; from db.models import Fan; assert HEAD=='0099_fan_identity_sync'; assert hasattr(Fan,'of_profile_checked_at'); print('schema imports passed')"
        if name == 'twin-onlyfans-worker-1':
            code += "; from onlyfans_runtime.profile_sync import step; from onlyfans_runtime import read_failures, native_worker"
        run_private(['docker', 'run', '--rm', '--network', 'none', '--memory', '256m', '--cpus', '.5',
                     '-e', 'PYTHONPATH=/app/service', '--entrypoint', 'python', image, '-B', '-c', code], 'smoke-' + name)
        candidates[name] = image
        print(json.dumps({'staged': name, 'image': image, 'changedFiles': len(selected)}), flush=True)
    baseline()
    private(ROOT / 'candidates.json', json.dumps(candidates))
    print('All exact-base derivative images staged; no running service changed', flush=True)


def oneshot(old, image, code, purpose):
    config = copy.deepcopy(old['Config'])
    config.update(Image=image, Entrypoint=['python'], Cmd=['-B', '-c', code],
                  Labels={'io.twin.roster_sync': '20261029', 'io.twin.purpose': purpose}, Healthcheck={'Test': ['NONE']})
    config['HostConfig'] = copy.deepcopy(old['HostConfig'])
    config['HostConfig'].update(RestartPolicy={'Name': 'no', 'MaximumRetryCount': 0},
                                Memory=536870912, NanoCpus=500000000)
    # Use the existing default network, with no API/sender aliases on a helper.
    name = 'twin-roster-sync-' + purpose + '-' + str(time.time_ns())
    cid = docker('POST', '/containers/create?name=' + name, config)['Id']
    docker('POST', '/containers/' + cid + '/start')
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = inspect(cid)['State']
        if not state['Running']:
            run_private(['docker', 'logs', cid], purpose + '-output')
            assert state['ExitCode'] == 0, purpose + ' failed; private diagnostic retained'
            return
        time.sleep(1)
    docker('POST', '/containers/' + cid + '/stop?t=5')
    raise RuntimeError(purpose + ' exceeded deadline')


DB_PREFIX = """import os
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from db.schema import check_schema,upgrade,_config
from alembic import command
url=make_url(os.environ['DATABASE_URL']).set(drivername='postgresql+psycopg2')
e=create_engine(url,connect_args={'options':'-c lock_timeout=5000 -c statement_timeout=30000'})
"""


def health():
    for _ in range(45):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=3) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health deadline exceeded')


def restore(old, name, new_id):
    current = docker('GET', '/containers/' + name + '/json', missing=True)
    if current and current['Id'] != old['Id']:
        assert current['Id'] == new_id, 'Concurrent change; rollback refused'
        docker('POST', '/containers/' + new_id + '/stop?t=30')
        docker('POST', '/containers/' + new_id + '/update', {'RestartPolicy': {'Name': 'no'}})
        docker('POST', '/containers/' + new_id + '/rename?name=' + name + '.failed-sync-' + new_id[:10])
        for network in networks(current):
            docker('POST', '/networks/' + network + '/disconnect', {'Container': new_id, 'Force': True})
    actual = inspect(old['Id'])
    if actual['Name'] != '/' + name:
        docker('POST', '/containers/' + old['Id'] + '/rename?name=' + name)
    for network, endpoint in networks(old).items():
        if network not in inspect(old['Id'])['NetworkSettings']['Networks']:
            docker('POST', '/networks/' + network + '/connect', {'Container': old['Id'], 'EndpointConfig': endpoint})
    docker('POST', '/containers/' + old['Id'] + '/update', {'RestartPolicy': old['HostConfig']['RestartPolicy']})
    docker('POST', '/containers/' + old['Id'] + '/start')


def create_replacement(old, name, image):
    assert inspect(name)['Id'] == old['Id'] and not inspect(name)['State']['Running']
    archive = name + '.rollback-sync-' + old['Id'][:10]
    assert docker('GET', '/containers/' + archive + '/json', missing=True) is None
    config = copy.deepcopy(old['Config'])
    config['Image'] = image
    config['Labels'] = {**(config.get('Labels') or {}), 'io.twin.roster_sync': '20261029'}
    config['HostConfig'] = copy.deepcopy(old['HostConfig'])
    points = networks(old)
    if points:
        config['NetworkingConfig'] = {'EndpointsConfig': points}
    docker('POST', '/containers/' + old['Id'] + '/update', {'RestartPolicy': {'Name': 'no'}})
    docker('POST', '/containers/' + old['Id'] + '/rename?name=' + archive)
    for network in points:
        docker('POST', '/networks/' + network + '/disconnect', {'Container': old['Id'], 'Force': True})
    return docker('POST', '/containers/create?name=' + name, config)['Id']


def cutover():
    old = baseline()
    candidates = json.loads((ROOT / 'candidates.json').read_text())
    assert set(candidates) == set(EXPECTED)
    private(ROOT / 'baseline.private.json', json.dumps(old))
    compose_before = COMPOSE.read_text()
    private(ROOT / 'compose-before.private.json', compose_before)
    worker = old['twin-onlyfans-worker-1']
    image = candidates['twin-onlyfans-worker-1']
    oneshot(worker, image, DB_PREFIX + "with e.connect() as c:\n c.exec_driver_sql('BEGIN READ ONLY')\n check_schema(c,expected='0098_ledger_exclusion')\n c.rollback()\nprint('Pre-migration structure verified')", 'preflight')
    # Full logical backup stays on this same host. No message text is exported.
    db = inspect('twin-onlyfans-database-1')
    db_env = dict(x.split('=', 1) for x in db['Config']['Env'])
    backups = ROOT / 'backups'; backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / ('before-sync-' + str(time.time_ns()) + '.dump')
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        with (ROOT / 'backup.private.log').open('wb') as err:
            os.chmod(ROOT / 'backup.private.log', 0o600)
            result = subprocess.run(['docker', 'exec', db['Id'], 'pg_dump', '-Fc', '-U', db_env.get('POSTGRES_USER', 'postgres'),
                                     '-d', db_env.get('POSTGRES_DB', db_env.get('POSTGRES_USER', 'postgres'))], stdout=out, stderr=err, timeout=600)
    assert result.returncode == 0 and backup.stat().st_size > 1000, 'Backup failed'
    print(json.dumps({'backupBytes': backup.stat().st_size}), flush=True)
    fresh = baseline()
    assert all(fresh[name]['Id'] == old[name]['Id'] for name in old), 'Concurrent change before cutover'
    assert COMPOSE.read_text() == compose_before, 'Compose changed before cutover'
    migration_attempted = False
    changed = {}
    stopped = []
    try:
        for name in EXPECTED:
            docker('POST', '/containers/' + old[name]['Id'] + '/stop?t=45')
            stopped.append(name)
        migration_attempted = True
        oneshot(worker, image, DB_PREFIX + "with e.begin() as c:\n assert c.scalar(text('SELECT version_num FROM alembic_version'))=='0098_ledger_exclusion'\n upgrade(c)\nprint('0099_fan_identity_sync committed')", 'migrate')
        for name, candidate in candidates.items():
            changed[name] = create_replacement(old[name], name, candidate)
        for name in ['twin-onlyfans-api-1', 'twin-onlyfans-profile-learner', 'twin-onlyfans-worker-1', 'twin-onlyfans-fastt-supervisor-1']:
            docker('POST', '/containers/' + changed[name] + '/start')
            if name == 'twin-onlyfans-api-1': health()
        time.sleep(5)
        for name, cid in changed.items():
            current = inspect(name)
            assert current['Id'] == cid and current['Image'] == candidates[name] and current['State']['Running']
            assert current['Config']['Env'] == old[name]['Config']['Env'], 'Environment changed'
        for name in UNCHANGED:
            assert inspect(name)['Id'] == old[name]['Id'], 'Concurrent service change'
        oneshot(worker, image, DB_PREFIX + "with e.connect() as c:\n c.exec_driver_sql('BEGIN READ ONLY')\n check_schema(c)\n c.rollback()\nprint('Post-migration schema verified')", 'verify')
        assert COMPOSE.read_text() == compose_before, 'Compose changed during cutover'
        compose = json.loads(compose_before)
        for service, name in [('api', 'twin-onlyfans-api-1'), ('worker', 'twin-onlyfans-worker-1'), ('fastt-supervisor', 'twin-onlyfans-fastt-supervisor-1')]:
            compose['services'][service]['image'] = candidates[name]
        temp = COMPOSE.with_suffix('.roster-sync.tmp')
        private(temp, json.dumps(compose))
        os.chmod(temp, COMPOSE.stat().st_mode & 0o777)
        os.replace(temp, COMPOSE)
        result = {'images': candidates, 'containers': changed, 'schema': '0099_fan_identity_sync', 'deployedAtEpoch': time.time()}
        private(ROOT / 'deployed.json', json.dumps(result))
        print(json.dumps(result), flush=True)
    except Exception:
        for name, cid in changed.items():
            docker('POST', '/containers/' + cid + '/stop?t=45')
        if migration_attempted:
            oneshot(worker, image, DB_PREFIX + "with e.begin() as c:\n revision=c.scalar(text('SELECT version_num FROM alembic_version'))\n assert revision in {'0098_ledger_exclusion','0099_fan_identity_sync'}\n if revision=='0099_fan_identity_sync': command.downgrade(_config(c),'0098_ledger_exclusion')\n check_schema(c,expected='0098_ledger_exclusion')\nprint('Schema rollback verified')", 'rollback-schema')
        for name in stopped:
            restore(old[name], name, changed.get(name))
        health()
        print('Rollout failed; previous running services restored', flush=True)
        raise


def main():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    locks = []
    for path in [ROOT / 'cutover.lock', Path('/var/lib/twin-release/profiles-memory-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/agency-crm-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/astra-compatible-20261028/cutover.lock')]:
        handle = path.open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(handle)
    {'stage': stage, 'cutover': cutover}[sys.argv[1]]()


if __name__ == '__main__':
    main()
