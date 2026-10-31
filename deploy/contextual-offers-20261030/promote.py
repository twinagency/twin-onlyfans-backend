"""Pinned, default-off promotion. Run on the established production host only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

ROOT = Path('/var/lib/twin-release/contextual-offers-20261030-astra-6172d27')
COMPOSE = Path('/etc/twin-onlyfans/media-agency-release-compose.json')
REVISION = '0b4e7eaf0afd87846723f134313d5db2eb78727f'
BASE = {
    'api': 'sha256:49cf1882099727ab3a6447f3df082c24343572ac4f800242f3f26362cd54b472',
    'worker': 'sha256:eb49d6dac1aafa4167a11625256ebaecfb06a5a4f7681c8e38b3049317a64579',
    'fastt-supervisor': 'sha256:deff89ed978f4e59236387abab1c4bde85aed0e649e0fe23f1a10b009ae47bf2',
}
NEW = {
    'api': 'sha256:cf7dd2382962def7ca18f8b26a39713e61cab5bf5bb637093ce8bf95068527ff',
    'worker': 'sha256:3257dba4b75a0a0bbf43a9a0b99a17e04a0af11f76b4105a770b9667fd17c0df',
    'fastt-supervisor': 'sha256:42a374a0e29fa08419c2ec4987299d187215a28066528a48d1ac94c79a97e84a',
}

def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def inspect(name):
    return json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]

def sql(query):
    return subprocess.check_output(['docker', 'exec', 'twin-onlyfans-database-1',
        'psql', '-X', '-U', 'twin_onlyfans', '-d', 'twin_onlyfans', '-A', '-t', '-q', '-c', query])

def private(path, value):
    path.write_bytes(value if isinstance(value, bytes) else value.encode())
    path.chmod(0o600)

def controls():
    # Backups remain on the host. No personal rows or credentials are printed.
    queries = {
        'configuration': 'SELECT account_id,ai_chatter_config_json,persona,persona_facts_json,model,model_by_purpose,daily_cost_cap_cents,reasoning_effort FROM account_ai_config ORDER BY account_id',
        'runtime': 'SELECT account_id,binding_id,enabled,configuration_revision FROM of_runtime ORDER BY account_id',
        'takeover': 'SELECT account_id,fan_id,mode,takeover_state,revision FROM of_conversation_state ORDER BY account_id,fan_id',
        'connections': 'SELECT account_id,binding_id,owner_id,status,revision FROM of_provider_connection ORDER BY account_id',
    }
    return {key: sql('SELECT coalesce(jsonb_agg(to_jsonb(x)),\'[]\'::jsonb) FROM ('+query+') x') for key,query in queries.items()}

def modes_off():
    rows = json.loads(sql('SELECT coalesce(jsonb_agg(to_jsonb(x)),\'[]\'::jsonb) FROM (SELECT account_id,ai_chatter_config_json FROM account_ai_config) x'))
    for row in rows:
        raw = json.loads(row['ai_chatter_config_json'] or '{}')
        assert raw.get('fastt_contextual_sales_mode', 'off') == 'off', 'Contextual mode already enabled; inspect drift'

def compose(*args):
    run(['docker', 'compose', '-p', 'twin-onlyfans', '-f', str(COMPOSE), *args])

def migration(action):
    compose('run', '--rm', '--no-deps', '-T', '-e', 'PYTHONPATH=/app/service',
        '--entrypoint', 'python', 'api', '-m', 'db.schema', action, '--url-env', 'DATABASE_URL')

def restore_schema():
    assert sql('SELECT version_num FROM alembic_version').decode().strip() == '0101_contextual_offers'
    modes_off()
    assert int(sql('SELECT count(*) FROM of_contextual_offer')) == 0, 'Reconcile contextual origins before rollback'
    compose('run', '--rm', '--no-deps', '-T', '-e', 'PYTHONPATH=/app/service',
        '--entrypoint', 'python', 'api', '-c',
        "import os; from sqlalchemy import create_engine; from alembic import command; from db.schema import _config; e=create_engine(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql+psycopg2:')); c=e.connect(); t=c.begin(); command.downgrade(_config(c),'0100_manual_free_media'); t.commit(); c.close(); e.dispose()")

def main():
    os.umask(0o077)
    assert hashlib.sha256(COMPOSE.read_bytes()).hexdigest() == 'b0837e09491a16aaa418ff44f461e8e5bc840fbe8c27bb4d29ed0b4e4d8ac510', 'Compose drift'
    assert sql('SELECT version_num FROM alembic_version').decode().strip() == '0100_manual_free_media'
    modes_off()
    for role,image in BASE.items():
        current=inspect('twin-onlyfans-'+role+'-1')
        assert current['Image']==image and current['State']['Running'], role+' baseline drift'
        assert inspect(NEW[role])['Config']['Labels']['org.twin.contextual.source']==REVISION
    # Require the actual grafted sender's tests, not just the historical tree.
    assert '47 passed' in (ROOT/'sender-tests2.log').read_text()
    assert '"paid_cards": 1' in (ROOT/'sender-tests2.log').read_text()
    assert 'all cases passed (17 checks)' in (ROOT/'sender-pg-tests2.log').read_text()
    assert '1 passed' in (ROOT/'sender-pg-tests2.log').read_text()
    baseline=(ROOT/'baseline-service.log').read_text()
    candidate=(ROOT/'sender-regressions.log').read_text()
    assert 'timeout waiting for pace stop outcome' in baseline and 'timeout waiting for pace stop outcome' in candidate
    record=ROOT/'promotion-0b4e7ea'; record.mkdir(mode=0o700,exist_ok=False)
    old=COMPOSE.read_bytes();private(record/'compose.before.json',old)
    dump=subprocess.check_output(['docker','exec','twin-onlyfans-database-1','pg_dump','-U','twin_onlyfans','-d','twin_onlyfans','--schema-only','--no-owner','--no-privileges'])
    private(record/'schema.before.sql',dump)
    configuration=json.loads(old)
    for role,image in NEW.items():configuration['services'][role]['image']=image
    private(record/'compose.after.json',json.dumps(configuration,indent=2))
    stopped=False; advanced=False
    try:
        compose('stop','-t','45','fastt-supervisor','worker','api');stopped=True
        before=controls()
        for key,value in before.items():private(record/(key+'.before.json'),value)
        private(COMPOSE,json.dumps(configuration,indent=2))
        migration('upgrade');advanced=True
        assert controls()==before, 'Migration changed configuration, activation, connection or takeover'
        modes_off()
        compose('up','-d','--no-deps','api','worker','fastt-supervisor')
        ready=False
        for _ in range(45):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=2) as response:
                    assert response.status==200 and json.load(response)['ok'] is True
                if all(inspect('twin-onlyfans-'+role+'-1')['State']['Running'] for role in NEW):ready=True;break
            except Exception:pass
            time.sleep(1)
        assert ready,'Startup health failed'
        migration('check')
        modes_off()
        result={'revision':REVISION,'images':NEW,'schema':'0101_contextual_offers','contextualMode':'off','configurationAndActivationUnchangedByMigration':True,'apiHealth':'passed','rollbackBaseline':str(ROOT/'baseline-0b4e7ea'),'rollbackCompose':str(record/'compose.before.json')}
        private(record/'result.json',json.dumps(result,indent=2));print(json.dumps(result))
    except Exception:
        if stopped:
            compose('stop','-t','45','fastt-supervisor','worker','api')
            if advanced:restore_schema()
            private(COMPOSE,old)
            compose('up','-d','--no-deps','api','worker','fastt-supervisor')
            print('Restored the captured pre-release images and compose after a failed promotion')
        raise

if __name__=='__main__':main()
