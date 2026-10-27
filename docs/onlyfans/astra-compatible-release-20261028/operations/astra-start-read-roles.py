import pathlib,json,subprocess,hashlib,os,time,datetime,fcntl
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private';B=R/'production-backup'
def run(a,timeout=180,input=None):return subprocess.check_output(a,text=True,input=input,stderr=subprocess.PIPE,timeout=timeout).strip()
def save(p,x):p.write_text(json.dumps(x,indent=2));os.chmod(p,0o600)
lock=(R/'cutover.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert json.loads((B/'migration-proof.json').read_text())['schema']=='0097_release_controls'
assert json.loads((R/'broker-upgrade-proof.json').read_text())['ready']
assert json.loads((R/'session-upgrade-proof.json').read_text())['ready']
assert not(R/'read-roles-started.json').exists()
inventory=json.loads((R/'cutover-inventory.json').read_text())
for c in inventory['configs']:assert hashlib.sha256(pathlib.Path(c['path']).read_bytes()).hexdigest()==c['sha256'],'Deployment config drift'
for x in json.loads((B/'stopped.json').read_text()):assert not json.loads(run(['docker','inspect',x['name']]))[0]['State']['Running']
mapping=[('main.forward','/etc/twin-onlyfans/media-agency-release-compose.json'),('broker.forward','/etc/twin-onlyfans/managed-proxy/compose.json'),('signin.forward','/etc/twin-onlyfans/creator-sign-in-release-compose.json'),('dashboard.forward','/etc/twin-onlyfans/dashboard-release-compose.json')]
for source,target in mapping:
 p=pathlib.Path(target);stage=p.with_suffix(p.suffix+'.astra-new')
 stage.write_bytes((P/(source+'.json')).read_bytes());os.chmod(stage,0o600);os.replace(stage,p)
# The legacy one-off runner remains an explicit ops-only command, never a second default sender.
main=json.loads((P/'main.forward.json').read_text())
assert main['services']['worker']['environment']['ONLYFANS_FASTT_SCOPE']=='db:*'
fenced=json.loads(json.dumps(main));e=fenced['services']['api']['environment']
for key in ['ONLYFANS_CONTROL_TOKEN','ONLYFANS_CONFIGURATION_TOKEN','ONLYFANS_CONTENT_TOKEN','ONLYFANS_CAMPAIGN_TOKEN','ONLYFANS_CONNECTION_TOKEN','TWIN_ADMIN_API_KEY']:e.pop(key,None)
e['ONLYFANS_MANUAL_ENABLED']='0'
save(P/'main.fenced.json',fenced)
brokerProject=json.loads((P/'twin-onlyfans-managed-proxy-1.json').read_text())['Config']['Labels']['com.docker.compose.project']
run(['docker','compose','-p',brokerProject,'-f','/etc/twin-onlyfans/managed-proxy/compose.json','up','-d','--no-deps','--no-build','--pull','never','managed-proxy'],timeout=120)
run(['docker','compose','-p','twin-onlyfans','-f',str(P/'main.fenced.json'),'up','-d','--no-deps','--no-build','--pull','never','api','worker','creator-sign-in'],timeout=120)
run(['docker','rename','next-app','next-app-pre-astra-20261028'])
run(['docker','compose','-p','twin-dashboard-release','-f','/etc/twin-onlyfans/dashboard-release-compose.json','up','-d','--no-deps','--no-build','--pull','never','dashboard'],timeout=120)
# FASTT supervisor remains stopped. The preserved db:* ownership prevents the reader worker from sending.
save(R/'read-roles-started.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'senderStopped':True,'apiCommandsClosed':True,'source':'293b23dbdf8dd5e97f27d6c0bef8ef4710415c06'})
print(json.dumps({'phase':'read-roles-started','senderStopped':True,'apiCommandsClosed':True}))

