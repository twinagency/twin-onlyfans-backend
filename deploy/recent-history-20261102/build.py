"""Build reproducible task-only overlays on pinned live bases. No live mutation."""
import hashlib,json,os,pathlib,py_compile,shutil,subprocess,sys
os.umask(0o077)
HERE=pathlib.Path(__file__).resolve().parent
P=pathlib.Path(sys.argv[1]);P.mkdir(parents=True,exist_ok=False)
REV=sys.argv[2]
assert len(REV)==40 and all(c in '0123456789abcdef' for c in REV)
BASE={'worker':'sha256:f728669d70d9fcb89e34ced38e7efe808311293d0b175422b004a46114284fe5',
      'api':'sha256:04532a2d90349893407c08c77a883a6cc62c9ed8386d4eb29b21ab3fc9ef9975'}
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
packets=[json.loads((HERE/f).read_text()) for f in ['initial-overlay.json','cohort-overlay.json','compat-overlay.json']]
def patches(root,packet):
 for e in packet['patches']:
  p=root/e['path'];s=p.read_text().replace('\r\n','\n');edits=[]
  for h in e['hunks']:
   assert s.count(h['context'])==1,e['path']+' context drift'
   start=s.index(h['context'])+len(h['prefix']);assert s[start:start+len(h['old'])]==h['old']
   edits.append((start,start+len(h['old']),h['new']))
  for a,b,v in sorted(edits,reverse=True):s=s[:a]+v+s[b:]
  p.write_text(s)
manifest={'revision':REV,'stageOnly':True,'productionChanged':False,'images':{}}
for role,base in BASE.items():
 name='twin-onlyfans-'+role+'-1'
 assert json.loads(run(['docker','inspect',name]))[0]['Image']==base,'Live base changed'
 root=P/role;root.mkdir();run(['docker','cp',name+':/app/service',str(root/'service')])
 for index,packet in enumerate(packets):
  patches(root,packet)
  for e in packet['files']:
   if '/tests/' in e['path']:continue
   p=root/e['path']
   if p.exists() and index==0:
    expected=e.get('expected_base_sha256');assert expected and hashlib.sha256(p.read_text().replace('\r\n','\n').encode()).hexdigest()==expected,e['path']+' base drift'
   elif p.exists() and index==1:raise RuntimeError('Cohort file unexpectedly exists')
   elif p.exists() and not e.get('taskOwned'):raise RuntimeError('Unreviewed replacement')
   p.parent.mkdir(parents=True,exist_ok=True);p.write_text(e['content'])
 for path in (root/'service').rglob('*.py'):py_compile.compile(str(path),doraise=True)
 tag='twin-recent-history:'+role+'-'+REV[:12]
 (root/'Dockerfile').write_text('FROM '+base+'\nCOPY service/ /app/service/\nLABEL org.opencontainers.image.revision="'+REV+'"\n')
 with (root/'build.log').open('w') as out:subprocess.run(['docker','build','--network','none','-t',tag,str(root)],check=True,stdout=out,stderr=subprocess.STDOUT)
 manifest['images'][role]={'base':base,'image':json.loads(run(['docker','image','inspect',tag]))[0]['Id'],'tag':tag}
# Synthetic qualification image: no production environment, credentials, volumes or network.
test=P/'tests';test.mkdir();shutil.copytree(P/'worker/service',test/'service')
for packet in packets:
 for e in packet['files']+packet.get('testFiles',[]):
  if '/tests/' not in e['path'] and e['path']!='deploy/profile-learning/schema.sql':continue
  p=test/e['path'];p.parent.mkdir(parents=True,exist_ok=True);p.write_text(e['content'])
(test/'Dockerfile').write_text('FROM sha256:458659b6d160219cce93b45c227a63e802f38e2c5e4c832eb57e087710537bd3\nCOPY service/ /app/service/\nCOPY deploy/ /app/deploy/\nWORKDIR /app/service\n')
tag='twin-recent-history:tests-'+REV[:12]
with (test/'build.log').open('w') as out:subprocess.run(['docker','build','--network','none','-t',tag,str(test)],check=True,stdout=out,stderr=subprocess.STDOUT)
manifest['testImage']=json.loads(run(['docker','image','inspect',tag]))[0]['Id']
(P/'build.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest),flush=True)
