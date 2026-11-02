"""Host-side preflight and private backup only. No production writes/restarts."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import time

os.umask(0o077)
P = pathlib.Path('/var/lib/twin-release/recent-history-20261102-stage')
C = pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def inspect(name):
    return json.loads(run(['docker', 'inspect', name]))[0]


assert '22 passed' in (P / 'live-base-tests.log').read_text()
assert 'ready (0110_recent_history)' in (P / 'live-schema-rehearsal.log').read_text()
assert shutil.disk_usage(P).free > 4 * 1024**3, 'Insufficient backup headroom'
before = {}
for role, name in [('worker', 'twin-onlyfans-worker-1'), ('api', 'twin-onlyfans-api-1')]:
    live = inspect(name)
    expected = 'sha256:f728669d70d9' if role == 'worker' else 'sha256:04532a2d9034'
    assert live['Image'].startswith(expected), 'Concurrent release changed the base'
    before[role] = live
    candidate = inspect('twin-recent-history:cohort-' + role + '-20261102')['Id']
    before[role + 'CandidateImage'] = candidate
for key, name in [('importer', 'twin-onlyfans-history-importer-1'),
                  ('learner', 'twin-onlyfans-profile-learner'),
                  ('supervisor', 'twin-onlyfans-fastt-supervisor-1')]:
    before[key] = inspect(name)
    assert not before[key]['State']['Running'], key + ' state changed'
(P / 'containers.before.private.json').write_text(json.dumps(before))
(P / 'compose.before.private.json').write_bytes(C.read_bytes())
backup = P / 'database.before.dump'
assert not backup.exists(), 'Backup already exists; inspect rather than overwrite'
with backup.open('wb') as output:
    subprocess.run(['docker', 'exec', 'twin-onlyfans-database-1', 'sh', '-c',
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges'],
        check=True, stdout=output, stderr=subprocess.PIPE, timeout=900)
assert backup.stat().st_size > 1000000
h = hashlib.sha256()
with backup.open('rb') as stream:
    for part in iter(lambda: stream.read(1048576), b''):
        h.update(part)
(P / 'backup.sha256').write_text(h.hexdigest() + ' database.before.dump\n')
result = {'preparedAt': time.time(), 'productionChanged': False,
          'backupBytes': backup.stat().st_size, 'backupSha256': h.hexdigest(),
          'workerCandidateImage': before['workerCandidateImage'],
          'apiCandidateImage': before['apiCandidateImage'],
          'composeSha256': hashlib.sha256(C.read_bytes()).hexdigest()}
(P / 'preflight.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
