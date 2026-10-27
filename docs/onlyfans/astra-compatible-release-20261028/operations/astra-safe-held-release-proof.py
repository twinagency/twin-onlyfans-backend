import pathlib,json,hashlib,datetime
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028')
names=['approved-validation.json','prepared-cutover.json','activation-hold-proof.json','maintenance-db-resources.json','production-session-smoke.json','session-upgrade-proof.json','session-upgrade-sql-proof.json','broker-upgrade-proof.json','broker-drain-proof.json','production-backup/completed.json','production-backup/restore-proof.json','production-backup/migration-proof.json','production-backup/session-proof.json']
out={'collectedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'hostEvidenceRoot':str(R),'files':{},'offhostBackupCopy':'blocked: automatic approval review requires explicit permission for S3 payload and destination'}
for name in names:
 p=R/name
 assert p.is_file(),name
 raw=p.read_bytes();value=json.loads(raw)
 if name=='production-backup/migration-proof.json':
  value['unchangedTableCount']=len(value.pop('unchangedTables'))
 out['files'][name]={'sha256':hashlib.sha256(raw).hexdigest(),'data':value}
p=R/'read-roles-proof.json';raw=p.read_bytes();v=json.loads(raw);hashes=v.pop('sourceFiles')
v['sourceFileCount']=len(hashes);v['sourceManifestSha256']=hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
out['files'][p.name]={'sha256':hashlib.sha256(raw).hexdigest(),'data':v}
print(json.dumps(out,separators=(',',':')))
