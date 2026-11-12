"""Manual incident-specific recovery. No activation, retry, purchase or hold release."""
import datetime,hashlib,json,os,re,subprocess,sys
from pathlib import Path
import incident_state as state
ROOT=Path('/unconfigured-twin-supervisor')
SUP=ROOT/'supervision-v11'
HERE=Path(__file__).resolve().parent
def read(path):return json.loads(Path(path).read_bytes())
def command(args,**kw):return subprocess.check_output(args,timeout=90,**kw)
def assert_sender_stopped():
 if command(['docker','ps','-q','--filter','name='+CONFIG['senderContainer']]).strip():
  raise RuntimeError('sender must remain stopped')
 if subprocess.run(['systemctl','is-active','--quiet',CONFIG['senderUnit']]).returncode==0:
  raise RuntimeError('sender service must remain stopped')
CONFIG={}
def parse_args(argv):
 """No arguments, or --resume DIR, or (readiness_unavailable only) --evidence-ref REF with any number of --named-send ID."""
 usage='expected no args, --resume evidence-directory, or --evidence-ref REF [--named-send ID ...]'
 resume=ref=None;named=[];args=[a for a in argv[1:] if a not in ('--apply','--dry-run')];i=0
 if argv.count('--apply')>1 or argv.count('--dry-run')>1 or ('--apply' in argv and '--dry-run' in argv):raise RuntimeError(usage)
 while i<len(args):
  flag=args[i]
  if flag not in ('--resume','--evidence-ref','--named-send') or i+1>=len(args):raise RuntimeError(usage)
  value=args[i+1];i+=2
  if flag=='--named-send':named.append(value);continue
  if (flag=='--resume' and resume is not None) or (flag=='--evidence-ref' and ref is not None):raise RuntimeError(usage)
  if flag=='--resume':resume=value
  else:ref=value
 if resume is not None and (ref is not None or named):raise RuntimeError(usage)
 if named and ref is None:raise RuntimeError(usage)
 return resume,ref,named
def attest_readiness(evidence,manifest,ref,named):
 """readiness_unavailable: the operator's evidence reference and the exact sends named are retained and hashed with the evidence."""
 if not state.evidence_reference_ok(ref):
  raise RuntimeError('readiness_unavailable needs an operator evidence reference: --evidence-ref operator:fansapi:<statement>')
 if len(named)!=len(set(named)):raise RuntimeError('named sends are duplicated')
 named=sorted(named)
 if not state.named_sends_ok(named):raise RuntimeError('named sends are malformed')
 sends=read(evidence/'unresolved-sends.json')
 if not state.named_sends_ok(sends) or sends!=named:
  raise RuntimeError('every unresolved send must be named with --named-send, and only unresolved sends: unresolved='+json.dumps(sends))
 (evidence/'operator-attestation.json').write_bytes(state.encoded(
  {'evidence_reference':ref,'named_sends':named,'cause_caveat':state.READINESS_CAVEAT}))
 return named
