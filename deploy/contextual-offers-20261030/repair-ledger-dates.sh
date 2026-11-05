set -eu
python3 - <<'PY'
import subprocess,json,os,hashlib
from pathlib import Path
r=Path('/var/lib/twin-release/ledger-date-filters-20261030');p=Path('/etc/twin-onlyfans/media-agency-release-compose.json')
d=json.loads(subprocess.check_output(['docker','inspect','twin-onlyfans-worker-1']))[0]
assert d['Image']=='sha256:9adf223bae627176b621399a56ca239716125f4df5c5e41263502a8621a769d2'
for path,expected in [('service/onlyfans_runtime/provider_connector.py','5acb65e4630879c067356a409959a6ab8d22b3b9'),('service/onlyfans_runtime/ledger.py','9688a666f4a3a5bad8980bbf56285519e77939af')]:
 b=subprocess.check_output(['docker','exec','twin-onlyfans-worker-1','cat','/app/'+path])
 assert hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()==expected
log=(r/'test.log').read_text()
assert '149 passed' in log and '11 failed' in log
fixed=(r/'harness-complete.log').read_text()
assert '69 passed' in fixed and 'failed' not in fixed
os.umask(0o077);backup=r/'worker.before.json';assert not backup.exists();backup.write_text(json.dumps(d))
(r/'compose.before.json').write_bytes(p.read_bytes())
(r/'Dockerfile').write_text('FROM '+d['Image']+'\nLABEL org.twin.ledger-date-filters.source="ce1ec7a052a1ae434c67d8fb15fc3124650a4af0"\nCOPY service/onlyfans_runtime/provider_connector.py /app/service/onlyfans_runtime/provider_connector.py\nCOPY service/onlyfans_runtime/ledger.py /app/service/onlyfans_runtime/ledger.py\n')
subprocess.run(['docker','build','-q','-t','twin-ledger-dates-ce1ec7a:worker',str(r)],check=True)
new=json.loads(subprocess.check_output(['docker','image','inspect','twin-ledger-dates-ce1ec7a:worker']))[0]['Id']
subprocess.run(['docker','create','--name','ledger-date-baseline-20261030','--network','none','--entrypoint','true',d['Image']],check=True,stdout=subprocess.DEVNULL)
old=p.read_bytes();data=json.loads(old);data['services']['worker']['image']=new
cmd=['docker','compose','-p','twin-onlyfans','-f',str(p)]
try:
 p.write_text(json.dumps(data,indent=2));p.chmod(0o600)
 subprocess.run(cmd+['up','-d','--no-deps','worker'],check=True)
 live=json.loads(subprocess.check_output(['docker','inspect','twin-onlyfans-worker-1']))[0]
 assert live['Image']==new and live['State']['Running']
 result={'revision':'ce1ec7a052a1ae434c67d8fb15fc3124650a4af0','worker_image':new,'previous_image':d['Image'],'schema':'unchanged/0101_contextual_offers','baseline':str(r),'mode':'unchanged/Maile shadow only'}
 (r/'result.json').write_text(json.dumps(result));print(json.dumps(result))
except Exception:
 p.write_bytes(old);subprocess.run(cmd+['up','-d','--no-deps','worker'],check=True);raise
PY
