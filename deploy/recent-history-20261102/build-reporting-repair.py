"""Build the reporting and importer refresh repair on the deployed immutable pilot images."""
import hashlib,json,os,pathlib,subprocess,sys
os.umask(0o077)
HERE=pathlib.Path(__file__).resolve().parent
P=pathlib.Path('/var/lib/twin-release/recent-history-20261102-resume')
REV=sys.argv[1];assert len(REV)==40 and all(c in '0123456789abcdef' for c in REV)
B=P/('repair-'+REV[:12]);B.mkdir(exist_ok=False)
old=json.loads((P/'deployment-repair.json').read_text())
assert old['sourceCommit']=='91c260ba54451ba51525cd3cc2ab0737bedc1592'
compose=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json').read_bytes()
packet=json.loads((HERE/'reporting-repair.json').read_text())
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
manifest={'revision':REV,'stageOnly':True,'productionChanged':False,'images':{},'composeSha256':hashlib.sha256(compose).hexdigest()}
for role in ['worker','api']:
 base=old['images'][role];name='twin-onlyfans-'+role+'-1'
 assert json.loads(run(['docker','inspect',name]))[0]['Image']==base,'Concurrent release changed'
 root=B/role;root.mkdir()
 for item in packet['files']:
  p=root/item['path'];p.parent.mkdir(parents=True,exist_ok=True)
  run(['docker','cp',name+':/app/'+item['path'],str(p)])
  assert hashlib.sha256(p.read_bytes()).hexdigest()==item['expectedSha256'],'File differs from qualified pilot'
  p.write_text(item['content'])
 (root/'Dockerfile').write_text('FROM '+base+'\nCOPY service/ /app/service/\nLABEL org.opencontainers.image.revision="'+REV+'"\n')
 tag='twin-recent-history:'+role+'-'+REV[:12]
 with (root/'build.log').open('w') as out:subprocess.run(['docker','build','--network','none','-t',tag,str(root)],check=True,stdout=out,stderr=subprocess.STDOUT)
 manifest['images'][role]={'base':base,'image':json.loads(run(['docker','image','inspect',tag]))[0]['Id'],'tag':tag}
root=B/'tests';root.mkdir()
for item in packet['files']+packet['tests']:
 p=root/item['path'];p.parent.mkdir(parents=True,exist_ok=True);p.write_text(item['content'])
(root/'Dockerfile').write_text('FROM sha256:e00888c7d45e70ceb3b03f0ad60af543ab0b1180f9e8061e686f9d9a889406fe\nCOPY service/ /app/service/\nCOPY deploy/ /app/deploy/\n')
tag='twin-recent-history:tests-'+REV[:12]
with (root/'build.log').open('w') as out:subprocess.run(['docker','build','--network','none','-t',tag,str(root)],check=True,stdout=out,stderr=subprocess.STDOUT)
manifest['testImage']=json.loads(run(['docker','image','inspect',tag]))[0]['Id']
(B/'build.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest),flush=True)
subprocess.run([sys.executable,str(HERE/'qualify.py'),str(B)],check=True)
