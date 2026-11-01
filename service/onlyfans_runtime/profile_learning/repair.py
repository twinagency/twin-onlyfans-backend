"""Rebuild only affected fan profiles from existing, valid observations, without model calls."""
import json
from copy import deepcopy
from sqlalchemy import text
from sqlalchemy.orm import Session
from .evidence import quality_fact,merge,project,summary,FIELD_LOCKS
from .storage import valid_observation,SCOPE
from .worker import scoped


def repair_existing(store,tenant,*,limit=500):
    with Session(store.engine) as s:
        rows=s.execute(text('''SELECT o.tenant_id,o.account_id,o.binding_id,o.fan_id,o.document_json
            FROM of_profile_observations o JOIN of_creator_bindings b ON b.id=o.binding_id
            AND b.account_id=o.account_id WHERE o.tenant_id=:t AND b.tenant_id=:t
            AND b.revoked_at IS NULL AND o.fan_id>0 ORDER BY o.account_id,o.fan_id'''),{'t':tenant}).all()
        targets={}
        for t,a,b,f,raw in rows:
            original=json.loads(raw)
            if quality_fact(deepcopy(original))!=original:
                targets[(t,a,b,f)]=dict(tenant_id=t,account_id=a,binding_id=b,fan_id=f)
    repaired=[]
    for key,p in list(targets.items())[:limit]:
        with Session(store.engine) as s,s.begin():
            scoped(store,s,tenant,p['account_id'],p['binding_id'])
            row=s.execute(text(f'SELECT document_json,lease_until FROM of_profile_learning WHERE {SCOPE} FOR UPDATE'),p).first()
            if row is None:continue
            from datetime import datetime
            if row[1] and row[1]>datetime.utcnow():continue
            observations=[json.loads(raw) for raw in s.execute(text(
                f'SELECT document_json FROM of_profile_observations WHERE {SCOPE}'),p).scalars()]
            observations=[f for f in observations if valid_observation(s,p['account_id'],p['binding_id'],f)]
            from onlyfans_runtime.launch_history_memory.memory import locked_fields
            locks=locked_fields(s,p['account_id'],p['fan_id'])
            facts=[f for f in merge([],observations) if not locks & FIELD_LOCKS.get(f['field'],set())]
            # Archived observations and operator corrections are retained unchanged.
            s.execute(text(f'DELETE FROM of_profile_memories WHERE {SCOPE}'),p)
            for f in facts:
                f['evidence_count']=len(f.get('evidence',[]));f['history_count']=len(f.get('history',[]))
                f['evidence']=f.get('evidence',[])[-8:];f['history']=[]
                f['conflicts']=[{k:v for k,v in c.items() if k not in {'history','conflicts'}} for c in f.get('conflicts',[])[:8]]
                f=project(f)
                s.execute(text('''INSERT INTO of_profile_memories(tenant_id,account_id,binding_id,fan_id,
                    memory_key,field,status,kind,document_json) VALUES(:tenant_id,:account_id,:binding_id,:fan_id,
                    :key,:field,:status,:kind,:doc)'''),{**p,'key':f['memory_key'],'field':f['field'],
                    'status':f['status'],'kind':f['kind'],'doc':json.dumps(f,ensure_ascii=False)})
            doc=json.loads(row[0]);doc.update(summary=summary(facts),memory_count=len(facts),quality_revision='20261102')
            s.execute(text(f'UPDATE of_profile_learning SET document_json=:doc WHERE {SCOPE}'),
                {**p,'doc':json.dumps(doc,ensure_ascii=False)})
            repaired.append({'account':p['account_id'],'fan':p['fan_id'],'memories':len(facts)})
    return {'affected_profiles':len(targets),'repaired':repaired}
