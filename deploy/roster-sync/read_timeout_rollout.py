"""Bounded GET deadline adjustment after live curl-28 evidence; worker only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import rollout as release

ROOT = release.ROOT
WORKER = 'twin-onlyfans-worker-1'
BASE = 'sha256:99d658000f8f9425226991fefdbcb653d70f97c080327e6265eb1d59f1b3ff5d'
BEFORE = 'a78612e0aff8c23f1791a5aa4c3d2a9c826000918855f80521d375fbe2d9e4ac'
FILE = 'service/onlyfans_runtime/provider_connector.py'


def main():
    locks = []
    for path in [ROOT / 'cutover.lock', Path('/var/lib/twin-release/profiles-memory-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/agency-crm-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/astra-compatible-20261028/cutover.lock')]:
        handle = path.open('a'); fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); locks.append(handle)
    old = release.inspect(WORKER)
    assert old['Image'] == BASE and old['State']['Running'], 'Worker changed'
    others = {n: release.inspect(n)['Id'] for n in ['twin-onlyfans-api-1','twin-onlyfans-fastt-supervisor-1','twin-onlyfans-profile-learner','next-app']}
    current = subprocess.check_output(['docker','exec',WORKER,'python','-B','-c',
        "from pathlib import Path; import sys; sys.stdout.buffer.write(Path('/app/"+FILE+"').read_bytes())"])
    assert hashlib.sha256(current).hexdigest() == BEFORE, 'Worker source changed'
    payload = json.loads((ROOT / 'read-timeout-source.json').read_text())
    candidate = payload['content'].encode()
    assert hashlib.sha256(candidate).hexdigest() == payload['sha256']
    dest = ROOT / 'build-read-timeout'; (dest / 'service/onlyfans_runtime').mkdir(parents=True,exist_ok=True)
    (dest / FILE).write_bytes(candidate)
    subprocess.run(['docker','tag',BASE,'twin-onlyfans:roster-sync-read-timeout-base'],check=True)
    (dest / 'Dockerfile').write_text('FROM twin-onlyfans:roster-sync-read-timeout-base\nCOPY service/ /app/service/\nLABEL io.twin.read_timeout="10"\n')
    tag = 'twin-onlyfans:roster-sync-read-timeout-20261029'
    release.run_private(['docker','build','--pull=false','--network','none','-t',tag,str(dest)],'build-read-timeout')
    image = json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
    release.run_private(['docker','run','--rm','--network','none','--memory','256m','--cpus','.5',
        '-e','PYTHONPATH=/app/service','--entrypoint','python',image,'-B','-c',
        "from onlyfans_runtime.provider_connector import NativeProviderConnector; from db.schema import HEAD; assert HEAD=='0099_fan_identity_sync'; import inspect; assert inspect.signature(NativeProviderConnector).parameters['read_timeout_seconds'].default==10; assert inspect.signature(NativeProviderConnector).parameters['timeout_seconds'].default==5"], 'smoke-read-timeout')
    before_compose = release.COMPOSE.read_text()
    release.private(ROOT / 'before-read-timeout.private.json',json.dumps(old))
    release.private(ROOT / 'compose-before-read-timeout.private.json',before_compose)
    assert release.inspect(WORKER)['Id'] == old['Id'], 'Concurrent worker change'
    cid = None
    try:
        release.docker('POST','/containers/'+old['Id']+'/stop?t=45')
        cid = release.create_replacement(old,WORKER,image)
        release.docker('POST','/containers/'+cid+'/start')
        time.sleep(10)
        now = release.inspect(WORKER)
        assert now['Id']==cid and now['Image']==image and now['State']['Running'] and now['RestartCount']==0
        assert now['Config']['Env']==old['Config']['Env'], 'Environment changed'
        assert all(release.inspect(n)['Id']==i for n,i in others.items()), 'Another service changed'
        assert release.COMPOSE.read_text()==before_compose, 'Compose changed'
        compose=json.loads(before_compose);compose['services']['worker']['image']=image
        tmp=release.COMPOSE.with_suffix('.read-timeout.tmp');release.private(tmp,json.dumps(compose))
        os.chmod(tmp,release.COMPOSE.stat().st_mode & 0o777);os.replace(tmp,release.COMPOSE)
        previous=(ROOT/'deployed.json').read_text();release.private(ROOT/'deployed-before-read-timeout.json',previous)
        result=json.loads(previous);result['images'][WORKER]=image;result['containers'][WORKER]=cid
        result.update(readTimeoutSeconds=10,writeTimeoutSeconds=5,readTimeoutSource=payload['source'],readTimeoutDeployedAtEpoch=time.time())
        release.private(ROOT/'deployed.json',json.dumps(result));print(json.dumps(result),flush=True)
    except Exception:
        release.restore(old,WORKER,cid)
        raise


if __name__=='__main__':main()
