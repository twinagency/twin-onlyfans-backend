"""Bounded history page and bare-line-break fixes from live evidence; worker only."""
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
BASE = 'sha256:6133be0247c7dd2cc009554d84a143412791635deb62013f6ded6f3d6a676bc6'
BEFORE = {'service/onlyfans_runtime/provider_connector.py':'3f5183105251931fccd4e463d5e40544552f3799ca0a9d0204d368e49917c928', 'service/onlyfans_runtime/native_protocol.py':'77f46f8a499a38181534d1794da56be86a2dd15ed6acddbdf31cd76e83e96ec9'}


def main():
    locks = []
    for path in [ROOT / 'cutover.lock', Path('/var/lib/twin-release/profiles-memory-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/agency-crm-20261029/cutover.lock'),
                 Path('/var/lib/twin-release/astra-compatible-20261028/cutover.lock')]:
        handle = path.open('a'); fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); locks.append(handle)
    old = release.inspect(WORKER)
    assert old['Image'] == BASE and old['State']['Running'], 'Worker changed'
    others = {n: release.inspect(n)['Id'] for n in ['twin-onlyfans-api-1','twin-onlyfans-fastt-supervisor-1','twin-onlyfans-profile-learner','next-app']}
    payload = json.loads((ROOT / 'history-pages-source.json').read_text())
    assert {item['path'] for item in payload['files']}==set(BEFORE)
    dest = ROOT / 'build-history-pages'; (dest / 'service/onlyfans_runtime').mkdir(parents=True,exist_ok=True)
    for item in payload['files']:
        path=item['path']
        current=subprocess.check_output(['docker','exec',WORKER,'python','-B','-c',
            "from pathlib import Path; import sys; sys.stdout.buffer.write(Path('/app/"+path+"').read_bytes())"])
        assert hashlib.sha256(current).hexdigest()==BEFORE[path], 'Worker source changed'
        candidate=item['content'].encode()
        assert hashlib.sha256(candidate).hexdigest()==item['sha256']
        (dest/path).write_bytes(candidate)
    subprocess.run(['docker','tag',BASE,'twin-onlyfans:roster-sync-history-pages-base'],check=True)
    (dest / 'Dockerfile').write_text('FROM twin-onlyfans:roster-sync-history-pages-base\nCOPY service/ /app/service/\nLABEL io.twin.history_page_limit="20"\n')
    tag = 'twin-onlyfans:roster-sync-history-pages-20261029'
    release.run_private(['docker','build','--pull=false','--network','none','-t',tag,str(dest)],'build-history-pages')
    image = json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
    release.run_private(['docker','run','--rm','--network','none','--memory','256m','--cpus','.5',
        '-e','PYTHONPATH=/app/service','--entrypoint','python',image,'-B','-c',
        "from onlyfans_runtime.provider_connector import NativeProviderConnector; from onlyfans_runtime.native_protocol import history_text; assert history_text('first<br>second')=='first\\nsecond'; from db.schema import HEAD; assert HEAD=='0099_fan_identity_sync'; import inspect; assert inspect.signature(NativeProviderConnector).parameters['read_timeout_seconds'].default==10; assert inspect.signature(NativeProviderConnector).parameters['timeout_seconds'].default==5"], 'smoke-history-pages')
    before_compose = release.COMPOSE.read_text()
    release.private(ROOT / 'before-history-pages.private.json',json.dumps(old))
    release.private(ROOT / 'compose-before-history-pages.private.json',before_compose)
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
        tmp=release.COMPOSE.with_suffix('.history-pages.tmp');release.private(tmp,json.dumps(compose))
        os.chmod(tmp,release.COMPOSE.stat().st_mode & 0o777);os.replace(tmp,release.COMPOSE)
        previous=(ROOT/'deployed.json').read_text();release.private(ROOT/'deployed-before-history-pages.json',previous)
        result=json.loads(previous);result['images'][WORKER]=image;result['containers'][WORKER]=cid
        result.update(historyPageLimit=20,historyPagesSource=payload['source'],historyPagesDeployedAtEpoch=time.time())
        release.private(ROOT/'deployed.json',json.dumps(result));print(json.dumps(result),flush=True)
    except Exception:
        release.restore(old,WORKER,cid)
        raise


if __name__=='__main__':main()