def main():
 resume,ref,named=parse_args(sys.argv)
 apply='--apply' in sys.argv
 # The release pin is staged only after independent review; never chosen implicitly.
 release=read(SUP/'reviewed-recovery-release.json')
 image=release['image'];commit=release['commit']
 if re.fullmatch('sha256:[a-f0-9]{64}',image) is None or re.fullmatch('[a-f0-9]{40}',commit) is None:
  raise RuntimeError('invalid reviewed recovery pin')
 meta=json.loads(command(['docker','image','inspect',image]))[0]
 if meta['Id']!=image or meta['Config']['Labels'].get('org.opencontainers.image.revision')!=commit:
  raise RuntimeError('recovery image revision mismatch')
 assert_sender_stopped()
 marker=(SUP/'outage-open.json').read_bytes()
 incident_hash=state.digest(marker);spec=state.incident_spec(incident_hash,marker)
 if spec is None:raise RuntimeError('incident changed or not approved')
 if not state.unresolved(SUP/'outage-open.json'):
  print(json.dumps({'result':'replayed','sendingEnabled':False,'incident_sha256':incident_hash}))
  return
 if resume is not None:
  evidence=Path(resume).resolve()
  if evidence.parent!=ROOT/'evidence' or not evidence.name.startswith('monitor-incident-recovery-'):
   raise RuntimeError('invalid saved evidence path')
  manifest=read(evidence/'manifest.json');sources=read(evidence/'source-hashes.json')
  receipt=read(evidence/'receipt.json');evidence_hash=state.digest(state.encoded(sources))
  if receipt.get('evidence_sha256')!=evidence_hash:raise RuntimeError('saved evidence changed')
 else:
  # Every attempt collects a fresh bounded supported interval and preserves old evidence.
  name='monitor-incident-recovery-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
  evidence=ROOT/'evidence'/name
  command([sys.executable,str(HERE/'collect_monitor_evidence.py'),str(evidence)])
  manifest=read(evidence/'manifest.json')
  readiness=manifest.get('classification')==state.READINESS
  if readiness:named=attest_readiness(evidence,manifest,ref,named)
  elif ref is not None or named:raise RuntimeError('--evidence-ref and --named-send are only for readiness_unavailable incidents')
  validator=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
    '--security-opt','no-new-privileges','-e','PYTHONPATH=/app:/app/service:/app/ops/plan-c-supervision',
    '-v',str(evidence)+':/proof:ro','-v',str(HERE/'validate_interval.py')+':/opt/validate.py:ro',
    '-e','TWIN_SUPERVISION_ACCOUNT='+CONFIG.get('account',state.ACCOUNT),
    '-e','TWIN_SUPERVISION_BINDING='+CONFIG.get('binding',state.BINDING),
    '-e','TWIN_SUPERVISION_EPOCH='+str(CONFIG.get('epoch',state.EPOCH)),
    '-e','TWIN_SUPERVISION_CONTEXT='+json.dumps({k:CONFIG[k] for k in ('account','binding','epoch','providerAccount','fans','publicUrl') if k in CONFIG},sort_keys=True),
    '--entrypoint','python',image,'/opt/validate.py']
  findings=json.loads(command(validator))
  if findings.get('interval_valid_for_bounded_scope') is not True:raise RuntimeError('interval proof refused')
  (evidence/'scope-findings.json').write_bytes(state.encoded(findings))
  # Bind every retained source file into one audit digest. The receipt is excluded
  # because it itself contains this digest.
  sources={p.name:state.digest(p.read_bytes()) for p in evidence.iterdir() if p.is_file()}
  evidence_hash=state.digest(state.encoded(sources))
  (evidence/'source-hashes.json').write_bytes(state.encoded(sources))
  actor=read(ROOT/'config/pilot-operator-identity.json')['actor']
  if spec['pinned']:
   receipt={'version':state.V1,'incident_sha256':incident_hash,
    'account_id':state.ACCOUNT,'binding_id':state.BINDING,'connection_epoch':CONFIG.get('epoch',1),
    'scope_revision':manifest['scope_revision'],'classification':spec['classification'],
    'actor':actor,'audit_command_key':state.digest(('monitor-resolution:'+state.APPROVED_INCIDENT+':'+evidence_hash).encode()),
    'evidence_sha256':evidence_hash,'resolved_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
  else:
   # Class/evidence-based resolution: immutable incident identity, affected interval, expected
   # revision and a command identity bound to all of them. Never a send permit or hold release.
   if (manifest.get('resolution_kind')!='class_based' or manifest.get('incident_sha256')!=incident_hash
       or manifest.get('classification') not in state.CLASSES or type(manifest.get('scope_revision')) is not int
       or manifest.get('incident_id')!=spec['incident_id']):raise RuntimeError('class-based evidence does not match this incident')
   interval={'start':spec['interval_start'],'end':manifest['collected_at']}
   receipt={'version':state.V2,'incident_sha256':incident_hash,'incident_id':spec['incident_id'],
    'account_id':state.ACCOUNT,'binding_id':state.BINDING,'connection_epoch':CONFIG.get('epoch',1),
    'scope_revision':manifest['scope_revision'],'classification':manifest['classification'],
    'affected_interval':interval,'delivery_url_sha256':manifest['delivery_url_sha256'],'config_version':manifest['config_version'],
    'actor':actor,'audit_command_key':state.command_key_v2(incident_hash,evidence_hash,interval['start'],interval['end'],manifest['scope_revision']),
    'evidence_sha256':evidence_hash,'resolved_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
   if readiness:
    # v3: the thin v2 form can never carry readiness_unavailable. Reference, named sends and the caveat are part of the identity.
    receipt.update(version=state.V3,evidence_reference=ref,named_sends=named,cause_caveat=state.READINESS_CAVEAT,
     audit_command_key=state.command_key_v3(incident_hash,evidence_hash,interval['start'],interval['end'],manifest['scope_revision'],ref,named))
  (evidence/'receipt.json').write_bytes(state.encoded(receipt))
 # A preview may retain its collected proof, but cannot audit, publish a
 # resolution marker, or modify the budget/incident state.
 for name,h in sources.items():
  if Path(name).name!=name or state.digest((evidence/name).read_bytes())!=h:raise RuntimeError('evidence changed')
 if not state._valid_resolution(receipt,incident_hash,marker):raise RuntimeError('receipt does not match stopped incident')
 if not apply:
  print(json.dumps({'result':'dry_run','evidence':str(evidence),'sendingEnabled':False,'holdsReleased':False,
    'incident_sha256':incident_hash,'receipt':receipt,'nextAction':'Review this evidence, then use --apply --resume with this evidence directory'},sort_keys=True))
  return
 os.chmod(evidence,0o750);os.chown(evidence,0,65532)
 for f in evidence.iterdir():
  os.chmod(f,0o640);os.chown(f,0,65532)
 def audit(proof):
  assert_sender_stopped()
  # Prevent source substitution between collection and DB commit.
  for name,h in sources.items():
   if state.digest((evidence/name).read_bytes())!=h:raise RuntimeError('evidence changed')
  args=['docker','run','--rm','--network','host','--user','65532:65532','--read-only',
   '--cap-drop','ALL','--security-opt','no-new-privileges','-e','PYTHONPATH=/app:/app/service',
   '-e','ONLYFANS_PROCESS_ROLE=worker','-v',str(ROOT/'config/pilot-worker-database-url')+':/run/pilot-database-url:ro',
   '-v',str(ROOT/'config/pilot-operator-identity.json')+':/run/pilot-identity.json:ro',
   '-v',str(evidence)+':/proof:ro','-v',str(HERE/'db_audit.py')+':/opt/audit.py:ro',
   '-e','TWIN_SUPERVISION_ACCOUNT='+CONFIG.get('account',state.ACCOUNT),
   '-e','TWIN_SUPERVISION_TENANT='+CONFIG.get('tenant','fixture'),
   '--entrypoint','python',image,'/opt/audit.py']
  result=json.loads(command(args))
  if result.get('recorded') is not True or result.get('sendingEnabled') is not False or result.get('evidenceSha256')!=evidence_hash:
   raise RuntimeError('database audit not confirmed')
  # Keep the largest established usage. A stopped snapshot cannot permit sends.
  budget=read(SUP/'budget-state.json')
  budget['prior_estimate']=max(budget['prior_estimate'],manifest['credit_estimate'])
  budget['scope_status']='stopped'
  state.atomic(SUP/'budget-state.json',state.encoded(budget))
 caveat=receipt.get('cause_caveat')
 if caveat:print('CAVEAT: '+caveat,file=sys.stderr)   # shown to the operator before and with the outcome
 result=state.resolve(SUP,receipt,audit)
 print(json.dumps({'result':result,'evidence':str(evidence),'sendingEnabled':False,
                   'incident_sha256':incident_hash,**({'cause_caveat':caveat} if caveat else {})},sort_keys=True))
if __name__=='__main__':
 from operation_config import load
 CONFIG=load();ROOT=CONFIG['root'];SUP=ROOT/'supervision-v11'
 state.ACCOUNT=CONFIG['account'];state.BINDING=CONFIG['binding'];state.EPOCH=CONFIG['epoch']
 main()
