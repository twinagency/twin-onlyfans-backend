"""Guarded host-local rollout, authorised by Jesse on 29 October 2026.

Run on the existing dashboard host only. Keeps stopped rollback containers,
images and private configuration on that host. Does not change reply controls.
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
import time
import urllib.request

ROOT = Path('/var/lib/twin-release/profiles-memory-20261029')
SOURCE = 'b7d634da9506f959eeabd36f95f9e34bbde3275b'
EXPECTED = {
    'twin-onlyfans-api-1': 'sha256:e5cd643dd613b89e893435064705d215f5c281ea783012f57381bba5f40ff4b6',
    'twin-onlyfans-fastt-supervisor-1': 'sha256:80341b333fb171478d555a5d729fb6375420afaf5d0a38b64ad4cacc467bf756',
    'twin-onlyfans-worker-1': 'sha256:e02594de92cfba0abd52cc53f77177b54cf16393b33a3101949e0fe37831cf59',
    'next-app': 'sha256:3e7aff70aa8d10c370c60000fcafd3c3be0e3a65347a3a0719c8412c9f9bca74',
}
CANDIDATES = {
    'twin-onlyfans-api-1': 'sha256:341e18b253ecd06572153853c711083829496cb162562de4f3f65ca61bda740c',
    'twin-onlyfans-fastt-supervisor-1': 'sha256:8d6f19ba1c0220f5416de43827bd5ddd0ded39a8539197fe34d3983fdbe86e37',
}
LEARNER = 'twin-onlyfans-profile-learner'
COMPOSE = Path('/etc/twin-onlyfans/media-agency-release-compose.json')


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(60)
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
            p = ROOT / 'docker-error.private'
            p.write_bytes(body)
            p.chmod(0o600)
            raise RuntimeError('Docker operation failed; private diagnostic retained')
        return json.loads(body) if body else None
    finally:
        c.close()


def inspect(name):
    return docker('GET', '/containers/' + name + '/json')


def private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


def networks(old):
    if old['HostConfig']['NetworkMode'] in {'host', 'none'}:
        return {}
    return {name: {'Aliases': [a for a in point.get('Aliases') or []
                              if a not in {old['Id'], old['Id'][:12]}]}
            for name, point in old['NetworkSettings']['Networks'].items()}


def health():
    for _ in range(50):
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
    if current:
        assert current['Id'] == new_id, 'Concurrent change, rollback refused'
        docker('POST', '/containers/' + new_id + '/stop?t=25')
        docker('POST', '/containers/' + new_id + '/rename?name=' + name + '.failed-profiles-' + new_id[:10])
        for network in networks(current):
            docker('POST', '/networks/' + network + '/disconnect', {'Container': new_id, 'Force': True})
    docker('POST', '/containers/' + old['Id'] + '/rename?name=' + name)
    for network, endpoint in networks(old).items():
        if network not in inspect(old['Id'])['NetworkSettings']['Networks']:
            docker('POST', '/networks/' + network + '/connect', {'Container': old['Id'], 'EndpointConfig': endpoint})
    docker('POST', '/containers/' + old['Id'] + '/start')


def replace(old, name, image):
    assert inspect(name)['Id'] == old['Id'], 'Concurrent production change'
    archive = name + '.rollback-profiles-' + old['Id'][:10]
    assert docker('GET', '/containers/' + archive + '/json', missing=True) is None
    config = copy.deepcopy(old['Config'])
    config['Image'] = image
    env = dict(x.split('=', 1) for x in config['Env'])
    env.update(ONLYFANS_PROFILE_LEARNING_ENABLED='1', BUILD_SHA=SOURCE)
    config['Env'] = [k + '=' + v for k, v in env.items()]
    config['Labels'].update({'io.twin.profile_memory': SOURCE})
    config['HostConfig'] = copy.deepcopy(old['HostConfig'])
    points = networks(old)
    if points:
        config['NetworkingConfig'] = {'EndpointsConfig': points}
    new_id = None
    renamed = False
    try:
        docker('POST', '/containers/' + old['Id'] + '/stop?t=30')
        docker('POST', '/containers/' + old['Id'] + '/rename?name=' + archive)
        renamed = True
        for network in points:
            docker('POST', '/networks/' + network + '/disconnect', {'Container': old['Id'], 'Force': True})
        new_id = docker('POST', '/containers/create?name=' + name, config)['Id']
        docker('POST', '/containers/' + new_id + '/start')
        if name == 'twin-onlyfans-api-1':
            health()
        time.sleep(3)
        current = inspect(name)
        assert current['Id'] == new_id and current['Image'] == image and current['State']['Running']
        return new_id
    except Exception:
        if renamed:
            restore(old, name, new_id)
        else:
            docker('POST', '/containers/' + old['Id'] + '/start')
        raise


def migrate():
    sql = '\n'.join((ROOT / 'deploy/profile-learning' / name).read_text()
                    for name in ('schema.sql', 'notifications-postgresql.sql'))
    code = '''import os,json
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'])
with e.begin() as c:
 c.exec_driver_sql("SET LOCAL lock_timeout='3s'")
 c.exec_driver_sql("SET LOCAL statement_timeout='30s'")
 assert c.scalar(text("SELECT version_num FROM alembic_version"))=='0098_ledger_exclusion'
 c.exec_driver_sql(SQL)
 assert c.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'of_profile_%'"))>=7
 print('Profile schema and incremental trigger committed',flush=True)
e.dispose()
'''.replace('SQL', repr(sql))
    subprocess.run(['docker', 'exec', '-i', 'twin-onlyfans-api-1', 'python', '-'], input=code.encode(), check=True)


def learner(old):
    assert docker('GET', '/containers/' + LEARNER + '/json', missing=True) is None
    config = copy.deepcopy(old['Config'])
    config['Image'] = CANDIDATES['twin-onlyfans-api-1']
    config['Entrypoint'] = ['python']
    config['Cmd'] = ['-m', 'onlyfans_runtime.profile_learning.worker']
    config['Labels'] = {'io.twin.profile_memory': SOURCE, 'io.twin.component': 'profile-learner'}
    env = dict(x.split('=', 1) for x in config['Env'])
    env.update(ONLYFANS_PROCESS_ROLE='worker', ONLYFANS_WORKER_TENANT='twin-internal',
               ONLYFANS_PROFILE_CONCURRENCY='2', PYTHONPATH='/app/service')
    config['Env'] = [k + '=' + v for k, v in env.items()]
    config['HostConfig'] = copy.deepcopy(old['HostConfig'])
    config['HostConfig'].update(Memory=536870912, NanoCpus=500000000,
                                RestartPolicy={'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                                LogConfig={'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}})
    new_id = docker('POST', '/containers/create?name=' + LEARNER, config)['Id']
    docker('POST', '/containers/' + new_id + '/start')
    return new_id


def main():
    locks = []
    for path in [ROOT / 'cutover.lock', Path('/var/lib/twin-release/agency-crm-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/astra-compatible-20261028/cutover.lock')]:
        handle = path.open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(handle)
    baseline = {name: inspect(name) for name in EXPECTED}
    for name, expected in EXPECTED.items():
        assert baseline[name]['Image'] == expected and baseline[name]['State']['Running'], name + ' changed'
    private(ROOT / 'rollout-baseline.private.json', json.dumps(baseline))
    compose_before = COMPOSE.read_text()
    private(ROOT / 'compose-before-profiles.private.json', compose_before)
    assert list((ROOT / 'backups').glob('schema-before-profiles-*.sql')), 'Missing schema backup'
    for image in CANDIDATES.values():
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--memory', '128m', '--cpus', '.25',
                        '--entrypoint', 'python', image, '-c',
                        "from onlyfans_runtime.profile_learning.temporal import resolve; from onlyfans_runtime.profile_learning.evidence import validate; assert resolve('next week','2026-01-04T20:00:00Z','Australia/Brisbane')[0]=='2026-01-12'; print('Image import and timezone smoke passed')"], check=True)
    migrate()
    changed = {}
    learner_id = None
    try:
        for name, image in CANDIDATES.items():
            changed[name] = replace(baseline[name], name, image)
        learner_id = learner(baseline['twin-onlyfans-worker-1'])
        for name in ('twin-onlyfans-worker-1', 'next-app'):
            assert inspect(name)['Id'] == baseline[name]['Id'], 'Concurrent service change'
        health()
        compose = json.loads(compose_before)
        for service, name in [('api', 'twin-onlyfans-api-1'), ('fastt-supervisor', 'twin-onlyfans-fastt-supervisor-1')]:
            compose['services'][service]['image'] = CANDIDATES[name]
            env = compose['services'][service].setdefault('environment', {})
            assert isinstance(env, dict), 'Unexpected compose environment shape'
            env['ONLYFANS_PROFILE_LEARNING_ENABLED'] = '1'
        temp = COMPOSE.with_suffix('.profiles.tmp')
        private(temp, json.dumps(compose))
        os.chmod(temp, COMPOSE.stat().st_mode & 0o777)
        os.replace(temp, COMPOSE)
        result = {'source': SOURCE, 'images': CANDIDATES, 'containers': changed, 'learner': learner_id,
                  'syncWorkerPreserved': baseline['twin-onlyfans-worker-1']['Id'], 'deployedAtEpoch': time.time()}
        private(ROOT / 'deployed-profiles.json', json.dumps(result))
        print(json.dumps(result), flush=True)
    except Exception:
        if learner_id:
            docker('POST', '/containers/' + learner_id + '/stop?t=30')
        for name, new_id in reversed(list(changed.items())):
            restore(baseline[name], name, new_id)
        health()
        raise


if __name__ == '__main__':
    main()
