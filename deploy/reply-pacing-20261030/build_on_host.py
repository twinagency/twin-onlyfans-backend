import argparse,base64,gzip,json,pathlib,subprocess,hashlib,datetime
parser=argparse.ArgumentParser(description='Build pinned API/sender overlays from a reviewed base64 gzip bundle')
parser.add_argument('bundle',type=pathlib.Path)
BUNDLE=parser.parse_args().bundle.read_text().strip()
ROOT=pathlib.Path('/var/lib/twin-release/reply-pacing-20261030-208785')
BASE={'api':'sha256:cf7dd2382962def7ca18f8b26a39713e61cab5bf5bb637093ce8bf95068527ff','sender':'sha256:42a374a0e29fa08419c2ec4987299d187215a28066528a48d1ac94c79a97e84a'}
NAMES={'api':'twin-onlyfans-api-1','sender':'twin-onlyfans-fastt-supervisor-1'}
def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)
def inspect(name):return json.loads(run(['docker','inspect',name]).stdout)[0]
def blob(raw):return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
def emit(v):print(json.dumps(v),flush=True)
ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
bundle=json.loads(gzip.decompress(base64.b64decode(BUNDLE)))
manifest={'baseline':BASE,'images':{},'hashes':{},'built_at':datetime.datetime.now(datetime.UTC).isoformat()}
for role,files in bundle['roles'].items():
 assert inspect(NAMES[role])['Image']==BASE[role],'Live source changed before build'
 target=ROOT/role;target.mkdir(exist_ok=True)
 patches=[]
 for path,entry in files.items():
  dest=target/path;dest.parent.mkdir(parents=True,exist_ok=True)
  if 'patch' in entry:
   run(['docker','cp',NAMES[role]+':/app/'+path,str(dest)])
   assert blob(dest.read_bytes())==entry['baseline_sha'],'Baseline file drift: '+role+' '+path
   patches.append(entry['patch'])
  else:dest.write_text(entry['content'])
 patch=target/'feature.patch';patch.write_text(''.join(patches))
 run(['git','apply','--check',str(patch)],cwd=target)
 run(['git','apply',str(patch)],cwd=target)
 for path,entry in files.items():assert blob((target/path).read_bytes())==entry['candidate_sha'],path+' candidate mismatch'
 dockerfile=target/'Dockerfile';dockerfile.write_text('FROM '+BASE[role]+'\nCOPY service/ /app/service/\nLABEL org.twin.reply.source="208785-reconciled"\n')
 image='twin-reply-pacing-208785:'+role
 r=run(['docker','build','--network','none','-t',image,str(target)],timeout=240)
 (ROOT/(role+'-build.log')).write_text(r.stdout+r.stderr)
 manifest['images'][role]=inspect(image)['Id'];manifest['hashes'][role]={p:e['candidate_sha'] for p,e in files.items()}
 emit({'built':role,'image':manifest['images'][role]})
 test=ROOT/(role+'-tests');test.mkdir(exist_ok=True)
 for path,content in bundle['tests'].items():
  dest=test/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(content)
 lines=['FROM '+image]
 for package in ['pytest','_pytest','pluggy','iniconfig','packaging','pygments','py.py']:
  path='/usr/local/lib/python3.12/site-packages/'+package
  lines.append('COPY --from=twin-paid-media-repair-20261029:tests '+path+' '+path)
 lines.append('COPY service/ /app/service/')
 (test/'Dockerfile').write_text('\n'.join(lines)+'\n')
 r=run(['docker','build','--network','none','-t',image+'-tests',str(test)],timeout=180)
 (ROOT/(role+'-test-build.log')).write_text(r.stdout+r.stderr)
(ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2))
emit({'prepared':True,'production_changed':False})
