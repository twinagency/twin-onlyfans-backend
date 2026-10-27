import pathlib,json,hashlib,subprocess,datetime,urllib.request
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028')
assert not (R/'activation-proof.json').exists()
roles=[]
for name in ['twin-onlyfans-api-1','twin-onlyfans-worker-1','twin-onlyfans-managed-proxy-1','twin-onlyfans-creator-sign-in-1','next-app','twin-onlyfans-fastt-supervisor-1']:
 d=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
 is_sender=name.endswith('fastt-supervisor-1')
 assert bool(d['State']['Running']) != is_sender
 assert not d['State']['OOMKilled']
 if name=='twin-onlyfans-api-1':
  env=dict(v.split('=',1)for v in d['Config']['Env']if'='in v)
  assert env.get('ONLYFANS_MANUAL_ENABLED')=='0' and not env.get('ONLYFANS_CONTROL_TOKEN') and not env.get('ONLYFANS_CONFIGURATION_TOKEN')
 roles.append({'name':name,'image':d['Image'],'running':d['State']['Running'],'restartCount':d['RestartCount'],'oom':False})
with urllib.request.urlopen('https://dashboard.tgaiclone.xyz/login',timeout=20)as response:assert response.status==200
proof={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'READ_SERVICES_DEPLOYED_SENDER_ACTIVATION_HELD','backend':'293b23dbdf8dd5e97f27d6c0bef8ef4710415c06','dashboard':'4599999761dbf2ff2b517359642d10262324867a','schema':'0097_release_controls','publicLoginStatus':200,'roles':roles,'senderActivated':False,'apiCommandsClosed':True,'accountSettingsChanged':False,'manualMessagesSent':False,'reasons':['Provider ledger coverage has not completed; account of-176774528 reported provider_rate_limited and retains its backoff.','Four unlinked purchases occurred after the current binding for enabled account of-323358988; their provider observations contain no message ID.','Historical unlinked purchases are informational, not grounds to clear or create holds. Disabled account of-87030857 remains disabled.'],'nextActions':['Allow the existing bounded reader to reconcile without bypassing provider backoff.','Obtain exact provider evidence for the four current-binding purchases and use the existing audited ledger resolution; do not infer links from amount/time alone.','Refresh financial readiness, then run the prepared activation and post-deploy scripts only if the required gates pass.','Controlled paid-provider/model journey and scale validation remain unproved.'],'offhostBackupCopy':'Not completed. Automatic approval review requires explicit permission for encrypted backup payload and existing S3 destination.'}
(R/'activation-hold-proof.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
