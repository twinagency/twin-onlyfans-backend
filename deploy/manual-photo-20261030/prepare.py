import hashlib, json, pathlib, subprocess, os, ast, difflib

ROOT = pathlib.Path('/var/lib/twin-release/manual-photo-20261030-164b0d6')
ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
PAIRS = [["\n    def _eligibility(self, s, runtime, policy, state, fan, origin, now, *, own_attempt_id=None):\n        if policy is None or not policy.enabled or policy.transport_kind not in {\"synthetic\", \"native\"}:\n","\n    @staticmethod\n    def _free_manual_attachment(row):\n        \"\"\"Derive the narrow archive exception from the saved immutable intent.\"\"\"\n        if row.origin != \"manual\" or row.price_cents != 0 or row.catalog_item_id is None:\n            return False\n        try:\n            snapshot = json.loads(row.catalog_snapshot_json)\n            return (snapshot.get(\"manual_vault\") is True\n                    and snapshot.get(\"price_cents\") == 0\n                    and bool(snapshot.get(\"media_ids\")))\n        except (ValueError, TypeError, AttributeError):\n            return False\n\n    def _eligibility(self, s, runtime, policy, state, fan, origin, now, *, own_attempt_id=None,\n                     free_manual_attachment=False):\n        if policy is None or not policy.enabled or policy.transport_kind not in {\"synthetic\", \"native\"}:\n"],["            return \"account_disabled\"\n        if (runtime.recovery_needed or runtime.pending_events or runtime.last_sync_at is None\n                or runtime.last_sync_at < now - timedelta(seconds=MAX_SYNC_AGE_SECONDS)\n","            return \"account_disabled\"\n        # A chosen free attachment is authored by the operator, not generated\n        # from imported messages. Archive state cannot grant or revoke it.\n        # Paid offers and every automated origin retain their history gates.\n        needs_history = not (origin == \"manual\" and free_manual_attachment)\n        if needs_history and (runtime.recovery_needed or runtime.pending_events or runtime.last_sync_at is None\n                or runtime.last_sync_at < now - timedelta(seconds=MAX_SYNC_AGE_SECONDS)\n"],["                return \"history_not_current\"\n        if s.scalar(select(OfIngress.event_key).where(\n                OfIngress.account_id == runtime.account_id, OfIngress.status == \"quarantined\").limit(1)) is not None:\n","                return \"history_not_current\"\n        if needs_history and s.scalar(select(OfIngress.event_key).where(\n                OfIngress.account_id == runtime.account_id, OfIngress.status == \"quarantined\").limit(1)) is not None:\n"],["            fan = s.get(Fan, (account_id, fan_id))\n            reason = self._eligibility(s, runtime, policy, state, fan, origin, now)\n            if reason:\n","            fan = s.get(Fan, (account_id, fan_id))\n            reason = self._eligibility(s, runtime, policy, state, fan, origin, now,\n                free_manual_attachment=manual_media is not None and manual_media[\"priceCents\"] == 0)\n            if reason:\n"],["                obsolete = not self._engagement().current(s, intent, runtime, now)\n            reason = \"snapshot_changed\" if obsolete else self._eligibility(s, runtime, policy, state, fan, intent.origin, now)\n            if reason:\n","                obsolete = not self._engagement().current(s, intent, runtime, now)\n            reason = \"snapshot_changed\" if obsolete else self._eligibility(s, runtime, policy, state, fan, intent.origin, now,\n                free_manual_attachment=self._free_manual_attachment(intent))\n            if reason:\n"],["            reason = \"snapshot_changed\" if obsolete else self._eligibility(\n                s, runtime, policy, state, fan, row.origin, now, own_attempt_id=row.attempt_id)\n            if reason is None and (policy.active_intent_id != row.intent_id or not row.reservation_held\n","            reason = \"snapshot_changed\" if obsolete else self._eligibility(\n                s, runtime, policy, state, fan, row.origin, now, own_attempt_id=row.attempt_id,\n                free_manual_attachment=self._free_manual_attachment(row))\n            if reason is None and (policy.active_intent_id != row.intent_id or not row.reservation_held\n"]]
EXPECTED = {
 'api': ('twin-onlyfans-api-1', 'sha256:17ff96b2f9e352f5daad108b66caebd932af0c1092a2f8c41eec7a52054e26ca', '11aedf1bdf1fba0106126b2777121cee9445d60b8dcd8e6d5ff53a1f0778626e'),
 'fastt-supervisor': ('twin-onlyfans-fastt-supervisor-1', 'sha256:6839345480127fdb28e4a958a0ffeb80e19dcc0ff6a1925b9780787912968645', '8c9d513f04434c4978778f6a35ad6e5010d4407f85a74253c537ab3745c8d4de'),
 'worker': ('twin-onlyfans-worker-1', 'sha256:ce1bcd5eeade5381890e633c3127cd0787b48553f43e330a6925e02ab84545e5', '8c9d513f04434c4978778f6a35ad6e5010d4407f85a74253c537ab3745c8d4de'),
}

def run(args, **kw):
    return subprocess.check_output(args, text=True, **kw)

def inspect(name):
    return json.loads(run(['docker', 'inspect', name]))[0]

