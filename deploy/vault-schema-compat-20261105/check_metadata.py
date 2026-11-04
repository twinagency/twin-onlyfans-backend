import subprocess,json,os
from pathlib import Path
directory=Path('/var/lib/twin-release/vault-system-rollout-20261105/legacy-schema')
source=(directory/'check_metadata.py').read_text()
outputs=[]
for image in ['twin-repair:vault-system-main-20261105','twin-repair:vault-system-legacy-schema-20261105']:
    output=subprocess.check_output(['docker','run','--rm','-i','--network','none','--entrypoint','python','-e','PYTHONPATH=/app/service',image,'-'],input=source,text=True)
    result=json.loads(output)
    outputs.append(result)
    print(json.dumps({'image':image,**result}))
assert outputs[0]==outputs[1], 'schema metadata contract mismatch'

# Reuse the already-authorized service connection in memory; never serialize
# its environment or credential values. Only the candidate's schema check runs.
container=json.loads(subprocess.check_output(['docker','inspect','twin-onlyfans-worker-1']))[0]
entries=dict(v.split('=',1) for v in container['Config']['Env'])
environment=os.environ.copy()
environment['DATABASE_URL']=entries['DATABASE_URL']
check='''import os,json
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from db.schema import check_schema,revision,HEAD
engine=create_engine(make_url(os.environ['DATABASE_URL']).set(drivername='postgresql+psycopg2'),connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=15000'})
with engine.connect() as connection:
    assert connection.scalar(text('SHOW transaction_read_only'))=='on'
    check_schema(connection)
    print(json.dumps({'candidateFullSchemaCheck':'passed','revision':revision(connection),'expected':HEAD,'transactionReadOnly':True}))
engine.dispose()
'''
subprocess.run(['docker','run','--rm','-i','--network','container:twin-onlyfans-worker-1','--entrypoint','python','-e','PYTHONPATH=/app/service','-e','DATABASE_URL','twin-repair:vault-system-legacy-schema-20261105','-'],input=check,text=True,env=environment,check=True)
