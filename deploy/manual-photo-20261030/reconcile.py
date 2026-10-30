import json,pathlib,os
root=pathlib.Path('/var/lib/twin-release/manual-photo-20261030-164b0d6')
manifest=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
old=json.loads((root/'compose.before.json').read_text());new=json.loads(manifest.read_text())
old['services']['fastt-supervisor']['image']='sha256:6839345480127fdb28e4a958a0ffeb80e19dcc0ff6a1925b9780787912968645'
assert old==new,'Unreviewed manifest change'
p=root/'compose.reconciled.json'
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:f.write(manifest.read_text())
print('Reconciled only the already-running sender image',flush=True)
