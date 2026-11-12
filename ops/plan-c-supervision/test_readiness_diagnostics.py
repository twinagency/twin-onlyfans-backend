"""The host retains a bounded, sanitized readiness failure before stopping."""
import json
from unittest.mock import patch
import pytest
from test_supervisor_timing import m,GuardFailure

def test_http_code_and_phase_are_retained_without_response_prose(tmp_path):
 data={'code':'loop_stale','mode':'active','failures':0,'consecutiveFailures':0,
       'inFlight':{'phase':'model','ageSeconds':51,'deadlineInSeconds':0},
       'error':'secret response prose','account':'private account'}
 with patch.object(m,'ROOT',tmp_path),patch.object(m,'status_request',return_value=(503,json.dumps(data).encode())),patch.object(m,'atomic') as record:
  with pytest.raises(GuardFailure,match='readiness_unavailable'):m.status_json('http://fixture/readyz')
  saved=record.call_args.args[1]
  assert saved['http_status']==503 and saved['code']=='loop_stale' and saved['inFlight']['phase']=='model'
  assert 'secret' not in json.dumps(saved) and 'private account' not in json.dumps(saved)

def test_dead_process_is_retained_as_no_http_response(tmp_path):
 with patch.object(m,'ROOT',tmp_path),patch.object(m,'status_request',side_effect=GuardFailure('network_check_failed')),patch.object(m,'atomic') as record:
  with pytest.raises(GuardFailure,match='network_check_failed'):m.status_json('http://fixture/readyz')
  assert record.call_args.args[1]['http_status'] is None

def test_sender_log_survives_container_removal_as_private_bounded_file(tmp_path):
 with patch.object(m,'ROOT',tmp_path),patch.object(m,'CONFIG',{'senderContainer':'fixture-sender'}),patch.object(m.subprocess,'check_output',return_value=b'x'*300000) as logs:
  assert m.retain_sender_logs()
  [saved]=list((tmp_path/'evidence').glob('sender-before-stop-*.log'))
  assert saved.stat().st_mode&0o777==0o600 and saved.stat().st_size==262144
  assert logs.call_args.args[0]==['docker','logs','--timestamps','--tail','200','fixture-sender']

def test_missing_container_log_never_masks_original_stop(tmp_path):
 with patch.object(m,'ROOT',tmp_path),patch.object(m,'CONFIG',{'senderContainer':'fixture-sender'}),patch.object(m.subprocess,'check_output',side_effect=RuntimeError('missing container')):
  assert m.retain_sender_logs() is False

def test_heartbeat_from_another_account_or_epoch_cannot_authorize_sender(tmp_path):
 import gated_worker as gate
 state={'scope_status':'active','scope_revision':7,'boot_id':'fixture','last_ok_monotonic':100,
        'account_id':'creator-a','binding_id':'binding-a','connection_epoch':2}
 heartbeat=tmp_path/'heartbeat';heartbeat.write_text(json.dumps(state))
 boot=tmp_path/'boot';boot.write_text('fixture')
 with patch.object(gate,'GATE',heartbeat),patch.object(gate,'BOOT',boot),patch.object(gate,'unresolved',return_value=False),patch.object(gate.time,'monotonic',return_value=101):
  assert gate.permitted(7,account='creator-a',binding='binding-a',epoch=2)
  assert not gate.permitted(7,account='creator-b',binding='binding-a',epoch=2)
  assert not gate.permitted(7,account='creator-a',binding='binding-b',epoch=2)
  assert not gate.permitted(7,account='creator-a',binding='binding-a',epoch=3)
