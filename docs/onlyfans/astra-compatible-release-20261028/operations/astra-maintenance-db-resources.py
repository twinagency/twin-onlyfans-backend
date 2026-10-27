import subprocess,json,pathlib,time,datetime
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');name='twin-onlyfans-database-1'
def run(a):return subprocess.check_output(a,text=True,stderr=subprocess.PIPE,timeout=30)
d=json.loads(run(['docker','inspect',name]))[0];old=d['HostConfig']
assert old['NanoCpus']==500000000 and old['Memory']==268435456
assert not json.loads(run(['docker','inspect','twin-onlyfans-fastt-supervisor-1']))[0]['State']['Running']
proof={'startedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'originalCpu':old['NanoCpus'],'originalMemory':old['Memory'],'temporaryCpu':1500000000,'temporaryMemory':1073741824}
try:
 run(['docker','update','--cpus','1.5','--memory','1g','--memory-swap','2g',name])
 for i in range(120):
  if (R/'production-backup/migration-proof.json').exists():break
  time.sleep(5)
finally:
 run(['docker','update','--cpus','0.5','--memory',str(old['Memory']),'--memory-swap',str(old['MemorySwap']),name])
 d=json.loads(run(['docker','inspect',name]))[0]
 assert d['HostConfig']['NanoCpus']==old['NanoCpus'] and d['HostConfig']['Memory']==old['Memory'] and d['State']['Running'] and not d['State']['OOMKilled']
 proof['restoredAt']=datetime.datetime.now(datetime.timezone.utc).isoformat();proof['restored']=True
 (R/'maintenance-db-resources.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