def protected(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(value)

SMOKE = '''import json
from datetime import datetime, timezone
from types import SimpleNamespace as N
from onlyfans_runtime.text_delivery import TextAuthority
class Read:
    def __init__(self): self.calls = 0
    def scalar(self, _): self.calls += 1; return 'synthetic-quarantine'
a = TextAuthority.__new__(TextAuthority)
a._retained_hold = lambda *args: False
now = datetime.now(timezone.utc).replace(tzinfo=None)
r = N(enabled=True,recovery_needed=True,pending_events=1,last_sync_at=None,account_id='synthetic')
p = N(enabled=True,transport_kind='synthetic')
s = N(mode='human_controlled',takeover_state='effective',unresolved_attempt_id=None)
f = N(is_muted=False,fan_id=101)
q = Read()
assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True) is None
assert q.calls == 0
r.recovery_needed=False;r.pending_events=0;r.last_sync_at=now
for origin in ['manual','automated','campaign','engagement']:
    assert a._eligibility(q,r,p,s,f,origin,now) == 'history_quarantined'
for origin in ['automated','campaign','engagement']:
    assert a._eligibility(q,r,p,s,f,origin,now,free_manual_attachment=True) == 'history_quarantined'
checks=8
for mode in ['paused','no_contact','automated']:
    s.mode=mode
    assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True) == ('contact_blocked' if mode in ['paused','no_contact'] else 'human_control_required')
    checks+=1
s.mode='human_controlled';s.takeover_state='requested'
assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True)=='takeover_pending';checks+=1
s.takeover_state='effective';s.unresolved_attempt_id='synthetic-unknown'
assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True)=='unresolved_delivery';checks+=1
s.unresolved_attempt_id=None;f.is_muted=True
assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True)=='contact_blocked';checks+=1
f.is_muted=False;p.enabled=False
assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True)=='delivery_disabled';checks+=1
p.enabled=True;r.enabled=False
assert a._eligibility(q,r,p,s,f,'manual',now,free_manual_attachment=True)=='account_disabled';checks+=1
for origin,price,snapshot,expected in [('manual',0,'{"manual_vault":true,"price_cents":0,"media_ids":[11]}',True),('manual',500,'{"manual_vault":true,"price_cents":0,"media_ids":[11]}',False),('automated',0,'{"manual_vault":true,"price_cents":0,"media_ids":[11]}',False),('manual',0,'invalid',False),('manual',0,'[]',False)]:
    assert a._free_manual_attachment(N(origin=origin,price_cents=price,catalog_item_id=1,catalog_snapshot_json=snapshot)) is expected
    checks+=1
print(json.dumps({'offline_guard_checks':checks,'passed':True}))
'''
protected(ROOT/'smoke.py', SMOKE)
manifest = pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
protected(ROOT/'compose.before.json', manifest.read_text())
effective=json.loads(run(['docker','compose','-p','twin-onlyfans','-f',str(manifest),'config','--format','json']))
summary={}
for service,(name,image,source_hash) in EXPECTED.items():
    before=inspect(name)
    assert before['Image']==image, 'Running image changed: '+service
    protected(ROOT/(service+'.before.json'),json.dumps(before))
    d=ROOT/service; d.mkdir(mode=0o700)
    source=d/'text_delivery.before.py'
    subprocess.run(['docker','cp',name+':/app/service/onlyfans_runtime/text_delivery.py',str(source)],check=True,capture_output=True)
    old=source.read_text(); assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash, 'Source changed: '+service
    new=old
    for left,right in PAIRS:
        assert new.count(left)==1, 'Unmatched delta: '+service
        new=new.replace(left,right,1)
    ast.parse(new)
    assert old.count('manual=True')==new.count('manual=True'), 'Paid-media preservation failed'
    (d/'text_delivery.py').write_text(new)
    (d/'change.diff').write_text(''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True))))
    dockerfile='FROM '+image+'\nCOPY text_delivery.py /app/service/onlyfans_runtime/text_delivery.py\nLABEL org.twin.manual-photo.source="164b0d6ca5dabf5abcb3da7e994fa52df9e932c5"\nRUN python -m py_compile /app/service/onlyfans_runtime/text_delivery.py\n'
    (d/'Dockerfile').write_text(dockerfile)
    tag='twin-onlyfans:manual-photo-164b0d6-'+service
    with (d/'build.log').open('w') as log:
        subprocess.run(['docker','build','--network','none','-t',tag,str(d)],check=True,stdout=log,stderr=subprocess.STDOUT)
    candidate=json.loads(run(['docker','image','inspect',tag]))[0]['Id']
    smoke=run(['docker','run','--rm','--network','none','--entrypoint','python','-e','PYTHONPATH=/app/service','-e','PYTHON_DOTENV_DISABLED=1','--mount','type=bind,src='+str(ROOT/'smoke.py')+',dst=/tmp/manual-photo-smoke.py,readonly',candidate,'/tmp/manual-photo-smoke.py'])
    (d/'smoke.json').write_text(smoke)
    im=json.loads(run(['docker','image','inspect',image]))[0]
    desired=effective['services'][service]
    env=dict(x.split('=',1) for x in (im['Config'].get('Env') or []));env.update({k:str(v) for k,v in desired.get('environment',{}).items()})
    current=dict(x.split('=',1) for x in (before['Config'].get('Env') or []))
    assert current==env, 'Effective environment drift: '+service
    summary[service]={'container':name,'before_id':before['Id'],'before_image':image,'candidate_image':candidate,'before_source':source_hash,'after_source':hashlib.sha256(new.encode()).hexdigest(),'command_drift':desired.get('command',im['Config'].get('Cmd'))!=before['Config'].get('Cmd'),'smoke':json.loads(smoke)}
    print(json.dumps({'prepared':service,**summary[service]}),flush=True)
protected(ROOT/'prepared.json',json.dumps(summary,indent=2))
print('PREPARED_NO_CUTOVER',flush=True)
