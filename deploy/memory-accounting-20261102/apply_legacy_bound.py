import pathlib,subprocess,json,fcntl,os,time,hashlib
os.umask(0o077)
P=pathlib.Path('/var/lib/twin-release/memory-accounting-20261102/legacy-bound')
locks=[]
for p in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(p,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
tests=(P/'tests.log').read_text();assert '15 passed' in tests and 'failed' not in tests and 'ERROR' not in tests,'Test gate incomplete/failing'
source=(P/'bound_legacy_reservations.py').read_text()
def query(q):
 sql="select coalesce(json_agg(row_to_json(t)),'[]'::json) from ("+q+") t"
 r=subprocess.run(['docker','exec','twin-onlyfans-database-1','sh','-c','PGOPTIONS="-c statement_timeout=30000 -c default_transaction_read_only=on" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"','_',sql],capture_output=True,text=True,check=True)
 return json.loads(r.stdout)
accounts="('of-24181859','of-312988353','of-323358988','of-63665785','of-87030857','of-99421145')"
snapshot={'at':time.time(),'source_sha256':hashlib.sha256(source.encode()).hexdigest(),
 'calls':query("select id,account_id,status,cost_cents,response_text from grok_calls where purpose='gen_info' and status='unknown' and response_text is null and account_id in "+accounts+" and called_at>='2026-11-01' and called_at<'2026-11-02'"),
 'ledger':query("select * from grok_daily_cost where day='2026-11-01' and provider='openrouter' and account_id in "+accounts),
 'receipts':query("select account_id,count(*) receipts from of_profile_receipts where fan_id>0 and account_id in "+accounts+" group by account_id")}
assert len(snapshot['calls'])==1874 and sum(r['cost_cents'] for r in snapshot['calls'])==297141,'Snapshot changed'
(P/'before.json').write_text(json.dumps(snapshot,default=str))
r=subprocess.run(['docker','exec','-i','twin-onlyfans-profile-learner','python','-','--apply'],input=source,capture_output=True,text=True)
if r.returncode:raise RuntimeError(r.stderr[-4000:])
result=json.loads(r.stdout);result['at']=time.time();result['source_sha256']=snapshot['source_sha256']
(P/'applied.json').write_text(json.dumps(result));print(json.dumps(result),flush=True)
