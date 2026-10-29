import importlib.util,pathlib,json,fcntl,os,time,copy
root=pathlib.Path('/var/lib/twin-release/profiles-memory-20261029')
spec=importlib.util.spec_from_file_location('profiles_rollout',root/'rollout.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.SOURCE='8a912627879e2d59d65fe6cca0b21a78a5b2634a'
locks=[]
for path in [root/'cutover.lock',pathlib.Path('/var/lib/twin-release/agency-crm-20261029/cutover.lock'),pathlib.Path('/var/lib/twin-release/astra-compatible-20261028/cutover.lock')]:
 f=path.open('a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
gate=json.loads((root/'canary-v5-passed.json').read_text())
assert gate['passed'] and gate['source']==m.SOURCE,'Canary not accepted'
expected={'twin-onlyfans-api-1':'sha256:3e6e4579fefd31d1736a3a45578d3aff115bfb711f10c287248e236de78112cc','twin-onlyfans-fastt-supervisor-1':'sha256:f542dedd0ac57a729f3a67725675a4ea73a53704836482c1e91e93367360bf83',m.LEARNER:'sha256:3e6e4579fefd31d1736a3a45578d3aff115bfb711f10c287248e236de78112cc'}
baseline={name:m.inspect(name) for name in [*expected,'twin-onlyfans-worker-1','next-app']}
for name,image in expected.items():
 assert baseline[name]['Image']==image and baseline[name]['State']['Running'],'Concurrent change '+name
candidate=json.loads((root/'candidate-v5.json').read_text())
images={'twin-onlyfans-api-1':candidate['api'],'twin-onlyfans-fastt-supervisor-1':candidate['sender'],m.LEARNER:candidate['api']}
baseline[m.LEARNER]['Config']['Labels'].update({k:v for k,v in baseline['twin-onlyfans-api-1']['Config']['Labels'].items() if k.startswith('com.docker.compose.')})
baseline[m.LEARNER]['Config']['Labels']['com.docker.compose.service']='profile-learner'
m.private(root/'rollout-v5-baseline.private.json',json.dumps(baseline))
before=m.COMPOSE.read_text();m.private(root/'compose-before-v5.private.json',before)
original_docker=m.docker
def graceful(method,path,data=None,missing=False):
 if '/containers/'+baseline[m.LEARNER]['Id']+'/stop?t=30'==path:
  path=path.replace('t=30','t=60')
 return original_docker(method,path,data,missing)
m.docker=graceful
changed={}
try:
 for name,image in images.items():
  changed[name]=m.replace(baseline[name],name,image)
 assert m.inspect('next-app')['Id']==baseline['next-app']['Id'],'Concurrent dashboard change'
 m.health()
 assert m.COMPOSE.read_text()==before,'Concurrent compose change; preserve the newer release'
 compose=json.loads(before)
 for svc,name in [('api','twin-onlyfans-api-1'),('fastt-supervisor','twin-onlyfans-fastt-supervisor-1')]:
  compose['services'][svc]['image']=images[name]
  compose['services'][svc]['environment'].update(ONLYFANS_PROFILE_LEARNING_ENABLED='1',BUILD_SHA=m.SOURCE)
 worker=copy.deepcopy(compose['services']['worker'])
 worker.update(image=images[m.LEARNER],entrypoint=['python'],command=['-m','onlyfans_runtime.profile_learning.worker'],container_name=m.LEARNER,restart='unless-stopped',mem_limit='512m',cpus=.5)
 worker.pop('depends_on',None)
 worker['environment'].update(ONLYFANS_PROCESS_ROLE='worker',ONLYFANS_WORKER_TENANT='twin-internal',ONLYFANS_PROFILE_CONCURRENCY='2',PYTHONPATH='/app/service',BUILD_SHA=m.SOURCE)
 compose['services']['profile-learner']=worker
 temp=m.COMPOSE.with_suffix('.profiles-v5.tmp');m.private(temp,json.dumps(compose));os.chmod(temp,m.COMPOSE.stat().st_mode&0o777);assert m.COMPOSE.read_text()==before,'Concurrent compose change; preserve the newer release'
 os.replace(temp,m.COMPOSE)
 result={'source':m.SOURCE,'images':images,'containers':changed,'deployedAtEpoch':time.time(),'syncWorkerPreserved':m.inspect('twin-onlyfans-worker-1')['Id']}
 m.private(root/'deployed-profiles-v5.json',json.dumps(result))
 print(json.dumps(result),flush=True)
except Exception:
 for name,new_id in reversed(list(changed.items())):m.restore(baseline[name],name,new_id)
 m.health()
 raise
