"""Enable one approved, already-active canary through the existing audited API.

Run on the release host with --account-id. Credentials and detailed evidence stay
on that host. This does not activate accounts, reconnect providers or send tests.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path('/var/lib/twin-release/reply-pacing-20261030-208785')
API = 'twin-onlyfans-api-1'
EXPECTED = {
    API: 'sha256:ca09c9079b72e045795ee35ce4ab921ac4b7398b95dd885488179de66f6c687b',
    'twin-onlyfans-fastt-supervisor-1': 'sha256:2fa1dd0604814eb0a287e67599d4215a648169c3078c1ac0c871069656c01e8c',
    'next-app': 'sha256:60c4edf7947e3af3cbe33454b2df90db508d58572a7a40bf3d5698ec2a33d1fb',
    'next-app-of': 'sha256:60c4edf7947e3af3cbe33454b2df90db508d58572a7a40bf3d5698ec2a33d1fb',
}

def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=45).stdout

def sql(query):
    return run(['docker', 'exec', 'twin-onlyfans-database-1', 'psql', '-X', '-U',
                'twin_onlyfans', '-d', 'twin_onlyfans', '-Atqc', query]).strip()

def private(name, value):
    path = ROOT / name
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)

def api(account, payload=None):
    program = '''import json,os,urllib.request,urllib.error
url='http://127.0.0.1:8787/onlyfans/configuration/v1/accounts/'+ACCOUNT+'?contextual=1'
headers={'Authorization':'Bearer '+os.environ['ONLYFANS_CONFIGURATION_TOKEN'],
         'X-OnlyFans-Configuration-Version':'of-configuration.v3','Content-Type':'application/json'}
request=urllib.request.Request(url,headers=headers,data=None if PAYLOAD is None else json.dumps(PAYLOAD).encode())
try:
 with urllib.request.urlopen(request,timeout=20) as response:
  print(json.dumps({'status':response.status,'body':json.load(response)}))
except urllib.error.HTTPError as error:
 print(json.dumps({'status':error.code,'body':{'ok':False}}))
'''
    raw = run(['docker', 'exec', API, 'python', '-c',
               'ACCOUNT=' + repr(account) + '\nPAYLOAD=' + repr(payload) + '\n' + program])
    result = json.loads(raw)
    assert result['status'] == 200 and result['body'].get('ok') is True, 'Configuration API refused request'
    return result['body']

def control_digest(account):
    queries = {
        'configuration': "SELECT account_id,ai_chatter_config_json::jsonb-'native_reply_pacing_enabled' AS settings,persona,persona_facts_json,model,model_by_purpose,daily_cost_cap_cents,reasoning_effort FROM account_ai_config ORDER BY account_id",
        'runtime': "SELECT account_id,binding_id,enabled,activated_at FROM of_runtime ORDER BY account_id",
        'connections': "SELECT account_id,binding_id,owner_id,status,revision FROM of_provider_connection ORDER BY account_id",
        'limits': "SELECT account_id,enabled,limit_units,throttle_seconds,media_enabled FROM of_delivery_policy ORDER BY account_id",
    }
    return {key: sql("SELECT md5(coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text) FROM (" + query + ') x')
            for key, query in queries.items()}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', required=True)
    args = parser.parse_args()
    account = args.account_id
    assert re.fullmatch(r'of-[0-9]{1,20}', account), 'Invalid canary reference'
    os.umask(0o077)
    lock = open('/etc/twin-onlyfans/paid-media-release.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for name, expected in EXPECTED.items():
        current = json.loads(run(['docker', 'inspect', name]))[0]
        assert current['State']['Running'] and current['Image'] == expected, 'Release image drift'
    assert sql('SELECT version_num FROM alembic_version') == '0101_contextual_offers', 'Schema head drift'
    assert sql("SELECT to_regclass('fastt_runtime.native_reply_pacing') IS NOT NULL") == 't', 'Pacing table missing'
    eligible = """SELECT EXISTS(SELECT 1 FROM of_runtime r
JOIN of_provider_connection c USING(account_id) JOIN of_delivery_policy p USING(account_id)
WHERE r.account_id='%s' AND r.enabled AND c.status='verified'
AND c.binding_id=r.binding_id AND c.expires_at>CURRENT_TIMESTAMP AND c.last_error IS NULL AND p.enabled
AND EXISTS(SELECT 1 FROM of_conversation_state s WHERE s.account_id=r.account_id
AND s.mode='automated' AND s.takeover_state='none'))""" % account
    assert sql(eligible) == 't', 'Canary is not currently eligible'
    request_path = ROOT / 'canary-a-request.private.json'
    if request_path.exists():
        saved = json.loads(request_path.read_text())
        assert saved['account'] == account, 'Existing canary request differs'
        payload = saved['payload']
        before = saved['before']
        controls = saved['controls']
        started = saved['started_at']
    else:
        assert sql("SELECT count(*)=0 FROM account_ai_config WHERE coalesce(ai_chatter_config_json::jsonb->'native_reply_pacing_enabled','false'::jsonb)<>'false'::jsonb OR coalesce(ai_chatter_config_json::jsonb->'native_reply_format_enabled','false'::jsonb)<>'false'::jsonb") == 't', 'Reply switches already changed'
        before = api(account)['configuration']
        assert before['nativeReplyPacingEnabled'] is False and before['nativeReplyFormatEnabled'] is False
        controls = control_digest(account)
        started = datetime.datetime.now(datetime.UTC).isoformat()
        approval = 'codex:208785:2026-10-30:user-approved-deploy:canary-a:' + account
        payload = {
            'patch': {'nativeReplyPacingEnabled': True},
            'expectedRevision': before['configurationRevision'],
            'expectedFingerprint': before['fingerprint'],
            'commandKey': hashlib.sha256((approval + ':enable').encode()).hexdigest(),
            'actor': 'op-session:' + hashlib.sha256(approval.encode()).hexdigest(),
        }
        private(request_path.name, {'account': account, 'payload': payload, 'before': before,
                                   'controls': controls, 'started_at': started})
    ack = api(account, payload)
    private('canary-a-ack.private.json', ack)
    after = api(account)['configuration']
    private('canary-a-readback.private.json', after)
    assert ack.get('applied') is True, 'Configuration was not applied'
    assert after['configurationRevision'] == ack['recordedRevision'] == payload['expectedRevision'] + 1, 'Configuration revision drift'
    assert after['fingerprint'] == ack['recordedFingerprint'], 'Configuration fingerprint drift'
    assert after['nativeReplyPacingEnabled'] is True and after['nativeReplyFormatEnabled'] is False
    assert control_digest(account) == controls, 'Unrelated control changed during canary enablement'
    assert sql("SELECT count(*)=0 FROM account_ai_config WHERE (account_id<>'%s' AND coalesce(ai_chatter_config_json::jsonb->'native_reply_pacing_enabled','false'::jsonb)<>'false'::jsonb) OR coalesce(ai_chatter_config_json::jsonb->'native_reply_format_enabled','false'::jsonb)<>'false'::jsonb" % account) == 't', 'Unexpected reply switch scope'
    assert sql("SELECT count(*)=1 FROM of_configuration_change WHERE account_id='%s' AND command_key='%s'" % (account, payload['commandKey'])) == 't', 'Audited change missing'
    result = {'pacing_canary_enabled': True, 'single_existing_creator': True,
              'formatting_off_everywhere': True, 'audited_readback_verified': True,
              'activation_connection_caps_other_settings_preserved': True,
              'started_at': started, 'customer_tests_sent': False}
    private('canary-a-result.json', result)
    print(json.dumps(result))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'completed': False, 'error_type': type(error).__name__,
                          'gate': str(error) if isinstance(error, AssertionError) else 'Inspect private host evidence'}))
        raise SystemExit(1)
