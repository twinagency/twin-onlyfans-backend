import json,subprocess,datetime,pathlib,sys
P=pathlib.Path('/var/lib/twin-release/parallel-history-20261101-17211')
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
queries={
'history':"select account_id,count(*) fans,sum(pages_completed) pages,sum(messages_added) added,count(*) filter(where status='complete') complete,max(updated_at) latest from of_launch_history_cursor group by account_id order by account_id",
'memory':"select account_id,count(*) jobs,sum(processed_messages) processed,count(*) filter(where processed_messages>0) learned,max(last_completed_at) latest from of_profile_learning group by account_id order by account_id",
'holds':"select account_id,hold_code,hold_until from of_profile_accounts where hold_until>current_timestamp order by account_id",
'reads':"select b.account_id,b.tokens,b.rate_ceiling,b.backoff_until,b.last_kind,(select count(*) from of_provider_read_lease l where l.account_id=b.account_id and l.lease_until>current_timestamp) leases from of_provider_read_budget b order by b.account_id",
'connections':"select account_id,status,last_error from of_provider_connection order by account_id",
'db':"select state,wait_event_type,count(*) from pg_stat_activity group by state,wait_event_type order by state"
}
record={'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
for name,q in queries.items():
 try:
  sql='select coalesce(json_agg(row_to_json(t)),\'[]\'::json) from ('+q+') t'
  out=run(['docker','exec','twin-onlyfans-database-1','sh','-c','psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"','_',sql]);record[name]=json.loads(out)
 except subprocess.CalledProcessError as e:record[name]={'error':e.stderr[-1000:]}
for n in ['twin-onlyfans-worker-1','twin-onlyfans-api-1','twin-onlyfans-profile-learner','twin-onlyfans-history-importer-1','twin-onlyfans-fastt-supervisor-1']:
 try:
  d=json.loads(run(['docker','inspect',n]))[0];record[n]={'running':d['State']['Running'],'status':d['State']['Status'],'restarts':d['RestartCount'],'started':d['State']['StartedAt'],'image':d['Image']}
 except Exception:record[n]={'missing':True}
with (P/'monitor.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
print(json.dumps(record))
