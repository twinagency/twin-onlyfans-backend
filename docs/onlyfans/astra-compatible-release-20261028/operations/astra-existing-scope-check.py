import subprocess,json,pathlib,datetime
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028')
code=r'''
import os,json
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'],connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=10000'})
queries={
'enabledConversations':"""SELECT c.account_id,count(*) AS count FROM of_conversation_state c JOIN of_runtime r ON r.account_id=c.account_id WHERE r.enabled AND c.mode='automated' AND c.takeover_state='none' GROUP BY c.account_id""",
'unlinkedInEnabledConversations':"""SELECT t.account_id,count(*) AS count FROM transactions t JOIN of_runtime r ON r.account_id=t.account_id JOIN of_creator_bindings b ON b.id=r.binding_id JOIN of_conversation_state c ON c.account_id=t.account_id AND c.fan_id=t.fan_id WHERE r.enabled AND c.mode='automated' AND c.takeover_state='none' AND t.source='ledger' AND t.kind='ppv_message' AND t.message_id IS NULL AND t.status IN ('pending','cleared','refund_pending') AND t.occurred_at>=b.created_at GROUP BY t.account_id""",
'pendingHumanSends':"""SELECT status,count(*) AS count FROM of_text_intent WHERE status IN ('queued','dispatching') GROUP BY status""",
'uncertainStates':"""SELECT mode,takeover_state,requires_attention,count(*) AS count FROM of_conversation_state WHERE requires_attention GROUP BY mode,takeover_state,requires_attention"""
}
with e.connect()as c:
 print(json.dumps({k:[dict(x) for x in c.execute(text(q)).mappings()]for k,q in queries.items()},default=str))
'''
p=subprocess.run(['docker','exec','twin-onlyfans-worker-1','python','-c',code],text=True,capture_output=True,timeout=45)
assert p.returncode==0,p.stderr
v=json.loads(p.stdout);v['at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
(R/'existing-automation-scope-proof.json').write_text(json.dumps(v,indent=2))
print(json.dumps(v))
