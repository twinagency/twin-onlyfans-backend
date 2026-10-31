set -eu
python3 - <<'PY'
from pathlib import Path
import subprocess,json,os,time,urllib.request
os.umask(0o077)
r=Path('/var/lib/twin-release/settings-compat-20261030')
p=Path('/etc/twin-onlyfans/media-agency-release-compose.json')
def inspect(n):return json.loads(subprocess.check_output(['docker','inspect',n]))[0]
expected={'twin-onlyfans-api-1':'sha256:ca09c9079b72e045795ee35ce4ab921ac4b7398b95dd885488179de66f6c687b','twin-onlyfans-fastt-supervisor-1':'sha256:2fa1dd0604814eb0a287e67599d4215a648169c3078c1ac0c871069656c01e8c','twin-onlyfans-worker-1':'sha256:ce1bcd5eeade5381890e633c3127cd0787b48553f43e330a6925e02ab84545e5'}
for name,want in expected.items():assert inspect(name)['Image']==want,'Concurrent source change: '+name
for name in ('next-app','next-app-of'):
 d=inspect(name);assert dict(e.split('=',1) for e in d['Config']['Env'])['GIT_SHA']=='8cde4384ab5f8af5e45a838d1d8c81ff41d9a245','Frontend not ready'
assert '6 passed' in (r/'candidate.log').read_text()
new=(r/'candidate-image.txt').read_text()
assert new=='sha256:17ff96b2f9e352f5daad108b66caebd932af0c1092a2f8c41eec7a52054e26ca'
state=inspect('twin-onlyfans-api-1')
(r/'api.promotion-before.json').write_text(json.dumps(state))
(r/'compose.promotion-before.json').write_bytes(p.read_bytes())
subprocess.run(['docker','create','--name','settings-compat-api-baseline-20261030','--network','none','--entrypoint','true',state['Image']],check=True,stdout=subprocess.DEVNULL)
cfg=json.loads(p.read_text());cfg['services']['api']['image']=new
cmd=['docker','compose','-p','twin-onlyfans','-f',str(p)]
try:
 p.write_text(json.dumps(cfg,indent=2));p.chmod(0o600)
 subprocess.run(cmd+['up','-d','--no-deps','api'],check=True)
 ok=False
 for _ in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=2) as resp:assert json.load(resp)['ok']
   ok=True;break
  except Exception:time.sleep(1)
 assert ok,'API health failed'
 live=inspect('twin-onlyfans-api-1');assert live['Image']==new and live['State']['Running']
 for name in ('twin-onlyfans-fastt-supervisor-1','twin-onlyfans-worker-1'):assert inspect(name)['Image']==expected[name]
 result={'source':'5727cb6ca07b3811782e881285bebfd9268b8454','api_image':new,'previous_api_image':state['Image'],'health':'passed','sender_unchanged':expected['twin-onlyfans-fastt-supervisor-1'],'worker_unchanged':expected['twin-onlyfans-worker-1']}
 (r/'api.result.json').write_text(json.dumps(result));print(json.dumps(result))
except Exception:
 current=json.loads(p.read_text());assert current['services']['api']['image']==new
 current['services']['api']['image']=state['Image'];p.write_text(json.dumps(current,indent=2))
 subprocess.run(cmd+['up','-d','--no-deps','api'],check=True)
 raise
PY
docker exec -i twin-onlyfans-api-1 python - <<'PY'
import os,json,urllib.request
base='http://127.0.0.1:8787/onlyfans/configuration/v1/accounts/of-323358988'
headers={'Authorization':'Bearer '+os.environ['ONLYFANS_CONFIGURATION_TOKEN'],'X-OnlyFans-Configuration-Version':'of-configuration.v3'}
for suffix,wanted in [('',set()),('?contextual=1',{'fasttContextualSalesMode'}),('?replyPolicy=1',{'nativeReplyPacingEnabled','nativeReplyFormatEnabled'}),('?activation=1&contextual=1&replyPolicy=1',{'fasttContextualSalesMode','nativeReplyPacingEnabled','nativeReplyFormatEnabled'})]:
 with urllib.request.urlopen(urllib.request.Request(base+suffix,headers=headers)) as resp:data=json.load(resp)['configuration']
 fields={k for k in ('fasttContextualSalesMode','nativeReplyPacingEnabled','nativeReplyFormatEnabled') if k in data}
 assert fields==wanted,(suffix,fields)
 assert data['onRequestSalesEnabled'] is True
 print(json.dumps({'query':suffix,'fields':sorted(fields),'revision':data['configurationRevision'],'contextual':data.get('fasttContextualSalesMode'),'pacing':data.get('nativeReplyPacingEnabled'),'format':data.get('nativeReplyFormatEnabled')}))
PY