import pathlib,json,subprocess,os,datetime,fcntl
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private'
def run(a,timeout=150):return subprocess.check_output(a,text=True,stderr=subprocess.PIPE,timeout=timeout).strip()
lock=(R/'cutover.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert (R/'production-backup/migration-proof.json').exists(),'Only for post-migration schema-compatible rollback'
assert json.loads((R/'production-backup/migration-proof.json').read_text())['schema']=='0097_release_controls'
# Drain all writers and connection mutations first; never restart an old sender on the new schema.
for n in ['next-app','twin-onlyfans-api-1','twin-onlyfans-creator-sign-in-1','twin-onlyfans-fastt-supervisor-1','twin-onlyfans-worker-1','twin-onlyfans-managed-proxy-1']:
 run(['docker','update','--restart=no',n]);run(['docker','stop','--time','120',n])
 d=json.loads(run(['docker','inspect',n]))[0]
 assert not d['State']['Running']and not d['State']['OOMKilled']and d['State']['ExitCode']!=137
main=json.loads((P/'main.forward.json').read_text())
s=main['services']['api'];s['image']='sha256:2b84c20fd69de3df26e7ce52079efde0fac8e0e5be2bf49d39fd10b63a18e46f'
for key in ['ONLYFANS_CONTROL_TOKEN','ONLYFANS_CONFIGURATION_TOKEN','ONLYFANS_CONTENT_TOKEN','ONLYFANS_CAMPAIGN_TOKEN','ONLYFANS_CONNECTION_TOKEN','TWIN_ADMIN_API_KEY']:s['environment'].pop(key,None)
s['environment']['ONLYFANS_MANUAL_ENABLED']='0'
path=P/'main.rollback-fenced.json';path.write_text(json.dumps(main,indent=2));os.chmod(path,0o600)
run(['docker','compose','-p','twin-onlyfans','-f',str(path),'up','-d','--no-deps','--no-build','--pull','never','api'])
dash=json.loads((P/'dashboard.forward.json').read_text());dash['services']['dashboard']['image']='sha256:4c0b428548486845372ac853e6fdf3cb114659b231b344da018c3a0793ceca7f'
dash['services']['dashboard']['environment']['GIT_SHA']='871b3e8955bf8af357f6f66d56c4446bf98fee8e'
path=P/'dashboard.rollback-fenced.json';path.write_text(json.dumps(dash,indent=2));os.chmod(path,0o600)
run(['docker','compose','-p','twin-dashboard-release','-f',str(path),'up','-d','--no-deps','--no-build','--pull','never','dashboard'])
report={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'mode':'read-only','backend':'a550199c42a9873c59c5bd74f9c2d7bd1793d392','writersStopped':True,'schemaKept':'0097_release_controls','sessionSQLKept':True,'databaseRestorePerformed':False,'requiresForwardFixBeforeResuming':True}
(R/'rollback-activation-proof.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
