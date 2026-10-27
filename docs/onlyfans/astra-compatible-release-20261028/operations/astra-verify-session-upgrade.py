import pathlib,json,subprocess,datetime
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private';B=R/'production-backup'
assert(B/'session-proof.json').exists()
assert json.loads((R/'session-upgrade-sql-proof.json').read_text())['privilegesVerified'] is True
code=r'''
import os,json,hashlib,urllib.request
url=os.environ['SUPABASE_URL'].rstrip('/');assert url=='https://aouaenqayfltoqrmpitg.supabase.co'
headers={'apikey':os.environ['SUPABASE_SERVICE_KEY'],'Authorization':'Bearer '+os.environ['SUPABASE_SERVICE_KEY'],'Range-Unit':'items'}
before=json.load(open('/backup/session-proof.json'));counts={}
for table,key in [('dashboard_owner_accounts','id'),('dashboard_owner_sessions','token_hash')]:
 rows=[];offset=0
 while True:
  req=urllib.request.Request(url+'/rest/v1/'+table+'?select=*&order='+key,headers={**headers,'Range':str(offset)+'-'+str(offset+999)})
  with urllib.request.urlopen(req,timeout=20)as r:chunk=json.load(r)
  rows.extend(chunk);offset+=len(chunk)
  if len(chunk)<1000:break
  assert offset<100000
 digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 assert before['tables'][table]=={'rows':len(rows),'sha256':digest},'Existing session rows changed'
 counts[table]=len(rows)
# Invalid tokens cannot create or revoke any row; exercise visibility of all three upgraded RPCs.
for name in ['dashboard_admin_session_create','dashboard_owner_session_verify','dashboard_owner_session_revoke']:
 req=urllib.request.Request(url+'/rest/v1/rpc/'+name,data=json.dumps({'p_session_hash':'invalid-release-probe'}).encode(),headers={**headers,'Content-Type':'application/json'},method='POST')
 with urllib.request.urlopen(req,timeout=10)as r:assert r.status==200 and json.load(r)is False
print(json.dumps({'ready':True,'existingRowsUnchanged':True,'counts':counts,'rpcVisibilityVerified':True}))
'''
args=['docker','run','--rm','--network','host','--env-file',str(P/'session-backup.env'),'--mount','type=bind,source='+str(B)+',target=/backup,readonly','--entrypoint','python','sha256:b6a39c70a5b7ac10d898861db608bba1cc3cfa6cfcb77ca9ff56d409c7886c9e','-c',code]
result=json.loads(subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=150))
result['at']=datetime.datetime.now(datetime.timezone.utc).isoformat();result['project']='aouaenqayfltoqrmpitg'
(R/'session-upgrade-proof.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
